"""Fable cost estimation and project budget enforcement (Phase F3).

Three responsibilities, deliberately kept as pure functions over an
already-loaded project/shot set rather than a service class: the callers
are FableService (approval gates), worker.fable_runner (per-shot, inside
an already-open fenced transaction), and the CLI (read-only reporting) --
each already owns its own session, and none of them wants a second
object holding another one.

The rules this module exists to keep honest:

- **An estimate is never spend.** `budget_spent_amount` accumulates only
  `cost_amount` values a provider reported for a generation that actually
  completed. An estimate influences whether generation is ALLOWED to
  start; it never moves the running total.
- **Unknown stays unknown.** A provider that publishes no price returns
  `known=False` (providers.base.CinematicCostEstimate), and a project
  estimate containing even one unknown shot is itself unknown -- reported
  as such, never quietly treated as zero. The Fable spec's rule: show
  "unknown", never a guessed number.
- **No invented exchange rates.** Two currencies in one budget is refused
  (BudgetCurrencyMismatchError), not converted.
- **Refusal is a review, not a failure.** Every refusal here raises a
  non-retryable PipelineError whose callers route to REVIEW_REQUIRED, so
  a human raises the limit or stops -- retrying unchanged could only
  reach the same answer.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from reel_harness.core.cinematic_state import DEFAULT_SHOT_RESOLUTION
from reel_harness.core.errors import (
    BudgetCurrencyMismatchError,
    BudgetExceededError,
    PaidGenerationNotAllowedError,
)
from reel_harness.db.cinematic_models import FableScene, FableShot, FableTake, StoryProject
from reel_harness.providers.base import CinematicCostEstimate, CinematicGenerationRequest
from reel_harness.providers.registry import provider_charges_money

# Money compared at a fixed number of decimal places. Budgets are small
# per-project figures in whole currency units (a few dollars), and float
# accumulation of per-second prices otherwise makes a sum land a
# hair above its own limit and refuse a generation the operator
# explicitly paid for.
_MONEY_PLACES = 6


def _round(amount: float) -> float:
    return round(amount, _MONEY_PLACES)


@dataclass(frozen=True)
class ProjectCostEstimate:
    """What generating every remaining shot of a project would cost.

    `known` is False when ANY shot could not be priced or when the shots
    priced in more than one currency -- a partial total would read as a
    complete one, which is exactly the misreport this dataclass exists to
    prevent. `amount` is still filled in for the shots that COULD be
    priced (with `unpriced_shot_count` saying how many were not), so the
    caller can show "at least X, plus N unknown" rather than nothing.
    """

    known: bool
    amount: float | None
    currency: str | None
    shot_count: int
    unpriced_shot_count: int
    detail: str


@dataclass(frozen=True)
class BudgetStatus:
    """A project's spending position. `spent` is real reported spend
    only; `unpriced_take_count` is how many completed takes the provider
    gave no figure for, so a total that under-counts says so."""

    limit_amount: float | None
    currency: str | None
    spent_amount: float
    remaining_amount: float | None
    unpriced_take_count: int


def estimate_request_for_shot(shot: FableShot, project: StoryProject) -> CinematicGenerationRequest:
    """The request shape used purely for PRICING a shot. The prompt is
    deliberately empty: every provider surveyed for Fable prices a
    generation by duration/resolution/aspect ratio, never by prompt
    content, and compiling the real prompt here would mean loading the
    character bible for every shot just to throw the text away. The
    duration/aspect/resolution values are the same ones
    worker.fable_runner will actually request, which is what makes the
    estimate comparable to the eventual bill.
    """
    return CinematicGenerationRequest(
        prompt="",
        duration_sec=shot.duration_sec or 2.0,
        aspect_ratio=project.aspect_ratio,
        resolution=DEFAULT_SHOT_RESOLUTION,
        correlation_id=f"{project.id}:{shot.id}:estimate",
    )


def estimate_project_cost(
    project: StoryProject, shots: list[FableShot], provider, *, takes_per_shot: int = 1,
) -> ProjectCostEstimate:
    """Prices `shots` with `provider`. `takes_per_shot` multiplies the
    per-shot price -- N candidate takes are N billed generations, and the
    estimate has to say so before the operator approves (the setting that
    drives it lands with F3's multiple-takes commit; the parameter exists
    now so the budget gate is never off by a factor of N once it does)."""
    if takes_per_shot < 1:
        raise ValueError(f"takes_per_shot must be >= 1, got {takes_per_shot}")

    total = 0.0
    currencies: set[str] = set()
    unpriced = 0
    for shot in shots:
        estimate: CinematicCostEstimate = provider.estimate_cost(
            estimate_request_for_shot(shot, project)
        )
        if not estimate.known or estimate.amount is None:
            unpriced += 1
            continue
        total += estimate.amount * takes_per_shot
        if estimate.currency:
            currencies.add(estimate.currency)

    if len(currencies) > 1:
        return ProjectCostEstimate(
            known=False, amount=None, currency=None, shot_count=len(shots),
            unpriced_shot_count=unpriced,
            detail=(
                f"provider priced shots in more than one currency "
                f"({', '.join(sorted(currencies))}) -- not summed"
            ),
        )

    currency = next(iter(currencies), None)
    known = unpriced == 0 and len(shots) > 0
    if known:
        detail = f"{len(shots)} shot(s) x {takes_per_shot} take(s), priced by {provider.provider_id}"
    elif not shots:
        detail = "project has no shots to price"
    else:
        detail = (
            f"{unpriced} of {len(shots)} shot(s) could not be priced by "
            f"{provider.provider_id} -- total is a lower bound, not an estimate"
        )
    return ProjectCostEstimate(
        known=known, amount=_round(total) if currencies else None, currency=currency,
        shot_count=len(shots), unpriced_shot_count=unpriced, detail=detail,
    )


def assert_paid_generation_allowed(
    project: StoryProject, provider_id: str, allow_paid_generation: bool,
) -> None:
    """The double gate. A free tier passes unconditionally; a
    cost-incurring provider needs BOTH the operator-wide
    `allow_paid_generation` switch and this project's own explicit budget
    limit. Deliberately mirrors the allow_public_upload gate: one global
    decision, one per-object decision, neither implying the other.

    Takes the switch as a plain bool rather than a Settings object so
    core stays free of a config import -- the caller (service, worker, or
    CLI) already holds the settings it reads it from."""
    if not provider_charges_money(provider_id):
        return
    if not allow_paid_generation:
        raise PaidGenerationNotAllowedError(
            f"provider {provider_id!r} costs money and REEL_HARNESS_ALLOW_PAID_GENERATION "
            f"is not enabled"
        )
    if project.budget_limit_amount is None:
        raise PaidGenerationNotAllowedError(
            f"provider {provider_id!r} costs money and project {project.id} has no budget "
            f"limit set (fable-budget --limit)"
        )


def assert_within_budget(project: StoryProject, amount: float | None, currency: str | None) -> None:
    """Refuses when the project's recorded spend plus `amount` would pass
    its limit. A project with no limit is unlimited HERE by design -- the
    "no limit means refuse" rule belongs to the paid gate above and
    applies only to providers that charge; a free provider must not need
    a budget to run at all.

    `amount is None` (an unpriceable generation) is NOT treated as free:
    with a limit in force it is refused, because allowing an unknown
    charge against a ceiling is precisely what a ceiling forbids."""
    if project.budget_limit_amount is None:
        return
    if amount is None:
        raise BudgetExceededError(
            f"project {project.id} has a budget limit but the provider published no price "
            f"for this generation -- refusing an unbounded charge"
        )
    if currency and project.budget_currency and currency != project.budget_currency:
        raise BudgetCurrencyMismatchError(
            f"provider quoted {currency} but project {project.id}'s budget is in "
            f"{project.budget_currency} -- no conversion is applied"
        )
    projected = _round(project.budget_spent_amount + amount)
    if projected > _round(project.budget_limit_amount):
        raise BudgetExceededError(
            f"project {project.id} budget exhausted: spent {_round(project.budget_spent_amount)} "
            f"+ {_round(amount)} would reach {projected}, over the "
            f"{_round(project.budget_limit_amount)} {project.budget_currency or ''}".strip()
            + " limit"
        )


def record_spend(project: StoryProject, amount: float | None, currency: str | None) -> bool:
    """Accumulates one REAL, provider-reported cost onto the project.
    Returns whether anything was accumulated -- False means the provider
    reported no figure, which the caller records as an unpriced take
    rather than as zero spend.

    The caller must run this inside the same transaction that persists
    the take it belongs to; that is what keeps the running total and its
    line items from diverging across a crash."""
    if amount is None:
        return False
    if currency and project.budget_currency and currency != project.budget_currency:
        raise BudgetCurrencyMismatchError(
            f"provider billed {currency} but project {project.id}'s budget is in "
            f"{project.budget_currency} -- refusing to accumulate an unconvertible amount"
        )
    project.budget_spent_amount = _round(project.budget_spent_amount + amount)
    return True


def recorded_spend(session, project_id: str) -> tuple[float, str | None, int]:
    """Recomputes spend from the take rows themselves: (amount, currency,
    unpriced_take_count). This is the audit path -- `budget_spent_amount`
    is a running total, and a running total nobody can check against its
    own line items is a number to be suspicious of."""
    takes = session.execute(
        select(FableTake)
        .join(FableShot, FableTake.shot_id == FableShot.id)
        .join(FableScene, FableShot.scene_id == FableScene.id)
        .where(FableScene.project_id == project_id)
    ).scalars().all()
    total = 0.0
    currency: str | None = None
    unpriced = 0
    for take in takes:
        if take.cost_amount is None:
            # Only a take that actually produced media counts as an
            # unpriced charge; a submitted-then-failed take was never
            # billed for anything to be missing.
            if take.media_path is not None:
                unpriced += 1
            continue
        total += take.cost_amount
        currency = currency or take.cost_currency
    return _round(total), currency, unpriced


def budget_status(session, project: StoryProject) -> BudgetStatus:
    _, _, unpriced = recorded_spend(session, project.id)
    remaining = (
        _round(project.budget_limit_amount - project.budget_spent_amount)
        if project.budget_limit_amount is not None else None
    )
    return BudgetStatus(
        limit_amount=project.budget_limit_amount, currency=project.budget_currency,
        spent_amount=_round(project.budget_spent_amount), remaining_amount=remaining,
        unpriced_take_count=unpriced,
    )
