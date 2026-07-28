from __future__ import annotations

from dataclasses import dataclass

from reel_harness.providers.base import MediaCandidate

SELECTION_VERSION = "asset-selection-v1"

_ORIENTATION_ASPECT = {"portrait": 9 / 16, "landscape": 16 / 9, "square": 1.0}


@dataclass(frozen=True)
class SelectionPolicy:
    """Hard filters + scoring weights for choosing one asset among a page of
    search results. Every field is deterministic input -- no randomness, no
    wall-clock, no external state -- so selection is fully reproducible given
    the same candidate list."""

    min_width: int = 480
    min_height: int = 480
    min_duration_sec: float = 1.0
    max_duration_sec: float = 60.0
    require_commercial_use: bool = True
    require_modification_allowed: bool = True
    # None = no content-type restriction (the Protocol is media-type agnostic
    # -- the Fake provider legitimately returns images). The real pipeline
    # wiring doesn't need to set this either: Pexels' video-search endpoint
    # only ever returns video/mp4 candidates in the first place. Set this
    # explicitly (e.g. "video/") only when a provider's results can mix media
    # types and only one is acceptable.
    require_content_type_prefix: str | None = None
    target_orientation: str = "portrait"
    version: str = SELECTION_VERSION


def _passes_hard_filters(candidate: MediaCandidate, policy: SelectionPolicy, exclude_ids: frozenset[str]) -> bool:
    """License and technical requirements that can never be traded off for a
    higher score -- a candidate failing any of these is not a worse choice,
    it is not a choice at all."""
    if candidate.candidate_id in exclude_ids:
        return False
    if candidate.license_type is None:
        return False
    if policy.require_commercial_use and not candidate.commercial_use_allowed:
        return False
    if policy.require_modification_allowed and not candidate.modification_allowed:
        return False
    if policy.require_content_type_prefix is not None and candidate.content_type is not None and (
        not candidate.content_type.startswith(policy.require_content_type_prefix)
    ):
        return False
    if candidate.width is not None and candidate.width < policy.min_width:
        return False
    if candidate.height is not None and candidate.height < policy.min_height:
        return False
    if candidate.duration_sec is not None and not (
        policy.min_duration_sec <= candidate.duration_sec <= policy.max_duration_sec
    ):
        return False
    return True


def score_candidate(candidate: MediaCandidate, policy: SelectionPolicy) -> float:
    """Higher is better. Rewards aspect-ratio fit to the target orientation
    (dominant term), a mild resolution bonus, closeness of duration to the
    policy's midpoint, and the provider's own result ranking."""
    target_ratio = _ORIENTATION_ASPECT.get(policy.target_orientation, 9 / 16)
    score = 0.0
    if candidate.width and candidate.height:
        actual_ratio = candidate.width / candidate.height
        aspect_fit = max(0.0, 1.0 - abs(actual_ratio - target_ratio))
        score += aspect_fit * 50.0
        score += min(candidate.height, 2160) / 2160 * 20.0
    if candidate.duration_sec is not None:
        midpoint = (policy.min_duration_sec + policy.max_duration_sec) / 2
        span = max(policy.max_duration_sec - policy.min_duration_sec, 1.0)
        duration_fit = max(0.0, 1.0 - abs(candidate.duration_sec - midpoint) / span)
        score += duration_fit * 20.0
    score += max(0.0, 10.0 - candidate.provider_rank)
    return score


def select_asset(
    candidates: list[MediaCandidate], policy: SelectionPolicy, exclude_ids: frozenset[str] = frozenset(),
) -> MediaCandidate | None:
    """Filters candidates through the hard license/technical requirements,
    scores the survivors, and returns the highest-scoring one. Ties (including
    every candidate scoring identically) are broken by candidate_id ascending
    so the result never depends on provider response ordering."""
    eligible = [c for c in candidates if _passes_hard_filters(c, policy, exclude_ids)]
    if not eligible:
        return None
    ranked = sorted(eligible, key=lambda c: (-score_candidate(c, policy), c.candidate_id))
    return ranked[0]
