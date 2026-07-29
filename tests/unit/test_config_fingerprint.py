from __future__ import annotations

import json

from reel_harness.config import Settings
from reel_harness.ops.fingerprint import config_fingerprint, fingerprint_hash


def test_fingerprint_never_contains_secrets() -> None:
    settings = Settings(
        llm_provider="openai_compatible", llm_base_url="https://llm.example.com/v1", llm_model="gpt-x",
        llm_api_key="sk-super-secret-llm-key",
        tts_provider="openai_compatible", tts_base_url="https://tts.example.com/v1",
        tts_api_key="tts-super-secret-key",
        asset_provider="pexels", asset_base_url="https://api.pexels.com/videos",
        asset_api_key="pexels-super-secret-key",
        youtube_client_id="yt-client", youtube_client_secret="yt-super-secret",
        tiktok_client_key="tt-client", tiktok_client_secret="tt-super-secret",
        instagram_app_id="ig-app", instagram_app_secret="ig-super-secret",
        app_api_key="app-super-secret-api-key",
    )
    fingerprint = config_fingerprint(settings)
    blob = json.dumps(fingerprint).lower()
    for secret in (
        "sk-super-secret-llm-key", "tts-super-secret-key", "pexels-super-secret-key",
        "yt-super-secret", "tt-super-secret", "ig-super-secret", "app-super-secret-api-key",
    ):
        assert secret.lower() not in blob


def test_fingerprint_strips_url_path_and_query_keeping_only_host() -> None:
    settings = Settings(
        llm_provider="openai_compatible", llm_model="m", llm_api_key="k",
        llm_base_url="https://llm.example.com:8443/v1/chat?api_key=shouldnotleak",
    )
    fingerprint = config_fingerprint(settings)
    assert fingerprint["llm_host"] == "llm.example.com:8443"
    assert "shouldnotleak" not in json.dumps(fingerprint)
    assert "/v1/chat" not in json.dumps(fingerprint)


def test_fingerprint_is_deterministic_for_identical_config() -> None:
    a = config_fingerprint(Settings())
    b = config_fingerprint(Settings())
    assert a == b
    assert fingerprint_hash(a) == fingerprint_hash(b)


def test_fingerprint_hash_changes_when_config_changes() -> None:
    base = fingerprint_hash(config_fingerprint(Settings()))
    changed = fingerprint_hash(config_fingerprint(Settings(allow_public_upload=True)))
    assert base != changed


def test_fingerprint_includes_schema_and_app_version() -> None:
    from reel_harness._version import __version__
    from reel_harness.db.schema import SCHEMA_VERSION

    fingerprint = config_fingerprint(Settings())
    assert fingerprint["schema_version"] == SCHEMA_VERSION
    assert fingerprint["app_version"] == __version__


def test_fingerprint_reports_fake_providers_by_default() -> None:
    fingerprint = config_fingerprint(Settings())
    assert fingerprint["llm_provider"] == "fake"
    assert fingerprint["tts_provider"] == "fake"
    assert fingerprint["asset_provider"] == "fake"
    assert fingerprint["llm_host"] is None
