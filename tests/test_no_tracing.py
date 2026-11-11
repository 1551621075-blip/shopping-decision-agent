"""Ambient tracing configuration cannot export shopping sessions or tool data."""

import importlib
import importlib.util
import os

import pytest
from langsmith import Client

from shopping_advisor.actions import Actions
from shopping_advisor.schemas import Command, DomainError, MessageCommand
from shopping_advisor.storage import Store


def runtime():
    assert importlib.util.find_spec("shopping_advisor.agent") is not None, "Runner not implemented"
    assert importlib.util.find_spec("shopping_advisor.provider") is not None, "Provider not implemented"
    return (importlib.import_module("shopping_advisor.agent").Runner,
            importlib.import_module("shopping_advisor.provider").ScriptedProvider)


def test_environment_cannot_enable_tracing(tmp_path, monkeypatch):
    Runner, ScriptedProvider = runtime()
    for key in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.setenv(key, "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:1")
    sent = []
    monkeypatch.setattr(Client, "create_run", lambda *args, **kwargs: sent.append("create_run"))
    monkeypatch.setattr(Client, "update_run", lambda *args, **kwargs: sent.append("update_run"))
    store, tokens = Store.initialize(tmp_path / "data", now=1900000000)
    alice = store.authenticate(tokens["alice"])
    actions = Actions(store)
    sess = actions.create_session(alice, Command(action_id="new"))
    result = actions.message(alice, sess["id"], MessageCommand(
        action_id="message", expected_version=sess["version"], text="想买笔记本"))
    provider = ScriptedProvider([{"tool": "respond", "arguments": {
        "kind": "guidance", "evidence_ids": [], "text": "你好"}}])
    Runner(store, provider).run(alice, result["run"]["id"])
    assert sent == [] and len(provider.seen_contexts) == 1
    assert os.environ["LANGSMITH_TRACING"] == "true"


@pytest.mark.parametrize("key", ["LANGCHAIN_TRACING", "LANGCHAIN_HANDLER"])
@pytest.mark.parametrize("value", ["true", "1", "FALSE", "synthetic-setting"])
def test_legacy_tracing_fails_before_runner_without_mutating_environment(monkeypatch, key, value):
    Runner, ScriptedProvider = runtime()
    monkeypatch.delenv("LANGCHAIN_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_HANDLER", raising=False)
    monkeypatch.setenv(key, value)
    with pytest.raises(DomainError) as raised:
        Runner(None, ScriptedProvider([]))
    assert raised.value.code == "tracing_config" and value not in raised.value.message
    assert os.environ[key] == value


@pytest.mark.parametrize("value", [None, "", "0", "false", "False"])
def test_disabled_legacy_switches_are_accepted(monkeypatch, value):
    Runner, ScriptedProvider = runtime()
    for key in ("LANGCHAIN_TRACING", "LANGCHAIN_HANDLER"):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    Runner(None, ScriptedProvider([]))
