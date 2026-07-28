from __future__ import annotations

import re
from dataclasses import dataclass, replace

QUERY_VERSION = "asset-query-v1"

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_DISALLOWED_CHARS_RE = re.compile(r"[^\w\s,.'-]", re.UNICODE)
MAX_QUERY_LENGTH = 100

# Deterministic relaxation ladder: how many leading words of the sanitized
# query survive at each level (None = unchanged). Only the query TEXT
# broadens on relaxation -- orientation, min resolution, duration bounds, and
# safe_search are never relaxed (see docs/OPERATIONS.md).
_RELAXATION_WORD_COUNTS: tuple[int | None, ...] = (None, 4, 2)
MAX_RELAXATION_LEVEL = len(_RELAXATION_WORD_COUNTS) - 1


@dataclass(frozen=True)
class AssetQuery:
    text: str
    orientation: str
    min_duration: float
    max_duration: float
    min_width: int
    min_height: int
    safe_search: bool
    relaxation_level: int = 0
    version: str = QUERY_VERSION


def sanitize_query_text(raw: str, max_length: int = MAX_QUERY_LENGTH) -> str:
    """Deterministic, rule-based sanitization of an LLM-authored visual_query
    into a string safe to send to a stock-media search API: strips control
    characters and punctuation the query has no business carrying, collapses
    whitespace, and bounds length. Never sends the scene's full narration --
    only its own short visual_query."""
    text = _CONTROL_CHARS_RE.sub(" ", raw)
    text = _DISALLOWED_CHARS_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:max_length].strip()


def build_scene_query(
    scene: dict, *,
    orientation: str,
    min_width: int,
    min_height: int,
    min_duration: float,
    max_duration: float,
    safe_search: bool = True,
) -> AssetQuery:
    """Builds a deterministic search query for one script scene, using only
    the scene's own visual_query field (never the voiceover/narration text)."""
    raw = str(scene.get("visual_query") or "")
    text = sanitize_query_text(raw) or "video"
    duration_hint = float(scene.get("duration_hint_sec") or 0.0)
    scene_min_duration = min(max(duration_hint, min_duration), max_duration) if duration_hint > 0 else min_duration
    return AssetQuery(
        text=text, orientation=orientation, min_duration=scene_min_duration, max_duration=max_duration,
        min_width=min_width, min_height=min_height, safe_search=safe_search,
    )


def relax_query(query: AssetQuery) -> AssetQuery | None:
    """One step of the deterministic relaxation ladder, applied when a search
    returns no candidates for the current query text. Returns None once the
    ladder is exhausted (the caller must then fail explicitly, e.g.
    ASSET_NOT_FOUND -- relaxing never means loosening a safety condition)."""
    next_level = query.relaxation_level + 1
    if next_level > MAX_RELAXATION_LEVEL:
        return None
    word_count = _RELAXATION_WORD_COUNTS[next_level]
    words = query.text.split(" ")
    if word_count is None or word_count >= len(words):
        # This level would not actually broaden anything -- move straight to
        # the next rung without changing the text yet.
        return relax_query(replace(query, relaxation_level=next_level))
    return replace(query, text=" ".join(words[:word_count]), relaxation_level=next_level)
