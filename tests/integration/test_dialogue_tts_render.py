"""The synthesised-dialogue render path, end to end on the fake tier.

Costs nothing and exercises the whole chain: a project reaches EDITING,
the render synthesises every spoken line, mixes it over the assembled
film and validates the result. What it proves is that the wiring holds --
setting to service to provider to ffmpeg -- which is exactly the part
that unit tests of the pure helpers cannot reach.
"""
from __future__ import annotations

import pytest

from reel_harness.media.deps import check_ffmpeg_available

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="ffmpeg/ffprobe not available")

STORY = (
    "늦은 밤 편의점. 스무 살의 준호는 창밖을 보고 있었다. "
    '검은 코트의 노인이 들어왔다. "우산 있나?" 노인이 물었다. '
    '준호는 낡은 우산을 건넸다. "가져가세요." '
    "노인은 우산을 들고 빗속으로 걸어 나갔다."
)


def _project_to_editing(ctx):
    project, _ = ctx.fable.create_project(
        title="대사 합성", source_text=STORY, idempotency_key="tts-render-1",
        target_duration_sec=32,
    )
    ctx.fable.set_budget(project.id, limit_amount=100.0, currency="FAKE")
    ctx.fable.adapt_project(project.id)
    ctx.fable.approve_story(project.id)
    ctx.fable.generate_references(project.id)
    for character in ctx.fable.project_characters(project.id):
        ctx.fable.approve_reference(character.id)
    ctx.fable.approve_characters(project.id)
    ctx.fable.approve_shots(project.id)

    from reel_harness.worker.fable_daemon import FableDaemon, FableDaemonConfig

    daemon = FableDaemon(
        ctx.session_factory, storage=ctx.fable_storage,
        provider_for_shot=ctx.cinematic_provider_for_shot,
        config=FableDaemonConfig(
            worker_id="tts-test", poll_interval_seconds=0.0,
            lease_timeout_seconds=300, heartbeat_interval_seconds=60,
            allow_paid_generation=False,
            takes_per_shot=ctx.settings.fable_takes_per_shot,
            spoken_by_video=False,
        ),
    )
    for _ in range(50):
        if daemon._poll_once() is None:
            break
    for shot in ctx.fable.project_shots(project.id):
        for take in ctx.fable.shot_takes(shot.id):
            if take.status == "DOWNLOADED":
                ctx.fable.select_take(take.id)
    return project


def _ctx(tmp_path, dialogue_source: str):
    from reel_harness.bootstrap import AppContext
    from reel_harness.config import Settings

    return AppContext(Settings(
        database_url=f"sqlite:///{(tmp_path / 'tts.db').as_posix()}",
        jobs_dir=tmp_path / "jobs",
        fable_projects_dir=tmp_path / "fable",
        credential_dir=tmp_path / "creds",
        narrative_provider="fake", cinematic_provider="fake",
        reference_image_provider="fake", tts_provider="fake",
        fable_dialogue_source=dialogue_source,
    ))


def test_a_film_with_synthesised_dialogue_renders_and_validates(tmp_path) -> None:
    ctx = _ctx(tmp_path, "tts")
    project = _project_to_editing(ctx)
    assert ctx.fable.get_project(project.id).status == "EDITING"

    final = ctx.fable.render_final(project.id)
    assert final.is_file()
    assert ctx.fable.get_project(project.id).status == "FINAL_REVIEW"

    # The mix is in place of the assembly, not beside it.
    assert not final.with_name("final.mixed.mp4").exists()

    from reel_harness.media.ffprobe_validate import build_ffprobe_argv, parse_ffprobe_output
    from reel_harness.media.runner import run

    probe = run(build_ffprobe_argv(check_ffmpeg_available().ffprobe.path, final))
    assert probe.returncode == 0
    parsed = parse_ffprobe_output(probe.stdout)
    assert parsed.duration_sec > 0


def test_the_video_path_still_works_untouched(tmp_path) -> None:
    """The switch defaults to video and that path must be unaffected."""
    ctx = _ctx(tmp_path, "video")
    project = _project_to_editing(ctx)
    final = ctx.fable.render_final(project.id)
    assert final.is_file()
    assert ctx.fable.get_project(project.id).status == "FINAL_REVIEW"


def test_synthesis_failure_stops_the_render_rather_than_shipping_a_mute_film(tmp_path) -> None:
    """A film that quietly comes back with no dialogue is indistinguishable
    from one where the synthesiser was never called -- and the video is
    already paid for by this point."""
    from reel_harness.core.errors import ValidationFailedError

    ctx = _ctx(tmp_path, "tts")
    project = _project_to_editing(ctx)

    class Broken:
        provider_id = "broken"

        def synthesize(self, *args, **kwargs):
            raise RuntimeError("upstream is down")

    ctx.fable._tts_resolver = lambda: Broken()
    with pytest.raises(ValidationFailedError) as excinfo:
        ctx.fable.render_final(project.id)
    assert "speech synthesis failed" in str(excinfo.value)
    # Still EDITING, so re-running retries without regenerating a shot.
    assert ctx.fable.get_project(project.id).status == "EDITING"
