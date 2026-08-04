"""Negotiating a shot's generation parameters against provider
capabilities (pipeline.generation_plan), and the worker actually using
them.

These are regression tests for four gaps that made the real cinematic
provider unusable: the worker hardcoded a resolution no real provider
serves, passed planned durations no real provider accepts, never attached
the approved reference sheets at all, and never recorded which sheet a
take came from. Every one of them would have surfaced only as a failed
paid run.
"""
from __future__ import annotations

import pytest

from reel_harness.core.cinematic_state import DEFAULT_SHOT_RESOLUTION
from reel_harness.pipeline.generation_plan import (
    GenerationPlanConflict,
    resolve_duration,
    resolve_parameters,
    resolve_resolution,
    select_reference_images,
)
from reel_harness.providers.base import CinematicCapabilities


def _caps(**kwargs) -> CinematicCapabilities:
    defaults = {
        "text_to_video": True, "image_to_video": True, "first_frame": True,
        "last_frame": False, "character_reference": True, "multiple_references": True,
        "max_character_references": 3, "video_reference": False, "native_audio": False,
        "lip_sync": False, "supports_seed": True, "supports_negative_prompt": True,
        "supported_durations_sec": frozenset({2.0, 4.0, 6.0, 8.0}),
        "supported_aspect_ratios": frozenset({"9:16", "16:9"}),
        "supported_resolutions": frozenset({"360p"}),
        "max_concurrent_jobs": None,
    }
    return CinematicCapabilities(**{**defaults, **kwargs})


class _Character:
    def __init__(self, images=None, approved=True, fingerprint="fp") -> None:
        self.reference_images = images
        self.reference_approved = approved
        self.reference_fingerprint = fingerprint


# -- resolution ----------------------------------------------------------

def test_the_preferred_resolution_wins_when_supported() -> None:
    assert resolve_resolution(_caps(), "360p") == "360p"


def test_a_provider_with_one_resolution_is_simply_honored() -> None:
    """The 360p constant was written for the fake tier. Fighting a real
    provider that only serves 720p would be pointless -- and before this,
    the worker did exactly that and failed every shot."""
    caps = _caps(supported_resolutions=frozenset({"720p"}))
    assert resolve_resolution(caps, "360p") == "720p"


def test_an_ambiguous_resolution_mismatch_is_refused_not_guessed() -> None:
    caps = _caps(supported_resolutions=frozenset({"720p", "1080p"}))
    with pytest.raises(GenerationPlanConflict, match="no single choice"):
        resolve_resolution(caps, "360p")


def test_a_provider_with_no_resolutions_is_refused() -> None:
    with pytest.raises(GenerationPlanConflict, match="cannot generate anything"):
        resolve_resolution(_caps(supported_resolutions=frozenset()), "360p")


# -- duration ------------------------------------------------------------

def test_an_exactly_supported_duration_is_used_as_planned() -> None:
    assert resolve_duration(_caps(), 4.0) == 4.0


def test_an_unsupported_duration_rounds_UP_to_the_next_supported_one() -> None:
    """Generating less than the beat needs would cut the shot short, which
    is a worse failure than generating a little extra."""
    assert resolve_duration(_caps(), 3.0) == 4.0
    assert resolve_duration(_caps(supported_durations_sec=frozenset({8.0})), 3.0) == 8.0


def test_a_plan_longer_than_the_provider_can_generate_is_refused() -> None:
    """Silently shortening a shot changes the film."""
    caps = _caps(supported_durations_sec=frozenset({8.0}))
    with pytest.raises(GenerationPlanConflict, match="at most 8.0s"):
        resolve_duration(caps, 12.0)


def test_the_planned_duration_is_reported_alongside_the_real_one() -> None:
    """So the approval gate can show the difference rather than the
    operator finding it in the finished cut."""
    caps = _caps(supported_durations_sec=frozenset({8.0}), supported_resolutions=frozenset({"720p"}))
    parameters = resolve_parameters(caps, 3.0, DEFAULT_SHOT_RESOLUTION)
    assert parameters.duration_sec == 8.0
    assert parameters.planned_duration_sec == 3.0
    assert parameters.duration_differs is True
    assert parameters.extra_seconds == 5.0
    assert parameters.resolution == "720p"


def test_no_difference_is_reported_when_the_plan_is_honored() -> None:
    parameters = resolve_parameters(_caps(), 4.0, "360p")
    assert parameters.duration_differs is False
    assert parameters.extra_seconds == 0.0


# -- reference selection -------------------------------------------------

def test_approved_views_are_selected_in_priority_order(tmp_path) -> None:
    """Face first: it carries identity most densely. Wardrobe last: a
    garment is the easiest thing for a prompt to restate in words."""
    images = {}
    for view in ("face", "three_quarter", "full_body", "wardrobe"):
        path = tmp_path / f"{view}.png"
        path.write_bytes(b"x")
        images[view] = str(path)

    selected = select_reference_images(_Character(images), _caps(max_character_references=3))
    assert [p.stem for p in selected] == ["face", "three_quarter", "full_body"]


def test_selection_is_capped_at_what_the_provider_accepts(tmp_path) -> None:
    images = {}
    for view in ("face", "three_quarter", "full_body", "wardrobe"):
        path = tmp_path / f"{view}.png"
        path.write_bytes(b"x")
        images[view] = str(path)
    assert len(select_reference_images(_Character(images), _caps(max_character_references=1))) == 1
    assert len(select_reference_images(_Character(images), _caps(max_character_references=4))) == 4


def test_a_single_reference_provider_gets_only_the_face(tmp_path) -> None:
    images = {}
    for view in ("face", "three_quarter"):
        path = tmp_path / f"{view}.png"
        path.write_bytes(b"x")
        images[view] = str(path)
    caps = _caps(multiple_references=False, max_character_references=4)
    assert [p.stem for p in select_reference_images(_Character(images), caps)] == ["face"]


def test_an_UNAPPROVED_sheet_never_reaches_generation(tmp_path) -> None:
    """Approval is what the CHARACTER_REVIEW gate MEANS. A sheet that
    slipped through unapproved would make the gate decorative."""
    path = tmp_path / "face.png"
    path.write_bytes(b"x")
    character = _Character({"face": str(path)}, approved=False)
    assert select_reference_images(character, _caps()) == []


def test_a_provider_without_character_reference_gets_none(tmp_path) -> None:
    path = tmp_path / "face.png"
    path.write_bytes(b"x")
    caps = _caps(character_reference=False)
    assert select_reference_images(_Character({"face": str(path)}), caps) == []


def test_a_recorded_path_whose_file_vanished_is_skipped(tmp_path) -> None:
    """Losing one view is better than losing the whole generation -- the
    adapter would reject the request outright."""
    present = tmp_path / "face.png"
    present.write_bytes(b"x")
    character = _Character({"face": str(present), "three_quarter": str(tmp_path / "gone.png")})
    selected = select_reference_images(character, _caps())
    assert [p.stem for p in selected] == ["face"]


def test_a_character_with_no_sheet_yields_nothing(tmp_path) -> None:
    assert select_reference_images(_Character(None), _caps()) == []
    assert select_reference_images(None, _caps()) == []


# -- the worker actually uses all of it -----------------------------------

@pytest.fixture
def project_env(session_factory, tmp_path):
    from reel_harness.core.fable_service import FableService
    from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector
    from reel_harness.providers.fake_reference_image import FakeReferenceImageProvider
    from reel_harness.storage.local import LocalFilesystemStorage
    from tests.conftest import walk_casting

    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    fable = FableService(
        session_factory, storage=storage, narrative_director=FakeNarrativeDirector(),
        reference_provider=FakeReferenceImageProvider(),
    )
    project, _ = fable.create_project(title="t", source_text="s", idempotency_key="plan-test")
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    walk_casting(fable, project.id)
    fable.approve_characters(project.id)
    fable.approve_shots(project.id)
    return session_factory, storage, project, fable


class _RecordingProvider:
    """The fake provider, wrapped so the request it received can be
    inspected -- the only place the wiring is observable."""

    def __init__(self, inner, capabilities=None) -> None:
        self._inner = inner
        self.provider_id = inner.provider_id
        self.capabilities = capabilities or inner.capabilities
        self.requests = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def create_generation(self, request):
        self.requests.append(request)
        return self._inner.create_generation(request)

    def estimate_cost(self, request):
        return self._inner.estimate_cost(request)


def test_the_worker_attaches_the_approved_reference_sheet(project_env) -> None:
    """The gap that made casting decorative: before this, no code path
    passed a reference image to the video provider at all, so the whole
    F3 casting phase never reached generation."""
    from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
    from reel_harness.worker.fable_lease import lease_next_shot
    from reel_harness.worker.fable_runner import run_shot

    session_factory, storage, _, _ = project_env
    provider = _RecordingProvider(FakeCinematicVideoProvider())
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(session, shot, provider, storage, lease_token=shot.lease_token,
                 sleep=lambda _s: None)

    assert provider.requests, "the provider was never called"
    references = provider.requests[0].reference_image_paths
    assert references, "the approved reference sheet never reached the provider"
    assert all(p.is_file() for p in references)


def test_the_take_records_which_reference_sheet_it_used(project_env) -> None:
    """A take generated before a character was re-cast is otherwise
    indistinguishable from one generated after."""
    from reel_harness.db.cinematic_models import FableTake
    from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
    from reel_harness.worker.fable_lease import lease_next_shot
    from reel_harness.worker.fable_runner import run_shot

    session_factory, storage, _, _ = project_env
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(session, shot, FakeCinematicVideoProvider(), storage,
                 lease_token=shot.lease_token, sleep=lambda _s: None)

    with session_factory() as session:
        take = session.query(FableTake).filter(FableTake.shot_id == shot.id).first()
        assert take.reference_fingerprint, "the take does not record its reference sheet"


def test_the_worker_requests_what_a_720p_only_provider_supports(project_env) -> None:
    """The gap that failed every real-provider shot: 360p was hardcoded."""
    from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
    from reel_harness.worker.fable_lease import lease_next_shot
    from reel_harness.worker.fable_runner import run_shot

    session_factory, storage, _, _ = project_env
    inner = FakeCinematicVideoProvider()
    caps = CinematicCapabilities(
        **{**inner.capabilities.__dict__,
           "supported_resolutions": frozenset({"720p"}),
           "supported_durations_sec": frozenset({8.0})},
    )
    provider = _RecordingProvider(inner, capabilities=caps)
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(session, shot, provider, storage, lease_token=shot.lease_token,
                 sleep=lambda _s: None)

    request = provider.requests[0]
    assert request.resolution == "720p"
    assert request.duration_sec == 8.0


def test_approve_shots_refuses_a_plan_the_provider_cannot_generate(
    session_factory, tmp_path,
) -> None:
    """Told once, at the gate, for free -- instead of every shot failing
    one at a time in the worker after generation was approved."""
    from reel_harness.core.fable_service import FableService
    from reel_harness.core.service import InvalidActionError
    from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
    from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector
    from reel_harness.providers.fake_reference_image import FakeReferenceImageProvider
    from reel_harness.storage.local import LocalFilesystemStorage
    from tests.conftest import walk_casting

    inner = FakeCinematicVideoProvider()
    # A provider that can only make 1s clips cannot make any planned shot
    # (the adaptation schema's floor is 2s).
    caps = CinematicCapabilities(
        **{**inner.capabilities.__dict__, "supported_durations_sec": frozenset({1.0})},
    )
    provider = _RecordingProvider(inner, capabilities=caps)

    fable = FableService(
        session_factory, storage=LocalFilesystemStorage(tmp_path / "f"),
        narrative_director=FakeNarrativeDirector(),
        reference_provider=FakeReferenceImageProvider(),
        cinematic_provider_resolver=lambda project: provider,
    )
    project, _ = fable.create_project(title="t", source_text="s", idempotency_key="conflict")
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    walk_casting(fable, project.id)
    fable.approve_characters(project.id)

    with pytest.raises(InvalidActionError, match="cannot be generated by"):
        fable.approve_shots(project.id)
    assert fable.get_project(project.id).status == "SHOT_REVIEW"


def test_the_estimate_prices_and_reports_the_generated_runtime(
    session_factory, tmp_path,
) -> None:
    """A per-second provider generating 8s for a 3s plan would otherwise
    be budgeted at a fraction of the real charge -- and the operator would
    never see that the film got longer."""
    from reel_harness.core.fable_service import FableService
    from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
    from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector
    from reel_harness.storage.local import LocalFilesystemStorage

    inner = FakeCinematicVideoProvider()
    caps = CinematicCapabilities(
        **{**inner.capabilities.__dict__, "supported_durations_sec": frozenset({8.0})},
    )
    eight_second_only = _RecordingProvider(inner, capabilities=caps)

    fable = FableService(
        session_factory, storage=LocalFilesystemStorage(tmp_path / "f"),
        narrative_director=FakeNarrativeDirector(),
        cinematic_provider_resolver=lambda project: eight_second_only,
    )
    project, _ = fable.create_project(title="t", source_text="s", idempotency_key="runtime")
    fable.adapt_project(project.id)

    estimate = fable.estimate_cost(project.id)
    assert estimate.known is True
    assert estimate.generated_runtime_sec > estimate.planned_runtime_sec
    assert estimate.runtime_differs is True
    # The fake tier bills per second, so the price follows the real length.
    assert estimate.amount == pytest.approx(estimate.generated_runtime_sec * 0.01)


def test_a_veo_shaped_provider_now_completes_a_shot(project_env) -> None:
    """The whole point of this module, end to end: a provider with Veo's
    real constraints (720p only, 8s only, 3 references max) is driven
    through the actual worker and produces a take.

    Before the negotiation existed this failed at validate_request with
    `unsupported resolution '360p'` on every single shot -- which is what
    a real paid run would have done, one shot at a time, after the
    operator approved generation."""
    from reel_harness.db.cinematic_models import FableTake
    from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
    from reel_harness.providers.google_cinematic_video import (
        MAX_REFERENCE_IMAGES,
        REFERENCE_DURATION_SEC,
        REFERENCE_RESOLUTION,
    )
    from reel_harness.worker.fable_lease import lease_next_shot
    from reel_harness.worker.fable_runner import run_shot

    session_factory, storage, _, _ = project_env
    inner = FakeCinematicVideoProvider()
    # The real adapter's own declared limits, borrowed verbatim so this
    # tracks the adapter rather than a copy of its numbers.
    veo_shaped = CinematicCapabilities(**{
        **inner.capabilities.__dict__,
        "supported_resolutions": frozenset({REFERENCE_RESOLUTION}),
        "supported_durations_sec": frozenset({REFERENCE_DURATION_SEC}),
        "max_character_references": MAX_REFERENCE_IMAGES,
    })
    provider = _RecordingProvider(inner, capabilities=veo_shaped)

    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(session, shot, provider, storage, lease_token=shot.lease_token,
                 sleep=lambda _s: None)
        assert shot.status == "REVIEW_REQUIRED", shot.failure_summary

    request = provider.requests[0]
    assert request.resolution == REFERENCE_RESOLUTION
    assert request.duration_sec == REFERENCE_DURATION_SEC
    assert 0 < len(request.reference_image_paths) <= MAX_REFERENCE_IMAGES

    with session_factory() as session:
        take = session.query(FableTake).filter(FableTake.shot_id == shot.id).one()
        assert take.status == "DOWNLOADED"
        assert take.reference_fingerprint
