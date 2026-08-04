"""How long the worker waits for one generation, and what it does when
that runs out.

Regression tests for a defect that only a paid run could reveal: the poll
budget was a COUNT (60 polls x 0.2s = twelve seconds), sized for the fake
tier which settles in one or two polls. A real video model takes one to
three minutes, so every real generation hit the ceiling, was written off
as UPSTREAM_TRANSIENT, and was billed anyway while the provider kept
working on a job nobody was listening for.

The fix is a wall-clock deadline, and a timeout that says what actually
happened rather than borrowing the word "transient".
"""
from __future__ import annotations

import pytest

from reel_harness.providers.base import CinematicGenerationHandle, CinematicGenerationStatus
from reel_harness.worker.fable_runner import (
    DEFAULT_GENERATION_TIMEOUT_SEC,
    DEFAULT_POLL_INTERVAL_SEC,
    _poll_until_settled,
)


class _Clock:
    """A monotonic clock the test advances by sleeping, so the deadline is
    exercised without the test itself waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Provider:
    """Answers `generating` a fixed number of times, then settles."""

    def __init__(self, generating_polls: int, final="succeeded") -> None:
        self._remaining = generating_polls
        self._final = final
        self.polls = 0

    def get_generation_status(self, handle) -> CinematicGenerationStatus:
        self.polls += 1
        if self._remaining > 0:
            self._remaining -= 1
            return CinematicGenerationStatus(state="generating")
        return CinematicGenerationStatus(state=self._final)


def _handle() -> CinematicGenerationHandle:
    return CinematicGenerationHandle(
        provider_job_reference="operations/abc123", provider_id="google",
    )


def _poll(provider, clock, timeout=DEFAULT_GENERATION_TIMEOUT_SEC, interval=5.0):
    return _poll_until_settled(provider, _handle(), clock.sleep, timeout, interval, clock)


# -- the budget ----------------------------------------------------------

def test_the_default_budget_is_minutes_not_seconds() -> None:
    """The whole defect in one assertion: a real video generation takes
    one to three MINUTES, and the old budget was twelve seconds."""
    assert DEFAULT_GENERATION_TIMEOUT_SEC >= 300


def test_a_generation_taking_two_minutes_now_succeeds() -> None:
    """At the old 60 x 0.2s budget this returned a timeout at twelve
    seconds and the take was written off."""
    clock = _Clock()
    provider = _Provider(generating_polls=24)  # 24 x 5s = 120s
    status, timed_out = _poll(provider, clock)
    assert timed_out is False
    assert status.state == "succeeded"
    assert clock.now == pytest.approx(120.0)


def test_an_immediate_answer_costs_no_sleep_at_all() -> None:
    """The fake and demo tiers settle on the first poll, so tests must not
    pay the real-world cadence."""
    clock = _Clock()
    provider = _Provider(generating_polls=0)
    status, timed_out = _poll(provider, clock)
    assert (status.state, timed_out) == ("succeeded", False)
    assert provider.polls == 1
    assert clock.now == 0.0


def test_the_deadline_is_wall_clock_not_a_poll_count() -> None:
    """A count silently means different things at different intervals --
    which is exactly how a 60-poll budget came to mean twelve seconds."""
    fast = _Clock()
    _poll(_Provider(generating_polls=10_000), fast, timeout=60.0, interval=1.0)
    slow = _Clock()
    _poll(_Provider(generating_polls=10_000), slow, timeout=60.0, interval=20.0)
    # Same budget, very different poll counts, both stop at ~60s.
    assert fast.now == pytest.approx(60.0, abs=20.0)
    assert slow.now == pytest.approx(60.0, abs=20.0)


def test_polling_stops_at_the_deadline() -> None:
    clock = _Clock()
    provider = _Provider(generating_polls=10_000)
    status, timed_out = _poll(provider, clock, timeout=30.0, interval=5.0)
    assert timed_out is True
    assert status.state == "generating"
    assert clock.now <= 35.0


# -- what a settled generation reports ------------------------------------

@pytest.mark.parametrize("final", ["succeeded", "failed", "moderated", "cancelled"])
def test_any_settled_state_ends_polling_immediately(final) -> None:
    clock = _Clock()
    provider = _Provider(generating_polls=2, final=final)
    status, timed_out = _poll(provider, clock)
    assert timed_out is False
    assert status.state == final


# -- the timeout outcome through the worker -------------------------------

def test_a_timeout_is_not_called_transient_and_names_the_provider_job(
    session_factory, tmp_path,
) -> None:
    """A generation still running at the provider has almost certainly
    been billed. Calling it "transient" invites a retry that pays for the
    same shot twice, so it gets its own code -- and the provider's job
    reference, the only way to find the work afterwards, is in the
    message."""
    from reel_harness.core.fable_service import FableService
    from reel_harness.db.cinematic_models import FableTake
    from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
    from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector
    from reel_harness.providers.fake_reference_image import FakeReferenceImageProvider
    from reel_harness.storage.local import LocalFilesystemStorage
    from reel_harness.worker.fable_lease import lease_next_shot
    from reel_harness.worker.fable_runner import run_shot
    from tests.conftest import walk_casting

    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    fable = FableService(
        session_factory, storage=storage, narrative_director=FakeNarrativeDirector(),
        reference_provider=FakeReferenceImageProvider(),
    )
    project, _ = fable.create_project(title="t", source_text="s", idempotency_key="timeout")
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    walk_casting(fable, project.id)
    fable.approve_characters(project.id)
    fable.approve_shots(project.id)

    # A provider that accepts the submission and then never finishes.
    provider = FakeCinematicVideoProvider()
    provider.get_generation_status = lambda handle: CinematicGenerationStatus(state="generating")

    clock = _Clock()
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(
            session, shot, provider, storage, lease_token=shot.lease_token,
            sleep=clock.sleep, generation_timeout_sec=30.0, poll_interval_sec=5.0,
            monotonic=clock,
        )
        assert shot.failure_code == "GENERATION_TIMEOUT"
        assert "already billed" in shot.failure_summary
        # A human decision, not a failure: the money is spent and the
        # result may yet arrive.
        assert shot.status == "REVIEW_REQUIRED"

    with session_factory() as session:
        take = session.query(FableTake).filter(FableTake.shot_id == shot.id).one()
        # NOT marked FAILED -- it did not fail, it is still running.
        assert take.status != "FAILED"
        assert take.provider_job_reference, "the only way to find the work afterwards"
        assert take.provider_job_reference in shot.failure_summary


def test_the_default_interval_is_sane_for_a_real_provider() -> None:
    """Fast enough to notice a finished job, slow enough not to hammer a
    long-running operation endpoint."""
    assert 1.0 <= DEFAULT_POLL_INTERVAL_SEC <= 15.0
