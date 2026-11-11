"""Recovery is accounting and freezing, never replay or implicit resumption."""

import importlib
import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from shopping_advisor.actions import Actions
from shopping_advisor.schemas import Command, DomainError, MessageCommand, ReconcileCommand
from shopping_advisor.storage import Store


def runtime():
    for name in ("provider", "agent", "service"):
        assert importlib.util.find_spec("shopping_advisor." + name) is not None, f"Missing runtime {name}"
    return tuple(importlib.import_module("shopping_advisor." + name) for name in ("provider", "agent", "service"))


@pytest.fixture
def setup(tmp_path):
    store, tokens = Store.initialize(tmp_path / "data", now=1900000000)
    people = {name: store.authenticate(token) for name, token in tokens.items()}
    actions = Actions(store)
    sess = actions.create_session(people["alice"], Command(action_id="session"))
    return store, people, actions, sess


def queued(setup):
    _, people, actions, sess = setup
    return actions.message(people["alice"], sess["id"], MessageCommand(
        action_id="message", expected_version=sess["version"], text="帮我选笔记本"))["run"]


def lost_call(store, run, *, call_id="call-lost", state="started", tokens=None):
    with store.transaction() as db:
        db.execute("INSERT INTO calls(id,run_id,owner,state,usage_kind,context,tokens) VALUES(?,?,?,?,?,?,?)",
                   (call_id, run["id"], run["owner"], state, "real", "{}", tokens))


@pytest.mark.parametrize("state", ["queued", "running"])
def test_recovery_interrupts_live_runs_without_replaying_pending_action(setup, state):
    provider_module, agent, _ = runtime()
    store, people, actions, sess = setup
    run = queued(setup)
    with store.transaction() as db:
        store.save(db, run, {**run["payload"], "state": state,
                             "pending_action": {"tool": "read_context", "arguments": {}}})
    provider = provider_module.ScriptedProvider([])
    agent.Runner(store, provider).recover()
    view = actions.view(people["alice"], sess["id"])
    assert view["runs"][0]["payload"]["state"] == "interrupted"
    assert view["runs"][0]["payload"]["pending_action"] is None
    assert view["session"]["payload"]["active_run"] is None and view["evidences"] == []
    assert provider.seen_contexts == []


def test_recovery_and_idempotent_operator_reconciliation_never_resume(setup):
    provider_module, agent, _ = runtime()
    store, people, actions, sess = setup
    run = queued(setup)
    lost_call(store, run)
    provider = provider_module.ScriptedProvider([])
    runner = agent.Runner(store, provider)
    runner.recover()
    assert actions.view(people["alice"], sess["id"])["session"]["payload"]["blocked_unknown"] is True
    command = ReconcileCommand(action_id="reconcile", call_id="call-lost", tokens=410,
                               note="合成测试账本记录，无可用结果", outcome="no_usable_result")
    with pytest.raises(DomainError):
        runner.reconcile(people["alice"], command)
    receipt = runner.reconcile(people["operator"], command)
    assert runner.reconcile(people["operator"], command) == receipt and receipt["resumed"] is False
    assert provider.seen_contexts == []
    metrics = runner.metrics(people["alice"], sess["id"])
    assert (metrics["real_tokens"], metrics["model_calls"], metrics["unknown_calls"]) == (410, 1, 0)
    view = actions.view(people["alice"], sess["id"])
    assert view["session"]["payload"]["blocked_unknown"] is False
    assert view["session"]["payload"]["active_run"] is None and view["evidences"] == []
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE action='reconcile'").fetchone()[0] >= 1


@pytest.mark.parametrize("replacement", [None, 409])
def test_reconcile_requires_actual_usage_and_preserves_known_tokens(setup, replacement):
    provider_module, agent, _ = runtime()
    store, people, actions, sess = setup
    run = queued(setup)
    lost_call(store, run, tokens=410)
    runner = agent.Runner(store, provider_module.ScriptedProvider([]))
    runner.recover()
    command = ReconcileCommand(action_id="reconcile", call_id="call-lost", tokens=replacement,
                               note="测试不能覆盖用量", outcome="no_usable_result")
    with pytest.raises(DomainError) as raised:
        runner.reconcile(people["operator"], command)
    assert raised.value.code == ("usage_required" if replacement is None else "known_usage")
    assert runner.metrics(people["alice"], sess["id"])["real_tokens"] == 410
    assert actions.view(people["alice"], sess["id"])["session"]["payload"]["blocked_unknown"] is True


def test_all_unknown_calls_must_be_reconciled_before_unblocking(setup):
    provider_module, agent, _ = runtime()
    store, people, actions, sess = setup
    run = queued(setup)
    for call_id in ("call-one", "call-two"):
        lost_call(store, run, call_id=call_id)
    runner = agent.Runner(store, provider_module.ScriptedProvider([]))
    runner.recover()
    for index, call_id in enumerate(("call-one", "call-two")):
        runner.reconcile(people["operator"], ReconcileCommand(
            action_id="reconcile-" + call_id, call_id=call_id, tokens=10,
            note="合成核对", outcome="no_usable_result"))
        assert actions.view(people["alice"], sess["id"])["session"]["payload"]["blocked_unknown"] is (index == 0)
    assert runner.metrics(people["alice"], sess["id"])["real_tokens"] == 20


def test_service_lock_precedes_recovery_and_close_releases_lock(setup, monkeypatch):
    provider_module, agent, service_module = runtime()
    store, _, _, _ = setup
    recovered = []
    original = agent.Runner.recover

    def recover(self):
        recovered.append(True)
        return original(self)

    monkeypatch.setattr(agent.Runner, "recover", recover)
    one = service_module.Service(store, provider_module.ScriptedProvider([]))
    try:
        with pytest.raises(DomainError, match="已有服务"):
            service_module.Service(store, provider_module.ScriptedProvider([]))
        assert recovered == [True]
    finally:
        one.close()
        one.close()
    two = service_module.Service(store, provider_module.ScriptedProvider([]))
    two.close()
    assert len(recovered) == 2


def test_service_submits_once_and_closed_service_refuses_writes(setup):
    provider_module, _, service_module = runtime()
    store, people, actions, sess = setup
    provider = provider_module.ScriptedProvider([{"tool": "respond", "arguments": {
        "kind": "guidance", "text": "unused", "evidence_ids": []}}])
    service = service_module.Service(store, provider)
    command = MessageCommand(action_id="message", expected_version=sess["version"], text="选购建议")
    try:
        first = service.submit(people["alice"], sess["id"], command)
        assert service.submit(people["alice"], sess["id"], command) == first
    finally:
        service.close()
    assert len(provider.seen_contexts) == 1
    assert actions.view(people["alice"], sess["id"])["runs"][0]["payload"]["state"] == "completed"
    with pytest.raises(DomainError, match="停止"):
        service.submit(people["alice"], sess["id"], command)


def test_read_only_views_and_metrics_never_invoke_model_or_write(setup):
    provider_module, agent, _ = runtime()
    store, people, actions, sess = setup
    provider = provider_module.ScriptedProvider([])
    runner = agent.Runner(store, provider)
    tables = ("objects", "receipts", "audit", "calls")
    with store.transaction() as db:
        before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] for table in tables}
    for _ in range(3):
        actions.view(people["alice"], sess["id"])
        runner.metrics(people["alice"], sess["id"])
    with store.transaction() as db:
        after = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] for table in tables}
    assert before == after and provider.seen_contexts == []


def test_service_close_cannot_overtake_committed_submission(setup, monkeypatch):
    provider_module, _, service_module = runtime()
    store, people, actions, sess = setup
    provider = provider_module.ScriptedProvider([{"tool": "respond", "arguments": {
        "kind": "guidance", "text": "", "evidence_ids": []}}])
    service = service_module.Service(store, provider)
    committed, release = threading.Event(), threading.Event()
    closing, shutting_down = threading.Event(), threading.Event()
    original_message, original_shutdown = service.actions.message, service._pool.shutdown

    def pause_after_commit(*args, **kwargs):
        result = original_message(*args, **kwargs)
        committed.set()
        assert release.wait(5)
        return result

    def shutdown(*args, **kwargs):
        shutting_down.set()
        return original_shutdown(*args, **kwargs)

    def close():
        closing.set()
        service.close()

    monkeypatch.setattr(service.actions, "message", pause_after_commit)
    monkeypatch.setattr(service._pool, "shutdown", shutdown)
    command = MessageCommand(action_id="message", expected_version=sess["version"], text="办公选购")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            submission = pool.submit(service.submit, people["alice"], sess["id"], command)
            try:
                assert committed.wait(5)
                closure = pool.submit(close)
                assert closing.wait(5)
                assert not shutting_down.wait(0.1)
            finally:
                release.set()
            submission.result(5)
            closure.result(5)
        view = actions.view(people["alice"], sess["id"])
        assert view["runs"][0]["payload"]["state"] == "completed"
        assert view["session"]["payload"]["active_run"] is None
        assert len(provider.seen_contexts) == 1
    finally:
        release.set()
        service.close()


def test_service_close_drains_already_accepted_queued_work(setup, monkeypatch):
    provider_module, _, service_module = runtime()
    store, people, actions, sess = setup
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()

    class Delayed:
        is_test_provider = True
        calls = 0

        def decide(self, context, schema):
            self.calls += 1
            entered.set()
            assert release.wait(5)
            return provider_module.ProviderReply({"tool": "respond", "arguments": {
                "kind": "guidance", "text": "", "evidence_ids": []}}, 128)

    provider = Delayed()
    service = service_module.Service(store, provider)
    service._pool.shutdown(wait=True)
    service._pool = ThreadPoolExecutor(max_workers=1)
    original = service._pool.shutdown

    def shutdown(*args, **kwargs):
        closing.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(service._pool, "shutdown", shutdown)
    other = actions.create_session(people["alice"], Command(action_id="other"))
    try:
        for index, session in enumerate((sess, other)):
            service.submit(people["alice"], session["id"], MessageCommand(
                action_id=f"message-{index}", expected_version=session["version"], text="办公选购"))
        assert entered.wait(5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            closure = pool.submit(service.close)
            try:
                assert closing.wait(5)
            finally:
                release.set()
            closure.result(5)
        assert provider.calls == 2
        for session in (sess, other):
            view = actions.view(people["alice"], session["id"])
            assert view["runs"][0]["payload"]["state"] == "completed"
            assert view["session"]["payload"]["active_run"] is None
    finally:
        release.set()
        service.close()
