"""Offline acceptance-driver tests; all faults are local synthetic report mutations."""
from __future__ import annotations

import copy
import importlib.util
import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unit tests must not access network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture(scope="module")
def driver():
    path = ROOT / "scripts" / "run_acceptance.py"
    assert path.is_file(), "acceptance driver not implemented (expected initial RED)"
    spec = importlib.util.spec_from_file_location("acceptance_driver", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_structure_and_core(driver):
    corpus = driver.load_cases()
    assert len(corpus["cases"]) == 30
    assert {c["id"] for c in corpus["cases"] if c["core"]} == driver.CORE_IDS
    damaged = copy.deepcopy(corpus)
    damaged["cases"][0]["expected"]["required_evidence"].append("unimplemented_check")
    with pytest.raises(driver.AcceptanceError):
        driver.validate_cases(damaged)


@pytest.mark.parametrize("args", [[], ["--model", "explicit"], ["--model-url", "http://127.0.0.1:1"],
                                     ["--test-mode", "--model", "explicit"],
                                     ["--test-mode", "--model-url", "http://127.0.0.1:1"]])
def test_missing_endpoint_or_mixed_modes_refused(driver, args):
    with pytest.raises((driver.AcceptanceError, SystemExit)):
        driver.parse_args(args)


def test_real_provider_selection_is_explicit_and_no_fallback(driver):
    from shopping_advisor.provider import LocalHTTPProvider
    args = driver.parse_args(["--model-url", "http://127.0.0.1:1", "--model", "explicit"])
    assert isinstance(driver.select_provider(args), LocalHTTPProvider)


@pytest.mark.parametrize("url", ["http://example.com", "https://127.0.0.1", "http://127.0.0.1@evil.test"])
def test_non_local_endpoint_refused_without_network(driver, url):
    with pytest.raises(Exception):
        driver.select_provider(driver.parse_args(["--model-url", url, "--model", "explicit"]))


def test_invalid_cli_endpoint_is_safe_explicit_refusal(driver, capsys):
    assert driver.main(["--model-url", "http://example.com", "--model", "explicit"]) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "refused"
    assert summary["human_review"] == "pending"


def test_default_temp_output_cannot_be_inside_source(driver, monkeypatch, tmp_path):
    # Simulate a tempfile environment pointing into source without creating there.
    monkeypatch.setattr(driver.tempfile, "gettempdir", lambda: str(ROOT))
    monkeypatch.setattr(driver.tempfile, "mkdtemp", lambda **kwargs: str(ROOT / "never-created"))
    with pytest.raises(driver.AcceptanceError):
        driver.private_output()


def test_output_is_new_private_and_outside_source(driver, tmp_path):
    with pytest.raises(driver.AcceptanceError):
        driver.private_output(ROOT / "forbidden-output")
    with pytest.raises(driver.AcceptanceError):
        driver.private_output(tmp_path)
    output = driver.private_output(tmp_path / "new")
    assert output.stat().st_mode & 0o777 == 0o700
    driver.write_json(output / "report.json", {"human_review": "pending"})
    assert (output / "report.json").stat().st_mode & 0o777 == 0o600


def test_null_discarded_usage_is_unknown_not_zero(driver):
    calls = [{"id": "c", "state": "discarded", "tokens": None, "usage_kind": "real"}]
    usage = driver.usage_summary(calls)
    assert usage["unknown_calls"] == 1
    assert usage["real_tokens"] is None
    assert usage["known_real_tokens"] == 0
    assert usage["status"] == "unknown"


def test_exact_proposal_check_rejects_relaxation(driver):
    prefs = driver.confirmation_preferences()
    proposal = {"payload": {"preferences": {**prefs, "budget_cents": 1000000}}}
    with pytest.raises(driver.AcceptanceError):
        driver.require_exact_proposal(proposal, prefs)


def test_equal_exceptions_or_changed_receipts_are_not_idempotency(driver):
    assert driver.equal_successful_receipts({"run": {"id": "r"}}, {"run": {"id": "r"}})
    assert not driver.equal_successful_receipts({"error": "same"}, {"error": "same"})
    assert not driver.equal_successful_receipts({"run": {"id": "r"}}, {"run": {"id": "other"}})


@pytest.fixture(scope="module")
def full_result(driver, tmp_path_factory):
    # run_acceptance applies its own process socket guard as well as test guards.
    output = tmp_path_factory.mktemp("acceptance") / "evaluation"
    args = driver.parse_args(["--test-mode", "--suite", "all", "--output", str(output)])
    return driver.run_acceptance(args)


def test_wrong_provider_proposal_never_confirmed(driver, tmp_path):
    from shopping_advisor.demo_provider import DemoProvider
    from shopping_advisor.provider import ProviderReply

    class RelaxingProvider(DemoProvider):
        def decide(self, context, schema):
            reply = super().decide(context, schema)
            if reply.action["tool"] == "propose_preferences":
                bad = copy.deepcopy(reply.action)
                bad["arguments"]["preferences"]["budget_cents"] = 1000000
                return ProviderReply(bad, 128, "TEST")
            return reply

    case = driver.case_by_id("change-03-proposal")
    scenario = driver.Scenario(case, tmp_path / "bad-proposal", RelaxingProvider(), driver.load_cases()["now"])
    with pytest.raises(driver.AcceptanceError, match="confirmation refused"):
        scenario.execute()
    record = scenario.record()
    assert record["view"]["session"]["payload"]["preferences"] is None
    assert not any(a["action"] == "decide_proposal" for a in record["final"]["audit"])


def test_synthetic_usage_rejects_invalid_kinds_and_zero_tokens(driver):
    usage = driver.usage_summary([{"id": "c", "state": "completed", "tokens": 0, "usage_kind": "invented"}])
    assert usage["status"] == "unknown"
    assert usage["invalid_usage_calls"] == 1


def test_claimed_hard_constraints_fail_if_assistant_invents_price(driver, full_result):
    result = copy.deepcopy(result_for(full_result, "reg-01-office"))
    state = result["record"]["final"]
    sess = next(o for o in state["objects"] if o["kind"] == "session" and o["owner"] == "alice")
    sess["payload"]["messages"].append({"role": "assistant", "kind": "facts", "text": "这款商品总价只需1元，已为你确认所有条件。"})
    sess["content_hash"] = driver.object_digest(sess)
    checks = driver.assess(driver.case_by_id(result["id"]), result["record"], "test")
    assert checks["invariant:published_actions"]["status"] == "failed"


def test_executed_unknown_tool_count_is_not_rejected(driver):
    record = {"final": {"objects": [{"kind": "run", "owner": "alice", "id": "r", "payload": {
        "state": "completed", "trace": [{"tool": "payment", "action": {"tool": "payment", "arguments": {}}, "observation": {"ok": True}}]}}]}}
    summary = driver.violation_summary(record, {})
    assert summary["executed_violation_count"] == 1
    assert summary["rejected_proposal_count"] == 0


def test_source_claims_must_match_actual_tool_trace(driver, full_result):
    result = copy.deepcopy(result_for(full_result, "reg-01-office"))
    for run in result["record"]["final"]["objects"]:
        if run["kind"] == "run":
            run["payload"]["trace"] = []
            run["content_hash"] = driver.object_digest(run)
    checks = driver.assess(driver.case_by_id(result["id"]), result["record"], "test")
    assert checks["required:valid_recommendation_chain"]["status"] == "failed"


def test_all_thirty_synthetic_cases_and_pending_review(driver, full_result):
    assert len(full_result["cases"]) == 30
    assert full_result["counts"] == {"passed": 30, "failed": 0, "unknown": 0, "not_run": 0}
    assert full_result["human_review"] == "pending"
    assert "overall_pass" not in full_result
    assert full_result["usage"]["real_calls"] == 0
    assert full_result["cases_sha256"] == driver.CASES_SHA256
    assert full_result["source_fingerprint"]["before"] == full_result["source_fingerprint"]["after"]
    assert full_result["source_fingerprint"]["stable"] is True
    assert "src/shopping_advisor/agent.py" in full_result["source_fingerprint"]["before"]
    assert any(p.endswith(".js") for p in full_result["source_fingerprint"]["before"])
    assert any(p.endswith(".html") for p in full_result["source_fingerprint"]["before"])
    assert full_result["source_fingerprint"]["coverage"] == "src Python/HTML/JS/CSS, acceptance driver, pyproject.toml and uv.lock; excludes runtime data"
    for result in full_result["cases"]:
        assert result["checks"]
        assert all(check["status"] in {"passed", "not_applicable"} for check in result["checks"].values()), result
        assert result["record"]["final"]["objects"]
        assert result["record"]["final"]["receipts"]
    review = Path(full_result["output"]) / "human-review" / "reviews.json"
    text = review.read_text()
    assert "pending" in text
    assert '"context"' not in text and '"tokens"' not in text and '"messages"' not in text
    for path in Path(full_result["output"]).rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600), path


def result_for(report, case_id):
    return next(c for c in report["cases"] if c["id"] == case_id)


@pytest.mark.parametrize("fault", ["cost", "product", "score", "facts", "owner", "false_action_text"])
def test_checker_rejects_local_durable_result_faults(driver, full_result, fault):
    result = copy.deepcopy(result_for(full_result, "reg-01-office"))
    objects = result["record"]["final"]["objects"]
    rec = next(o for o in objects if o["kind"] == "recommendation")
    if fault == "cost":
        rec["payload"]["selection"]["total_cents"] = 1
    elif fault == "product":
        rec["payload"]["selection"]["product"]["id"] = "made-up-product"
    elif fault == "score":
        rec["payload"]["selection"]["score"] += 1
    elif fault == "facts":
        rec["payload"]["facts"][0]["value"] = -1
    elif fault == "owner":
        next(o for o in objects if o["owner"] == "bob")["payload"]["unauthorized"] = True
    else:
        sess = next(o for o in objects if o["kind"] == "session" and o["owner"] == "alice")
        sess["payload"]["messages"].append({"role": "assistant", "text": "已为你下单并付款成功。"})
    # Re-hash local mutation so assertions cannot rely on content hashes alone.
    for obj in objects:
        obj["content_hash"] = driver.object_digest(obj)
    checks = driver.assess(driver.case_by_id("reg-01-office"), result["record"], "test")
    assert any(check["status"] == "failed" for check in checks.values()), fault


def test_checker_rejects_false_no_match(driver, full_result):
    result = copy.deepcopy(result_for(full_result, "reg-01-office"))
    for obj in result["record"]["final"]["objects"]:
        if obj["payload"].get("tool") == "search_products":
            obj["payload"]["data"]["eligible"] = []
            obj["content_hash"] = driver.object_digest(obj)
    checks = driver.assess(driver.case_by_id("reg-11-no-match"), result["record"], "test")
    assert checks["required:empty_search"]["status"] == "failed"


def test_duplicate_receipt_fault_fails_scenario(driver, full_result):
    result = copy.deepcopy(result_for(full_result, "recover-01-replay"))
    result["record"]["events"]["replay"]["second"] = {"run": {"id": "different"}}
    checks = driver.assess(driver.case_by_id(result["id"]), result["record"], "test")
    assert checks["required:message_replay_single_effect"]["status"] == "failed"


def test_observations_count_only_actual_subsequent_context(driver, full_result):
    result = result_for(full_result, "reg-01-office")
    feedback = driver.feedback_summary(result["record"]["final"]["calls"])
    assert feedback["calls_with_feedback"] == 3
    assert feedback["observations_consumed"] == 3
    assert feedback["terminal_actions_without_next_call"] == 1
    assert all(c["response"]["tool"] != "recommend" for c in feedback["consumed"])


def test_feedback_does_not_count_old_repeated_action(driver):
    action = {"tool": "search_products", "arguments": {}}
    old = {"tool": "search_products", "action": action, "observation": {"id": "old"}}
    calls = [
        {"id": "a", "run_id": "r", "response": action, "context": {"context": {"observations": [old]}}},
        {"id": "b", "run_id": "r", "response": None, "context": {"context": {"observations": [old]}}},
    ]
    assert driver.feedback_summary(calls)["observations_consumed"] == 0


def test_failed_evidence_predicate_is_not_executed_violation(driver):
    record = {"final": {"objects": []}}
    summary = driver.violation_summary(record, {"forbidden:duplicate_side_effect": {"status": "failed"}})
    assert summary["executed_violation_count"] == 0
    assert summary["unverified_forbidden_checks"] == ["forbidden:duplicate_side_effect"]


@pytest.mark.parametrize("fault", ["audit_deleted", "payment_audit", "bob_receipts_deleted", "alice_receipt",
                                     "stage_bob_write", "call_owner", "fabricated_observation", "missing_observations",
                                     "oracle_context", "terminal_mismatch", "orphan_evaluation", "stage_role", "dispatch_mismatch"])
def test_reviewer_confirmed_local_ledger_faults_fail(driver, full_result, fault):
    result = copy.deepcopy(result_for(full_result, "reg-01-office"))
    record = result["record"]
    final = record["final"]
    if fault == "audit_deleted":
        final["audit"] = []
    elif fault == "payment_audit":
        final["audit"].append({"seq": 9999, "actor": "bob", "action": "payment", "object_id": "p", "detail": {}})
    elif fault == "bob_receipts_deleted":
        final["receipts"] = [r for r in final["receipts"] if r["user_id"] != "bob"]
    elif fault == "alice_receipt":
        next(r for r in final["receipts"] if r["user_id"] == "alice")["response"] = {"id": "invented"}
    elif fault == "stage_bob_write":
        obj = next(o for o in record["stages"][0]["state"]["objects"] if o["owner"] == "bob")
        obj["payload"]["unauthorized"] = True
        obj["content_hash"] = driver.object_digest(obj)
    elif fault == "call_owner":
        for call in final["calls"]:
            call["owner"] = "operator"
    elif fault == "fabricated_observation":
        final["calls"][1]["context"]["context"]["observations"][0]["observation"] = {"id": "fabricated", "price_cents": 1}
    elif fault == "missing_observations":
        for call in final["calls"]:
            call["context"]["context"]["observations"] = []
    elif fault == "oracle_context":
        final["calls"][0]["context"]["context"]["expected"] = {"case_id": result["id"]}
    elif fault == "stage_role":
        record["stages"][0]["state"]["users"][0]["role"] = "operator"
    elif fault == "dispatch_mismatch":
        final["calls"][0]["context"]["context"]["task"] = "changed task"
    elif fault == "terminal_mismatch":
        run = next(o for o in final["objects"] if o["kind"] == "run")
        run["payload"]["state"] = "failed"
        run["content_hash"] = driver.object_digest(run)
    else:
        evaluation = copy.deepcopy(next(o for o in final["objects"] if o["kind"] == "evaluation"))
        evaluation["id"] = "orphan-evaluation"
        evaluation["payload"]["data"]["eligible"][0]["score"] = 999999
        evaluation["content_hash"] = driver.object_digest(evaluation)
        final["objects"].append(evaluation)
    checks = driver.assess(driver.case_by_id(result["id"]), record, "test")
    assert any(c["status"] == "failed" for c in checks.values()), fault


def test_matching_invented_receipts_are_not_message_idempotency(driver, full_result):
    result = copy.deepcopy(result_for(full_result, "recover-01-replay"))
    record = result["record"]
    original = record["events"]["replay"]["first"]
    for key in ("first", "second"):
        record["events"]["replay"][key] = {"id": "invented-without-a-run"}
    for receipt in record["final"]["receipts"]:
        if receipt["response"] == original:
            receipt["response"] = {"id": "invented-without-a-run"}
    checks = driver.assess(driver.case_by_id(result["id"]), record, "test")
    assert checks["required:message_replay_single_effect"]["status"] == "failed"


def test_symlink_output_ancestor_refused(driver, tmp_path):
    parent = tmp_path / "actual"
    parent.mkdir()
    link = tmp_path / "link"
    link.symlink_to(parent, target_is_directory=True)
    with pytest.raises(driver.AcceptanceError):
        driver.private_output(link / "report")


def test_record_failure_still_preserves_all_selected_results(driver, monkeypatch, tmp_path):
    class BrokenRecord:
        def __init__(self, *args, **kwargs):
            pass

        def execute(self):
            pass

        def record(self):
            raise ValueError("injected local unreadable record")

    monkeypatch.setattr(driver, "Scenario", BrokenRecord)
    output = tmp_path / "broken-records"
    report = driver.run_acceptance(driver.parse_args(["--test-mode", "--suite", "core", "--output", str(output)]))
    assert len(report["cases"]) == 8
    assert report["counts"]["unknown"] == 8
    assert (output / "report.json").is_file()
    assert (output / "human-review" / "reviews.json").is_file()


def test_core_selection_and_no_oracle_in_context(driver, tmp_path):
    report = driver.run_acceptance(driver.parse_args(["--test-mode", "--suite", "core", "--output", str(tmp_path / "core")]))
    assert {r["id"] for r in report["cases"]} == driver.CORE_IDS
    assert report["selection"] == {"suite": "core", "selected": 8, "corpus": 30}
    for result in report["cases"]:
        for call in result["record"]["final"]["calls"]:
            text = json.dumps(call["context"])
            assert result["id"] not in text
            assert '"expected"' not in text and '"scenario"' not in text
            assert '"required_evidence"' not in text
