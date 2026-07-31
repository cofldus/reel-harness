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


class ContentPolicyRefusedError(PipelineError):
    """A generative provider's safety filter refused the request.

    Deliberately NOT retryable and deliberately not a hard failure: per
    .claude/rules/architecture.md an uncertain content-policy outcome
    goes to REVIEW_REQUIRED for a human decision, never an automatic
    block and never a blind retry of the same prompt (which would just
    burn quota to reach the same refusal).
    """

    code = "CONTENT_POLICY_REVIEW"
    retryable = False


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


class PublisherAppReviewRequiredError(PipelineError):
    """The connected app has not passed the platform's review/audit
    process, so publishing is restricted below what was requested (e.g.
    TikTok's unaudited-app rule, which forces every post to SELF_ONLY
    visibility regardless of the requested privacy_level -- see
    docs/PUBLISHING.md). Never auto-retried and never silently downgraded
    to the restricted option -- surfaced explicitly so the operator learns
    their real capability instead of a generic validation failure."""

    code = "APP_REVIEW_REQUIRED"
    retryable = False


class PublisherPrivacyNotAllowedError(PipelineError):
    """The requested privacy_status is not in this account's own allowed
    set, per a freshly-fetched CreatorInfo (providers.base.CreatorInfo) --
    never silently substituted for an allowed option."""

    code = "PRIVACY_NOT_ALLOWED"
    retryable = False


class PublisherCreatorNotEligibleError(PipelineError):
    """The connected creator/account cannot publish right now (e.g. an
    account-level warning surfaced by a creator-info query). Never
    auto-retried -- the operator must resolve the account-level issue."""

    code = "CREATOR_NOT_ELIGIBLE"
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


class VideoNotSupportedError(PipelineError):
    """The video's own properties (codec, aspect ratio, container) fall
    outside what the provider documents as supported, confirmed locally
    (via ffprobe) before ever attempting an upload -- see
    providers.instagram_media for the first adapter to check this
    up front against real documented limits."""

    code = "VIDEO_NOT_SUPPORTED"
    retryable = False


class VideoTooLargeError(PipelineError):
    """The video file exceeds the provider's documented maximum file
    size. Checked locally before any upload is attempted -- re-uploading
    the same file can never fix this."""

    code = "VIDEO_TOO_LARGE"
    retryable = False


class VideoTooLongError(PipelineError):
    """The video's duration falls outside the provider's documented
    min/max duration window. Checked locally before any upload is
    attempted."""

    code = "VIDEO_TOO_LONG"
    retryable = False


class PublisherPublishingLimitReachedError(PipelineError):
    """The account's own publishing-rate limit (e.g. Instagram's 100
    API-published posts per rolling 24-hour period) is exhausted. Not
    retried within the same process -- the limit resets on a
    provider-defined schedule, mirroring PublisherQuotaExceededError's
    treatment of YouTube's daily quota."""

    code = "PUBLISHING_LIMIT_REACHED"
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
