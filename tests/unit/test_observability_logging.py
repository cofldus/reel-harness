from __future__ import annotations

import json
import logging

from reel_harness.observability import _redact, configure_logging, log_stage_event, register_secret


def test_bearer_token_is_redacted_from_arbitrary_text() -> None:
    redacted = _redact("Authorization: Bearer sk-super-secret-token")
    assert "***REDACTED***" in redacted
    assert "sk-super-secret-token" not in redacted


def test_authorization_header_style_text_is_redacted() -> None:
    redacted = _redact("authorization=Bearer abc.def.ghi")
    assert "abc.def.ghi" not in redacted


def test_registered_secret_value_is_redacted_verbatim() -> None:
    register_secret("my-app-api-key-12345")
    redacted = _redact("using key my-app-api-key-12345 to authenticate")
    assert "my-app-api-key-12345" not in redacted
    assert "***REDACTED***" in redacted


def test_none_and_empty_secret_registration_is_a_no_op() -> None:
    register_secret(None)
    register_secret("")
    assert _redact("nothing sensitive here") == "nothing sensitive here"


def test_log_stage_event_emits_structured_fields(caplog) -> None:
    configure_logging("INFO")
    with caplog.at_level(logging.INFO, logger="reel_harness"):
        log_stage_event(job_id="job-1", stage="SCRIPT", attempt=1, event="stage_succeeded", duration_ms=12.5)

    assert len(caplog.records) == 1
    payload = json.loads(caplog.records[0].getMessage())
    assert payload == {
        "job_id": "job-1", "stage": "SCRIPT", "attempt": 1,
        "event": "stage_succeeded", "duration_ms": 12.5, "error_code": None,
    }


def test_log_stage_event_never_includes_script_body_text(caplog) -> None:
    configure_logging("INFO")
    with caplog.at_level(logging.INFO, logger="reel_harness"):
        log_stage_event(
            job_id="job-2", stage="SCRIPT", attempt=1, event="stage_failed", error_code="SCHEMA_INVALID",
        )
    assert "voiceover" not in caplog.text
    assert "scene" not in caplog.text


def test_registered_secret_is_redacted_even_inside_a_log_line(caplog) -> None:
    register_secret("top-secret-app-key")
    configure_logging("INFO")
    logger = logging.getLogger("reel_harness")
    with caplog.at_level(logging.INFO, logger="reel_harness"):
        logger.info("request used key top-secret-app-key")
    assert "top-secret-app-key" not in caplog.text
    assert "***REDACTED***" in caplog.text
