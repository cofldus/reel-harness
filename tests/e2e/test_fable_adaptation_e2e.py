"""Fable F2 end to end, fully offline: a real story goes through the
REAL adaptation pipeline (Fake director -> real parser -> real schema ->
real semantic and fidelity validators -> real persistence), then the
REAL worker lane generates takes using the REAL canonical prompt
compiler, and the selected takes render to a validated final film.

Nothing between the network boundary and the mp4 is stubbed. Real
ffmpeg required (the fake video provider materializes real clips).

This is the F2 completion bar for the offline path; live adaptation
against a real LLM endpoint is credential-gated and reported separately
-- never claimed here."""
from __future__ import annotations

import pytest

from reel_harness.core.fable_service import FableService
from reel_harness.db.cinematic_models import FableCharacter, FableTake, StoryProject
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.ffprobe_validate import build_ffprobe_argv, parse_ffprobe_output
from reel_harness.media.runner import run
from reel_harness.pipeline.shot_prompt import COMPILER_VERSION, prompt_fingerprint
from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector
from reel_harness.storage.local import LocalFilesystemStorage
from reel_harness.worker.fable_daemon import FableDaemon, FableDaemonConfig
from tests.conftest import walk_casting

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg for real clips")

STORY = (
    "그날 밤, 지우는 호텔 창밖의 비를 오래 바라보았다. "
    "전화벨이 울렸지만 받지 않았다. "
    "마침내 그녀는 천천히 문 쪽으로 돌아섰다."
)


def _service(session_factory, storage, director=None) -> FableService:
    from reel_harness.providers.fake_reference_image import FakeReferenceImageProvider

    return FableService(
        session_factory, storage=storage,
        provider_snapshot={"cinematic_provider": "fake", "narrative_provider": "fake"},
        narrative_director=director or FakeNarrativeDirector(),
        reference_provider=FakeReferenceImageProvider(),
    )


def test_adaptation_to_final_film_end_to_end(session_factory, tmp_path) -> None:
    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    fable = _service(session_factory, storage)

    project, _ = fable.create_project(
        title="비 오는 밤", source_text=STORY, idempotency_key="f2-e2e",
        genre="drama", tone="quiet",
    )

    # 1. Real adaptation -- validated document persisted as real entities.
    adapted = fable.adapt_project(project.id)
    assert adapted.status == "STORY_REVIEW"
    assert adapted.adaptation_fingerprint
    assert adapted.story_bible["logline"]
    assert adapted.story_bible["prohibited_elements"] == [
        "real people", "minors", "explicit content",
    ]

    with session_factory() as session:
        characters = session.query(FableCharacter).filter(
            FableCharacter.project_id == project.id,
        ).all()
        assert characters, "adaptation must produce at least one character"
        for character in characters:
            assert character.adult_confirmed is True
            assert character.age_range in {"20s", "30s", "40s", "50s", "60s"}
            assert character.bible["fixed_identity"], "compiler needs fixed identity"

    # Scene beats are genuine quotes from the source -- the fidelity
    # validator actually had something to check.
    normalized_source = STORY.replace(" ", "")
    shots = fable.project_shots(project.id)
    assert 4 <= len(shots) <= 15
    for shot in shots:
        beat = (shot.continuity_requirements or {}).get("source_beat", "")
        assert beat.replace(" ", "") in normalized_source

    # 2. Gates, then the REAL worker lane with the REAL prompt compiler.
    fable.approve_story(project.id)
    walk_casting(fable, project.id)
    fable.approve_characters(project.id)
    fable.approve_shots(project.id)

    daemon = FableDaemon(
        session_factory, storage, lambda shot: FakeCinematicVideoProvider(),
        FableDaemonConfig(
            worker_id="f2-e2e-worker", poll_interval_seconds=0.05,
            lease_timeout_seconds=300, heartbeat_interval_seconds=0.5,
            idle_exit_after_seconds=0.5,
        ),
    )
    assert daemon.run() == 0
    assert daemon.shots_processed == len(shots)
    assert fable.get_project(project.id).status == "TAKE_REVIEW"

    # 3. Every take carries a versioned compiler fingerprint, and each
    # shot's fingerprint is distinct (different actions compile
    # differently) -- the idempotency identity is real, not a constant.
    with session_factory() as session:
        takes = session.query(FableTake).all()
        assert len(takes) == len(shots)
        fingerprints = {take.prompt_fingerprint for take in takes}
        assert all(len(fp) == 32 for fp in fingerprints)
        assert len(fingerprints) > 1
        assert prompt_fingerprint("probe") != COMPILER_VERSION  # versioned hash, not a passthrough

    # 4. Selection -> final render -> validated film.
    for shot in fable.project_shots(project.id):
        take = fable.shot_takes(shot.id)[0]
        fable.select_take(take.id)
    assert fable.get_project(project.id).status == "EDITING"

    final_path = fable.render_final(project.id)
    assert final_path.exists()
    deps = check_ffmpeg_available()
    probe = run(build_ffprobe_argv(deps.ffprobe.path, final_path))
    assert probe.returncode == 0
    result = parse_ffprobe_output(probe.stdout)
    assert result.video_codec == "h264"
    assert result.has_audio_stream is True
    assert result.width == 360 and result.height == 640

    assert fable.approve_final(project.id).status == "COMPLETED"


def test_repair_loop_recovers_end_to_end(session_factory, tmp_path) -> None:
    """A director whose first output is schema-invalid still produces a
    fully-persisted adaptation -- the repair path is exercised through
    real persistence, not just in isolation."""
    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    director = FakeNarrativeDirector(mode="invalid_once")
    fable = _service(session_factory, storage, director)

    project, _ = fable.create_project(
        title="t", source_text=STORY, idempotency_key="f2-repair-e2e",
    )
    adapted = fable.adapt_project(project.id)

    assert adapted.status == "STORY_REVIEW"
    assert director.repair_calls == 1
    assert len(fable.project_shots(project.id)) >= 4


def test_failed_adaptation_leaves_no_partial_state(session_factory, tmp_path) -> None:
    """Repair exhaustion must leave the project adaptable again with zero
    orphaned children -- a half-written adaptation is never observable."""
    from reel_harness.pipeline.adaptation_parser import AdaptationValidationError

    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    fable = _service(session_factory, storage, FakeNarrativeDirector(mode="always_invalid"))

    project, _ = fable.create_project(
        title="t", source_text=STORY, idempotency_key="f2-fail-e2e",
    )
    with pytest.raises(AdaptationValidationError):
        fable.adapt_project(project.id)

    assert fable.project_shots(project.id) == []
    with session_factory() as session:
        db_project = session.get(StoryProject, project.id)
        assert db_project.status == "ADAPTING"  # resumable, not a dead end
        assert db_project.adaptation_fingerprint is None

    # A working director resumes the same project to completion.
    recovered = fable.adapt_project(project.id, director=FakeNarrativeDirector())
    assert recovered.status == "STORY_REVIEW"
    assert len(fable.project_shots(project.id)) >= 4
