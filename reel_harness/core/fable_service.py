"""Fable project lifecycle service (Phase F1) -- the one place project
state rules live, used by CLI/API/web alike (same discipline as
JobService: each method opens and closes its own session; callers only
ever see detached ORM objects; status changes only ever go through
core.cinematic_state's apply_* functions).

F1 ships a STUB adaptation: a deterministic split of the source text
into 1 character / 1 location / 2 scenes x 2 shots, so the whole
vertical slice (create -> adapt -> reviews -> generate -> select ->
final render) runs offline. The real NarrativeDirector (LLM-backed,
schema-validated, repair loop) replaces `_stub_adaptation` in F2 --
callers and states stay identical.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from reel_harness.core.adaptation_service import run_adaptation
from reel_harness.core.cinematic_state import (
    FableProjectStatus,
    FableShotStatus,
    apply_project_transition,
    apply_shot_transition,
)
from reel_harness.core.cost_service import (
    BudgetStatus,
    ProjectCostEstimate,
    assert_paid_generation_allowed,
    assert_within_budget,
    budget_status,
    estimate_project_cost,
)
from reel_harness.core.errors import BudgetExceededError, PaidGenerationNotAllowedError
from reel_harness.core.service import InvalidActionError, JobNotFoundError
from reel_harness.db.cinematic_models import (
    FableCharacter,
    FableLocation,
    FableScene,
    FableShot,
    FableTake,
    StoryProject,
)
from reel_harness.providers.base import AdaptationRequest
from reel_harness.storage.base import StorageBackend


class FableProjectNotFoundError(JobNotFoundError):
    pass


class FableService:
    def __init__(
        self, session_factory, storage: StorageBackend | None = None,
        provider_snapshot: dict | None = None, narrative_director=None,
        narrative_director_resolver=None, cinematic_provider_resolver=None,
        allow_paid_generation: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage
        self._provider_snapshot = provider_snapshot
        # Two ways in, by design: AppContext passes a RESOLVER so each
        # project's adaptation honors its own creation-time snapshot
        # (pinning discipline); tests pass a director instance directly.
        # Neither present means adapt_project refuses explicitly rather
        # than silently falling back to anything.
        self._narrative_director = narrative_director
        self._narrative_director_resolver = narrative_director_resolver
        # Same pinning discipline for the cinematic provider, used only to
        # PRICE a project (estimate/approval gate) -- the worker resolves
        # its own provider for the shot it actually generates.
        self._cinematic_provider_resolver = cinematic_provider_resolver
        # Settings.allow_paid_generation. False by default so a service
        # built without an opinion can never authorize spending.
        self._allow_paid_generation = allow_paid_generation

    # -- creation / read -------------------------------------------------

    def create_project(
        self, title: str, source_text: str, idempotency_key: str,
        *, language: str = "ko", genre: str | None = None, tone: str | None = None,
        target_duration_sec: int = 60, aspect_ratio: str = "9:16",
    ) -> tuple[StoryProject, bool]:
        """Returns (project, idempotent_replay) -- the unique constraint on
        idempotency_key is the duplicate guard, same idiom as
        JobService.create_job."""
        if aspect_ratio not in ("9:16", "16:9"):
            raise InvalidActionError(f"unsupported aspect ratio {aspect_ratio!r} (supported: 9:16, 16:9)")
        if not source_text.strip():
            raise InvalidActionError("source_text must not be empty")
        with self._session_factory() as session:
            existing = session.execute(
                select(StoryProject).where(StoryProject.idempotency_key == idempotency_key)
            ).scalar_one_or_none()
            if existing is not None:
                session.expunge(existing)
                return existing, True
            project = StoryProject(
                idempotency_key=idempotency_key, title=title, source_text=source_text,
                language=language, genre=genre, tone=tone,
                target_duration_sec=target_duration_sec, aspect_ratio=aspect_ratio,
                provider_config=self._provider_snapshot,
            )
            session.add(project)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                replay = session.execute(
                    select(StoryProject).where(StoryProject.idempotency_key == idempotency_key)
                ).scalar_one()
                session.expunge(replay)
                return replay, True
            session.refresh(project)
            session.expunge(project)
            return project, False

    def get_project(self, project_id: str) -> StoryProject:
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            session.expunge(project)
            return project

    def list_projects(self) -> list[StoryProject]:
        with self._session_factory() as session:
            projects = list(session.execute(
                select(StoryProject).order_by(StoryProject.created_at.desc())
            ).scalars())
            for project in projects:
                session.expunge(project)
            return projects

    def project_shots(self, project_id: str) -> list[FableShot]:
        with self._session_factory() as session:
            shots = self._shots_for_project(session, project_id)
            for shot in shots:
                session.expunge(shot)
            return shots

    def shot_takes(self, shot_id: str) -> list[FableTake]:
        with self._session_factory() as session:
            takes = list(session.execute(
                select(FableTake).where(FableTake.shot_id == shot_id)
                .order_by(FableTake.attempt_number)
            ).scalars())
            for take in takes:
                session.expunge(take)
            return takes

    # -- cost / budget ---------------------------------------------------

    def set_budget(
        self, project_id: str, limit_amount: float | None, currency: str | None = None,
    ) -> StoryProject:
        """Sets (or clears, with limit_amount=None) this project's spending
        ceiling. Setting a limit is the per-project half of the paid
        generation double gate -- without it, a cost-incurring provider is
        refused outright.

        A limit below what the project has ALREADY spent is refused: it
        would read as a promise the money can be recovered. Clearing a
        limit is allowed at any time and simply re-closes the paid gate;
        it never un-spends anything."""
        if limit_amount is not None and limit_amount <= 0:
            raise InvalidActionError(f"budget limit must be positive, got {limit_amount}")
        if limit_amount is not None and not currency:
            raise InvalidActionError("a budget limit requires an explicit currency")
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            if limit_amount is not None and limit_amount < project.budget_spent_amount:
                raise InvalidActionError(
                    f"budget limit {limit_amount} is below the {project.budget_spent_amount} "
                    f"already spent on this project"
                )
            if (
                limit_amount is not None and project.budget_currency
                and currency != project.budget_currency and project.budget_spent_amount > 0
            ):
                raise InvalidActionError(
                    f"project has already spent {project.budget_spent_amount} "
                    f"{project.budget_currency} -- refusing to redenominate the budget to "
                    f"{currency} (no conversion is applied)"
                )
            project.budget_limit_amount = limit_amount
            project.budget_currency = currency if limit_amount is not None else project.budget_currency
            session.commit()
            session.refresh(project)
            session.expunge(project)
            return project

    def budget_status(self, project_id: str) -> BudgetStatus:
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            return budget_status(session, project)

    def estimate_cost(self, project_id: str, provider=None) -> ProjectCostEstimate:
        """What generating this project's shots would cost, priced by the
        project's own pinned provider. Read-only -- nothing is approved,
        transitioned, or spent."""
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            provider = provider or self._resolve_cinematic_provider(project)
            shots = self._shots_for_project(session, project_id)
            return estimate_project_cost(project, shots, provider)

    def _resolve_cinematic_provider(self, project: StoryProject):
        if self._cinematic_provider_resolver is None:
            raise InvalidActionError(
                "no cinematic provider resolver is configured -- cost cannot be estimated"
            )
        return self._cinematic_provider_resolver(project)

    # -- adaptation ------------------------------------------------------

    def adaptation_fingerprint(self, project: StoryProject) -> str:
        """Deterministic identity of the adaptation INPUT. Changing the
        source text, any adaptation parameter, or the prompt version
        yields a different fingerprint; re-running with the same one is a
        replay, not a second paid call."""
        from reel_harness.providers.narrative_prompts import NARRATIVE_PROMPT_VERSION

        parts = [
            project.source_text, project.language, project.genre or "", project.tone or "",
            str(project.target_duration_sec), project.aspect_ratio, NARRATIVE_PROMPT_VERSION,
        ]
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]

    def adapt_project(self, project_id: str, director=None) -> StoryProject:
        """DRAFT -> ADAPTING -> STORY_REVIEW via the real Narrative
        Director (core.adaptation_service's bounded repair loop).

        Ordering is deliberate: ADAPTING is committed BEFORE any network
        call, so a crash mid-adaptation is visible as ADAPTING with no
        children, and re-running this method resumes it. The adaptation's
        own writes (bible + characters + locations + scenes + shots +
        fingerprint + STORY_REVIEW) all land in ONE transaction -- a
        partially-written adaptation can never be observed.

        Idempotency: an already-adapted project whose stored fingerprint
        matches the current input replays (no director call). A DIFFERENT
        fingerprint on an already-adapted project is refused -- the
        STORY_REVIEW reject path is how a re-adaptation is requested, so
        input drift can never silently discard an approved adaptation."""
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            if director is None:
                director = (
                    self._narrative_director_resolver(project)
                    if self._narrative_director_resolver is not None
                    else self._narrative_director
                )
            if director is None:
                raise InvalidActionError(
                    "no narrative director is configured -- set REEL_HARNESS_NARRATIVE_PROVIDER "
                    "(fake or openai-compatible)"
                )
            fingerprint = self.adaptation_fingerprint(project)
            # "Already adapted" means adaptation FINISHED (STORY_REVIEW or
            # beyond) with real children. DRAFT has never adapted, and
            # ADAPTING is either a crashed run or a rejection-triggered
            # re-adaptation -- both must proceed, not be refused.
            already_adapted = project.status not in (
                FableProjectStatus.DRAFT.value, FableProjectStatus.ADAPTING.value,
            ) and bool(self._scenes_for_project(session, project_id))
            if already_adapted:
                if project.adaptation_fingerprint == fingerprint:
                    session.expunge(project)
                    return project  # replay: no second paid call
                raise InvalidActionError(
                    "this project was already adapted from different input -- reject the story "
                    "review to re-adapt instead of changing the source mid-flight"
                )
            if project.status == FableProjectStatus.DRAFT.value:
                apply_project_transition(project, FableProjectStatus.ADAPTING)
                session.commit()

        request = self._adaptation_request(project)
        outcome = run_adaptation(director, request)

        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            assert project is not None
            self._persist_adaptation(session, project, outcome.adaptation)
            project.adaptation_fingerprint = fingerprint
            apply_project_transition(project, FableProjectStatus.STORY_REVIEW)
            session.commit()
            session.refresh(project)
            session.expunge(project)
            return project

    def _adaptation_request(self, project: StoryProject) -> AdaptationRequest:
        return AdaptationRequest(
            source_text=project.source_text, language=project.language,
            genre=project.genre, tone=project.tone,
            target_duration_sec=project.target_duration_sec,
            aspect_ratio=project.aspect_ratio,
        )

    def _persist_adaptation(self, session, project: StoryProject, adaptation) -> None:
        """Writes the validated adaptation. Replaces any previous
        adaptation's children (only reachable before GENERATING, so no
        take history can be lost -- asserted here rather than assumed)."""
        existing_scenes = self._scenes_for_project(session, project.id)
        if existing_scenes:
            existing_shots = self._shots_for_project(session, project.id)
            if any(shot.takes for shot in existing_shots):
                raise InvalidActionError(
                    "refusing to replace an adaptation whose shots already have takes"
                )
            for shot in existing_shots:
                session.delete(shot)
            for scene in existing_scenes:
                session.delete(scene)
            for character in session.execute(
                select(FableCharacter).where(FableCharacter.project_id == project.id)
            ).scalars():
                session.delete(character)
            for location in session.execute(
                select(FableLocation).where(FableLocation.project_id == project.id)
            ).scalars():
                session.delete(location)
            session.flush()

        bible = adaptation.story_bible
        project.story_bible = {
            "logline": adaptation.logline,
            "synopsis": adaptation.synopsis,
            **bible.model_dump(),
        }

        # adult_confirmed is set from the VALIDATED schema (is_adult is
        # Literal[True] and age_range is whitelisted), never trusted from
        # free text -- see pipeline.adaptation_schema.
        characters = {
            model.name: FableCharacter(
                project_id=project.id, name=model.name, role=model.role,
                age_range=model.age_range, adult_confirmed=True,
                bible={
                    "appearance": model.appearance, "wardrobe": model.wardrobe,
                    "hair": model.hair, "mannerisms": model.mannerisms,
                    "voice_profile": {"style": model.voice_style},
                    "fixed_identity": model.fixed_identity or {
                        "appearance": model.appearance, "hair": model.hair,
                        "wardrobe": model.wardrobe,
                    },
                },
            )
            for model in adaptation.characters
        }
        locations = {
            model.name: FableLocation(
                project_id=project.id, name=model.name, description=model.description,
                continuity={
                    "lighting": model.lighting, "time_of_day": model.time_of_day,
                    "weather": model.weather,
                },
            )
            for model in adaptation.locations
        }
        session.add_all([*characters.values(), *locations.values()])
        session.flush()

        for scene_model in adaptation.scenes:
            location = locations.get(scene_model.location_name)
            scene = FableScene(
                project_id=project.id, scene_order=scene_model.scene_order,
                location_id=location.id if location is not None else None,
                story_purpose=scene_model.story_purpose,
                emotional_beat=scene_model.emotional_beat,
                continuity_notes={"source_beat": scene_model.source_beat},
                dialogue={"lines": [d.model_dump() for d in scene_model.dialogue]},
                estimated_duration_sec=sum(s.duration_sec for s in scene_model.shots),
            )
            session.add(scene)
            session.flush()
            for shot_model in scene_model.shots:
                session.add(FableShot(
                    scene_id=scene.id, shot_order=shot_model.shot_order,
                    shot_size=shot_model.shot_size, camera_angle=shot_model.camera_angle,
                    camera_movement=shot_model.camera_movement, lens_style=shot_model.lens_style,
                    subject=shot_model.subject, action=shot_model.action,
                    expression=shot_model.expression, blocking=shot_model.blocking,
                    lighting=shot_model.lighting, duration_sec=shot_model.duration_sec,
                    dialogue_line=shot_model.dialogue_line,
                    continuity_requirements={"source_beat": scene_model.source_beat},
                ))
        session.flush()

    # -- review gates ----------------------------------------------------

    def approve_story(self, project_id: str) -> StoryProject:
        """STORY_REVIEW -> CASTING -> CHARACTER_REVIEW. F1 has no
        reference-image generation (that's F3), so casting is a
        passthrough -- but the CHARACTER_REVIEW gate still requires its
        own explicit approval before storyboarding, keeping the state
        walk identical to what F3 will slot reference generation into."""
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            apply_project_transition(project, FableProjectStatus.CASTING)
            apply_project_transition(project, FableProjectStatus.CHARACTER_REVIEW)
            session.commit()
            session.refresh(project)
            session.expunge(project)
            return project

    def approve_characters(self, project_id: str) -> StoryProject:
        """CHARACTER_REVIEW -> STORYBOARDING -> SHOT_REVIEW. Refuses if any
        character is not adult-confirmed -- virtual adult actors only,
        checked at the gate rather than trusted from creation."""
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            unconfirmed = session.execute(
                select(FableCharacter).where(
                    FableCharacter.project_id == project_id,
                    FableCharacter.adult_confirmed.is_(False),
                )
            ).scalars().first()
            if unconfirmed is not None:
                raise InvalidActionError(
                    f"character {unconfirmed.name!r} is not confirmed as a virtual adult actor"
                )
            apply_project_transition(project, FableProjectStatus.STORYBOARDING)
            apply_project_transition(project, FableProjectStatus.SHOT_REVIEW)
            session.commit()
            session.refresh(project)
            session.expunge(project)
            return project

    def approve_shots(self, project_id: str, provider=None) -> StoryProject:
        """SHOT_REVIEW -> GENERATING: THE cost gate. Every PLANNED shot
        becomes READY for the worker lane. Never called automatically.

        Being THE cost gate is now literal: a cost-incurring provider must
        pass the paid-generation double gate, and the whole project's
        estimate must fit inside the remaining budget, BEFORE any shot
        becomes claimable. Failing here costs nothing; failing at the
        worker means shots were queued that could never all be paid for.
        The worker re-checks per shot anyway -- config and budget can both
        change between this approval and a shot being picked up -- so this
        is the early, cheap answer, not the only one."""
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            shots = self._shots_for_project(session, project_id)
            if not shots:
                raise InvalidActionError("project has no shots to generate")
            self._assert_generation_affordable(project, shots, provider)
            apply_project_transition(project, FableProjectStatus.GENERATING)
            for shot in shots:
                if shot.status == FableShotStatus.PLANNED.value:
                    apply_shot_transition(shot, FableShotStatus.READY)
            session.commit()
            session.refresh(project)
            session.expunge(project)
            return project

    def _assert_generation_affordable(
        self, project: StoryProject, shots: list[FableShot], provider=None,
    ) -> None:
        """The approval-time half of cost enforcement. Raises
        InvalidActionError (not a PipelineError) because this is a user
        action being refused at a CLI/API boundary, not a shot failing
        mid-flight -- the caller gets a message, not a state change.

        A project with no budget limit and a free provider skips straight
        through: requiring a budget to run the offline fake tier would be
        ceremony, not safety."""
        if provider is None and self._cinematic_provider_resolver is None:
            # Nothing to price with. Only safe because the paid gate below
            # cannot be evaluated either -- so refuse rather than approve
            # blindly whenever spending is even possible.
            if self._allow_paid_generation:
                raise InvalidActionError(
                    "cannot verify generation cost: no cinematic provider resolver is "
                    "configured while paid generation is enabled"
                )
            return
        provider = provider or self._resolve_cinematic_provider(project)
        try:
            assert_paid_generation_allowed(
                project, provider.provider_id, self._allow_paid_generation,
            )
        except PaidGenerationNotAllowedError as exc:
            raise InvalidActionError(str(exc)) from exc
        if project.budget_limit_amount is None:
            return
        estimate = estimate_project_cost(project, shots, provider)
        if not estimate.known:
            raise InvalidActionError(
                f"project has a budget limit but its cost cannot be established: "
                f"{estimate.detail}"
            )
        try:
            assert_within_budget(project, estimate.amount, estimate.currency)
        except BudgetExceededError as exc:
            raise InvalidActionError(str(exc)) from exc

    # -- take selection --------------------------------------------------

    def select_take(self, take_id: str) -> FableShot:
        """Marks one take selected for its shot (REVIEW_REQUIRED ->
        SELECTED). Exactly one selected take per shot is enforced here --
        selecting a different take first deselects the previous one, and
        rejected siblings are kept (append-only retention), never deleted.
        When every shot in the project is SELECTED, the project advances
        TAKE_REVIEW -> EDITING."""
        with self._session_factory() as session:
            take = session.get(FableTake, take_id)
            if take is None:
                raise FableProjectNotFoundError(take_id)
            shot = session.get(FableShot, take.shot_id)
            assert shot is not None  # FK guarantees
            if take.status != "DOWNLOADED":
                raise InvalidActionError(
                    f"take {take_id} is not downloadable/reviewable (status={take.status})"
                )
            for sibling in session.execute(
                select(FableTake).where(FableTake.shot_id == shot.id, FableTake.selected.is_(True))
            ).scalars():
                sibling.selected = False
            take.selected = True
            apply_shot_transition(shot, FableShotStatus.SELECTED)
            self._maybe_advance_to_editing(session, shot)
            session.commit()
            session.refresh(shot)
            session.expunge(shot)
            return shot

    def _maybe_advance_to_editing(self, session, shot: FableShot) -> None:
        scene = session.get(FableScene, shot.scene_id)
        assert scene is not None
        project = session.get(StoryProject, scene.project_id)
        assert project is not None
        if project.status != FableProjectStatus.TAKE_REVIEW.value:
            return
        remaining = [
            s for s in self._shots_for_project(session, project.id)
            if s.status != FableShotStatus.SELECTED.value
        ]
        if not remaining:
            apply_project_transition(project, FableProjectStatus.EDITING)

    # -- final render ----------------------------------------------------

    def render_final(self, project_id: str) -> Path:
        """EDITING: concatenates the selected takes in scene/shot order
        into final/final.mp4 under the project's storage root (hard cuts
        -- F5's film editor adds transitions/mixing/grading), validates
        with ffprobe, then advances to FINAL_REVIEW."""
        from reel_harness.core.errors import DependencyError, ValidationFailedError
        from reel_harness.media import ffmpeg_render
        from reel_harness.media.deps import check_ffmpeg_available
        from reel_harness.media.ffprobe_validate import build_ffprobe_argv, parse_ffprobe_output
        from reel_harness.media.runner import run

        if self._storage is None:
            raise InvalidActionError("no fable storage configured")
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            if project.status != FableProjectStatus.EDITING.value:
                raise InvalidActionError(
                    f"final render requires status EDITING (got {project.status})"
                )
            selected_paths: list[Path] = []
            for shot in self._shots_for_project(session, project_id):
                take = session.execute(
                    select(FableTake).where(FableTake.shot_id == shot.id, FableTake.selected.is_(True))
                ).scalar_one_or_none()
                if take is None or not take.media_path:
                    raise InvalidActionError(f"shot {shot.id} has no selected take with media")
                selected_paths.append(Path(take.media_path))

            deps = check_ffmpeg_available()
            if not deps.all_available:
                raise DependencyError("ffmpeg/ffprobe are required for the final render")
            assert deps.ffmpeg.path is not None and deps.ffprobe.path is not None

            final_dir = self._storage.path_for(project_id, "final")
            final_dir.mkdir(parents=True, exist_ok=True)
            concat_list = final_dir / "concat.txt"
            ffmpeg_render.write_concat_list(selected_paths, concat_list)
            final_path = final_dir / "final.mp4"
            result = run(ffmpeg_render.concat_clips_argv(deps.ffmpeg.path, concat_list, final_path))
            if result.returncode != 0:
                raise ValidationFailedError(f"final concat failed: {result.stderr[-300:]}")

            probe = run(build_ffprobe_argv(deps.ffprobe.path, final_path))
            if probe.returncode != 0:
                raise ValidationFailedError(f"ffprobe failed on final render: {probe.stderr[-300:]}")
            parse_ffprobe_output(probe.stdout)  # malformed output raises

            apply_project_transition(project, FableProjectStatus.FINAL_REVIEW)
            session.commit()
            return final_path

    def approve_final(self, project_id: str) -> StoryProject:
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            apply_project_transition(project, FableProjectStatus.COMPLETED)
            session.commit()
            session.refresh(project)
            session.expunge(project)
            return project

    def cancel_project(self, project_id: str) -> StoryProject:
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            apply_project_transition(project, FableProjectStatus.CANCELLED)
            session.commit()
            session.refresh(project)
            session.expunge(project)
            return project

    # -- shared helpers --------------------------------------------------

    def _shots_for_project(self, session, project_id: str) -> list[FableShot]:
        return list(session.execute(
            select(FableShot)
            .join(FableScene, FableShot.scene_id == FableScene.id)
            .where(FableScene.project_id == project_id)
            .order_by(FableScene.scene_order, FableShot.shot_order)
        ).scalars())

    def _scenes_for_project(self, session, project_id: str) -> list[FableScene]:
        return list(session.execute(
            select(FableScene).where(FableScene.project_id == project_id)
            .order_by(FableScene.scene_order)
        ).scalars())
