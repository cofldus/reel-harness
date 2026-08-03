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


def test_genre_and_tone_are_optional_and_unset_means_none_not_empty_string() -> None:
    """"" is the select's "지정 안 함" option. It must reach the service as
    None -- an empty string would be interpolated into the prompt as a
    deleted word rather than an absent one."""
    result = validate_new_fable_form(
        title="t", source_text=STORY, language="ko", aspect_ratio="9:16", takes_per_shot=0,
        genre="", tone="",
    )
    assert result.ok
    assert result.value.genre is None
    assert result.value.tone is None


def test_genre_and_tone_accept_offered_choices_and_refuse_others() -> None:
    from reel_harness.web.fable_forms import GENRE_CHOICES, TONE_CHOICES

    for value, _label in GENRE_CHOICES:
        assert validate_new_fable_form(
            title="t", source_text=STORY, language="ko", aspect_ratio="9:16",
            takes_per_shot=0, genre=value,
        ).ok, value
    for value, _label in TONE_CHOICES:
        assert validate_new_fable_form(
            title="t", source_text=STORY, language="ko", aspect_ratio="9:16",
            takes_per_shot=0, tone=value,
        ).ok, value

    bad = validate_new_fable_form(
        title="t", source_text=STORY, language="ko", aspect_ratio="9:16",
        takes_per_shot=0, genre="comdey",
    )
    assert not bad.ok
    assert "genre" in bad.errors


def test_duration_choices_are_reachable_shot_counts_not_arbitrary_numbers() -> None:
    """Reference-driven shots are fixed at 8s and a plan caps at 15 shots,
    so every offered duration must be a multiple of 8 within that cap --
    otherwise the UI promises a length no plan can actually hit."""
    from reel_harness.web.fable_forms import DURATION_CHOICES

    for seconds, _label in DURATION_CHOICES:
        assert seconds % 8 == 0, seconds
        assert seconds // 8 <= 15, seconds
        assert validate_new_fable_form(
            title="t", source_text=STORY, language="ko", aspect_ratio="9:16",
            takes_per_shot=0, target_duration_sec=seconds,
        ).ok


def test_an_unoffered_duration_is_refused() -> None:
    result = validate_new_fable_form(
        title="t", source_text=STORY, language="ko", aspect_ratio="9:16",
        takes_per_shot=0, target_duration_sec=45,
    )
    assert not result.ok
    assert "target_duration_sec" in result.errors


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
        "genre": "drama", "tone": "quiet tension", "target_duration_sec": 48,
    }, follow_redirects=False)
    assert response.status_code == 303
    project_id = response.headers["location"].rsplit("/", 1)[-1]
    project = ctx.fable.get_project(project_id)
    assert project.takes_per_shot == 2
    # The selected hints reach the project, not just the form.
    assert project.genre == "drama"
    assert project.tone == "quiet tension"
    assert project.target_duration_sec == 48


def test_the_new_project_form_offers_every_choice_it_validates(client) -> None:
    """The rendered page and the validator must agree -- a select that
    offers a value the validator rejects is a dead end the user cannot
    escape without knowing why."""
    from reel_harness.web.fable_forms import DURATION_CHOICES, GENRE_CHOICES, TONE_CHOICES

    http, _ = client
    page = http.get("/fable/new").text
    for value, _label in GENRE_CHOICES:
        if value:
            assert f'value="{value}"' in page, value
    for value, _label in TONE_CHOICES:
        if value:
            assert f'value="{value}"' in page, value
    for seconds, _label in DURATION_CHOICES:
        assert f'value="{seconds}"' in page, seconds


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


# -- reference stills and casting an existing actor ------------------------


def _casting_project(ctx, key: str):
    """A project sitting at the casting gate with a generated sheet."""
    project = _create(ctx, key=key)
    ctx.fable.adapt_project(project.id)
    ctx.fable.approve_story(project.id)
    ctx.fable.generate_references(project.id)
    return project


def test_the_casting_page_shows_the_face_not_just_a_yes_no(client) -> None:
    """Approving a face you cannot see is not a review. Until the image
    route existed, this page listed only 있음/없음."""
    http, ctx = client
    project = _casting_project(ctx, "images")
    character = ctx.fable.project_characters(project.id)[0]

    page = http.get(f"/fable/{project.id}").text
    assert f"/fable/{project.id}/characters/{character.id}/reference/face" in page
    assert "<img" in page

    image = http.get(f"/fable/{project.id}/characters/{character.id}/reference/face")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_an_ungenerated_view_is_404_not_a_broken_image(client) -> None:
    http, ctx = client
    project = _create(ctx, key="no-sheet")
    ctx.fable.adapt_project(project.id)
    character = ctx.fable.project_characters(project.id)[0]
    response = http.get(f"/fable/{project.id}/characters/{character.id}/reference/face")
    assert response.status_code == 404


def test_a_reference_path_cannot_escape_the_storage_root(client, tmp_path) -> None:
    """The stored path is data. If it ever pointed outside the Fable root,
    serving it would hand out an arbitrary file."""
    http, ctx = client
    project = _casting_project(ctx, "escape")
    character = ctx.fable.project_characters(project.id)[0]

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nnot yours")
    from reel_harness.db.cinematic_models import FableCharacter

    with ctx.session_factory() as session:
        row = session.get(FableCharacter, character.id)
        row.reference_images = {**row.reference_images, "face": str(outside)}
        session.commit()

    response = http.get(f"/fable/{project.id}/characters/{character.id}/reference/face")
    assert response.status_code == 404


def test_reusable_actors_are_offered_only_at_the_casting_gate(client) -> None:
    http, ctx = client
    source = _casting_project(ctx, "reuse-source")
    for character in ctx.fable.project_characters(source.id):
        ctx.fable.approve_reference(character.id)

    target = _create(ctx, key="reuse-target")
    ctx.fable.adapt_project(target.id)
    # STORY_REVIEW: not casting yet, so nothing is offered.
    assert "배우 다시 쓰기" not in http.get(f"/fable/{target.id}").text

    ctx.fable.approve_story(target.id)  # -> CASTING
    page = http.get(f"/fable/{target.id}").text
    assert "배우 다시 쓰기" in page
    assert "이 배우로" in page


def test_casting_an_existing_actor_through_the_form(client) -> None:
    http, ctx = client
    source = _casting_project(ctx, "cast-source")
    for character in ctx.fable.project_characters(source.id):
        ctx.fable.approve_reference(character.id)
    source_character = ctx.fable.project_characters(source.id)[0]

    target = _create(ctx, key="cast-target")
    ctx.fable.adapt_project(target.id)
    ctx.fable.approve_story(target.id)

    token = _csrf(http)
    response = http.post(f"/fable/{target.id}/cast", data={
        "csrf_token": token, "source_character_id": source_character.id,
    }, follow_redirects=False)
    assert response.status_code == 303

    cast = ctx.fable.project_characters(target.id)[0]
    assert cast.reference_images == source_character.reference_images
    assert cast.reference_approved is False  # this film approves for itself


def test_casting_outside_the_gate_is_a_409_not_a_500(client) -> None:
    http, ctx = client
    source = _casting_project(ctx, "cast-409-source")
    for character in ctx.fable.project_characters(source.id):
        ctx.fable.approve_reference(character.id)
    source_character = ctx.fable.project_characters(source.id)[0]

    target = _create(ctx, key="cast-409-target")
    ctx.fable.adapt_project(target.id)  # STORY_REVIEW, not CASTING

    token = _csrf(http)
    response = http.post(f"/fable/{target.id}/cast", data={
        "csrf_token": token, "source_character_id": source_character.id,
    })
    assert response.status_code == 409


def test_a_reused_actors_stills_are_still_served_from_the_new_project(client) -> None:
    """Reuse shares the stills by path, so they live in the project that
    first cast them. The new project's page must still be able to show
    them or the reuse UI would display broken images."""
    http, ctx = client
    source = _casting_project(ctx, "serve-source")
    for character in ctx.fable.project_characters(source.id):
        ctx.fable.approve_reference(character.id)
    source_character = ctx.fable.project_characters(source.id)[0]

    target = _create(ctx, key="serve-target")
    ctx.fable.adapt_project(target.id)
    ctx.fable.approve_story(target.id)
    cast = ctx.fable.reuse_character(target.id, source_character.id)

    response = http.get(f"/fable/{target.id}/characters/{cast.id}/reference/face")
    assert response.status_code == 200
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


# -- the page has to be usable, not just correct --------------------------


def test_every_class_the_fable_templates_use_is_actually_defined() -> None:
    """Dead class names are invisible bugs: the page renders, nothing is
    styled, and no test notices. The cancel button spent a whole phase
    written as `.button-danger` against a stylesheet defining
    `.btn-danger`, so the destructive action looked identical to the safe
    ones."""
    import re
    from pathlib import Path

    web = Path(__file__).resolve().parents[2] / "reel_harness" / "web"
    css = (web / "static" / "app.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))

    used: set[str] = set()
    for template in (web / "templates").glob("fable*.html"):
        for attr in re.findall(r'class="([^"{}]*)"', template.read_text(encoding="utf-8")):
            used.update(attr.split())

    missing = sorted(used - defined)
    assert not missing, f"templates use undefined CSS classes: {missing}"


def test_the_money_button_states_the_cost(client) -> None:
    """A button that spends real money must not look like one that does
    not, and the amount belongs WITH the action rather than in a section
    the user scrolls past."""
    http, ctx = client
    project = _create(ctx, key="spend")
    ctx.fable.adapt_project(project.id)
    ctx.fable.approve_story(project.id)  # -> CASTING, references cost money

    page = http.get(f"/fable/{project.id}").text
    assert "유료 생성" in page
    assert "배우 이미지 생성" in page
    # Either a real amount or an explicit "unknown" -- never silence.
    assert ("약 " in page) or ("예상 비용 알 수 없음" in page)


def test_a_project_with_no_budget_says_generation_is_locked(client) -> None:
    http, ctx = client
    project = _create(ctx, key="locked")
    ctx.fable.adapt_project(project.id)
    ctx.fable.approve_story(project.id)
    page = http.get(f"/fable/{project.id}").text
    assert "예산 한도를 설정하기 전에는" in page


def test_the_pipeline_shows_where_the_project_is(client) -> None:
    from reel_harness.web.fable_view_models import build_pipeline_steps

    http, ctx = client
    project = _create(ctx, key="pipeline")
    ctx.fable.adapt_project(project.id)  # -> STORY_REVIEW

    steps = build_pipeline_steps("STORY_REVIEW")
    assert [s.state for s in steps][:3] == ["done", "current", "todo"]
    assert 'data-state="current"' in http.get(f"/fable/{project.id}").text


def test_a_completed_project_has_no_current_step() -> None:
    from reel_harness.web.fable_view_models import build_pipeline_steps

    states = [s.state for s in build_pipeline_steps("COMPLETED")]
    assert set(states) == {"done"}


def test_a_cancelled_project_is_marked_blocked_not_progressing() -> None:
    from reel_harness.web.fable_view_models import build_pipeline_steps

    states = [s.state for s in build_pipeline_steps("CANCELLED")]
    assert "blocked" in states
    assert "current" not in states


def test_an_empty_casting_section_explains_itself(client) -> None:
    """A section that vanishes when empty leaves no way to tell whether
    the step has not happened yet or has failed."""
    http, ctx = client
    project = _create(ctx, key="empty-cast")
    page = http.get(f"/fable/{project.id}").text
    assert "캐스팅" in page
    assert "각색을 실행하면" in page


# -- source refinement ---------------------------------------------------

def _refine_client(monkeypatch, tmp_path, *, allow_paid: bool):
    from reel_harness.bootstrap import AppContext
    from reel_harness.config import Settings

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'refine.db').as_posix()}",
        jobs_dir=tmp_path / "jobs",
        fable_projects_dir=tmp_path / "fable_projects",
        credential_dir=tmp_path / "secrets",
        narrative_provider="fake",
        allow_paid_generation=allow_paid,
    )
    ctx = AppContext(settings)
    monkeypatch.setattr(api_app, "_ctx", ctx)
    return TestClient(api_app.app), ctx


def _post_refine(http, **fields):
    http.get("/fable/new")
    token = http.cookies.get(CSRF_COOKIE_NAME)
    return http.post("/fable/refine", data={"csrf_token": token, **fields})


def test_refine_proposes_without_touching_what_the_user_wrote(monkeypatch, tmp_path) -> None:
    """The whole safety property of this feature: a proposal is shown, and
    the story field still holds the user's own words until they accept."""
    http, _ctx = _refine_client(monkeypatch, tmp_path, allow_paid=True)
    mine = "그는 오래 후회했다. 그리고 아무 말도 하지 않았다."
    page = _post_refine(http, source_text=mine, title="t", decision="refine").text

    assert "AI 보정 제안" in page
    # The textarea still contains the original, verbatim.
    assert mine in page.split("<textarea", 1)[1].split("</textarea>", 1)[0]


def test_refine_is_refused_when_paid_generation_is_off(monkeypatch, tmp_path) -> None:
    """It is a real LLM call, so it obeys the same money switch as every
    other paid call rather than being waved through as a convenience."""
    http, _ctx = _refine_client(monkeypatch, tmp_path, allow_paid=False)
    page = _post_refine(http, source_text="짧은 이야기입니다.", decision="refine").text
    assert "유료" in page and "AI 보정 제안" not in page


def test_accepting_a_proposal_puts_it_in_the_story_field(monkeypatch, tmp_path) -> None:
    http, _ctx = _refine_client(monkeypatch, tmp_path, allow_paid=True)
    page = _post_refine(
        http, source_text="원래 글", proposal="보정된 글입니다", decision="accept",
    ).text
    assert "보정된 글입니다" in page.split("<textarea", 1)[1].split("</textarea>", 1)[0]


def test_discarding_a_proposal_keeps_the_original(monkeypatch, tmp_path) -> None:
    http, _ctx = _refine_client(monkeypatch, tmp_path, allow_paid=True)
    page = _post_refine(
        http, source_text="원래 글", proposal="보정된 글입니다", decision="discard",
    ).text
    body = page.split("<textarea", 1)[1].split("</textarea>", 1)[0]
    assert "원래 글" in body and "보정된 글입니다" not in body


def test_refine_keeps_the_rest_of_the_form(monkeypatch, tmp_path) -> None:
    """A half-filled form that loses its other fields on a helper action is
    worse than no helper at all."""
    http, _ctx = _refine_client(monkeypatch, tmp_path, allow_paid=True)
    page = _post_refine(
        http, source_text="이야기", title="마지막 승객", genre="mystery",
        target_duration_sec=48, decision="refine",
    ).text
    assert 'value="마지막 승객"' in page
    # Genre is a radio chip group now, not a <select>: a native select's
    # popup list is OS-drawn and unstyleable.
    assert 'value="mystery"' in page
    body = page.split('name="genre" value="mystery"', 1)[1][:60]
    assert "checked" in body


def test_no_template_uses_an_inline_style_attribute() -> None:
    """The app sends `style-src 'self'` with no 'unsafe-inline', so a
    style="" attribute is silently dropped by the browser.

    This is not theoretical: the project progress bars and the budget
    meter were written with inline widths and had never rendered their
    value at all -- a spending indicator that always read empty. The CSP
    is right, so the rule is that presentation lives in the stylesheet.
    """
    import pathlib

    templates = pathlib.Path("reel_harness/web/templates")
    offenders = [
        f"{path.relative_to(templates)}:{number}"
        for path in templates.rglob("*.html")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if 'style="' in line
    ]
    assert not offenders, f"inline styles are dead under this CSP: {offenders}"


def test_the_csp_still_forbids_inline_styles() -> None:
    """Guards the assumption the test above rests on. If someone ever adds
    'unsafe-inline' this should be a deliberate, visible decision."""
    from fastapi.testclient import TestClient

    from reel_harness.api.app import app

    policy = TestClient(app).get("/fable").headers["content-security-policy"]
    assert "style-src 'self'" in policy
    assert "unsafe-inline" not in policy
