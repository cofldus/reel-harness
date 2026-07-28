"""Deterministic query building/relaxation and asset selection scoring. No
network, no ffmpeg required."""
from __future__ import annotations

from reel_harness.pipeline.asset_query import build_scene_query, relax_query, sanitize_query_text
from reel_harness.pipeline.asset_selection import SelectionPolicy, score_candidate, select_asset
from reel_harness.providers.base import MediaCandidate


def _candidate(candidate_id: str, **overrides) -> MediaCandidate:
    defaults: dict = dict(
        candidate_id=candidate_id, source_url=f"https://example.invalid/{candidate_id}",
        author="A", license_type="PEXELS_LICENSE", license_url="https://example.invalid/license",
        commercial_use_allowed=True, modification_allowed=True, content_type="video/mp4",
        width=1080, height=1920, duration_sec=6.0, provider_rank=0,
    )
    defaults.update(overrides)
    return MediaCandidate(**defaults)


def test_sanitize_strips_control_chars_punctuation_and_bounds_length() -> None:
    raw = "cats\x00\x07 chasing;; a\tlaser -- pointer!!!" + ("x" * 200)
    text = sanitize_query_text(raw, max_length=40)
    assert "\x00" not in text and "\x07" not in text
    assert ";" not in text and "!" not in text
    assert len(text) <= 40


def test_build_scene_query_uses_visual_query_not_voiceover() -> None:
    scene = {
        "voiceover": "This is a much longer narration line that must never be sent as a search query.",
        "visual_query": "cats on a windowsill",
        "duration_hint_sec": 5.0,
    }
    query = build_scene_query(
        scene, orientation="portrait", min_width=480, min_height=480, min_duration=1.0, max_duration=30.0,
    )
    assert query.text == "cats on a windowsill"
    assert query.orientation == "portrait"
    assert query.min_duration == 5.0
    assert query.relaxation_level == 0


def test_build_scene_query_falls_back_when_visual_query_sanitizes_to_empty() -> None:
    scene = {"visual_query": "\x00\x01\x02", "duration_hint_sec": 4.0}
    query = build_scene_query(
        scene, orientation="portrait", min_width=480, min_height=480, min_duration=1.0, max_duration=30.0,
    )
    assert query.text == "video"


def test_relax_query_ladder_is_deterministic_and_eventually_exhausts() -> None:
    scene = {"visual_query": "a red bicycle leaning against an old brick wall", "duration_hint_sec": 4.0}
    query = build_scene_query(
        scene, orientation="portrait", min_width=480, min_height=480, min_duration=1.0, max_duration=30.0,
    )
    level1 = relax_query(query)
    assert level1 is not None
    assert level1.relaxation_level == 1
    assert level1.text == "a red bicycle leaning"  # first 4 words
    assert level1.orientation == query.orientation  # safety conditions never relax
    assert level1.min_width == query.min_width

    level2 = relax_query(level1)
    assert level2 is not None
    assert level2.text == "a red"  # first 2 words

    level3 = relax_query(level2)
    assert level3 is None, "ladder must exhaust deterministically"


def test_relax_query_on_already_short_text_exhausts_without_looping_forever() -> None:
    scene = {"visual_query": "cat", "duration_hint_sec": 4.0}
    query = build_scene_query(
        scene, orientation="portrait", min_width=480, min_height=480, min_duration=1.0, max_duration=30.0,
    )
    assert relax_query(query) is None


def test_selection_prefers_closest_portrait_aspect_ratio() -> None:
    policy = SelectionPolicy()
    portrait = _candidate("a", width=1080, height=1920)
    landscape = _candidate("b", width=1920, height=1080)
    chosen = select_asset([landscape, portrait], policy)
    assert chosen is not None
    assert chosen.candidate_id == "a"


def test_selection_hard_filters_reject_non_commercial_or_non_modifiable() -> None:
    policy = SelectionPolicy()
    blocked_commercial = _candidate("a", commercial_use_allowed=False)
    blocked_modification = _candidate("b", modification_allowed=False)
    ok = _candidate("c")
    chosen = select_asset([blocked_commercial, blocked_modification, ok], policy)
    assert chosen is not None
    assert chosen.candidate_id == "c"


def test_selection_hard_filters_reject_missing_license() -> None:
    policy = SelectionPolicy()
    no_license = _candidate("a", license_type=None)
    ok = _candidate("c")
    assert select_asset([no_license], policy) is None
    assert select_asset([no_license, ok], policy).candidate_id == "c"


def test_selection_content_type_filter_is_opt_in_not_a_default() -> None:
    """The Protocol is media-type agnostic (the Fake provider legitimately
    returns images) -- the default policy must not silently reject them."""
    default_policy = SelectionPolicy()
    image_candidate = _candidate("a", content_type="image/png")
    assert select_asset([image_candidate], default_policy) is not None

    video_only_policy = SelectionPolicy(require_content_type_prefix="video/")
    wrong_type = _candidate("b", content_type="image/jpeg")
    ok = _candidate("c", content_type="video/mp4")
    assert select_asset([wrong_type], video_only_policy) is None
    assert select_asset([wrong_type, ok], video_only_policy).candidate_id == "c"


def test_selection_hard_filters_reject_below_min_resolution_and_out_of_duration_range() -> None:
    policy = SelectionPolicy(min_width=1000, min_height=1000, min_duration_sec=2.0, max_duration_sec=10.0)
    too_small = _candidate("a", width=100, height=100)
    too_short = _candidate("b", duration_sec=0.5)
    too_long = _candidate("c", duration_sec=99.0)
    ok = _candidate("d", width=1080, height=1920, duration_sec=6.0)
    assert select_asset([too_small, too_short, too_long, ok], policy).candidate_id == "d"


def test_selection_excludes_ids_already_used_by_another_scene() -> None:
    policy = SelectionPolicy()
    a = _candidate("a")
    b = _candidate("b")
    chosen = select_asset([a, b], policy, exclude_ids=frozenset({"a"}))
    assert chosen is not None
    assert chosen.candidate_id == "b"


def test_selection_returns_none_when_nothing_passes() -> None:
    policy = SelectionPolicy()
    assert select_asset([], policy) is None
    assert select_asset([_candidate("a", license_type=None)], policy) is None


def test_selection_tie_break_is_candidate_id_ascending_not_response_order() -> None:
    policy = SelectionPolicy()
    # Identical scoring inputs -- only candidate_id differs.
    z = _candidate("z")
    a = _candidate("a")
    assert score_candidate(z, policy) == score_candidate(a, policy)
    assert select_asset([z, a], policy).candidate_id == "a"
    assert select_asset([a, z], policy).candidate_id == "a", "must not depend on input ordering"
