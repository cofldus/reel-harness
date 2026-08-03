"""Narrative Director against any OpenAI-compatible /chat/completions
endpoint (Fable F2).

Subclasses OpenAICompatibleLLMProvider deliberately: adaptation talks to
the SAME endpoint shape with the SAME operational contract (bounded
retries on timeout/429/5xx, Retry-After honored, auth errors never
retried and never echoing the credential, request/response bodies never
logged, only request id + token usage surfaced). Re-implementing that
transport would mean maintaining two copies of a security-relevant code
path; inheriting it means the adaptation call is protected by exactly the
rules the script call already proved out.

What differs is only the prompt and the output budget: a full shot plan
is far larger than a short-form script, so max_output_tokens defaults
much higher here.

Vendor-neutral: the concrete vendor is chosen entirely by base_url +
model, exactly as with the script provider."""
from __future__ import annotations

from typing import Any

from reel_harness.providers.base import (
    AdaptationRequest,
    AdaptationResult,
    RefinementRequest,
    RefinementResult,
)
from reel_harness.providers.narrative_prompts import (
    ADAPTATION_SYSTEM_PROMPT,
    NARRATIVE_PROMPT_VERSION,
    REFINEMENT_PROMPT_VERSION,
    REFINEMENT_SYSTEM_PROMPT,
    build_refinement_prompt,
    build_repair_prompt,
    build_user_prompt,
)
from reel_harness.providers.openai_compatible_llm import OpenAICompatibleLLMProvider

# A full adaptation (bible + characters + locations + scenes + shots) is
# an order of magnitude larger than a short-form script.
DEFAULT_ADAPTATION_MAX_OUTPUT_TOKENS = 6000


class OpenAICompatibleNarrativeDirector(OpenAICompatibleLLMProvider):
    provider_id = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        connect_timeout: float = 10.0,
        read_timeout: float = 120.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
        temperature: float = 0.7,
        max_output_tokens: int = DEFAULT_ADAPTATION_MAX_OUTPUT_TOKENS,
        transport: Any = None,
    ) -> None:
        super().__init__(
            base_url=base_url, model=model, api_key=api_key,
            connect_timeout=connect_timeout, read_timeout=read_timeout,
            max_retries=max_retries, retry_backoff_seconds=retry_backoff_seconds,
            temperature=temperature, max_output_tokens=max_output_tokens,
            transport=transport,
        )

    def adapt_story(self, request: AdaptationRequest) -> AdaptationResult:
        content, request_id, usage = self._chat(
            ADAPTATION_SYSTEM_PROMPT, build_user_prompt(request),
        )
        # Validation is downstream (pipeline.adaptation_parser) -- the
        # adapter's contract is a non-empty raw text plus provenance.
        return AdaptationResult(
            raw_text=content, provider_id=self.provider_id, model_id=self.model_id,
            prompt_version=NARRATIVE_PROMPT_VERSION, request_id=request_id, usage=usage,
        )

    def repair_adaptation(
        self, request: AdaptationRequest, previous_raw: str, errors: list[str],
    ) -> AdaptationResult:
        """Re-ask carrying the previous output and the exact validation
        errors. The original user prompt is repeated first so the model
        still has the source story and parameters in context -- a repair
        that only saw the errors would be free to rewrite the adaptation
        from nothing."""
        user_prompt = (
            build_user_prompt(request) + "\n\n" + build_repair_prompt(previous_raw, errors)
        )
        content, request_id, usage = self._chat(ADAPTATION_SYSTEM_PROMPT, user_prompt)
        return AdaptationResult(
            raw_text=content, provider_id=self.provider_id, model_id=self.model_id,
            prompt_version=NARRATIVE_PROMPT_VERSION, request_id=request_id, usage=usage,
        )

    def refine_source(self, request: RefinementRequest) -> RefinementResult:
        """Rewrite the user's own prose so the pipeline can shoot it.

        Deliberately NOT wired into the repair loop: a refusal or a
        malformed response here is a dead end for one optional convenience,
        not a failed adaptation, so it degrades to "the model returned
        nothing usable" and the user keeps the text they already had.
        Retrying a creative rewrite would also just spend money on a second
        opinion nobody asked for."""
        content, request_id, usage = self._chat(
            REFINEMENT_SYSTEM_PROMPT, build_refinement_prompt(request),
        )
        refined, notes = _parse_refinement(content)
        return RefinementResult(
            refined_text=refined, notes=notes,
            provider_id=self.provider_id, model_id=self.model_id,
            prompt_version=REFINEMENT_PROMPT_VERSION, request_id=request_id, usage=usage,
        )


def _parse_refinement(content: str) -> tuple[str, list[str]]:
    """Tolerant of a fenced block, strict about the outcome.

    A model that returns prose instead of JSON has still done the work, so
    the whole response becomes the refined text rather than being thrown
    away -- but an EMPTY result raises, because silently handing the user
    back a blank textarea would destroy what they wrote.
    """
    import json

    text = content.strip()
    if text.startswith("```"):
        text = text.split(chr(10), 1)[-1].rsplit("```", 1)[0].strip()
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, dict):
        refined = str(document.get("refined_text") or "").strip()
        raw_notes = document.get("notes") or []
        notes = [str(note).strip() for note in raw_notes if str(note).strip()][:8]
    else:
        refined, notes = text, []
    if not refined:
        raise ValueError("refinement returned no text")
    return refined, notes
