from copy import deepcopy

import pytest

from shopping_advisor.actions import Actions
from shopping_advisor.schemas import (
    Command,
    DomainError,
    MessageCommand,
    PreferencesCommand,
    ProposalCommand,
    VersionedCommand,
)
from shopping_advisor.storage import Store

PREFS = {"budget_cents": 600000, "use": "coding", "priority": "value", "min_ram_gb": 16,
         "min_storage_gb": 512, "max_weight_g": None, "min_battery_minutes": None,
         "needs_discrete_gpu": False, "os": "any"}


def setup(tmp_path):
    store, tokens = Store.initialize(tmp_path / "data", now=1900000000)
    people = {key: store.authenticate(value) for key, value in tokens.items()}
    actions = Actions(store)
    sess = actions.create_session(people["alice"], Command(action_id="session"))
    return store, people, actions, sess


def test_model_proposal_does_not_change_preferences(tmp_path):
    store, people, actions, sess = setup(tmp_path)
    with store.transaction() as db:
        proposal = actions.propose_preferences(db, people["alice"], sess["id"], PREFS)
    view = actions.view(people["alice"], sess["id"])
    assert view["session"]["payload"]["preferences"] is None
    assert proposal["payload"]["state"] == "pending_confirmation"
    cmd = ProposalCommand(action_id="confirm", expected_version=proposal["version"],
                          content_hash=proposal["content_hash"], decision="confirm")
    result = actions.decide_proposal(people["alice"], proposal["id"], cmd)
    assert result["session"]["payload"]["preferences"] == PREFS
    assert result["session"]["payload"]["preferences_revision"] == 1
    assert actions.decide_proposal(people["alice"], proposal["id"], cmd) == result
    assert len(actions.view(people["alice"], sess["id"])["runs"]) == 0
    with pytest.raises(DomainError):
        actions.decide_proposal(people["alice"], proposal["id"],
                                cmd.model_copy(update={"action_id": "again"}))


@pytest.mark.parametrize("overrides", [{"expected_version": 2}, {"content_hash": "0" * 64}])
def test_exact_confirmation_rejects_changed_version_or_hash(tmp_path, overrides):
    store, people, actions, sess = setup(tmp_path)
    with store.transaction() as db:
        proposal = actions.propose_preferences(db, people["alice"], sess["id"], PREFS)
    args = {"action_id": "confirm", "expected_version": 1,
            "content_hash": proposal["content_hash"], "decision": "confirm", **overrides}
    with pytest.raises(DomainError):
        actions.decide_proposal(people["alice"], proposal["id"], ProposalCommand(**args))
    assert actions.view(people["alice"], sess["id"])["session"]["payload"]["preferences"] is None


def test_new_message_supersedes_draft_and_assent_is_not_confirmation(tmp_path):
    store, people, actions, sess = setup(tmp_path)
    with store.transaction() as db:
        proposal = actions.propose_preferences(db, people["alice"], sess["id"], PREFS)
    actions.message(people["alice"], sess["id"], MessageCommand(
        action_id="yes", expected_version=sess["version"], text="好的"))
    view = actions.view(people["alice"], sess["id"])
    assert view["session"]["payload"]["preferences"] is None
    assert view["proposals"][0]["payload"]["state"] == "superseded"
    with pytest.raises(DomainError):
        actions.decide_proposal(people["alice"], proposal["id"], ProposalCommand(
            action_id="late", expected_version=1, content_hash=proposal["content_hash"], decision="confirm"))


def test_customer_edit_cancels_queued_run_but_does_not_reset_usage(tmp_path):
    store, people, actions, sess = setup(tmp_path)
    result = actions.message(people["alice"], sess["id"], MessageCommand(
        action_id="msg", expected_version=1, text="6000元编程电脑"))
    changed = actions.set_preferences(people["alice"], sess["id"], PreferencesCommand(
        action_id="edit", expected_version=result["session"]["version"], preferences=PREFS))
    assert changed["payload"]["control_epoch"] == 2
    assert changed["payload"]["active_run"] is None
    with store.transaction() as db:
        assert store.get(db, "run", result["run"]["id"])["payload"]["state"] == "cancelled"
    assert changed["payload"]["tool_count"] == 0


def test_cross_customer_and_operator_cannot_edit_or_read_session(tmp_path):
    _, people, actions, sess = setup(tmp_path)
    for actor in (people["bob"], people["operator"]):
        with pytest.raises(DomainError):
            actions.view(actor, sess["id"])
        with pytest.raises(DomainError):
            actions.set_preferences(actor, sess["id"], PreferencesCommand(
                action_id="edit", expected_version=1, preferences=PREFS))


def test_idempotency_same_action_changed_request_conflicts(tmp_path):
    _, people, actions, sess = setup(tmp_path)
    cmd = PreferencesCommand(action_id="edit", expected_version=1, preferences=PREFS)
    original = actions.set_preferences(people["alice"], sess["id"], cmd)
    assert actions.set_preferences(people["alice"], sess["id"], cmd) == original
    with pytest.raises(DomainError, match="不同请求"):
        actions.set_preferences(people["alice"], sess["id"], PreferencesCommand(
            action_id="edit", expected_version=1, preferences={**PREFS, "budget_cents": 700000}))


def test_read_only_views_do_not_write_or_change_business_state(tmp_path, monkeypatch):
    monkeypatch.setattr("shopping_advisor.storage.time.time", lambda: 1900000000)
    store, people, actions, sess = setup(tmp_path)
    original = actions.view(people["alice"], sess["id"])
    original.pop("now")
    with store.transaction() as db:
        before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                  for table in ("objects", "receipts", "audit", "calls")}
    for elapsed in range(1, 4):
        monkeypatch.setattr("shopping_advisor.storage.time.time", lambda elapsed=elapsed: 1900000000 + elapsed)
        view = actions.view(people["alice"], sess["id"])
        assert view.pop("now") == 1900000000 + elapsed
        assert view == deepcopy(original)
    with store.transaction() as db:
        assert before == {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                          for table in before}


def test_expired_draft_does_not_become_confirmed(tmp_path):
    store, people, actions, sess = setup(tmp_path)
    with store.transaction() as db:
        proposal = actions.propose_preferences(db, people["alice"], sess["id"], PREFS)
        db.execute("UPDATE settings SET value=CAST(value AS INTEGER)+900 WHERE key='now'")
    with pytest.raises(DomainError, match="过期"):
        actions.decide_proposal(people["alice"], proposal["id"], ProposalCommand(
            action_id="confirm", expected_version=1, content_hash=proposal["content_hash"], decision="confirm"))


def test_unknown_call_blocks_new_message_even_after_cancel(tmp_path):
    store, people, actions, sess = setup(tmp_path)
    result = actions.message(people["alice"], sess["id"], MessageCommand(
        action_id="msg", expected_version=1, text="选电脑"))
    with store.transaction() as db:
        db.execute("INSERT INTO calls(id,run_id,owner,state,usage_kind,context) VALUES(?,?,?,?,?,?)",
                   ("call-lost", result["run"]["id"], "alice", "started", "real", "{}"))
    cancelled = actions.cancel(people["alice"], sess["id"], VersionedCommand(
        action_id="cancel", expected_version=result["session"]["version"]))
    with pytest.raises(DomainError, match="在途"):
        actions.message(people["alice"], sess["id"], MessageCommand(
            action_id="again", expected_version=cancelled["version"], text="继续"))
