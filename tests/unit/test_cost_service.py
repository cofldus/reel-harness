"""core.cost_service: what a project would cost, what it may spend, and
what it actually did spend.

The distinction every test here defends is estimate vs. spend. An
estimate decides whether generation may START; only a provider-reported
cost for a completed generation moves the running total. A provider that
publishes no price stays unknown all the way to the caller rather than
being rounded to zero, and two currencies in one budget are refused
rather than converted.
"""
from __future__ import annotations

import pytest

from reel_harness.core.cost_service import (
    assert_paid_generation_allowed,
    assert_within_budget,
    budget_status,
    estimate_project_cost,
    record_spend,
    recorded_spend,
)
from reel_harness.core.errors import (
    BudgetCurrencyMismatchError,
    BudgetExceededError,
    PaidGenerationNotAllowedError,
)
from reel_harness.core.fable_service import FableService
from reel_harness.db.cinematic_models import FableShot, StoryProject
from reel_harness.providers.base import CinematicCostEstimate
from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
from reel_harness.storage.local import LocalFilesystemStorage


class _PricedProvider:
    """A provider whose pricing the test dictates outright -- the point is
    the arithmetic and the honesty rules around it, not any real tariff."""

    def __init__(self, amount, currency="TEST", known=True, provider_id="paid-test") -> None:
        self.provider_id = provider_id
        self._estimate = CinematicCostEstimate(
            known=known, amount=amount, currency=currency, detail="test",
        )

    def estimate_cost(self, request) -> CinematicCostEstimate:
        return self._estimate


class _PerShotProvider:
    """Prices each successive call differently, for the mixed-currency and
    partially-unknown cases."""

    def __init__(self, estimates, provider_id="paid-test") -> None:
        self.provider_id = provider_id
        self._estimates = list(estimates)
        self._index = 0

    def estimate_cost(self, request) -> CinematicCostEstimate:
        estimate = self._estimates[self._index]
        self._index += 1
        return estimate


def _project(**kwargs) -> StoryProject:
    defaults = {
        "id": "p1", "idempotency_key": "k", "title": "t", "source_text": "s",
        "aspect_ratio": "9:16", "budget_spent_amount": 0.0,
    }
    return StoryProject(**{**defaults, **kwargs})


def _shots(count: int, duration: float = 2.0) -> list[FableShot]:
    return [
        FableShot(id=f"s{i}", scene_id="sc", shot_order=i, duration_sec=duration)
        for i in range(count)
    ]


# -- estimation ----------------------------------------------------------

def test_estimate_sums_every_shot() -> None:
    estimate = estimate_project_cost(_project(), _shots(4), _PricedProvider(0.25))
    assert estimate.known is True
    assert estimate.amount == 1.0
    assert estimate.currency == "TEST"
    assert estimate.shot_count == 4
    assert estimate.unpriced_shot_count == 0


def test_estimate_multiplies_by_takes_per_shot() -> None:
    """N candidate takes are N billed generations. An estimate that
    ignored the multiplier would authorize a budget it cannot cover."""
    estimate = estimate_project_cost(_project(), _shots(3), _PricedProvider(1.0), takes_per_shot=4)
    assert estimate.amount == 12.0


def test_estimate_is_unknown_when_any_shot_is_unpriced() -> None:
    """A partial total must not read as a complete one -- known=False even
    though three of four shots priced fine."""
    provider = _PerShotProvider([
        CinematicCostEstimate(known=True, amount=1.0, currency="TEST"),
        CinematicCostEstimate(known=True, amount=1.0, currency="TEST"),
        CinematicCostEstimate(known=False, amount=None, currency=None),
        CinematicCostEstimate(known=True, amount=1.0, currency="TEST"),
    ])
    estimate = estimate_project_cost(_project(), _shots(4), provider)
    assert estimate.known is False
    assert estimate.unpriced_shot_count == 1
    # The priced shots are still reported, as a stated lower bound.
    assert estimate.amount == 3.0
    assert "lower bound" in estimate.detail


def test_estimate_refuses_to_sum_across_currencies() -> None:
    provider = _PerShotProvider([
        CinematicCostEstimate(known=True, amount=1.0, currency="USD"),
        CinematicCostEstimate(known=True, amount=1.0, currency="EUR"),
    ])
    estimate = estimate_project_cost(_project(), _shots(2), provider)
    assert estimate.known is False
    assert estimate.amount is None
    assert "more than one currency" in estimate.detail


def test_estimate_of_a_project_with_no_shots_is_not_known() -> None:
    """Zero is a number, and "0.0, known" would read as "this is free"."""
    estimate = estimate_project_cost(_project(), [], _PricedProvider(1.0))
    assert estimate.known is False
    assert estimate.shot_count == 0


def test_takes_per_shot_must_be_at_least_one() -> None:
    with pytest.raises(ValueError):
        estimate_project_cost(_project(), _shots(1), _PricedProvider(1.0), takes_per_shot=0)


# -- the paid-generation double gate -------------------------------------

def test_free_provider_never_needs_the_gate() -> None:
    """The offline tiers cost nothing, so requiring a budget to run them
    would be ceremony rather than safety."""
    assert_paid_generation_allowed(_project(), "fake", allow_paid_generation=False)
    assert_paid_generation_allowed(_project(), "demo", allow_paid_generation=False)


def test_paid_provider_refused_without_the_global_switch() -> None:
    project = _project(budget_limit_amount=100.0, budget_currency="USD")
    with pytest.raises(PaidGenerationNotAllowedError) as exc:
        assert_paid_generation_allowed(project, "some-real-vendor", allow_paid_generation=False)
    assert exc.value.code == "PAID_GENERATION_NOT_ALLOWED"
    assert exc.value.retryable is False


def test_paid_provider_refused_without_a_project_budget() -> None:
    """The switch alone is not permission: the project must also name a
    number. Both halves independently, mirroring allow_public_upload."""
    with pytest.raises(PaidGenerationNotAllowedError):
        assert_paid_generation_allowed(_project(), "some-real-vendor", allow_paid_generation=True)


def test_paid_provider_allowed_when_both_halves_are_satisfied() -> None:
    project = _project(budget_limit_amount=5.0, budget_currency="USD")
    assert_paid_generation_allowed(project, "some-real-vendor", allow_paid_generation=True)


# -- budget enforcement --------------------------------------------------

def test_no_limit_means_no_budget_check() -> None:
    assert_within_budget(_project(), 999.0, "USD")


def test_charge_within_the_limit_passes() -> None:
    project = _project(budget_limit_amount=10.0, budget_currency="USD", budget_spent_amount=4.0)
    assert_within_budget(project, 6.0, "USD")  # exactly at the limit is still within it


def test_charge_over_the_limit_is_refused() -> None:
    project = _project(budget_limit_amount=10.0, budget_currency="USD", budget_spent_amount=9.5)
    with pytest.raises(BudgetExceededError) as exc:
        assert_within_budget(project, 1.0, "USD")
    assert exc.value.code == "BUDGET_EXCEEDED"
    assert exc.value.retryable is False  # only a human raising the limit changes this


def test_unpriceable_charge_is_refused_under_a_live_budget() -> None:
    """An unknown charge against a ceiling is exactly what a ceiling
    forbids -- it is refused, never treated as free."""
    project = _project(budget_limit_amount=10.0, budget_currency="USD")
    with pytest.raises(BudgetExceededError):
        assert_within_budget(project, None, None)


def test_foreign_currency_is_refused_not_converted() -> None:
    project = _project(budget_limit_amount=10.0, budget_currency="USD")
    with pytest.raises(BudgetCurrencyMismatchError):
        assert_within_budget(project, 1.0, "EUR")


def test_float_accumulation_does_not_falsely_exhaust_a_budget() -> None:
    """0.1 x 3 is not 0.3 in binary floating point. Without rounding at a
    fixed scale this refuses a generation the operator explicitly paid
    for, which is the worst kind of wrong to be about money."""
    project = _project(budget_limit_amount=0.3, budget_currency="USD")
    for _ in range(3):
        assert_within_budget(project, 0.1, "USD")
        record_spend(project, 0.1, "USD")
    assert project.budget_spent_amount == 0.3


# -- spend recording -----------------------------------------------------

def test_record_spend_accumulates_real_costs() -> None:
    project = _project(budget_limit_amount=10.0, budget_currency="USD")
    assert record_spend(project, 1.25, "USD") is True
    assert record_spend(project, 0.75, "USD") is True
    assert project.budget_spent_amount == 2.0


def test_record_spend_of_an_unreported_cost_moves_nothing() -> None:
    """A provider that published no figure must not be counted as zero
    spend -- it accumulates nothing AND says it accumulated nothing, so
    the caller can record the take as unpriced."""
    project = _project(budget_limit_amount=10.0, budget_currency="USD")
    assert record_spend(project, None, None) is False
    assert project.budget_spent_amount == 0.0


def test_record_spend_refuses_a_foreign_currency() -> None:
    project = _project(budget_limit_amount=10.0, budget_currency="USD")
    with pytest.raises(BudgetCurrencyMismatchError):
        record_spend(project, 1.0, "EUR")
    assert project.budget_spent_amount == 0.0


# -- audit against the line items ----------------------------------------

@pytest.fixture
def priced_project(session_factory, tmp_path):
    """A real adapted project with real shot rows, so the audit query is
    exercised against the actual scene/shot/take join rather than a
    hand-built object graph."""
    from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector

    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    fable = FableService(
        session_factory, storage=storage, narrative_director=FakeNarrativeDirector(),
    )
    project, _ = fable.create_project(title="t", source_text="s", idempotency_key="cost-test")
    fable.adapt_project(project.id)
    return fable, project.id


def test_recorded_spend_recomputes_the_total_from_takes(priced_project, session_factory) -> None:
    from reel_harness.db.cinematic_models import FableTake

    fable, project_id = priced_project
    shots = fable.project_shots(project_id)
    with session_factory() as session:
        for i, shot in enumerate(shots[:3]):
            session.add(FableTake(
                shot_id=shot.id, provider="fake", prompt_fingerprint=f"fp{i}",
                media_path=f"/tmp/{i}.mp4", cost_amount=0.5, cost_currency="FAKE",
            ))
        session.commit()

    with session_factory() as session:
        total, currency, unpriced = recorded_spend(session, project_id)
    assert total == 1.5
    assert currency == "FAKE"
    assert unpriced == 0


def test_recorded_spend_counts_completed_takes_the_provider_never_priced(
    priced_project, session_factory,
) -> None:
    """A take with media but no cost is a real charge nobody can total --
    counted explicitly so an under-reported spend is visible, not silent.
    A take that failed before producing media is NOT counted: there was
    never a bill for it to be missing."""
    from reel_harness.db.cinematic_models import FableTake

    fable, project_id = priced_project
    shots = fable.project_shots(project_id)
    with session_factory() as session:
        session.add(FableTake(
            shot_id=shots[0].id, provider="fake", prompt_fingerprint="fp-priced",
            media_path="/tmp/a.mp4", cost_amount=1.0, cost_currency="FAKE",
        ))
        session.add(FableTake(
            shot_id=shots[1].id, provider="fake", prompt_fingerprint="fp-unpriced",
            media_path="/tmp/b.mp4", cost_amount=None,
        ))
        session.add(FableTake(
            shot_id=shots[2].id, provider="fake", prompt_fingerprint="fp-failed",
            status="FAILED", media_path=None, cost_amount=None,
        ))
        session.commit()

    with session_factory() as session:
        total, _, unpriced = recorded_spend(session, project_id)
    assert total == 1.0
    assert unpriced == 1


def test_budget_status_reports_remaining_and_unpriced(priced_project, session_factory) -> None:
    fable, project_id = priced_project
    fable.set_budget(project_id, 10.0, "FAKE")
    with session_factory() as session:
        project = session.get(StoryProject, project_id)
        record_spend(project, 2.5, "FAKE")
        session.commit()
        status = budget_status(session, project)
    assert status.limit_amount == 10.0
    assert status.spent_amount == 2.5
    assert status.remaining_amount == 7.5
    assert status.unpriced_take_count == 0


def test_the_fake_provider_prices_a_real_project(priced_project) -> None:
    """End of the pricing path against the actual fake adapter: its
    obviously-fake per-second tariff, applied to the real shot durations
    an adaptation produced."""
    fable, project_id = priced_project
    estimate = fable.estimate_cost(project_id, provider=FakeCinematicVideoProvider())
    assert estimate.known is True
    assert estimate.currency == "FAKE"
    assert estimate.amount > 0
    assert estimate.shot_count == len(fable.project_shots(project_id))
