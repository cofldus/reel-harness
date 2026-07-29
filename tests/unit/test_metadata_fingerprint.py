"""pipeline.publish_metadata.metadata_fingerprint: deterministic, safe
reconciliation marker. No network."""
from __future__ import annotations

from reel_harness.pipeline.publish_metadata import metadata_fingerprint
from reel_harness.providers.base import PublicationMetadata


def _metadata(**overrides) -> PublicationMetadata:
    defaults = dict(
        title="Test video", description="A test description", tags=["a", "b"],
        category_id="22", privacy_status="private", made_for_kids=False,
    )
    defaults.update(overrides)
    return PublicationMetadata(**defaults)


def test_fingerprint_is_deterministic_for_identical_inputs() -> None:
    metadata = _metadata()
    fp_a = metadata_fingerprint("youtube", "default", "job-1", "checksum-abc", metadata)
    fp_b = metadata_fingerprint("youtube", "default", "job-1", "checksum-abc", metadata)
    assert fp_a == fp_b


def test_fingerprint_changes_if_the_checksum_changes() -> None:
    metadata = _metadata()
    fp_a = metadata_fingerprint("youtube", "default", "job-1", "checksum-abc", metadata)
    fp_b = metadata_fingerprint("youtube", "default", "job-1", "checksum-different", metadata)
    assert fp_a != fp_b


def test_fingerprint_changes_if_the_title_changes() -> None:
    fp_a = metadata_fingerprint("youtube", "default", "job-1", "checksum-abc", _metadata(title="Video A"))
    fp_b = metadata_fingerprint("youtube", "default", "job-1", "checksum-abc", _metadata(title="Video B"))
    assert fp_a != fp_b


def test_fingerprint_changes_if_the_account_changes() -> None:
    metadata = _metadata()
    fp_a = metadata_fingerprint("youtube", "acct-a", "job-1", "checksum-abc", metadata)
    fp_b = metadata_fingerprint("youtube", "acct-b", "job-1", "checksum-abc", metadata)
    assert fp_a != fp_b


def test_fingerprint_never_contains_the_job_id_or_title_verbatim() -> None:
    fp = metadata_fingerprint("youtube", "default", "job-super-secret-id-12345", "checksum-abc", _metadata())
    assert "job-super-secret-id-12345" not in fp
    assert "Test video" not in fp
