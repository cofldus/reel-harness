"""pipeline.stages.run_asset_fetching: deterministic search -> select ->
download, cross-scene dedup, query relaxation, and ASSET_NOT_FOUND when
nothing eligible survives. Uses a hand-written stub provider (not
FakeStockMediaProvider) so the test controls exactly what each search call
returns."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from reel_harness.core.errors import ReviewRequiredSignal
from reel_harness.pipeline.stages import run_asset_fetching
from reel_harness.providers.base import LocalAssetResult, MediaCandidate


class _FakeJob:
    id = "job-1"

    def __init__(self, scenes: list[dict]) -> None:
        self.script = {"scenes": scenes}


class _FakeStorage:
    def __init__(self, root: Path) -> None:
        self._root = root

    def job_dir(self, job_id: str) -> Path:
        return self._root / job_id


def _candidate(candidate_id: str, rank: int = 0) -> MediaCandidate:
    return MediaCandidate(
        candidate_id=candidate_id, source_url=f"https://example.invalid/{candidate_id}",
        author="A", license_type="PEXELS_LICENSE", license_url="https://example.invalid/license",
        provider_id="stub", download_url=f"https://example.invalid/{candidate_id}/dl",
        commercial_use_allowed=True, modification_allowed=True, attribution_text="A on Stub",
        width=1080, height=1920, duration_sec=6.0, content_type="video/mp4", provider_rank=rank,
    )


class _StubStockMedia:
    """Records every search() call's query text and honors exclude ids like a
    real provider would."""

    provider_id = "stub"

    def __init__(self, candidates_by_query: dict[str, list[MediaCandidate]]) -> None:
        self._by_query = candidates_by_query
        self.search_calls: list[tuple[str, frozenset]] = []

    def search(self, query, orientation, min_duration, **kwargs):
        exclude = kwargs.get("exclude_provider_asset_ids", frozenset())
        self.search_calls.append((query, exclude))
        results = self._by_query.get(query, [])
        return [c for c in results if c.candidate_id not in exclude]

    def download(self, candidate: MediaCandidate, dest_dir: Path) -> LocalAssetResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{candidate.candidate_id}.mp4"
        data = candidate.candidate_id.encode()
        path.write_bytes(data)
        return LocalAssetResult(
            local_path=path, checksum_sha256=hashlib.sha256(data).hexdigest(), mime_type="video/mp4",
            source_url=candidate.source_url, author=candidate.author, license_type=candidate.license_type,
            provider_id=candidate.provider_id, provider_asset_id=candidate.candidate_id,
            commercial_use_allowed=candidate.commercial_use_allowed,
            modification_allowed=candidate.modification_allowed, attribution_text=candidate.attribution_text,
            width=candidate.width, height=candidate.height, duration_sec=candidate.duration_sec,
        )


def _scene(visual_query: str, duration: float = 4.0) -> dict:
    return {"voiceover": "narration text never sent as a query", "visual_query": visual_query,
            "duration_hint_sec": duration}


def test_two_scenes_with_the_same_query_get_different_assets(tmp_path) -> None:
    job = _FakeJob([_scene("a red bicycle"), _scene("a red bicycle")])
    same_query_results = [_candidate("only-one", rank=0)]
    provider = _StubStockMedia({"a red bicycle": same_query_results})
    storage = _FakeStorage(tmp_path)

    with pytest.raises(ReviewRequiredSignal) as exc_info:
        run_asset_fetching(job, provider, storage)
    assert exc_info.value.reason_code == "ASSET_NOT_FOUND"
    # Scene 0 took the only candidate; scene 1's search excluded it and the
    # relaxation ladder (same single-candidate pool) never finds a fresh one.


def test_dedup_picks_a_different_candidate_when_more_than_one_is_eligible(tmp_path) -> None:
    job = _FakeJob([_scene("a red bicycle"), _scene("a red bicycle")])
    pool = [_candidate("first", rank=0), _candidate("second", rank=1)]
    provider = _StubStockMedia({"a red bicycle": pool})
    storage = _FakeStorage(tmp_path)

    results = run_asset_fetching(job, provider, storage)
    assert len(results) == 2
    assert results[0].provider_asset_id != results[1].provider_asset_id
    assert {results[0].provider_asset_id, results[1].provider_asset_id} == {"first", "second"}


def test_query_relaxation_ladder_is_exercised_when_full_query_has_no_results(tmp_path) -> None:
    job = _FakeJob([_scene("a red bicycle leaning against an old brick wall")])
    # Only the maximally-relaxed 2-word query has any candidates.
    provider = _StubStockMedia({"a red": [_candidate("relaxed-hit")]})
    storage = _FakeStorage(tmp_path)

    results = run_asset_fetching(job, provider, storage)
    assert len(results) == 1
    assert results[0].provider_asset_id == "relaxed-hit"
    assert results[0].query_text == "a red bicycle leaning against an old brick wall", (
        "the persisted query_text records the ORIGINAL scene query, not the relaxed one"
    )
    queries_tried = [q for q, _ in provider.search_calls]
    assert "a red bicycle leaning" in queries_tried, "the ladder must have tried the 4-word rung first"
    assert queries_tried[-1] == "a red"


def test_asset_not_found_after_relaxation_ladder_is_exhausted(tmp_path) -> None:
    job = _FakeJob([_scene("nothing matches this")])
    provider = _StubStockMedia({})  # every query returns []
    storage = _FakeStorage(tmp_path)

    with pytest.raises(ReviewRequiredSignal) as exc_info:
        run_asset_fetching(job, provider, storage)
    assert exc_info.value.reason_code == "ASSET_NOT_FOUND"


def test_license_ineligible_candidates_never_force_a_success(tmp_path) -> None:
    """A candidate that fails the hard license filters must never be selected
    just because it's the only thing returned -- relaxing the query text must
    never mean relaxing a license condition."""
    ineligible = MediaCandidate(
        candidate_id="bad-license", source_url="https://example.invalid/bad", author="A",
        license_type="PEXELS_LICENSE", license_url="https://example.invalid/license", provider_id="stub",
        download_url="https://example.invalid/bad/dl", commercial_use_allowed=False,  # <-- disqualifying
        modification_allowed=True, width=1080, height=1920, duration_sec=6.0, content_type="video/mp4",
    )
    job = _FakeJob([_scene("a red bicycle")])
    provider = _StubStockMedia({"a red bicycle": [ineligible]})
    storage = _FakeStorage(tmp_path)

    with pytest.raises(ReviewRequiredSignal) as exc_info:
        run_asset_fetching(job, provider, storage)
    assert exc_info.value.reason_code == "ASSET_NOT_FOUND"


def test_downloads_land_under_dest_root_when_given(tmp_path) -> None:
    job = _FakeJob([_scene("a red bicycle")])
    provider = _StubStockMedia({"a red bicycle": [_candidate("only")]})
    storage = _FakeStorage(tmp_path / "should-not-be-used")
    temp_root = tmp_path / "worker-temp"

    results = run_asset_fetching(job, provider, storage, dest_root=temp_root)
    assert results[0].local_path.is_relative_to(temp_root)
