"""/v1/fable/* routes.

The layer contract these tests hold: the API adds NO domain logic and NO
new permission. Every route calls the same FableService method the CLI
does, every gate that refuses the CLI refuses the API, and the error
contract is uniform -- 404 for something that does not exist, 409 for an
action that is not valid right now, 422 for a malformed request.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from reel_harness.api import app as api_app

STORY = "그날 밤, 그는 창밖을 바라보았다."


@pytest.fixture
def client(monkeypatch, tmp_path):
    from reel_harness.bootstrap import AppContext
    from reel_harness.config import Settings

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}",
        jobs_dir=tmp_path / "jobs",
        fable_projects_dir=tmp_path / "fable_projects",
        credential_dir=tmp_path / "secrets",
        app_api_key="test-key-long-enough-for-checks",
    )
    ctx = AppContext(settings)
    monkeypatch.setattr(api_app, "_ctx", ctx)
    return TestClient(api_app.app), ctx


def _auth():
    return {"Authorization": "Bearer test-key-long-enough-for-checks"}


def _create(client, key="api-1", **kwargs):
    payload = {
        "title": "비 오는 밤", "source_text": STORY, "idempotency_key": key, **kwargs,
    }
    return client.post("/v1/fable/projects", json=payload, headers=_auth())


# -- authentication ------------------------------------------------------

def test_every_fable_route_requires_the_api_key(client) -> None:
    http, _ = client
    assert http.get("/v1/fable/projects").status_code == 401
    assert http.post("/v1/fable/projects", json={}).status_code == 401


# -- creation and reads --------------------------------------------------

def test_create_and_read_a_project(client) -> None:
    http, _ = client
    response = _create(http)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["idempotent_replay"] is False

    fetched = http.get(f"/v1/fable/projects/{body['project_id']}", headers=_auth())
    assert fetched.status_code == 200
    assert fetched.json()["project_id"] == body["project_id"]


def test_creation_is_idempotent(client) -> None:
    http, _ = client
    first = _create(http, key="same").json()
    second = _create(http, key="same").json()
    assert first["project_id"] == second["project_id"]
    assert second["idempotent_replay"] is True


def test_an_invalid_creation_is_422_not_500(client) -> None:
    http, _ = client
    response = _create(http, key="bad", takes_per_shot=3)
    assert response.status_code == 422
    assert "takes_per_shot" in response.json()["detail"]


def test_unknown_project_is_404(client) -> None:
    http, _ = client
    assert http.get("/v1/fable/projects/does-not-exist", headers=_auth()).status_code == 404
    assert http.get("/v1/fable/projects/nope/shots", headers=_auth()).status_code == 404


def test_list_projects(client) -> None:
    http, _ = client
    _create(http, key="a")
    _create(http, key="b")
    body = http.get("/v1/fable/projects", headers=_auth()).json()
    assert len(body) == 2


# -- the gate walk -------------------------------------------------------

def test_the_full_gate_walk_through_the_api(client) -> None:
    """Every gate crossed over HTTP, in the same order and with the same
    refusals the CLI enforces."""
    http, _ = client
    project_id = _create(http, key="walk").json()["project_id"]

    assert http.post(f"/v1/fable/projects/{project_id}/adapt", headers=_auth()).json()["status"] == (
        "STORY_REVIEW"
    )

    approve = http.post(
        f"/v1/fable/projects/{project_id}/approve", json={"step": "story"}, headers=_auth(),
    )
    assert approve.json()["status"] == "CASTING", "casting is a real stop"

    references = http.post(f"/v1/fable/projects/{project_id}/references", headers=_auth())
    assert references.json()["status"] == "CHARACTER_REVIEW"

    characters = http.get(f"/v1/fable/projects/{project_id}/characters", headers=_auth()).json()
    assert characters
    assert all(c["reference_approved"] is False for c in characters)

    # The character gate refuses until every sheet is approved.
    refused = http.post(
        f"/v1/fable/projects/{project_id}/approve", json={"step": "characters"}, headers=_auth(),
    )
    assert refused.status_code == 409
    assert "no approved reference sheet" in refused.json()["detail"]

    for character in characters:
        approved = http.post(
            f"/v1/fable/characters/{character['character_id']}/approve", headers=_auth(),
        )
        assert approved.json()["reference_approved"] is True

    assert http.post(
        f"/v1/fable/projects/{project_id}/approve", json={"step": "characters"}, headers=_auth(),
    ).json()["status"] == "SHOT_REVIEW"
    assert http.post(
        f"/v1/fable/projects/{project_id}/approve", json={"step": "shots"}, headers=_auth(),
    ).json()["status"] == "GENERATING"


def test_approving_an_unknown_step_is_422(client) -> None:
    http, _ = client
    project_id = _create(http, key="step").json()["project_id"]
    response = http.post(
        f"/v1/fable/projects/{project_id}/approve", json={"step": "whatever"}, headers=_auth(),
    )
    assert response.status_code == 422
    assert "unknown step" in response.json()["detail"]


def test_approving_out_of_order_is_409_not_a_crash(client) -> None:
    """An invalid state transition is a refusal, never a 500."""
    http, _ = client
    project_id = _create(http, key="order").json()["project_id"]
    response = http.post(
        f"/v1/fable/projects/{project_id}/approve", json={"step": "shots"}, headers=_auth(),
    )
    assert response.status_code == 409


def test_generating_references_outside_casting_is_409(client) -> None:
    http, _ = client
    project_id = _create(http, key="cast-order").json()["project_id"]
    response = http.post(f"/v1/fable/projects/{project_id}/references", headers=_auth())
    assert response.status_code == 409


def test_rejecting_a_reference_clears_the_approval(client) -> None:
    http, _ = client
    project_id = _create(http, key="reject").json()["project_id"]
    http.post(f"/v1/fable/projects/{project_id}/adapt", headers=_auth())
    http.post(f"/v1/fable/projects/{project_id}/approve", json={"step": "story"}, headers=_auth())
    http.post(f"/v1/fable/projects/{project_id}/references", headers=_auth())
    character_id = http.get(
        f"/v1/fable/projects/{project_id}/characters", headers=_auth(),
    ).json()[0]["character_id"]

    http.post(f"/v1/fable/characters/{character_id}/approve", headers=_auth())
    rejected = http.post(f"/v1/fable/characters/{character_id}/reject", headers=_auth())
    assert rejected.json()["reference_approved"] is False


def test_unknown_character_is_404(client) -> None:
    http, _ = client
    assert http.post("/v1/fable/characters/nope/approve", headers=_auth()).status_code == 404


# -- budget --------------------------------------------------------------

def test_budget_round_trip(client) -> None:
    http, _ = client
    project_id = _create(http, key="budget").json()["project_id"]

    empty = http.get(f"/v1/fable/projects/{project_id}/budget", headers=_auth()).json()
    assert empty["limit_amount"] is None
    assert empty["spent_amount"] == 0.0

    set_response = http.put(
        f"/v1/fable/projects/{project_id}/budget",
        json={"limit_amount": 5.0, "currency": "FAKE"}, headers=_auth(),
    )
    assert set_response.status_code == 200
    assert set_response.json()["remaining_amount"] == 5.0

    # Clearing re-closes the paid gate and un-spends nothing.
    cleared = http.put(
        f"/v1/fable/projects/{project_id}/budget", json={"limit_amount": None}, headers=_auth(),
    )
    assert cleared.json()["limit_amount"] is None


def test_an_invalid_budget_is_409(client) -> None:
    http, _ = client
    project_id = _create(http, key="bad-budget").json()["project_id"]
    response = http.put(
        f"/v1/fable/projects/{project_id}/budget", json={"limit_amount": 5.0}, headers=_auth(),
    )
    assert response.status_code == 409
    assert "currency" in response.json()["detail"]


def test_estimate_is_read_only(client) -> None:
    http, _ = client
    project_id = _create(http, key="estimate").json()["project_id"]
    http.post(f"/v1/fable/projects/{project_id}/adapt", headers=_auth())

    estimate = http.get(f"/v1/fable/projects/{project_id}/estimate", headers=_auth()).json()
    assert estimate["known"] is True
    assert estimate["shot_count"] > 0
    assert http.get(f"/v1/fable/projects/{project_id}/budget", headers=_auth()).json()[
        "spent_amount"
    ] == 0.0


# -- shots and takes -----------------------------------------------------

def test_shots_expose_their_takes(client) -> None:
    http, _ = client
    project_id = _create(http, key="shots").json()["project_id"]
    http.post(f"/v1/fable/projects/{project_id}/adapt", headers=_auth())

    shots = http.get(f"/v1/fable/projects/{project_id}/shots", headers=_auth()).json()
    assert shots
    assert all(shot["status"] == "PLANNED" for shot in shots)
    assert all(shot["takes"] == [] for shot in shots)


def test_selecting_an_unknown_take_is_404(client) -> None:
    http, _ = client
    assert http.post("/v1/fable/takes/nope/select", headers=_auth()).status_code == 404


def test_rendering_before_editing_is_409(client) -> None:
    http, _ = client
    project_id = _create(http, key="render").json()["project_id"]
    response = http.post(f"/v1/fable/projects/{project_id}/render", headers=_auth())
    assert response.status_code == 409


def test_cancel_from_draft(client) -> None:
    http, _ = client
    project_id = _create(http, key="cancel").json()["project_id"]
    response = http.post(f"/v1/fable/projects/{project_id}/cancel", headers=_auth())
    assert response.json()["status"] == "CANCELLED"


# -- response shape ------------------------------------------------------

def test_responses_never_echo_the_source_text_or_a_local_path(client) -> None:
    """A column added to StoryProject must not leak through the API by
    accident -- the response models are explicit for exactly this reason."""
    http, _ = client
    project_id = _create(http, key="shape").json()["project_id"]
    http.post(f"/v1/fable/projects/{project_id}/adapt", headers=_auth())
    body = http.get(f"/v1/fable/projects/{project_id}", headers=_auth()).json()
    assert set(body) == {
        "project_id", "title", "status", "aspect_ratio", "takes_per_shot",
        "idempotent_replay", "failure_code", "failure_summary",
    }
    assert STORY not in str(body)
