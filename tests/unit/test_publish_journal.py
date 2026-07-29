"""publisher.journal.PublishJournal: append-only, fsync'd, integrity-
checked crash-recovery log. No network."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from reel_harness.publisher.journal import (
    PublishJournal,
    PublishJournalError,
    safe_session_reference_hash,
)


def test_read_events_on_an_unwritten_publication_is_empty(tmp_path) -> None:
    journal = PublishJournal(tmp_path / "journal")
    assert journal.read_events("does-not-exist") == []


def test_append_and_read_events_round_trips_in_order(tmp_path) -> None:
    journal = PublishJournal(tmp_path / "journal")
    journal.append(
        publication_id="pub-1", job_id="job-1", provider="youtube", account_reference="default",
        final_video_checksum="abc123", event="upload_session_created", timestamp=datetime.now(UTC),
        safe_session_hash="deadbeef",
    )
    journal.append(
        publication_id="pub-1", job_id="job-1", provider="youtube", account_reference="default",
        final_video_checksum="abc123", event="upload_completed", timestamp=datetime.now(UTC),
        provider_video_id="yt-video-1", provider_request_id="req-1",
    )
    events = journal.read_events("pub-1")
    assert [e["event"] for e in events] == ["upload_session_created", "upload_completed"]
    assert events[1]["provider_video_id"] == "yt-video-1"
    assert events[1]["provider_request_id"] == "req-1"


def test_different_publications_are_independent(tmp_path) -> None:
    journal = PublishJournal(tmp_path / "journal")
    journal.append(
        publication_id="pub-a", job_id="job-a", provider="youtube", account_reference="default",
        final_video_checksum="a", event="upload_completed", timestamp=datetime.now(UTC),
        provider_video_id="video-a",
    )
    journal.append(
        publication_id="pub-b", job_id="job-b", provider="youtube", account_reference="default",
        final_video_checksum="b", event="upload_completed", timestamp=datetime.now(UTC),
        provider_video_id="video-b",
    )
    assert journal.read_events("pub-a")[0]["provider_video_id"] == "video-a"
    assert journal.read_events("pub-b")[0]["provider_video_id"] == "video-b"


def test_append_refuses_a_publication_id_with_path_traversal(tmp_path) -> None:
    journal = PublishJournal(tmp_path / "journal")
    with pytest.raises(PublishJournalError):
        journal.append(
            publication_id="../escape", job_id="job-1", provider="youtube", account_reference="default",
            final_video_checksum="abc", event="upload_completed", timestamp=datetime.now(UTC),
        )


def test_append_refuses_a_record_containing_a_bearer_token(tmp_path) -> None:
    journal = PublishJournal(tmp_path / "journal")
    with pytest.raises(PublishJournalError, match="authorization|bearer"):
        journal.append(
            publication_id="pub-1", job_id="job-1", provider="youtube", account_reference="default",
            final_video_checksum="abc", event="upload_completed", timestamp=datetime.now(UTC),
            detail={"leaked": "Authorization: Bearer ya29.fake-leaked-token"},
        )


def test_corrupted_line_is_skipped_not_raised(tmp_path) -> None:
    journal = PublishJournal(tmp_path / "journal")
    journal.append(
        publication_id="pub-1", job_id="job-1", provider="youtube", account_reference="default",
        final_video_checksum="abc", event="upload_completed", timestamp=datetime.now(UTC),
        provider_video_id="video-1",
    )
    path = journal._path_for("pub-1")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{not valid json\n")
    events = journal.read_events("pub-1")
    assert len(events) == 1
    assert events[0]["provider_video_id"] == "video-1"


def test_tampered_record_is_skipped_via_integrity_checksum(tmp_path) -> None:
    journal = PublishJournal(tmp_path / "journal")
    journal.append(
        publication_id="pub-1", job_id="job-1", provider="youtube", account_reference="default",
        final_video_checksum="abc", event="upload_completed", timestamp=datetime.now(UTC),
        provider_video_id="video-1",
    )
    path = journal._path_for("pub-1")
    tampered = path.read_text(encoding="utf-8").replace("video-1", "video-EVIL")
    path.write_text(tampered, encoding="utf-8")
    assert journal.read_events("pub-1") == []


def test_safe_session_reference_hash_is_deterministic_and_one_way(tmp_path) -> None:
    uri = "https://upload.example.invalid/session/abcdef123456"
    hash_a = safe_session_reference_hash(uri)
    hash_b = safe_session_reference_hash(uri)
    assert hash_a == hash_b
    assert uri not in hash_a
    assert safe_session_reference_hash("https://upload.example.invalid/session/different") != hash_a
