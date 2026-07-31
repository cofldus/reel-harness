"""Fable web UI routes and view models (Phase F4).

Two properties get most of the attention here, both inherited from
Phase 5A/5B's own hard-won lessons:

1. **Every `can_*` mirrors the real service precondition.** A button the
   page offers must be one the service accepts; a button it hides must be
   one the service would refuse. 5B found a genuine backend bug this way.
2. **A form is disabled, never removed.** A page that hides a whole form
   also hides its CSRF field, which is the exact bug Phase 5A's
   CSRF-fragment mistake established the precedent for.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from reel_harness.api import app as api_app
from reel_harness.web.dependencies import CSRF_COOKIE_NAME
from reel_harness.web.fable_forms import validate_budget_form, validate_new_fable_form

STORY = "그날 밤, 그는 호텔 창밖의 비를 오래 바라보다 천천히 뒤를 돌아보았다."


@pytest.fixture
def client(monkeypatch, tmp_path):
    from reel_harness.bootstrap import AppContext
    from reel_harness.config import Settings

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'web.db').as_posix()}",
        jobs_dir=tmp_path / "jobs",
        fable_projects_dir=tmp_path / "fable_projects",
        credential_dir=tmp_path / "secrets",
    )
    ctx = AppContext(settings)
    monkeypatch.setattr(api_app, "_ctx", ctx)
    return TestClient(api_app.app), ctx


def _create(ctx, key="web-1", **kwargs):
    project, _ = ctx.fable.create_project(
        title="비 오는 밤", source_text=STORY, idempotency_key=key, **kwargs,
    )
    return project


def _csrf(http):
    """Visit a page to obtain a CSRF cookie, then submit its value back --
    the double-submit contract, exercised the way a browser does."""
    http.get("/fable")
    token = http.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return token


# -- form validation -----------------------------------------------------

def test_new_project_form_requires_a_title_and_a_real_story() -> None:
    result = validate_new_fable_form(
        title="  ", source_text="짧다", language="ko", aspect_ratio="9:16", takes_per_shot=0,
    )
    assert not result.ok
    assert "title" in result.errors
    assert "source_text" in result.errors


def test_new_project_form_takes_choices_come_from_the_domain() -> None:
    """A list retyped in the UI is a list that drifts from the one the
    service enforces."""
    ok = validate_new_fable_form(
        title="t", source_text=STORY, language="ko", aspect_ratio="9:16", takes_per_shot=4,
    )
    assert ok.ok and ok.value.takes_per_shot == 4

    bad = validate_new_fable_form(
        title="t", source_text=STORY, language="ko", aspect_ratio="9:16", takes_per_shot=3,
    )
    assert not bad.ok
    assert "takes_per_shot" in bad.errors


def test_unspecified_takes_stays_unspecified_not_one() -> None:
    """0 is the form's empty value and must leave the project on the
    operator-wide default rather than pinning it to 1."""
    result = validate_new_fable_form(
        title="t", source_text=STORY, language="ko", aspect_ratio="9:16", takes_per_shot=0,
    )
    assert result.ok
    assert result.value.takes_per_shot is None


def test_budget_form_refuses_a_malformed_limit() -> None:
    """A typo that silently became a bigger number is the worst possible
    failure mode for a spending ceiling."""
    assert not validate_budget_form(limit_amount="abc", currency="USD").ok
    assert not validate_budget_form(limit_amount="-5", currency="USD").ok
    assert not validate_budget_form(limit_amount="5", currency="").ok
    ok = validate_budget_form(limit_amount=" 5.5 ", currency=" usd ")
    assert ok.ok
    assert ok.value.limit_amount == 5.5
    assert ok.value.currency == "USD"


# -- view models: can_* mirrors the service ------------------------------

@pytest.mark.parametrize(("status", "expected"), [
    ("DRAFT", "can_adapt"),
    ("ADAPTING", "can_adapt"),
    ("STORY_REVIEW", "can_approve_story"),
    ("CASTING", "can_generate_references"),
    ("SHOT_REVIEW", "can_approve_shots"),
    ("EDITING", "can_render"),
    ("FINAL_REVIEW", "can_approve_final"),
])
def test_each_gate_offers_exactly_its_own_action(client, status, expected) -> None:
    from reel_harness.core.cost_service import BudgetStatus
    from reel_harness.web.fable_view_models import build_fable_detail_view

    _, ctx = client
    project = _create(ctx, key=f"gate-{status}")
    project.status = status
    view = build_fable_detail_view(
        project, [], [], BudgetStatus(None, None, 0.0, None, 0),
    )
    assert getattr(view, expected) is True
    others = [
        name for name in (
            "can_adapt", "can_approve_story", "can_generate_references",
            "can_approve_shots", "can_render", "can_approve_final",
        ) if name != expected
    ]
    assert not any(getattr(view, name) for name in others)


def test_a_terminal_project_offers_nothing_but_reading(client) -> None:
    from reel_harness.core.cost_service import BudgetStatus
    from reel_harness.web.fable_view_models import build_fable_detail_view

    _, ctx = client
    project = _create(ctx, key="terminal")
    project.status = "COMPLETED"
    view = build_fable_detail_view(project, [], [], BudgetStatus(None, None, 0.0, None, 0))
    assert view.is_terminal is True
    assert view.can_cancel is False
    assert not any((view.can_adapt, view.can_render, view.can_approve_final))


def test_character_gate_explains_why_it_is_blocked(client) -> None:
    """The button being absent is not self-explanatory, and "generate the
    sheets first" is exactly the next step."""
    from reel_harness.core.cost_service import BudgetStatus
    from reel_harness.web.fable_view_models import build_fable_detail_view

    _, ctx = client
    project = _create(ctx, key="blocked")
    ctx.fable.adapt_project(project.id)
    ctx.fable.approve_story(project.id)
    ctx.fable.generate_references(project.id)
    project = ctx.fable.get_project(project.id)

    view = build_fable_detail_view(
        project, ctx.fable.project_characters(project.id), [],
        BudgetStatus(None, None, 0.0, None, 0),
    )
    assert view.can_approve_characters is False
    assert "승인되지 않은" in view.characters_blocked_reason

    for character in ctx.fable.project_characters(project.id):
        ctx.fable.approve_reference(character.id)
    view = build_fable_detail_view(
        project, ctx.fable.project_characters(project.id), [],
        BudgetStatus(None, None, 0.0, None, 0),
    )
    assert view.can_approve_characters is True
    assert view.characters_blocked_reason is None


def test_an_incomplete_sheet_cannot_be_approved_from_the_ui(client) -> None:
    """Mirrors approve_reference's real precondition: approving three of
    four views would let a shot request one that does not exist."""
    from reel_harness.web.fable_view_models import build_character_view

    _, ctx = client
    project = _create(ctx, key="incomplete")
    ctx.fable.adapt_project(project.id)
    character = ctx.fable.project_characters(project.id)[0]
    character.reference_images = {"face": "/tmp/face.png"}
    view = build_character_view(character)
    assert view.sheet_complete is False
    assert view.can_approve is False


def test_only_a_downloaded_take_is_selectable(client) -> None:
    from reel_harness.db.cinematic_models import FableTake
    from reel_harness.web.fable_view_models import build_shot_view

    _, ctx = client
    project = _create(ctx, key="takes")
    ctx.fable.adapt_project(project.id)
    shot = ctx.fable.project_shots(project.id)[0]
    takes = [
        FableTake(shot_id=shot.id, provider="fake", prompt_fingerprint="fp",
                  attempt_number=1, status="SUBMITTED"),
        FableTake(shot_id=shot.id, provider="fake", prompt_fingerprint="fp",
                  attempt_number=2, status="DOWNLOADED"),
    ]
    view = build_shot_view(shot, takes)
    assert [t.can_select for t in view.takes] == [False, True]


def test_budget_view_flags_an_incomplete_spend(client) -> None:
    from reel_harness.core.cost_service import BudgetStatus
    from reel_harness.web.fable_view_models import build_budget_view

    view = build_budget_view(BudgetStatus(10.0, "USD", 4.0, 6.0, 2))
    assert view.spend_is_incomplete is True
    assert build_budget_view(BudgetStatus(10.0, "USD", 4.0, 6.0, 0)).spend_is_incomplete is False


# -- routes --------------------------------------------------------------

def test_list_and_new_pages_render(client) -> None:
    http, ctx = client
    assert http.get("/fable").status_code == 200
    _create(ctx, key="listed")
    listed = http.get("/fable")
    assert "비 오는 밤" in listed.text
    assert http.get("/fable/new").status_code == 200


def test_unknown_project_is_404(client) -> None:
    http, _ = client
    assert http.get("/fable/does-not-exist").status_code == 404


def test_creating_a_project_through_the_form(client) -> None:
    http, ctx = client
    token = _csrf(http)
    response = http.post("/fable", data={
        "csrf_token": token, "title": "새 이야기", "source_text": STORY,
        "language": "ko", "aspect_ratio": "9:16", "takes_per_shot": 2,
    }, follow_redirects=False)
    assert response.status_code == 303
    project_id = response.headers["location"].rsplit("/", 1)[-1]
    assert ctx.fable.get_project(project_id).takes_per_shot == 2


def test_an_invalid_submission_re_renders_the_form_not_a_json_422(client) -> None:
    http, _ = client
    token = _csrf(http)
    response = http.post("/fable", data={
        "csrf_token": token, "title": "", "source_text": "짧다",
        "language": "ko", "aspect_ratio": "9:16", "takes_per_shot": 0,
    })
    assert response.status_code == 422
    assert "제목을 입력해주세요" in response.text
    # The submitted value survives so the user does not retype it.
    assert "짧다" in response.text


def test_every_mutating_route_requires_csrf(client) -> None:
    http, ctx = client
    project = _create(ctx, key="csrf")
    for path, data in (
        ("/fable", {"title": "t"}),
        (f"/fable/{project.id}/adapt", {}),
        (f"/fable/{project.id}/references", {}),
        (f"/fable/{project.id}/approve", {"step": "story"}),
        (f"/fable/{project.id}/budget", {"limit_amount": "5", "currency": "USD"}),
        (f"/fable/{project.id}/render", {}),
        (f"/fable/{project.id}/cancel", {}),
        ("/fable/characters/x/reference", {"project_id": project.id}),
        ("/fable/takes/x/select", {"project_id": project.id}),
    ):
        assert http.post(path, data=data).status_code == 403, path


def test_the_full_gate_walk_by_clicking(client) -> None:
    http, ctx = client
    project = _create(ctx, key="walk")
    token = _csrf(http)

    def post(path, **data):
        return http.post(path, data={"csrf_token": token, **data}, follow_redirects=False)

    assert post(f"/fable/{project.id}/adapt").status_code == 303
    assert ctx.fable.get_project(project.id).status == "STORY_REVIEW"

    post(f"/fable/{project.id}/approve", step="story")
    assert ctx.fable.get_project(project.id).status == "CASTING"

    post(f"/fable/{project.id}/references")
    assert ctx.fable.get_project(project.id).status == "CHARACTER_REVIEW"

    for character in ctx.fable.project_characters(project.id):
        post(f"/fable/characters/{character.id}/reference",
             project_id=project.id, decision="approve")

    post(f"/fable/{project.id}/approve", step="characters")
    assert ctx.fable.get_project(project.id).status == "SHOT_REVIEW"
    post(f"/fable/{project.id}/approve", step="shots")
    assert ctx.fable.get_project(project.id).status == "GENERATING"


def test_a_refused_action_shows_the_reason_rather_than_a_traceback(client) -> None:
    """Refusals here are normal -- a gate not reached, a budget exhausted
    -- and the operator needs to read WHY."""
    http, ctx = client
    project = _create(ctx, key="refused")
    token = _csrf(http)
    response = http.post(
        f"/fable/{project.id}/references", data={"csrf_token": token}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]

    followed = http.get(response.headers["location"])
    assert followed.status_code == 200
    assert "CASTING" in followed.text


def test_setting_and_clearing_a_budget(client) -> None:
    http, ctx = client
    project = _create(ctx, key="budget")
    token = _csrf(http)

    http.post(f"/fable/{project.id}/budget", data={
        "csrf_token": token, "limit_amount": "7.5", "currency": "USD",
    }, follow_redirects=False)
    assert ctx.fable.budget_status(project.id).limit_amount == 7.5

    http.post(f"/fable/{project.id}/budget", data={
        "csrf_token": token, "clear": "true",
    }, follow_redirects=False)
    assert ctx.fable.budget_status(project.id).limit_amount is None


def test_a_malformed_budget_comes_back_as_a_message(client) -> None:
    http, ctx = client
    project = _create(ctx, key="bad-budget")
    token = _csrf(http)
    response = http.post(f"/fable/{project.id}/budget", data={
        "csrf_token": token, "limit_amount": "not-a-number", "currency": "USD",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert ctx.fable.budget_status(project.id).limit_amount is None


def test_the_budget_form_is_disabled_not_removed_when_unavailable(client) -> None:
    """A page that hides a whole form also hides its CSRF field -- the bug
    Phase 5A's own CSRF-fragment mistake established the precedent for."""
    http, ctx = client
    project = _create(ctx, key="terminal-form")
    ctx.fable.cancel_project(project.id)
    page = http.get(f"/fable/{project.id}")
    assert page.status_code == 200
    assert 'name="csrf_token"' in page.text, "a CSRF-carrying element must always exist"
    assert "disabled" in page.text


def test_the_status_fragment_stops_polling_when_a_person_is_needed(client) -> None:
    http, ctx = client
    project = _create(ctx, key="poll")
    ctx.fable.adapt_project(project.id)  # -> STORY_REVIEW, waiting on a person
    fragment = http.get(f"/fable/{project.id}/status")
    assert fragment.status_code == 200
    assert "hx-get" not in fragment.text


def test_the_video_route_404s_before_a_film_exists(client) -> None:
    http, ctx = client
    project = _create(ctx, key="video")
    assert http.get(f"/fable/{project.id}/video").status_code == 404


def test_the_nav_links_to_the_fable_section(client) -> None:
    http, _ = client
    assert 'href="/fable"' in http.get("/fable").text
