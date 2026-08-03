"""The bounded repair loop that turns a NarrativeDirector's raw output
into a validated adaptation (Fable F2).

Loop shape (see the F2 plan):
    attempt 0        -> adapt_story
    attempts 1..MAX  -> repair_adaptation(previous raw, collected errors)
    all exhausted    -> AdaptationValidationError (SCHEMA_INVALID,
                        retryable) so the existing stage-retry
                        classification takes over unchanged.

A refusal or empty response is NOT worth repairing -- the director
already declined, and re-asking with the same source only burns quota --
so those surface immediately as SchemaValidationError instead of
consuming the repair budget.

This module never touches the database; persistence is
core.fable_service's job. That separation is what lets the repair loop be
tested exhaustively without any DB fixture."""
from __future__ import annotations

from dataclasses import dataclass

from reel_harness.pipeline.adaptation_parser import (
    SHOT_SECONDS,
    AdaptationValidationError,
    parse_adaptation,
)
from reel_harness.pipeline.adaptation_schema import AdaptationModel
from reel_harness.providers.base import AdaptationRequest, NarrativeDirector

# Total LLM calls per adaptation is MAX_REPAIR_ATTEMPTS + 1.
MAX_REPAIR_ATTEMPTS = 2


@dataclass
class AdaptationOutcome:
    adaptation: AdaptationModel
    provider_id: str
    model_id: str
    prompt_version: str
    attempts: int  # total director calls made (1 = no repair needed)
    repair_errors: list[list[str]]  # errors that triggered each repair, in order
    usage: dict | None = None
    request_id: str | None = None


def run_adaptation(
    director: NarrativeDirector, request: AdaptationRequest,
    *, max_repair_attempts: int = MAX_REPAIR_ATTEMPTS,
) -> AdaptationOutcome:
    result = director.adapt_story(request)
    repair_errors: list[list[str]] = []

    for attempt in range(max_repair_attempts + 1):
        if not result.raw_text.strip():
            raise AdaptationValidationError(["director returned an empty response"])
        try:
            adaptation = parse_adaptation(
                result.raw_text, source_text=request.source_text, keep_ending=request.keep_ending,
                target_shot_count=max(1, round(request.target_duration_sec / SHOT_SECONDS)),
            )
        except AdaptationValidationError as exc:
            if attempt >= max_repair_attempts:
                raise
            repair_errors.append(exc.errors)
            result = director.repair_adaptation(request, result.raw_text, exc.errors)
            continue
        return AdaptationOutcome(
            adaptation=adaptation, provider_id=result.provider_id, model_id=result.model_id,
            prompt_version=result.prompt_version, attempts=attempt + 1,
            repair_errors=repair_errors, usage=result.usage, request_id=result.request_id,
        )
    raise AssertionError("unreachable: the loop either returns or raises")  # pragma: no cover
