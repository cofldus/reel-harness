"""Refusals as a UI state, budget presets, and deleting a character.

The thread connecting all three: a refusal is a normal part of using this
tool -- a gate not reached, a budget not set -- so the page has to say
what to do about it. Before this, `assert_paid_generation_allowed`'s own
sentence went straight to the screen, naming a CLI flag on a page that
has a button for the same thing.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from reel_harness.api import app as api_app
from reel_harness.web.dependencies import CSRF_COOKIE_NAME
from reel_harness.web.fable_errors import (
    KNOWN_FAILURE_CODES,
    format_for_redirect,
    present_error,
)
from reel_harness.web.fable_forms import (
    BUDGET_CONFIRM_THRESHOLD,
    BUDGET_PRESETS,
    DEFAULT_BUDGET_PRESET,
    validate_budget_form,
)

STORY = "그날 밤, 그는 호텔 창밖의 비를 오래 바라보다 천천히 뒤를 돌아보았다."


@pytest.fixture
def client(monkeypatch, tmp_path):
    from reel_harness.bootstrap import AppContext
    from reel_harness.config import Settings

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'err.db').as_posix()}",
        jobs_dir=tmp_path / "jobs",
        fable_projects_dir=tmp_path / "fable_projects",
        credential_dir=tmp_path / "secrets",
    )
    ctx = AppContext(settings)
    monkeypatch.setattr(api_app, "_ctx", ctx)
    return TestClient(api_app.app), ctx


def _csrf(http):
    http.get("/fable")
    token = http.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return token


# -- classifying a refusal ------------------------------------------------

def test_a_coded_refusal_becomes_guidance_not_the_raw_sentence() -> None:
    presented = present_error(format_for_redirect(
        "PAID_GENERATION_NOT_ALLOWED",
        "provider 'google' costs money and project 8f23 has no budget limit set "
        "(fable-budget --limit)",
    ))
    assert presented is not None
    assert "예산" in presented.title
    # Points at the control on THIS page, not at a CLI flag.
    assert presented.action_anchor == "budget"
    # The exact message survives rather than being thrown away.
    assert "fable-budget" in presented.detail


def test_every_known_code_offers_a_title_and_an_explanation() -> None:
    for code in KNOWN_FAILURE_CODES:
        presented = present_error(f"{code}: detail")
        assert presented.title, code
        assert presented.explanation, code


def test_an_unknown_code_still_shows_the_message_rather_than_swallowing_it() -> None:
    """Worse-looking, never wrong: a failure mode nobody styled yet must
    not render as something it is not."""
    presented = present_error("SOMETHING_NEW: the pipe burst")
    assert presented is not None
    assert "SOMETHING_NEW" in presented.explanation


def test_an_uncoded_service_message_becomes_the_body() -> None:
    """Service preconditions are already written for a person."""
    presented = present_error("승인되지 않은 레퍼런스 시트가 2개 있습니다.")
    assert presented is not None
    assert "레퍼런스 시트가 2개" in presented.explanation
    assert presented.severity == "info"


def test_no_error_presents_as_nothing() -> None:
    assert present_error(None) is None
    assert present_error("") is None


def test_a_timeout_warns_against_retrying_because_it_was_billed() -> None:
    """Retrying a timed-out generation pays for the same shot twice."""
    presented = present_error("GENERATION_TIMEOUT: still generating -- provider job ops/1")
    assert "과금" in presented.explanation
    assert "두 번" in presented.explanation


# -- budget presets -------------------------------------------------------

def test_presets_are_offered_but_nothing_is_applied_by_default() -> None:
    """The paid gate exists to require a DECISION. A preset makes the
    decision one click; it must not make it for you."""
    assert len(BUDGET_PRESETS) >= 3
    assert DEFAULT_BUDGET_PRESET in [amount for amount, _ in BUDGET_PRESETS]


def test_the_default_preset_is_the_restrictive_one_that_still_works() -> None:
    """Erring toward stopping early beats erring toward overspending --
    the two directions are not symmetric."""
    amounts = sorted(amount for amount, _ in BUDGET_PRESETS)
    assert DEFAULT_BUDGET_PRESET <= amounts[len(amounts) // 2]


def test_a_large_ceiling_needs_a_second_confirmation() -> None:
    """The difference between $5 and $50 is one keystroke, and only one of
    them is recoverable."""
    big = str(BUDGET_CONFIRM_THRESHOLD + 1)
    refused = validate_budget_form(limit_amount=big, currency="USD")
    assert not refused.ok
    assert "limit_amount" in refused.errors

    allowed = validate_budget_form(limit_amount=big, currency="USD", confirm_large=True)
    assert allowed.ok
    assert allowed.value.limit_amount == BUDGET_CONFIRM_THRESHOLD + 1


def test_an_ordinary_ceiling_needs_no_confirmation() -> None:
    assert validate_budget_form(limit_amount="5", currency="USD").ok


def test_the_page_offers_the_presets(client) -> None:
    http, ctx = client
    project, _ = ctx.fable.create_project(
        title="t", source_text=STORY, idempotency_key="presets",
    )
    page = http.get(f"/fable/{project.id}")
    assert page.status_code == 200
    for amount, _label in BUDGET_PRESETS:
        assert f'value="{amount}"' in page.text


def test_setting_a_budget_from_a_preset_works(client) -> None:
    http, ctx = client
    project, _ = ctx.fable.create_project(
        title="t", source_text=STORY, idempotency_key="preset-set",
    )
    token = _csrf(http)
    http.post(f"/fable/{project.id}/budget", data={
        "csrf_token": token, "limit_amount": str(DEFAULT_BUDGET_PRESET), "currency": "USD",
    }, follow_redirects=False)
    assert ctx.fable.budget_status(project.id).limit_amount == DEFAULT_BUDGET_PRESET


# -- deleting a character -------------------------------------------------

def _adapted(ctx, key):
    project, _ = ctx.fable.create_project(title="t", source_text=STORY, idempotency_key=key)
    ctx.fable.adapt_project(project.id)
    return project.id


def test_a_character_a_shot_still_uses_cannot_be_deleted(client) -> None:
    """Deleting one leaves those shots pointing at an actor that does not
    exist, and the prompt compiler would then generate them with no fixed
    identity at all -- a film whose lead changes face, with no error
    anywhere along the way."""
    from reel_harness.core.service import InvalidActionError

    _, ctx = client
    project_id = _adapted(ctx, "in-use")
    character = ctx.fable.project_characters(project_id)[0]
    with pytest.raises(InvalidActionError, match="사용 중"):
        ctx.fable.delete_character(character.id)


def test_an_unused_character_can_be_deleted(client) -> None:
    _, ctx = client
    project_id = _adapted(ctx, "unused")
    from reel_harness.db.cinematic_models import FableCharacter

    with ctx.session_factory() as session:
        stray = FableCharacter(project_id=project_id, name="아무도안쓰는배우")
        session.add(stray)
        session.commit()
        stray_id = stray.id

    assert ctx.fable.delete_character(stray_id) == project_id
    assert all(c.id != stray_id for c in ctx.fable.project_characters(project_id))


def test_the_delete_button_is_disabled_with_a_reason_not_hidden(client) -> None:
    """An absent button leaves the operator guessing why."""
    http, ctx = client
    project_id = _adapted(ctx, "delete-ui")
    page = http.get(f"/fable/{project_id}")
    assert "삭제" in page.text
    assert "사용 중입니다" in page.text


def test_deleting_through_the_route_refuses_when_in_use(client) -> None:
    http, ctx = client
    project_id = _adapted(ctx, "route-refuse")
    character = ctx.fable.project_characters(project_id)[0]
    token = _csrf(http)

    response = http.post(
        f"/fable/characters/{character.id}/delete",
        data={"csrf_token": token, "project_id": project_id}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    # Still there.
    assert any(c.id == character.id for c in ctx.fable.project_characters(project_id))


def test_deleting_requires_csrf(client) -> None:
    http, ctx = client
    project_id = _adapted(ctx, "csrf-delete")
    character = ctx.fable.project_characters(project_id)[0]
    response = http.post(
        f"/fable/characters/{character.id}/delete", data={"project_id": project_id},
    )
    assert response.status_code == 403


def test_deleting_an_unknown_character_is_404(client) -> None:
    http, _ = client
    token = _csrf(http)
    response = http.post(
        "/fable/characters/nope/delete", data={"csrf_token": token, "project_id": "x"},
    )
    assert response.status_code == 404


# -- take review: enlarging and un-choosing --------------------------------

def test_a_selected_take_can_be_unchosen(client) -> None:
    """Selecting is a judgement, and judgements get revised. Before this
    it was permanent from the UI even though nothing in the domain
    required that."""
    from reel_harness.core.cinematic_state import FableProjectStatus
    from reel_harness.db.cinematic_models import FableShot, FableTake, StoryProject

    _, ctx = client
    project_id = _adapted(ctx, "deselect")
    shot = ctx.fable.project_shots(project_id)[0]
    with ctx.session_factory() as session:
        session.add(FableTake(
            shot_id=shot.id, provider="fake", prompt_fingerprint="fp",
            attempt_number=1, status="DOWNLOADED", media_path="/tmp/a.mp4",
        ))
        session.get(FableShot, shot.id).status = "REVIEW_REQUIRED"
        session.get(StoryProject, project_id).status = FableProjectStatus.TAKE_REVIEW.value
        session.commit()

    take = ctx.fable.shot_takes(shot.id)[0]
    ctx.fable.select_take(take.id)
    assert ctx.fable.shot_takes(shot.id)[0].selected is True

    ctx.fable.deselect_take(take.id)
    assert ctx.fable.shot_takes(shot.id)[0].selected is False
    # Back to a decision, not left in limbo.
    assert ctx.fable.project_shots(project_id)[0].status == "REVIEW_REQUIRED"


def test_unchoosing_is_refused_once_the_film_is_past_review(client) -> None:
    """Un-choosing a shot already cut into the film would make the
    rendered film disagree with the project."""
    from reel_harness.core.cinematic_state import FableProjectStatus
    from reel_harness.core.service import InvalidActionError
    from reel_harness.db.cinematic_models import FableShot, FableTake, StoryProject

    _, ctx = client
    project_id = _adapted(ctx, "deselect-late")
    shot = ctx.fable.project_shots(project_id)[0]
    with ctx.session_factory() as session:
        session.add(FableTake(
            shot_id=shot.id, provider="fake", prompt_fingerprint="fp",
            attempt_number=1, status="DOWNLOADED", media_path="/tmp/a.mp4", selected=True,
        ))
        session.get(FableShot, shot.id).status = "SELECTED"
        session.get(StoryProject, project_id).status = FableProjectStatus.COMPLETED.value
        session.commit()

    take = ctx.fable.shot_takes(shot.id)[0]
    with pytest.raises(InvalidActionError, match="되돌릴 수 없습니다"):
        ctx.fable.deselect_take(take.id)


def test_unchoosing_an_unselected_take_is_refused(client) -> None:
    from reel_harness.core.service import InvalidActionError
    from reel_harness.db.cinematic_models import FableTake

    _, ctx = client
    project_id = _adapted(ctx, "deselect-noop")
    shot = ctx.fable.project_shots(project_id)[0]
    with ctx.session_factory() as session:
        session.add(FableTake(
            shot_id=shot.id, provider="fake", prompt_fingerprint="fp",
            attempt_number=1, status="DOWNLOADED", media_path="/tmp/a.mp4",
        ))
        session.commit()
    take = ctx.fable.shot_takes(shot.id)[0]
    with pytest.raises(InvalidActionError, match="선택된 상태가 아닙니다"):
        ctx.fable.deselect_take(take.id)


def test_the_page_ships_one_lightbox_not_one_per_take(client) -> None:
    """Rendered once rather than per card, so opening one clip does not
    mean the browser has parsed forty players."""
    http, ctx = client
    project_id = _adapted(ctx, "lightbox")
    page = http.get(f"/fable/{project_id}").text
    assert page.count('id="take-lightbox"') == 1
