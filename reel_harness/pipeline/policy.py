from __future__ import annotations

from reel_harness.core.errors import ReviewRequiredSignal
from reel_harness.core.state_machine import ReasonCode

# Deterministic, local rule check only (no external moderation API in Phase 0/1).
# Anything this doesn't clearly flag is allowed through to REVIEW_REQUIRED at the
# end of the pipeline anyway (a human always approves before COMPLETED) -- this
# check exists to catch obvious cases earlier, not to be the only safety net.
BANNED_TERMS = {"guaranteed cure", "100% profit", "risk-free investment"}


def check_policy(job_script: dict) -> None:
    text_blob = " ".join(scene["voiceover"] for scene in job_script["scenes"]).lower()
    for term in BANNED_TERMS:
        if term in text_blob:
            raise ReviewRequiredSignal(ReasonCode.CONTENT_POLICY_REVIEW.value, f"matched banned term: {term!r}")
