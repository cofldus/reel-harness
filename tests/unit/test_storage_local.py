from __future__ import annotations

import uuid

import pytest

from reel_harness.storage.local import InvalidJobIdError, LocalFilesystemStorage, PathTraversalError


@pytest.fixture
def local_storage(tmp_path):
    return LocalFilesystemStorage(tmp_path / "jobs")


def test_write_and_read_roundtrip(local_storage) -> None:
    job_id = str(uuid.uuid4())
    local_storage.write_bytes(job_id, "manifest.json", b'{"ok": true}')
    assert local_storage.read_bytes(job_id, "manifest.json") == b'{"ok": true}'


def test_job_id_must_look_like_a_uuid(local_storage) -> None:
    with pytest.raises(InvalidJobIdError):
        local_storage.job_dir("../../etc")
    with pytest.raises(InvalidJobIdError):
        local_storage.job_dir("not-a-uuid")


def test_rel_path_cannot_escape_job_dir(local_storage) -> None:
    job_id = str(uuid.uuid4())
    local_storage.write_bytes(job_id, "safe.txt", b"hello")
    with pytest.raises(PathTraversalError):
        local_storage.path_for(job_id, "../../../etc/passwd")
    with pytest.raises(PathTraversalError):
        local_storage.path_for(job_id, "../sibling-job-id/secret.txt")


def test_exists_is_false_for_traversal_attempt_instead_of_raising(local_storage) -> None:
    job_id = str(uuid.uuid4())
    assert local_storage.exists(job_id, "../../outside.txt") is False


def test_two_job_ids_get_fully_isolated_directories(local_storage) -> None:
    job_a, job_b = str(uuid.uuid4()), str(uuid.uuid4())
    local_storage.write_bytes(job_a, "final.mp4", b"video-a")
    local_storage.write_bytes(job_b, "final.mp4", b"video-b")
    assert local_storage.read_bytes(job_a, "final.mp4") == b"video-a"
    assert local_storage.read_bytes(job_b, "final.mp4") == b"video-b"
    assert local_storage.job_dir(job_a) != local_storage.job_dir(job_b)
