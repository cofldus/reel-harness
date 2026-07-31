"""Fable cinematic project domain (Phase F1) -- a SEPARATE model family
from the short-form reel Job pipeline, deliberately not bolted onto Job
(see docs/STATUS.md's Fable section and .claude/rules/architecture.md):
a cinematic project is a tree (project -> scenes -> shots -> takes) with
shot-level generation state, which the linear Job stage walk cannot
represent. Storage/worker/provider substrate is shared; the models are
not.

Same declarative Base as db.models, so db.schema.init_db()'s create_all
picks these tables up for both fresh and existing databases with no
ALTER-based migration (the same way the v5 publications tables landed).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reel_harness.db.models import Base, _now, new_uuid


class StoryProject(Base):
    """One cinematic production: source story -> adaptation -> casting ->
    storyboard -> generation -> edit -> final film. `story_bible` is one
    JSON document (premise/theme/setting/time_period/visual_style/
    color_language/narrative_point_of_view/ending/prohibited_elements)
    rather than a column per field: it is written once by adaptation,
    read as a whole by prompt compilation, and never queried by parts."""

    __tablename__ = "fable_projects"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_fable_project_idempotency"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    idempotency_key: Mapped[str]
    title: Mapped[str]
    source_text: Mapped[str]
    language: Mapped[str] = mapped_column(default="ko")
    genre: Mapped[str | None] = mapped_column(default=None)
    tone: Mapped[str | None] = mapped_column(default=None)
    target_duration_sec: Mapped[int] = mapped_column(default=60)
    aspect_ratio: Mapped[str] = mapped_column(default="9:16")

    story_bible: Mapped[dict | None] = mapped_column(JSON, default=None)
    # Provider snapshot at creation -- same pinning discipline as
    # Job.provider_config (provider id, model, safe host, never a key).
    provider_config: Mapped[dict | None] = mapped_column(JSON, default=None)
    # v9: deterministic identity of the adaptation INPUT (source text +
    # parameters + prompt version). Re-running adapt_project with the same
    # fingerprint on an already-adapted project is a no-op replay rather
    # than a second paid LLM call -- see core.fable_service.adapt_project.
    adaptation_fingerprint: Mapped[str | None] = mapped_column(default=None)
    # v10: spending ceiling for this project (core.cost_service). NULL
    # means "no limit set", which is NOT "unlimited": a cost-incurring
    # provider is refused outright until an operator names a number, so
    # the absence of a decision can never be mistaken for permission.
    # `budget_spent_amount` only ever accumulates costs a provider
    # actually reported for a completed generation -- never an estimate.
    budget_limit_amount: Mapped[float | None] = mapped_column(default=None)
    budget_currency: Mapped[str | None] = mapped_column(default=None)
    budget_spent_amount: Mapped[float] = mapped_column(default=0.0)

    status: Mapped[str] = mapped_column(default="DRAFT")
    failure_code: Mapped[str | None] = mapped_column(default=None)
    failure_summary: Mapped[str | None] = mapped_column(default=None)
    cancel_requested: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    characters: Mapped[list[FableCharacter]] = relationship(back_populates="project")
    locations: Mapped[list[FableLocation]] = relationship(back_populates="project")
    scenes: Mapped[list[FableScene]] = relationship(back_populates="project")


class FableCharacter(Base):
    """A VIRTUAL adult actor -- never a real person, celebrity, or user
    face (enforced at the service layer; `adult_confirmed` must be True
    before any reference or shot generation may reference this
    character). Appearance fields live in `bible` as one structured JSON
    document (face/hair/wardrobe/mannerisms/voice_profile...): written by
    adaptation, consumed whole by the prompt compiler, split there into
    fixed-identity vs per-shot-variable elements."""

    __tablename__ = "fable_characters"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("fable_projects.id"))
    name: Mapped[str]
    role: Mapped[str | None] = mapped_column(default=None)
    age_range: Mapped[str | None] = mapped_column(default=None)
    adult_confirmed: Mapped[bool] = mapped_column(default=False)
    bible: Mapped[dict | None] = mapped_column(JSON, default=None)

    # F3: generated reference imagery (approval-gated before any paid
    # video generation may use it). Nullable until then.
    #
    # `reference_image_path` is the FACE portrait specifically -- the one
    # view every other view was chained from, and the one a downstream
    # video model gets when it accepts a single character reference.
    # v11 `reference_images` carries the whole sheet as one JSON doc
    # ({view: path}), following the same one-document convention as
    # `bible`: written once by generation, read whole by whoever needs a
    # view, never queried by parts.
    reference_image_path: Mapped[str | None] = mapped_column(default=None)
    reference_fingerprint: Mapped[str | None] = mapped_column(default=None)
    reference_approved: Mapped[bool] = mapped_column(default=False)
    reference_images: Mapped[dict | None] = mapped_column(JSON, default=None)
    # v11: why the sheet is missing, when it is. A content-policy refusal
    # is recorded HERE rather than failing the project: the operator sees
    # which character was refused at the CHARACTER_REVIEW gate and edits
    # the bible or rejects, which is the human decision the architecture
    # rules require for an uncertain policy outcome.
    reference_failure_code: Mapped[str | None] = mapped_column(default=None)
    reference_failure_summary: Mapped[str | None] = mapped_column(default=None)
    # v11: what this character's sheet ACTUALLY cost, summed across its
    # views. The line item behind the project's running total, for the
    # same reason FableTake carries one: reference images spend real
    # money, so a spend audit that only counted takes would silently
    # under-report every project that generated a cast.
    reference_cost_amount: Mapped[float | None] = mapped_column(default=None)
    reference_cost_currency: Mapped[str | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(default=_now)

    project: Mapped[StoryProject] = relationship(back_populates="characters")


class FableLocation(Base):
    __tablename__ = "fable_locations"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("fable_projects.id"))
    name: Mapped[str]
    description: Mapped[str | None] = mapped_column(default=None)
    # lighting/time_of_day/weather/structure continuity anchors, one doc.
    continuity: Mapped[dict | None] = mapped_column(JSON, default=None)

    reference_image_path: Mapped[str | None] = mapped_column(default=None)
    reference_fingerprint: Mapped[str | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(default=_now)

    project: Mapped[StoryProject] = relationship(back_populates="locations")


class FableScene(Base):
    __tablename__ = "fable_scenes"
    __table_args__ = (
        UniqueConstraint("project_id", "scene_order", name="uq_fable_scene_order"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("fable_projects.id"))
    scene_order: Mapped[int]
    location_id: Mapped[str | None] = mapped_column(ForeignKey("fable_locations.id"), default=None)
    story_purpose: Mapped[str | None] = mapped_column(default=None)
    emotional_beat: Mapped[str | None] = mapped_column(default=None)
    continuity_notes: Mapped[dict | None] = mapped_column(JSON, default=None)
    dialogue: Mapped[dict | None] = mapped_column(JSON, default=None)
    estimated_duration_sec: Mapped[float | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(default=_now)

    project: Mapped[StoryProject] = relationship(back_populates="scenes")
    shots: Mapped[list[FableShot]] = relationship(back_populates="scene")


class FableShot(Base):
    """The leasable generation unit -- carries the same lease-fencing
    columns as Job/Publication (locked_by + lease_token + heartbeat_at)
    so the fable worker lane reuses the exact guarded-UPDATE claim
    discipline. Shot grammar fields are plain strings validated against
    core.cinematic_state's enums at the service layer, mirroring how
    Job.status stores JobStatus values as strings."""

    __tablename__ = "fable_shots"
    __table_args__ = (
        UniqueConstraint("scene_id", "shot_order", name="uq_fable_shot_order"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    scene_id: Mapped[str] = mapped_column(ForeignKey("fable_scenes.id"))
    shot_order: Mapped[int]

    shot_size: Mapped[str | None] = mapped_column(default=None)
    camera_angle: Mapped[str | None] = mapped_column(default=None)
    camera_movement: Mapped[str | None] = mapped_column(default=None)
    lens_style: Mapped[str | None] = mapped_column(default=None)
    subject: Mapped[str | None] = mapped_column(default=None)
    action: Mapped[str | None] = mapped_column(default=None)
    expression: Mapped[str | None] = mapped_column(default=None)
    blocking: Mapped[str | None] = mapped_column(default=None)
    lighting: Mapped[str | None] = mapped_column(default=None)
    duration_sec: Mapped[float | None] = mapped_column(default=None)
    dialogue_line: Mapped[str | None] = mapped_column(default=None)
    sound_notes: Mapped[str | None] = mapped_column(default=None)
    continuity_requirements: Mapped[dict | None] = mapped_column(JSON, default=None)

    status: Mapped[str] = mapped_column(default="PLANNED")
    retry_count: Mapped[int] = mapped_column(default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(default=None)
    failure_code: Mapped[str | None] = mapped_column(default=None)
    failure_summary: Mapped[str | None] = mapped_column(default=None)
    cancel_requested: Mapped[bool] = mapped_column(default=False)

    locked_by: Mapped[str | None] = mapped_column(default=None)
    lease_token: Mapped[str | None] = mapped_column(default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    scene: Mapped[FableScene] = relationship(back_populates="shots")
    takes: Mapped[list[FableTake]] = relationship(back_populates="shot")


class FableTake(Base):
    """One generation attempt for one shot -- append-only like Asset
    (rejected takes are kept under a retention policy, never deleted on
    selection). The (shot_id, prompt_fingerprint, attempt_number) unique
    constraint IS the duplicate-generation guard: a worker retrying after
    a lost provider response hits the constraint instead of paying for a
    second identical generation. Only safe provider metadata is stored --
    provider job id yes; tokens and signed URLs never."""

    __tablename__ = "fable_takes"
    __table_args__ = (
        UniqueConstraint(
            "shot_id", "prompt_fingerprint", "attempt_number",
            name="uq_fable_take_generation",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    shot_id: Mapped[str] = mapped_column(ForeignKey("fable_shots.id"))
    attempt_number: Mapped[int] = mapped_column(default=1)

    provider: Mapped[str]
    provider_job_reference: Mapped[str | None] = mapped_column(default=None)
    generation_seed: Mapped[int | None] = mapped_column(default=None)
    prompt_fingerprint: Mapped[str]
    reference_fingerprint: Mapped[str | None] = mapped_column(default=None)

    media_path: Mapped[str | None] = mapped_column(default=None)
    checksum_sha256: Mapped[str | None] = mapped_column(default=None)
    license: Mapped[str | None] = mapped_column(default=None)
    # v10: what this take ACTUALLY cost, as reported by the provider.
    # None means the provider published no per-generation figure -- an
    # honest gap (see CinematicVideoResult.cost_amount), never a guess,
    # and never silently counted as zero when reporting spend.
    cost_amount: Mapped[float | None] = mapped_column(default=None)
    cost_currency: Mapped[str | None] = mapped_column(default=None)

    status: Mapped[str] = mapped_column(default="SUBMITTED")
    quality: Mapped[dict | None] = mapped_column(JSON, default=None)
    continuity: Mapped[dict | None] = mapped_column(JSON, default=None)
    selected: Mapped[bool] = mapped_column(default=False)
    rejection_reasons: Mapped[dict | None] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    shot: Mapped[FableShot] = relationship(back_populates="takes")
