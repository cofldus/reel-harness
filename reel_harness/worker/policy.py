from __future__ import annotations

from reel_harness.core.state_machine import JobStatus, Stage

# (max_retries, backoff_seconds_by_attempt) per stage. The last backoff value
# repeats if retry_count exceeds the list length. These are engineering defaults,
# not measured SLAs -- tune per channel/provider once real usage data exists.
STAGE_RETRY_POLICY: dict[Stage, tuple[int, list[int]]] = {
    Stage.TOPIC: (3, [5, 10, 20]),
    Stage.SCRIPT: (3, [10, 20, 40]),
    Stage.POLICY: (0, []),
    Stage.ASSET: (4, [5, 10, 20, 40]),
    Stage.TTS: (3, [5, 10, 20]),
    Stage.RENDER: (1, [15]),
    Stage.VALIDATE: (0, []),
    Stage.PUBLISH: (3, [30, 60, 120]),
}

STAGE_ENTRY_STATUS: dict[Stage, JobStatus] = {
    Stage.TOPIC: JobStatus.TOPIC_GENERATING,
    Stage.SCRIPT: JobStatus.SCRIPT_GENERATING,
    Stage.POLICY: JobStatus.POLICY_CHECKING,
    Stage.ASSET: JobStatus.ASSET_FETCHING,
    Stage.TTS: JobStatus.TTS_GENERATING,
    Stage.RENDER: JobStatus.RENDERING,
    Stage.VALIDATE: JobStatus.VALIDATING,
    Stage.PUBLISH: JobStatus.PUBLISHING,
}

STAGE_ORDER: list[Stage] = [Stage.SCRIPT, Stage.POLICY, Stage.ASSET, Stage.TTS, Stage.RENDER, Stage.VALIDATE]

# Statuses a worker can hold a lease against; used by crash recovery to find jobs
# whose heartbeat has gone stale mid-stage.
ACTIVE_STAGE_STATUSES: set[JobStatus] = {
    JobStatus.TOPIC_GENERATING, JobStatus.SCRIPT_GENERATING, JobStatus.POLICY_CHECKING,
    JobStatus.ASSET_FETCHING, JobStatus.TTS_GENERATING, JobStatus.RENDERING,
    JobStatus.VALIDATING, JobStatus.PUBLISHING,
}
