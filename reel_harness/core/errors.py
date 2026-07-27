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


class ProviderNotConfiguredError(PipelineError):
    """A job's pinned provider snapshot cannot be satisfied by the current
    configuration (provider unregistered, credentials missing, or endpoint host
    mismatch). Never auto-retried and never silently replaced by another
    provider -- the operator must fix the configuration and retry explicitly."""

    code = "PROVIDER_NOT_CONFIGURED"
    retryable = False


class ProviderAuthError(PipelineError):
    """401/403 from an external provider: the configured credential is wrong or
    lacks permission. Never auto-retried -- retrying cannot fix a bad key."""

    code = "UPSTREAM_AUTH"
    retryable = False


class MissingPrerequisiteError(PipelineError):
    """Resuming a stage requires an earlier stage's persisted output (script,
    assets, tts audio, rendered video, render metadata) and it is missing or
    corrupted. Never auto-retried: re-running the same stage cannot recreate a
    predecessor's output -- the operator must retry from the stage that owns it."""

    code = "MISSING_PREREQUISITE"
    retryable = False


class UnsupportedResumeStageError(PipelineError):
    """retry_target_stage names a stage the pipeline cannot resume from (e.g.
    PUBLISH, or an unknown value persisted by older code). Never auto-retried."""

    code = "UNSUPPORTED_RESUME_STAGE"
    retryable = False


class ReviewRequiredSignal(Exception):
    """Not a failure: routes the job to REVIEW_REQUIRED with a reason_code."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)
