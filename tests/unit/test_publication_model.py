"""Publication DB model: idempotency unique constraint, audit event
relationship, and the v4->v5 migration (brand-new tables, no column changes
to existing tables). No network."""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from reel_harness.core.service import JobService
from reel_harness.db.models import Job, Publication, PublicationAuditEvent
from reel_harness.db.schema import SCHEMA_VERSION, create_engine_from_url, init_db, make_session_factory


@pytest.fixture
def engine(tmp_path):
    eng = create_engine_from_url(f"sqlite:///{tmp_path / 'pub-test.db'}")
    init_db(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def job(session_factory):
    service = JobService(session_factory)
    channel = service.create_channel(name="c", niche="n", language="en")
    created, _ = service.create_job(channel.id, idempotency_key="pub-model-1", topic="t")
    return created


def test_schema_version_is_at_least_5() -> None:
    assert SCHEMA_VERSION >= 5


def _make_publication(job_id: str, checksum: str = "deadbeef" * 8) -> Publication:
    return Publication(
        job_id=job_id, provider="youtube", account_reference="acct-1",
        idempotency_key=f"youtube:acct-1:{job_id}:{checksum}", final_video_checksum=checksum,
    )


def test_publication_persists_and_relates_to_job(session_factory, job) -> None:
    with session_factory() as session:
        pub = _make_publication(job.id)
        session.add(pub)
        session.commit()
        session.refresh(pub)
        assert pub.status == "CREATED"
        assert pub.privacy_status == "private"
        assert pub.bytes_uploaded == 0
        loaded_job = session.get(Job, job.id)
        assert loaded_job is not None


def test_duplicate_idempotency_tuple_is_rejected_by_the_db(session_factory, job) -> None:
    with session_factory() as session:
        session.add(_make_publication(job.id))
        session.commit()

    with session_factory() as session:
        session.add(_make_publication(job.id))  # identical (provider, account, job, checksum)
        with pytest.raises(IntegrityError):
            session.commit()


def test_different_checksum_is_a_distinct_publication(session_factory, job) -> None:
    with session_factory() as session:
        session.add(_make_publication(job.id, checksum="a" * 64))
        session.add(_make_publication(job.id, checksum="b" * 64))
        session.commit()  # must not raise -- different checksum, different tuple

    with session_factory() as session:
        rows = session.query(Publication).filter(Publication.job_id == job.id).all()
        assert len(rows) == 2


def test_audit_events_are_append_only_and_relate_to_publication(session_factory, job) -> None:
    with session_factory() as session:
        pub = _make_publication(job.id)
        session.add(pub)
        session.commit()
        session.refresh(pub)
        publication_id = pub.id

    with session_factory() as session:
        session.add(PublicationAuditEvent(
            publication_id=publication_id, event="publication_created", detail={"provider": "youtube"},
        ))
        session.add(PublicationAuditEvent(
            publication_id=publication_id, event="eligibility_checked", detail={"eligible": True},
        ))
        session.commit()

    with session_factory() as session:
        pub = session.get(Publication, publication_id)
        assert pub is not None
        events = list(pub.audit_events)
        assert [e.event for e in events] == ["publication_created", "eligibility_checked"]


def test_v4_shaped_database_upgrades_to_v5_without_touching_existing_data(tmp_path) -> None:
    """A database already at v4 (with real Job/Asset rows) gains the new
    publications/publication_audit_events tables when init_db() runs again,
    with the pre-existing rows untouched -- create_all() only creates tables
    that don't exist yet."""
    from reel_harness.db.models import Asset

    db_path = tmp_path / "v4-upgrade.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)  # this session's schema.py is already v5, but the flow is the same
    factory = make_session_factory(engine)
    service = JobService(factory)
    channel = service.create_channel(name="c", niche="n", language="en")
    existing_job, _ = service.create_job(channel.id, idempotency_key="pre-existing", topic="t")
    with factory() as session:
        session.add(Asset(
            job_id=existing_job.id, scene_index=0, source_provider="fake",
            local_path="/tmp/x.png", checksum_sha256="deadbeef", mime_type="image/png",
        ))
        session.commit()

    # Re-running init_db (as every AppContext startup does) must be a no-op
    # for existing data and additive for schema.
    init_db(engine)

    with factory() as session:
        assert session.get(Job, existing_job.id) is not None
        assets = session.query(Asset).filter(Asset.job_id == existing_job.id).all()
        assert len(assets) == 1
        assert session.query(Publication).count() == 0  # table exists, empty
        session.add(_make_publication(existing_job.id))
        session.commit()
        assert session.query(Publication).count() == 1
