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


# --- Publisher errors (Phase 3A) ---------------------------------------
# Same PipelineError-style classification (code + retryable) as the render
# pipeline's errors above, reused by worker.publish_runner exactly like
# worker.runner reuses PipelineError for the render stages.

class PublishNotEligibleError(PipelineError):
    """The job failed the publish-eligibility re-check at publication
    creation time. Never auto-retried -- eligibility failures require the
    operator to fix the underlying job/asset/approval state, not a retry."""

    code = "PUBLISH_NOT_ELIGIBLE"
    retryable = False


class PublisherNotConfiguredError(PipelineError):
    """A publication's pinned publisher snapshot cannot be satisfied by the
    current configuration/credentials. Never auto-retried, never silently
    switched to a different account -- see providers.registry."""

    code = "PUBLISHER_NOT_CONFIGURED"
    retryable = False


class PublisherPermissionDeniedError(PipelineError):
    """The authenticated account lacks permission for the requested
    operation (e.g. wrong channel, insufficient OAuth scope). Never
    auto-retried -- retrying cannot fix a permission problem."""

    code = "UPSTREAM_PERMISSION_DENIED"
    retryable = False


class PublisherQuotaExceededError(PipelineError):
    """The provider's daily quota is exhausted. Not retried within the same
    process -- quota resets on a provider-defined schedule (typically daily),
    so the publication worker should stop attempting this publication until
    an operator or a scheduled retry well in the future resumes it."""

    code = "UPSTREAM_QUOTA_EXCEEDED"
    retryable = False


class PublisherRateLimitedError(TransientProviderError):
    """429 from the publisher API. Retryable, honoring Retry-After -- same
    treatment as the LLM/TTS/asset adapters' 429 handling."""

    code = "UPSTREAM_RATE_LIMITED"
    retryable = True


class UploadSessionExpiredError(PipelineError):
    """The resumable upload session is no longer valid (expired or the
    provider has forgotten it). Retryable at the publication level: the
    worker starts a brand-new session rather than resuming the old one."""

    code = "UPLOAD_SESSION_EXPIRED"
    retryable = True


class UploadInterruptedError(TransientProviderError):
    """A chunk upload was interrupted by a transient network/transport
    error. Retryable by resuming from the provider-confirmed offset."""

    code = "UPLOAD_INTERRUPTED"
    retryable = True


class UploadRejectedError(PipelineError):
    """The provider rejected the uploaded content itself (bad codec, empty
    file, policy violation, etc. -- see docs/PUBLISHING.md's
    failureReason/rejectionReason tables). Never auto-retried: re-uploading
    the same bytes cannot fix a content problem."""

    code = "UPLOAD_REJECTED"
    retryable = False


class ProcessingFailedError(PipelineError):
    """The provider's post-upload processing failed (transcode failure,
    etc.). Never auto-retried."""

    code = "PROCESSING_FAILED"
    retryable = False


class MetadataInvalidError(PipelineError):
    """Title/description/tags/category failed provider-side or local
    validation. Never auto-retried without fixing the metadata."""

    code = "METADATA_INVALID"
    retryable = False


class DuplicatePublicationError(PipelineError):
    """An idempotency-scope match already exists (or the provider already
    has a video for this exact upload) -- see core.publish_service. Never
    auto-retried; the caller should use the existing publication instead."""

    code = "DUPLICATE_PUBLICATION"
    retryable = False


class PublicationCancelledSignal(Exception):
    """Not a failure: an operator-requested cancellation was observed and
    honored at a safe checkpoint. Mirrors ReviewRequiredSignal's role for
    the render pipeline."""
