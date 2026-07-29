from __future__ import annotations

import re

from reel_harness.manifest.schema import Manifest
from reel_harness.providers.base import PublicationMetadata

METADATA_VERSION = "publish-metadata-v1"

# Limits per the official video resource schema (checked 2026-07-28 -- see
# docs/PUBLISHING.md): title max 100 chars, description max 5000 BYTES,
# tags max 500 chars total (commas/quotes count).
TITLE_MAX_LENGTH = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_TOTAL_CHARS = 500
MAX_EMOJI_COUNT = 3

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_ANGLE_BRACKETS_RE = re.compile(r"[<>]")  # titles/descriptions must not contain < or >
_INLINE_WHITESPACE_RE = re.compile(r"[ \t]+")
# A conservative emoji/pictograph/flag block range -- a soft cosmetic cap,
# not a full Unicode emoji database.
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]")


def _limit_emoji(text: str, max_count: int = MAX_EMOJI_COUNT) -> str:
    count = 0
    kept = []
    for ch in text:
        if _EMOJI_RE.match(ch):
            count += 1
            if count > max_count:
                continue
        kept.append(ch)
    return "".join(kept)


def _sanitize_line(raw: str) -> str:
    """Strips control characters and angle brackets, collapses runs of
    spaces/tabs (never newlines -- callers that build multi-line text split
    on lines first), trims. Safe for one line of title/description/tag
    text."""
    text = _CONTROL_CHARS_RE.sub(" ", raw)
    text = _ANGLE_BRACKETS_RE.sub("", text)
    text = _INLINE_WHITESPACE_RE.sub(" ", text).strip()
    return text


def build_title(topic: str, script_title: str) -> str:
    raw = script_title or topic or "Untitled"
    text = _limit_emoji(_sanitize_line(raw))
    if not text:
        text = "Untitled"
    if len(text) > TITLE_MAX_LENGTH:
        text = text[:TITLE_MAX_LENGTH].rstrip()
    return text


def build_description(manifest: Manifest, footer_template: str | None = None) -> str:
    """Built only from already-safe manifest fields (script title/topic,
    asset attribution/author/source-page-URL) plus an optional operator-
    supplied footer -- never a local file path, an API key, a signed URL,
    the raw job id, a provider response body, or a prompt. AssetInfo's
    source_url/source_page_url are themselves already page URLs, never a
    local path or a signed download URL (see manifest.schema.AssetInfo /
    providers.pexels_stock_media)."""
    lines = [_sanitize_line(manifest.script_title or manifest.topic or "")]

    attributions: list[str] = []
    for asset in manifest.assets:
        if asset.attribution_text:
            attributions.append(_sanitize_line(asset.attribution_text))
        elif asset.author:
            note = asset.author
            if asset.source_page_url:
                note = f"{note} ({asset.source_page_url})"
            attributions.append(_sanitize_line(note))
    if attributions:
        lines.append("")
        lines.append("Footage credit:")
        seen: list[str] = []
        for a in attributions:
            if a not in seen:
                seen.append(a)
        lines.extend(f"- {a}" for a in seen)

    if footer_template:
        lines.append("")
        lines.append(_sanitize_line(footer_template))

    text = "\n".join(lines)
    encoded = text.encode("utf-8")
    if len(encoded) > DESCRIPTION_MAX_BYTES:
        text = encoded[:DESCRIPTION_MAX_BYTES].decode("utf-8", errors="ignore")
    return text


def build_tags(script_title: str, topic: str, channel_niche: str | None = None) -> list[str]:
    """Deterministic: same inputs always produce the same tag list in the
    same order (first-seen-wins dedup, no randomness)."""
    candidates: list[str] = []
    for source in (channel_niche, script_title, topic):
        if not source:
            continue
        for word in re.split(r"[\s,]+", source):
            cleaned = _sanitize_line(word)
            if cleaned and len(cleaned) >= 2:
                candidates.append(cleaned.lower())

    deduped: list[str] = []
    for tag in candidates:
        if tag not in deduped:
            deduped.append(tag)

    tags: list[str] = []
    total_len = 0
    for tag in deduped:
        added = len(tag) + (1 if tags else 0)  # +1 models the comma YouTube counts between tags
        if total_len + added > TAGS_MAX_TOTAL_CHARS:
            break
        tags.append(tag)
        total_len += added
    return tags


def build_publication_metadata(
    manifest: Manifest, *,
    privacy_status: str,
    category_id: str,
    made_for_kids: bool,
    channel_niche: str | None = None,
    footer_template: str | None = None,
    platform_options: dict | None = None,
) -> PublicationMetadata:
    """`title` doubles as TikTok's post caption (see
    providers.tiktok_publisher.build_post_text, which validates it against
    TikTok's own length/forbidden-marker rules) -- `build_title`'s 100-char
    cap is always far under TikTok's 2200-UTF-16-unit limit, so the same
    deterministic, manifest-only-derived title is safe to reuse as-is
    rather than building a second, TikTok-specific caption. `platform_options`
    carries TikTok-specific fields (see providers.base.PublicationMetadata's
    docstring); a provider without a concept of this (YouTube) always gets
    an empty dict."""
    return PublicationMetadata(
        title=build_title(manifest.topic, manifest.script_title),
        description=build_description(manifest, footer_template=footer_template),
        tags=build_tags(manifest.script_title, manifest.topic, channel_niche),
        category_id=category_id, privacy_status=privacy_status, made_for_kids=made_for_kids,
        platform_options=dict(platform_options) if platform_options else {},
    )


def metadata_fingerprint(
    provider: str, account_reference: str, job_id: str, final_video_checksum: str,
    metadata: PublicationMetadata,
) -> str:
    """A deterministic, safe reconciliation marker: a hash over exactly the
    facts that together uniquely identify "this specific upload attempt"
    (provider + account + job + final video bytes + the exact metadata that
    would be/was sent) -- never persisted anywhere visible to YouTube (it is
    NOT embedded in the description; see docs/PUBLISHING.md for why: an
    internal id in user-visible text has no upside and a real, if small,
    downside -- it leaks internal identifiers to anyone who reads the
    description). Recomputing this before a retry and comparing it against
    the value stored at upload-session-creation time confirms the retry is
    for the exact same intended upload, not a stale/changed one."""
    import hashlib
    import json

    payload = json.dumps({
        "provider": provider, "account_reference": account_reference,
        "job_id": job_id, "final_video_checksum": final_video_checksum,
        "title": metadata.title, "description": metadata.description,
        "tags": list(metadata.tags), "category_id": metadata.category_id,
        "privacy_status": metadata.privacy_status, "made_for_kids": metadata.made_for_kids,
        "platform_options": metadata.platform_options,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
