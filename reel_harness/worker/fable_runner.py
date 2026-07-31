"""Drives one leased Fable shot through generation:
READY -> SUBMITTED -> GENERATING -> DOWNLOADING -> VALIDATING ->
REVIEW_REQUIRED, producing one FableTake row whose media lives under
fable_projects/{project_id}/shots/{shot_id}/.

Every status commit is fenced on the shot's lease token
(worker.fable_lease.assert_shot_lease) so a worker that lost its lease
to stale recovery can never publish results over the new owner's.

F1 honesty notes: the fake provider "polls" to completion in one or two
cycles, so this runner polls inline with a bounded loop; F3/F5 move
long-poll pacing into a dedicated lane (next_poll_at, like the
publication processing poller). A "moderated" provider state routes the
shot to REVIEW_REQUIRED with the reason recorded -- a human decision,
never an automatic retry of the same prompt."""
from __future__ import annotations

import hashlib
import time

from reel_harness.core.cinematic_state import FableShotStatus, apply_shot_transition
from reel_harness.core.errors import PipelineError
from reel_harness.db.cinematic_models import FableScene, FableShot, FableTake, StoryProject
from reel_harness.observability import log_worker_event
from reel_harness.providers.base import CinematicGenerationRequest, CinematicVideoProvider
from reel_harness.storage.base import StorageBackend
from reel_harness.worker.fable_lease import assert_shot_lease

_POLL_LIMIT = 60
_POLL_INTERVAL_SEC = 0.2


def _fenced_commit(session, shot: FableShot, lease_token: str | None) -> bool:
    """Commit the current transaction only if the lease still holds --
    rolls back and returns False when fenced out."""
    if not assert_shot_lease(session, shot.id, lease_token):
        session.rollback()
        return False
    session.commit()
    return True


def compile_stub_prompt(shot: FableShot, project: StoryProject) -> str:
    """F1 placeholder for F2's ShotPromptCompiler: subject + action +
    grammar fields in a fixed order, deterministic for fingerprinting."""
    bible = project.story_bible or {}
    parts = [
        shot.subject or "a virtual adult actor",
        shot.action or "",
        shot.expression or "",
        shot.shot_size or "",
        shot.camera_angle or "",
        shot.camera_movement or "",
        shot.lighting or "",
        str(bible.get("visual_style", "")),
    ]
    return ", ".join(p for p in parts if p)


def prompt_fingerprint(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:32]


def run_shot(
    session, shot: FableShot, provider: CinematicVideoProvider, storage: StorageBackend,
    lease_token: str | None = None, sleep=time.sleep,
) -> FableShot:
    scene = session.get(FableScene, shot.scene_id)
    assert scene is not None
    project = session.get(StoryProject, scene.project_id)
    assert project is not None

    prompt = compile_stub_prompt(shot, project)
    fingerprint = prompt_fingerprint(prompt)
    attempt_number = 1 + len([t for t in shot.takes if t.prompt_fingerprint == fingerprint])
    request = CinematicGenerationRequest(
        prompt=prompt,
        duration_sec=shot.duration_sec or 2.0,
        aspect_ratio=project.aspect_ratio,
        resolution="360p",
        correlation_id=f"{project.id}:{shot.id}:{attempt_number}:{fingerprint}",
    )

    try:
        provider.validate_request(request)
        apply_shot_transition(shot, FableShotStatus.SUBMITTED)
        if not _fenced_commit(session, shot, lease_token):
            return shot
        handle = provider.create_generation(request)

        take = FableTake(
            shot_id=shot.id, attempt_number=attempt_number, provider=provider.provider_id,
            provider_job_reference=handle.provider_job_reference,
            prompt_fingerprint=fingerprint, status="SUBMITTED",
        )
        session.add(take)
        apply_shot_transition(shot, FableShotStatus.GENERATING)
        if not _fenced_commit(session, shot, lease_token):
            return shot

        for _ in range(_POLL_LIMIT):
            status = provider.get_generation_status(handle)
            if status.state != "generating":
                break
            sleep(_POLL_INTERVAL_SEC)
        else:
            status = provider.get_generation_status(handle)

        if status.state == "moderated":
            take.status = "MODERATED"
            take.rejection_reasons = {"moderation": status.moderation_reason}
            apply_shot_transition(
                shot, FableShotStatus.REVIEW_REQUIRED,
            )
            shot.failure_code = "CONTENT_POLICY_REVIEW"
            shot.failure_summary = status.moderation_reason
            _fenced_commit(session, shot, lease_token)
            return shot
        if status.state != "succeeded":
            take.status = "FAILED"
            apply_shot_transition(
                shot, FableShotStatus.FAILED,
                failure_code="UPSTREAM_TRANSIENT",
                failure_summary=status.failure_reason or f"generation ended in state {status.state!r}",
            )
            _fenced_commit(session, shot, lease_token)
            return shot

        apply_shot_transition(shot, FableShotStatus.DOWNLOADING)
        if not _fenced_commit(session, shot, lease_token):
            return shot
        dest_dir = storage.path_for(project.id, f"shots/{shot.id}")
        result = provider.download_result(handle, dest_dir)

        apply_shot_transition(shot, FableShotStatus.VALIDATING)
        if not _fenced_commit(session, shot, lease_token):
            return shot
        take.media_path = str(result.video_path)
        take.checksum_sha256 = result.checksum_sha256
        take.license = result.license
        take.generation_seed = result.generation_seed
        take.status = "DOWNLOADED"
        apply_shot_transition(shot, FableShotStatus.REVIEW_REQUIRED)
        _fenced_commit(session, shot, lease_token)
        return shot
    except PipelineError as exc:
        session.rollback()
        apply_shot_transition(
            shot, FableShotStatus.FAILED,
            failure_code=exc.code, failure_summary=str(exc)[:500],
        )
        _fenced_commit(session, shot, lease_token)
        log_worker_event(
            event="fable_shot_failed", worker_id=shot.locked_by or "unknown",
            job_id=shot.id, error=exc.code,
        )
        return shot
