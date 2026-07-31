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
never an automatic retry of the same prompt.

F3 adds the cost gates (core.cost_service), checked before the provider
is called at all: a shot this project may not pay for stops at
REVIEW_REQUIRED with no take row, since nothing was submitted and so
nothing was charged. On the way out, the take records what the provider
actually billed and the project's running total moves by that same
figure -- both inside the one fenced commit that persists the take."""
from __future__ import annotations

import time

from sqlalchemy import select

from reel_harness.core.cinematic_state import (
    DEFAULT_SHOT_RESOLUTION,
    FableShotStatus,
    apply_shot_transition,
)
from reel_harness.core.cost_service import (
    assert_paid_generation_allowed,
    assert_within_budget,
    estimate_request_for_shot,
    record_spend,
)
from reel_harness.core.errors import (
    BudgetCurrencyMismatchError,
    BudgetExceededError,
    PaidGenerationNotAllowedError,
    PipelineError,
)
from reel_harness.db.cinematic_models import (
    FableCharacter,
    FableLocation,
    FableScene,
    FableShot,
    FableTake,
    StoryProject,
)
from reel_harness.observability import log_worker_event
from reel_harness.pipeline.shot_prompt import compile_shot_prompt, prompt_fingerprint
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


def compile_prompt_for_shot(session, shot: FableShot, scene: FableScene, project: StoryProject) -> str:
    """Loads this shot's subject character and its scene's location, then
    delegates to the canonical provider-neutral compiler
    (pipeline.shot_prompt) -- the compiler itself stays a pure function."""
    character = session.execute(
        select(FableCharacter).where(
            FableCharacter.project_id == project.id, FableCharacter.name == shot.subject,
        )
    ).scalars().first()
    location_record = session.get(FableLocation, scene.location_id) if scene.location_id else None
    location = (
        {
            "name": location_record.name, "description": location_record.description,
            "continuity": location_record.continuity or {},
        }
        if location_record is not None else {}
    )
    return compile_shot_prompt(
        shot, project, character_bible=(character.bible if character is not None else None),
        location=location,
    )


def _shot_price(provider: CinematicVideoProvider, shot: FableShot, project: StoryProject):
    """This shot's estimated price, as (amount, currency). An estimate the
    provider marks unknown yields (None, None), which assert_within_budget
    refuses under a live budget rather than treating as free."""
    estimate = provider.estimate_cost(estimate_request_for_shot(shot, project))
    if not estimate.known:
        return None, None
    return estimate.amount, estimate.currency


def _block_for_review(
    session, shot: FableShot, lease_token: str | None, code: str, summary: str,
) -> FableShot:
    """Stops a shot BEFORE any provider call, for a reason only a human
    can resolve (budget/paid gate). Deliberately REVIEW_REQUIRED rather
    than FAILED: nothing is broken, a decision is missing. No take row
    exists, because nothing was ever submitted."""
    apply_shot_transition(shot, FableShotStatus.REVIEW_REQUIRED)
    shot.failure_code = code
    shot.failure_summary = summary[:500]
    _fenced_commit(session, shot, lease_token)
    return shot


def run_shot(
    session, shot: FableShot, provider: CinematicVideoProvider, storage: StorageBackend,
    lease_token: str | None = None, sleep=time.sleep, allow_paid_generation: bool = False,
) -> FableShot:
    scene = session.get(FableScene, shot.scene_id)
    assert scene is not None
    project = session.get(StoryProject, scene.project_id)
    assert project is not None

    prompt = compile_prompt_for_shot(session, shot, scene, project)
    fingerprint = prompt_fingerprint(prompt)
    attempt_number = 1 + len([t for t in shot.takes if t.prompt_fingerprint == fingerprint])
    request = CinematicGenerationRequest(
        prompt=prompt,
        duration_sec=shot.duration_sec or 2.0,
        aspect_ratio=project.aspect_ratio,
        resolution=DEFAULT_SHOT_RESOLUTION,
        correlation_id=f"{project.id}:{shot.id}:{attempt_number}:{fingerprint}",
    )

    try:
        # The cost gates run before validate_request, so a project that
        # must not spend money never reaches a provider call at all.
        # FableService.approve_shots checks the same two rules at the
        # approval gate; this is the enforcement point, because config and
        # budget can both change between approval and the moment a worker
        # actually picks the shot up. Nested inside the outer handler on
        # purpose: pricing itself can raise an ordinary PipelineError (an
        # unconfigured provider refuses to quote), and that is a shot
        # FAILURE, not a budget refusal.
        try:
            assert_paid_generation_allowed(project, provider.provider_id, allow_paid_generation)
            assert_within_budget(project, *_shot_price(provider, shot, project))
        except (
            PaidGenerationNotAllowedError, BudgetExceededError, BudgetCurrencyMismatchError,
        ) as exc:
            return _block_for_review(session, shot, lease_token, exc.code, str(exc))

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
        take.cost_amount = result.cost_amount
        take.cost_currency = result.cost_currency
        take.status = "DOWNLOADED"
        apply_shot_transition(shot, FableShotStatus.REVIEW_REQUIRED)
        # Spend accumulates from the REAL reported cost, never the
        # estimate that authorized the call, and in the SAME fenced
        # commit that persists the take -- so the running total and its
        # line items can never disagree across a crash. A provider that
        # reported no figure moves the total by nothing rather than by a
        # guess; cost_service.recorded_spend counts those takes so the
        # under-count is visible instead of silent.
        try:
            record_spend(project, result.cost_amount, result.cost_currency)
        except BudgetCurrencyMismatchError as exc:
            # The generation is already paid for and downloaded. Losing
            # the take row over an accounting disagreement would be the
            # worse outcome by far, so the take is kept with the
            # provider's own figures recorded verbatim, the running total
            # is left untouched (adding an unconvertible amount would
            # corrupt it), and the shot carries the reason for a human.
            shot.failure_code = exc.code
            shot.failure_summary = str(exc)[:500]
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
