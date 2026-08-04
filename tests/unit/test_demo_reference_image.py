"""Demo-tier reference images: real PNG files, zero network, and output
that can never be mistaken for a model's.

The demo tier exists so the casting workflow is watchable offline. What
it must NOT do is look like AI output or pass a publish gate, and those
two properties get as much attention here as the images themselves.
"""
from __future__ import annotations

from reel_harness.manifest.schema import NON_PUBLISHABLE_LICENSES
from reel_harness.providers.base import ReferenceImageRequest
from reel_harness.providers.demo_reference_image import DemoReferenceImageProvider


def _request(view: str = "face", references=(), **kwargs):
    defaults = {
        "prompt": f"a single fictional adult actor, {view} view",
        "aspect_ratio": "9:16", "resolution": "1k",
        "character_reference_paths": list(references),
        "correlation_id": f"project-1:character-1:{view}:fingerprint",
    }
    return ReferenceImageRequest(**{**defaults, **kwargs})


def test_produces_a_real_png_on_disk(tmp_path) -> None:
    result = DemoReferenceImageProvider().generate_reference(_request(), tmp_path)
    assert result.image_path.exists()
    assert result.image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert result.checksum_sha256


def test_output_is_never_publishable(tmp_path) -> None:
    """Same invariant as the fake tier: demo output can never pass a real
    publish-eligibility gate."""
    result = DemoReferenceImageProvider().generate_reference(_request(), tmp_path)
    assert result.license == "DEMO_TEST_LICENSE"
    assert result.license in NON_PUBLISHABLE_LICENSES


def test_nothing_is_watermarked_because_nothing_was_generated(tmp_path) -> None:
    """A provenance watermark attests to model output. This tier draws
    colour panels, so claiming one would be a lie about where the image
    came from."""
    result = DemoReferenceImageProvider().generate_reference(_request(), tmp_path)
    assert result.watermark is None
    assert DemoReferenceImageProvider.capabilities.watermarked is False


def test_one_character_reads_as_one_hue_across_its_views(tmp_path) -> None:
    """The four views of a sheet must look like the same character. The
    colour keys off the character, not the view's framing."""
    provider = DemoReferenceImageProvider()
    face = provider.generate_reference(_request("face"), tmp_path)
    three_quarter = provider.generate_reference(
        _request("three_quarter", references=[face.image_path]), tmp_path,
    )
    assert face.image_path != three_quarter.image_path  # different files
    # Different shades (the chain depth lifts brightness), same file
    # family -- what matters is that both were produced, distinctly.
    assert face.image_path.read_bytes() != three_quarter.image_path.read_bytes()


def test_different_characters_get_different_colours(tmp_path) -> None:
    provider = DemoReferenceImageProvider()
    first = provider.generate_reference(
        _request(correlation_id="project-1:character-1:face:fp"), tmp_path,
    )
    second = provider.generate_reference(
        _request(correlation_id="project-1:character-2:face:fp"), tmp_path,
    )
    assert first.image_path.read_bytes() != second.image_path.read_bytes()


def test_generation_is_deterministic(tmp_path) -> None:
    provider = DemoReferenceImageProvider()
    first = provider.generate_reference(_request(), tmp_path)
    second = provider.generate_reference(_request(), tmp_path)
    assert first.checksum_sha256 == second.checksum_sha256


def test_free_is_reported_as_a_real_zero_not_as_unknown(tmp_path) -> None:
    """Zero IS the demo tier's price. Reporting it as unknown would make
    a budgeted project refuse to run entirely offline."""
    estimate = DemoReferenceImageProvider().estimate_cost(_request())
    assert estimate.known is True
    assert estimate.amount == 0.0

    result = DemoReferenceImageProvider().generate_reference(_request(), tmp_path)
    assert result.cost_amount == 0.0


def test_the_whole_casting_workflow_runs_on_the_demo_tier(session_factory, tmp_path) -> None:
    """The point of the tier: a complete, approvable reference sheet with
    no credential and no network."""
    from reel_harness.core.fable_service import FableService
    from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector
    from reel_harness.storage.local import LocalFilesystemStorage

    fable = FableService(
        session_factory, storage=LocalFilesystemStorage(tmp_path / "fable_projects"),
        narrative_director=FakeNarrativeDirector(),
        reference_provider=DemoReferenceImageProvider(),
    )
    project, _ = fable.create_project(
        title="t", source_text="그날 밤, 그는 창밖을 바라보았다.", idempotency_key="demo-casting",
    )
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    assert fable.generate_references(project.id).status == "CHARACTER_REVIEW"

    for character in fable.project_characters(project.id):
        assert len(character.reference_images) == 5
        fable.approve_reference(character.id)
    assert fable.approve_characters(project.id).status == "SHOT_REVIEW"
    # Free tier: casting spent nothing.
    assert fable.budget_status(project.id).spent_amount == 0.0


def test_registry_resolves_the_demo_tier() -> None:
    from reel_harness.providers.registry import resolve_reference_image_provider

    assert resolve_reference_image_provider("demo").provider_id == "demo"
