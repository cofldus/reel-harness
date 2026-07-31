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

from reel_harness.core.cinematic_state import (
    FableProjectStatus,
    FableShotStatus,
    apply_project_transition,
    apply_shot_transition,
)
from reel_harness.core.service import InvalidActionError, JobNotFoundError
from reel_harness.db.cinematic_models import (
    FableCharacter,
    FableLocation,
    FableScene,
    FableShot,
    FableTake,
    StoryProject,
)
from reel_harness.storage.base import StorageBackend

# The fake provider supports {2.0, 4.0, 6.0, 8.0}s -- the stub plans 2s
# shots so the offline slice stays fast. F2's real adaptation plans real
# durations against the selected provider's capabilities.
_STUB_SHOT_DURATION_SEC = 2.0


class FableProjectNotFoundError(JobNotFoundError):
    pass


class FableService:
    def __init__(
        self, session_factory, storage: StorageBackend | None = None,
        provider_snapshot: dict | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage
        self._provider_snapshot = provider_snapshot

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

    # -- adaptation (F1 stub) --------------------------------------------

    def adapt_project(self, project_id: str) -> StoryProject:
        """DRAFT -> ADAPTING -> STORY_REVIEW, populating the story bible,
        characters, locations, scenes, and shots. F1's adaptation is a
        deterministic stub (no LLM); the states and review gates are the
        real ones."""
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            apply_project_transition(project, FableProjectStatus.ADAPTING)
            session.commit()

            self._stub_adaptation(session, project)
            apply_project_transition(project, FableProjectStatus.STORY_REVIEW)
            session.commit()
            session.refresh(project)
            session.expunge(project)
            return project

    def _stub_adaptation(self, session, project: StoryProject) -> None:
        digest = hashlib.sha256(project.source_text.encode()).hexdigest()[:8]
        project.story_bible = {
            "premise": project.source_text.strip()[:200],
            "theme": project.tone or "quiet tension",
            "setting": "a small rented room on a rainy night",
            "time_period": "present day",
            "visual_style": "soft practical lighting, muted palette",
            "color_language": {"palette": "cool neutrals", "contrast": "low", "grain": "subtle"},
            "narrative_point_of_view": "third person",
            "ending": "kept as written",
            "prohibited_elements": ["real people", "minors", "explicit content"],
            "stub_fingerprint": digest,
        }
        character = FableCharacter(
            project_id=project.id, name="배우 A", role="protagonist", age_range="30s",
            adult_confirmed=True,  # the stub authors this character AS an adult
            bible={
                "face": "oval face, calm eyes", "hair": "black short hair",
                "wardrobe": "grey coat", "mannerisms": "slow deliberate movements",
                "voice_profile": {"style": "low, restrained"},
            },
        )
        location = FableLocation(
            project_id=project.id, name="호텔 창가", description="a hotel window at night, rain outside",
            continuity={"lighting": "soft practicals", "time_of_day": "night", "weather": "rain"},
        )
        session.add_all([character, location])
        session.flush()

        beats = [
            ("도입", "looking out the window"),
            ("전환", "turning slowly toward the door"),
        ]
        for scene_index, (purpose, action) in enumerate(beats, start=1):
            scene = FableScene(
                project_id=project.id, scene_order=scene_index, location_id=location.id,
                story_purpose=purpose, emotional_beat="restrained unease",
                estimated_duration_sec=2 * _STUB_SHOT_DURATION_SEC,
            )
            session.add(scene)
            session.flush()
            for shot_index, shot_size in enumerate(("medium", "medium_close_up"), start=1):
                session.add(FableShot(
                    scene_id=scene.id, shot_order=shot_index, shot_size=shot_size,
                    camera_angle="eye_level", camera_movement="locked", lens_style="50mm",
                    subject=character.name, action=action,
                    expression="calm, faintly uneasy", lighting="soft practical",
                    duration_sec=_STUB_SHOT_DURATION_SEC,
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

    def approve_shots(self, project_id: str) -> StoryProject:
        """SHOT_REVIEW -> GENERATING: THE cost gate. Every PLANNED shot
        becomes READY for the worker lane. Never called automatically."""
        with self._session_factory() as session:
            project = session.get(StoryProject, project_id)
            if project is None:
                raise FableProjectNotFoundError(project_id)
            shots = self._shots_for_project(session, project_id)
            if not shots:
                raise InvalidActionError("project has no shots to generate")
            apply_project_transition(project, FableProjectStatus.GENERATING)
            for shot in shots:
                if shot.status == FableShotStatus.PLANNED.value:
                    apply_shot_transition(shot, FableShotStatus.READY)
            session.commit()
            session.refresh(project)
            session.expunge(project)
            return project

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
