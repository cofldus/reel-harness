"""publisher.oauth_flow_store.OAuthFlowStore: transient, single-use OAuth
connect-flow state bridging POST /connect and GET /callback. No network."""
from __future__ import annotations

from reel_harness.publisher.oauth_flow_store import OAuthFlowStore
from reel_harness.publisher.secret_store import FileSecretStore


def _store(tmp_path) -> OAuthFlowStore:
    secret_store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    return OAuthFlowStore(secret_store)


def test_create_then_pop_round_trips(tmp_path) -> None:
    store = _store(tmp_path)
    state = store.create("youtube", "default", "verifier-value")
    result = store.pop(state)
    assert result is not None
    assert result["provider"] == "youtube"
    assert result["account_reference"] == "default"
    assert result["verifier"] == "verifier-value"


def test_pop_is_single_use(tmp_path) -> None:
    store = _store(tmp_path)
    state = store.create("youtube", "default", "verifier-value")
    assert store.pop(state) is not None
    assert store.pop(state) is None


def test_pop_unknown_state_returns_none(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.pop("never-created") is None


def test_expired_entry_is_rejected(tmp_path) -> None:
    store = OAuthFlowStore(
        FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo"), ttl_seconds=-1,
    )
    state = store.create("tiktok", "default", "verifier-value")
    assert store.pop(state) is None


def test_concurrent_flows_for_the_same_account_do_not_collide(tmp_path) -> None:
    store = _store(tmp_path)
    state_a = store.create("youtube", "default", "verifier-a")
    state_b = store.create("youtube", "default", "verifier-b")
    assert state_a != state_b

    result_a = store.pop(state_a)
    result_b = store.pop(state_b)
    assert result_a["verifier"] == "verifier-a"
    assert result_b["verifier"] == "verifier-b"


def test_states_are_high_entropy_and_unique(tmp_path) -> None:
    store = _store(tmp_path)
    states = {store.create("youtube", "default", f"v{i}") for i in range(20)}
    assert len(states) == 20
    assert all(len(s) >= 24 for s in states)
