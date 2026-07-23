from __future__ import annotations


class PipelineError(Exception):
    """Base class for stage execution failures that the worker must classify."""

    code: str = "PIPELINE_ERROR"
    retryable: bool = True


class DependencyError(PipelineError):
    """A required external binary (ffmpeg/ffprobe) is missing. Never auto-retried."""

    code = "BLOCKED_DEPENDENCY"
    retryable = False


class ValidationFailedError(PipelineError):
    """Rendered output failed a technical quality check (resolution, audio, ...)."""

    code = "TECHNICAL_VALIDATION_FAILED"
    retryable = False


class SchemaValidationError(PipelineError):
    """LLM output did not match the expected script schema."""

    code = "SCHEMA_INVALID"
    retryable = True


class TransientProviderError(PipelineError):
    """Timeout / 429 / 5xx / connection error from an external provider."""

    code = "UPSTREAM_TRANSIENT"
    retryable = True


class ReviewRequiredSignal(Exception):
    """Not a failure: routes the job to REVIEW_REQUIRED with a reason_code."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)
