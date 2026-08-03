"""Drives one leased Fable shot through generation:
READY -> SUBMITTED -> GENERATING -> DOWNLOADING -> VALIDATING ->
REVIEW_REQUIRED, producing FableTake rows whose media lives under
fable_projects/{project_id}/shots/{shot_id}/.

A shot may be asked for several CANDIDATE takes (F3), in which case it
walks the SUBMITTED..VALIDATING cycle once per take -- each with its own
distinct seed and its own budget check -- and only reaches
REVIEW_REQUIRED once the batch is done.

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

import hashlib
import time

from sqlalchemy import select

from reel_harness.core.cinematic_state import (
    DEFAULT_SHOT_RESOLUTION,
    SUPPORTED_TAKES_PER_SHOT,
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
from reel_harness.pipeline.generation_plan import (
    GenerationPlanConflict,
    resolve_parameters,
    select_reference_images,
)
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


def subject_character(session, shot: FableShot, project: StoryProject):
    """The character this shot is OF. Its approved reference sheet is what
    keeps one virtual actor recognizable across separately generated
    clips -- the entire reason casting exists."""
    return session.execute(
        select(FableCharacter).where(
            FableCharacter.project_id == project.id, FableCharacter.name == shot.subject,
        )
    ).scalars().first()


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


def _shot_price(
    provider: CinematicVideoProvider, shot: FableShot, project: StoryProject,
    duration_sec: float | None = None,
):
    """This shot's estimated price, as (amount, currency). Priced at the
    duration that will ACTUALLY be requested -- a provider billing per
    second and generating 8s for a 3s plan would otherwise be budgeted at
    a third of the real charge. An estimate the provider marks unknown
    yields (None, None), which assert_within_budget refuses under a live
    budget rather than treating as free."""
    estimate = provider.estimate_cost(
        estimate_request_for_shot(shot, project, duration_sec=duration_sec)
    )
    if not estimate.known:
        return None, None
    return estimate.amount, estimate.currency


def takes_per_shot_for(project: StoryProject | None, default: int = 1) -> int:
    """How many candidate takes this project wants per shot. A per-project
    override beats the operator-wide default, and an unset override means
    "use the default" rather than "one" -- the column is NULL for every
    project created before takes were configurable."""
    override = getattr(project, "takes_per_shot", None)
    count = override if override else default
    if count not in SUPPORTED_TAKES_PER_SHOT:
        raise ValueError(
            f"takes_per_shot must be one of {sorted(SUPPORTED_TAKES_PER_SHOT)}, got {count}"
        )
    return count


def _seed_for_attempt(fingerprint: str, attempt_number: int) -> int:
    """A deterministic but DISTINCT seed per take.

    Distinct because N takes generated from one prompt with one seed are N
    copies of the same clip -- the operator would be choosing between
    identical options. Deterministic because a re-run after a crash must
    reproduce the take it already paid for rather than buy a different
    one, which is the same reasoning the prompt fingerprint exists for."""
    digest = hashlib.sha256(f"{fingerprint}:{attempt_number}".encode()).hexdigest()
    # Kept inside a signed 32-bit range: every surveyed provider's seed
    # parameter is an int32, and a value that overflows would be rejected
    # or silently truncated.
    return int(digest[:8], 16) % 2_147_483_647


def run_shot(
    session, shot: FableShot, provider: CinematicVideoProvider, storage: StorageBackend,
    lease_token: str | None = None, sleep=time.sleep, allow_paid_generation: bool = False,
    takes_per_shot: int = 1,
) -> FableShot:
    """Generates `takes_per_shot` candidate takes for one leased shot.

    Each take is a separate paid generation with its own distinct seed and
    its own budget check, so a project that can afford two takes but not
    four stops after two with the shot still reviewable -- partial
    candidates are useful, and refusing to produce any would waste the
    ones it could pay for.

    Similarly, a transient failure on take 3 does not discard takes 1 and
    2: the shot only FAILS when it produced nothing at all."""
    scene = session.get(FableScene, shot.scene_id)
    assert scene is not None
    project = session.get(StoryProject, scene.project_id)
    assert project is not None

    prompt = compile_prompt_for_shot(session, shot, scene, project)
    fingerprint = prompt_fingerprint(prompt)
    existing = len([t for t in shot.takes if t.prompt_fingerprint == fingerprint])
    wanted = max(0, takes_per_shot - existing)
    if wanted == 0:
        # Every take this shot was asked for already exists -- a replay
        # after a crash, not a reason to buy more.
        return _finish_without_generating(session, shot, lease_token)

    produced = 0
    refused = False
    failure: tuple[str, str] | None = None
    for offset in range(wanted):
        attempt_number = existing + offset + 1
        outcome, detail = _run_one_take(
            session, shot, scene, project, provider, storage, prompt, fingerprint,
            attempt_number, lease_token, sleep, allow_paid_generation,
        )
        if outcome == "produced":
            produced += 1
            continue
        if outcome == "fenced":
            return shot
        # A refusal or failure stops the batch: whatever it was, asking
        # again immediately would hit the same wall.
        refused = outcome == "refused"
        failure = detail
        break

    # The batch's outcome, decided in ONE place. Having ANY candidate makes
    # this a human decision rather than a failure -- a shot with two good
    # takes and a third that timed out is reviewable, and throwing that
    # away would discard generations the project already paid for.
    if produced > 0 or refused:
        apply_shot_transition(shot, FableShotStatus.REVIEW_REQUIRED)
        if failure is not None:
            shot.failure_code, shot.failure_summary = failure
    else:
        assert failure is not None  # produced == 0 and not refused
        apply_shot_transition(
            shot, FableShotStatus.FAILED,
            failure_code=failure[0], failure_summary=failure[1],
        )
    _fenced_commit(session, shot, lease_token)
    return shot


def _finish_without_generating(session, shot: FableShot, lease_token: str | None) -> FableShot:
    """Every take this shot was asked for already exists. Only the status
    still needs to catch up, and only if a crash left it behind."""
    if shot.status != FableShotStatus.REVIEW_REQUIRED.value:
        apply_shot_transition(shot, FableShotStatus.REVIEW_REQUIRED)
        _fenced_commit(session, shot, lease_token)
    return shot


def _run_one_take(
    session, shot: FableShot, scene: FableScene, project: StoryProject,
    provider: CinematicVideoProvider, storage: StorageBackend, prompt: str,
    fingerprint: str, attempt_number: int, lease_token: str | None, sleep,
    allow_paid_generation: bool,
) -> tuple[str, tuple[str, str] | None]:
    """One candidate take.

    Returns (outcome, failure) where outcome is "produced", "refused"
    (a policy/budget stop -- a human decision), "failed" (something
    broke) or "fenced" (this worker lost its lease). The take row is
    already committed either way; the SHOT's final status is the
    caller's decision, because it depends on what the other takes in the
    batch did."""
    try:
        # Order matters. Asking the provider to quote the PLANNED shot
        # comes first because an unconfigured provider raises here, and
        # "not configured" is the truer reason than any conclusion drawn
        # from its deliberately-empty placeholder capabilities -- reading
        # those first would misreport a broken configuration as a plan
        # conflict a human is expected to resolve.
        _shot_price(provider, shot, project)

        # What the PROVIDER can actually generate, not what the fake
        # tier's constants assume. Before this the worker hardcoded 360p
        # and passed the planned duration straight through, so every
        # real-provider shot failed validate_request before a frame
        # existed.
        try:
            parameters = resolve_parameters(
                provider.capabilities, shot.duration_sec or 2.0, DEFAULT_SHOT_RESOLUTION,
            )
        except GenerationPlanConflict as exc:
            return "refused", ("GENERATION_PLAN_CONFLICT", str(exc)[:500])

        character = subject_character(session, shot, project)
        references = select_reference_images(character, provider.capabilities)
        request = CinematicGenerationRequest(
            prompt=prompt,
            duration_sec=parameters.duration_sec,
            aspect_ratio=project.aspect_ratio,
            resolution=parameters.resolution,
            reference_image_paths=references,
            seed=_seed_for_attempt(fingerprint, attempt_number),
            correlation_id=f"{project.id}:{shot.id}:{attempt_number}:{fingerprint}",
        )

        # The cost gates run before validate_request, so a project that
        # must not spend money never reaches a provider call at all. They
        # run per TAKE, not per shot: each candidate is its own paid
        # generation, so a project that can afford two but not four stops
        # after two rather than either overspending or refusing outright.
        # FableService.approve_shots checks the same two rules at the
        # approval gate; this is the enforcement point, because config and
        # budget can both change between approval and the moment a worker
        # actually picks the shot up. Nested inside the outer handler on
        # purpose: pricing itself can raise an ordinary PipelineError (an
        # unconfigured provider refuses to quote), and that is a shot
        # FAILURE, not a budget refusal.
        try:
            assert_paid_generation_allowed(project, provider.provider_id, allow_paid_generation)
            assert_within_budget(
                project, *_shot_price(provider, shot, project, parameters.duration_sec),
            )
        except (
            PaidGenerationNotAllowedError, BudgetExceededError, BudgetCurrencyMismatchError,
        ) as exc:
            return "refused", (exc.code, str(exc))

        provider.validate_request(request)
        apply_shot_transition(shot, FableShotStatus.SUBMITTED)
        if not _fenced_commit(session, shot, lease_token):
            return "fenced", None
        handle = provider.create_generation(request)

        take = FableTake(
            shot_id=shot.id, attempt_number=attempt_number, provider=provider.provider_id,
            provider_job_reference=handle.provider_job_reference,
            prompt_fingerprint=fingerprint, status="SUBMITTED",
            generation_seed=request.seed,
            # WHICH reference sheet this take was generated from. Without
            # it a take generated before a character was re-cast is
            # indistinguishable from one generated after, and the column
            # existed unpopulated until this was wired up.
            reference_fingerprint=(
                character.reference_fingerprint if references and character else None
            ),
        )
        session.add(take)
        apply_shot_transition(shot, FableShotStatus.GENERATING)
        if not _fenced_commit(session, shot, lease_token):
            return "fenced", None

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
            _fenced_commit(session, shot, lease_token)
            return "refused", ("CONTENT_POLICY_REVIEW", status.moderation_reason or "")
        if status.state != "succeeded":
            take.status = "FAILED"
            _fenced_commit(session, shot, lease_token)
            return "failed", (
                "UPSTREAM_TRANSIENT",
                status.failure_reason or f"generation ended in state {status.state!r}",
            )

        apply_shot_transition(shot, FableShotStatus.DOWNLOADING)
        if not _fenced_commit(session, shot, lease_token):
            return "fenced", None
        dest_dir = storage.path_for(project.id, f"shots/{shot.id}")
        result = provider.download_result(handle, dest_dir)

        apply_shot_transition(shot, FableShotStatus.VALIDATING)
        if not _fenced_commit(session, shot, lease_token):
            return "fenced", None
        take.media_path = str(result.video_path)
        take.checksum_sha256 = result.checksum_sha256
        take.license = result.license
        take.generation_seed = result.generation_seed or request.seed
        take.cost_amount = result.cost_amount
        take.cost_currency = result.cost_currency
        take.status = "DOWNLOADED"
        # Spend accumulates from the REAL reported cost, never the
        # estimate that authorized the call, and in the SAME fenced
        # commit that persists the take -- so the running total and its
        # line items can never disagree across a crash. A provider that
        # reported no figure moves the total by nothing rather than by a
        # guess; cost_service.recorded_spend counts those takes so the
        # under-count is visible instead of silent.
        mismatch: tuple[str, str] | None = None
        try:
            record_spend(project, result.cost_amount, result.cost_currency)
        except BudgetCurrencyMismatchError as exc:
            # The generation is already paid for and downloaded. Losing
            # the take row over an accounting disagreement would be the
            # worse outcome by far, so the take is kept with the
            # provider's own figures recorded verbatim, the running total
            # is left untouched (adding an unconvertible amount would
            # corrupt it), and the shot carries the reason for a human.
            mismatch = (exc.code, str(exc)[:500])
        if not _fenced_commit(session, shot, lease_token):
            return "fenced", None
        if mismatch is not None:
            return "refused", mismatch
        return "produced", None
    except PipelineError as exc:
        session.rollback()
        log_worker_event(
            event="fable_shot_failed", worker_id=shot.locked_by or "unknown",
            job_id=shot.id, error=exc.code,
        )
        return "failed", (exc.code, str(exc)[:500])
