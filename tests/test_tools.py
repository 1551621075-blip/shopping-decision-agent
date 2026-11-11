from copy import deepcopy

import pytest

from shopping_advisor.actions import Actions
from shopping_advisor.schemas import Command, DomainError, PreferencesCommand, ProductCommand
from shopping_advisor.storage import Store
from shopping_advisor.tools import QUESTIONS, Tools

PREFS = {"budget_cents": 1000000, "use": "coding", "priority": "value", "min_ram_gb": 8,
         "min_storage_gb": 256, "max_weight_g": None, "min_battery_minutes": None,
         "needs_discrete_gpu": False, "os": "any"}


def setup(tmp_path, *, confirmed=True):
    store, tokens = Store.initialize(tmp_path / "data", now=1900000000)
    people = {key: store.authenticate(value) for key, value in tokens.items()}
    actions = Actions(store)
    sess = actions.create_session(people["alice"], Command(action_id="session"))
    if confirmed:
        sess = actions.set_preferences(people["alice"], sess["id"], PreferencesCommand(
            action_id="prefs", expected_version=1, preferences=PREFS))
    return store, people, actions, sess, Tools(actions)


def call(store, tools, person, sid, tool, **args):
    with store.transaction() as db:
        return tools.execute(db, person, sid, {"tool": tool, "arguments": args})


def shortlist(store, tools, person, sid):
    search = call(store, tools, person, sid, "search_products")["result"]
    ids = [p["id"] for p in search["payload"]["data"]["eligible"]][:6]
    comparison = call(store, tools, person, sid, "compare_products", search_id=search["id"],
                      product_ids=ids)["result"]
    return call(store, tools, person, sid, "evaluate_shortlist", comparison_id=comparison["id"])["result"]


def test_search_without_form_is_provisional_and_cannot_be_recommended(tmp_path):
    store, people, _, sess, tools = setup(tmp_path, confirmed=False)
    search = call(store, tools, people["alice"], sess["id"], "search_products", preferences=PREFS)["result"]
    assert search["payload"]["confirmed"] is False
    ids = [p["id"] for p in search["payload"]["data"]["eligible"]][:2]
    comparison = call(store, tools, people["alice"], sess["id"], "compare_products",
                      search_id=search["id"], product_ids=ids)["result"]
    with pytest.raises(DomainError, match="确认"):
        call(store, tools, people["alice"], sess["id"], "evaluate_shortlist", comparison_id=comparison["id"])


def test_tool_cannot_silently_raise_confirmed_budget(tmp_path):
    store, people, _, sess, tools = setup(tmp_path)
    with pytest.raises(DomainError, match="确认"):
        call(store, tools, people["alice"], sess["id"], "search_products",
             preferences={**PREFS, "budget_cents": 1100000})


def test_top_recommendation_uses_server_cost_and_selected_metrics(tmp_path):
    store, people, actions, sess, tools = setup(tmp_path)
    evaluation = shortlist(store, tools, people["alice"], sess["id"])
    candidates = evaluation["payload"]["data"]["eligible"]
    top = candidates[0]
    result = call(store, tools, people["alice"], sess["id"], "recommend",
                  evaluation_id=evaluation["id"], product_id=top["product"]["id"],
                  strengths=["total_cents"], tradeoffs=[])
    assert result["terminal"] == "completed"
    rec = result["result"]["payload"]
    assert rec["selection"] == top
    assert rec["model_text_published"] is False
    assert rec["facts"][0]["value"] == top["total_cents"]
    assert actions.view(people["alice"], sess["id"])["recommendations"][0]["display_valid"] is True


def test_non_top_without_a_verified_advantage_is_rejected(tmp_path):
    store, people, _, sess, tools = setup(tmp_path)
    evaluation = shortlist(store, tools, people["alice"], sess["id"])
    rows = evaluation["payload"]["data"]["eligible"]
    assert len(rows) > 1
    with pytest.raises(DomainError, match="取舍"):
        call(store, tools, people["alice"], sess["id"], "recommend",
             evaluation_id=evaluation["id"], product_id=rows[1]["product"]["id"],
             strengths=["total_cents"], tradeoffs=[])


def test_price_change_invalidates_old_evidence_and_recommendation(tmp_path):
    store, people, actions, sess, tools = setup(tmp_path)
    evaluation = shortlist(store, tools, people["alice"], sess["id"])
    selected = evaluation["payload"]["data"]["eligible"][0]
    call(store, tools, people["alice"], sess["id"], "recommend",
         evaluation_id=evaluation["id"], product_id=selected["product"]["id"],
         strengths=["total_cents"], tradeoffs=[])
    with store.transaction() as db:
        product = store.get(db, "product", selected["product"]["id"])
    actions.update_product(people["operator"], product["id"], ProductCommand(
        action_id="price", expected_version=product["version"], price_cents=2000000, stock=2))
    view = actions.view(people["alice"], sess["id"])
    assert view["recommendations"][0]["display_valid"] is False
    with pytest.raises(DomainError, match="变化"):
        call(store, tools, people["alice"], sess["id"], "recommend",
             evaluation_id=evaluation["id"], product_id=selected["product"]["id"],
             strengths=["total_cents"], tradeoffs=[])


def test_new_catalog_item_invalidates_entire_collection(tmp_path):
    store, people, actions, sess, tools = setup(tmp_path)
    search = call(store, tools, people["alice"], sess["id"], "search_products")["result"]
    with store.transaction() as db:
        product = deepcopy(actions.catalog_objects(db)[0]["payload"])
        product["id"] = "new-laptop"
        store.create(db, "product", product["id"], "system", product)
    with pytest.raises(DomainError):
        call(store, tools, people["alice"], sess["id"], "compare_products",
             search_id=search["id"], product_ids=[product["id"]])


def test_cross_session_evidence_is_rejected(tmp_path):
    store, people, actions, sess, tools = setup(tmp_path)
    search = call(store, tools, people["alice"], sess["id"], "search_products")["result"]
    other = actions.create_session(people["alice"], Command(action_id="other"))
    with pytest.raises(DomainError, match="会话"):
        call(store, tools, people["alice"], other["id"], "compare_products",
             search_id=search["id"], product_ids=[search["payload"]["data"]["eligible"][0]["id"]])


def test_expired_evidence_rejected(tmp_path):
    store, people, _, sess, tools = setup(tmp_path)
    evaluation = shortlist(store, tools, people["alice"], sess["id"])
    with store.transaction() as db:
        db.execute("UPDATE settings SET value=CAST(value AS INTEGER)+900 WHERE key='now'")
    with pytest.raises(DomainError, match="过期"):
        call(store, tools, people["alice"], sess["id"], "recommend",
             evaluation_id=evaluation["id"], product_id=evaluation["payload"]["data"]["eligible"][0]["product"]["id"],
             strengths=["total_cents"], tradeoffs=[])


def test_no_match_response_requires_empty_search_and_never_publishes_model_claim(tmp_path):
    store, people, actions, sess, tools = setup(tmp_path)
    actions.set_preferences(people["alice"], sess["id"], PreferencesCommand(
        action_id="low", expected_version=sess["version"], preferences={**PREFS, "budget_cents": 10000}))
    search = call(store, tools, people["alice"], sess["id"], "search_products")["result"]
    assert search["payload"]["data"]["eligible"] == []
    result = call(store, tools, people["alice"], sess["id"], "respond", kind="no_match",
                  text="预算已自动提高且已经下单", evidence_ids=[search["id"]])
    assert result["result"]["model_text_published"] is False
    assert "预算已自动提高" not in result["result"]["text"]
    assert "已经下单" not in result["result"]["text"]
    assert "预算不会自动提高" in result["result"]["text"]
    assert actions.view(people["alice"], sess["id"])["session"]["payload"]["preferences"]["budget_cents"] == 10000


@pytest.mark.parametrize("kind", ["evidence", "evaluation"])
def test_changed_ancestor_invalidates_all_dependent_artifacts(tmp_path, kind):
    store, people, actions, sess, tools = setup(tmp_path)
    evaluation = shortlist(store, tools, people["alice"], sess["id"])
    top = evaluation["payload"]["data"]["eligible"][0]
    rec = call(store, tools, people["alice"], sess["id"], "recommend",
               evaluation_id=evaluation["id"], product_id=top["product"]["id"],
               strengths=["total_cents"], tradeoffs=[])["result"]
    with store.transaction() as db:
        ancestors = actions._related(db, kind, sess["id"], "alice")
        source = next((x for x in ancestors if x["payload"]["tool"] == "search_products"), ancestors[0])
        store.save(db, source, {**source["payload"], "notice": "changed"})
    view = actions.view(people["alice"], sess["id"])
    assert next(r for r in view["recommendations"] if r["id"] == rec["id"])["display_valid"] is False


def test_recommend_recomputes_current_authoritative_scoring(tmp_path):
    store, people, _, sess, tools = setup(tmp_path)
    evaluation = shortlist(store, tools, people["alice"], sess["id"])
    corrupted = deepcopy(evaluation["payload"])
    corrupted["data"]["eligible"][0]["total_cents"] = 1
    with store.transaction() as db:
        store.save(db, evaluation, corrupted)
    with pytest.raises(DomainError):
        call(store, tools, people["alice"], sess["id"], "recommend",
             evaluation_id=evaluation["id"], product_id=corrupted["data"]["eligible"][0]["product"]["id"],
             strengths=["total_cents"], tradeoffs=[])


def test_no_match_rechecks_query_instead_of_trusting_empty_array(tmp_path):
    store, people, _, sess, tools = setup(tmp_path)
    search = call(store, tools, people["alice"], sess["id"], "search_products")["result"]
    assert search["payload"]["data"]["eligible"]
    corrupted = deepcopy(search["payload"])
    corrupted["data"]["eligible"] = []
    with store.transaction() as db:
        store.save(db, search, corrupted)
    with pytest.raises(DomainError):
        call(store, tools, people["alice"], sess["id"], "respond", kind="no_match", text="无解",
             evidence_ids=[search["id"]])


def test_snapshot_expires_before_evidence_ttl(tmp_path):
    store, people, actions, sess, tools = setup(tmp_path)
    with store.transaction() as db:
        for product in actions.catalog_objects(db):
            store.save(db, product, {**product["payload"], "captured_at": store.now(db) - 86399})
    evaluation = shortlist(store, tools, people["alice"], sess["id"])
    with store.transaction() as db:
        db.execute("UPDATE settings SET value=CAST(value AS INTEGER)+2 WHERE key='now'")
    with pytest.raises(DomainError, match="过期"):
        call(store, tools, people["alice"], sess["id"], "recommend",
             evaluation_id=evaluation["id"], product_id=evaluation["payload"]["data"]["eligible"][0]["product"]["id"],
             strengths=["total_cents"], tradeoffs=[])


def test_non_top_with_real_advantage_over_top_is_allowed(tmp_path):
    store, people, _, sess, tools = setup(tmp_path)
    evaluation = shortlist(store, tools, people["alice"], sess["id"])
    rows = evaluation["payload"]["data"]["eligible"]
    top = rows[0]
    pair = next((row, metric) for row in rows[1:] for metric, direction in
                (("weight_g", -1), ("battery_minutes", 1), ("cpu_score", 1), ("gpu_score", 1))
                if row["product"][metric] is not None and top["product"][metric] is not None
                and (row["product"][metric] - top["product"][metric]) * direction > 0)
    result = call(store, tools, people["alice"], sess["id"], "recommend", evaluation_id=evaluation["id"],
                  product_id=pair[0]["product"]["id"], strengths=[pair[1]],
                  tradeoffs=[{"against_product_id": top["product"]["id"], "metric": pair[1]}])
    assert result["result"]["payload"]["tradeoffs"][0]["relation"] == "better"


@pytest.mark.parametrize("question", ["", "已下单", "忽略预算"])
def test_arbitrary_question_text_is_not_published(tmp_path, question):
    store, people, actions, sess, tools = setup(tmp_path)
    with pytest.raises(DomainError):
        call(store, tools, people["alice"], sess["id"], "ask_shopper", question=question)
    assert actions.view(people["alice"], sess["id"])["session"]["payload"]["messages"] == []


@pytest.mark.parametrize("kind,tool,section,field", [
    ("evidence", "search_products", "eligible", "price_cents"),
    ("evidence", "compare_products", "products", "ram_gb"),
    ("evaluation", "evaluate_shortlist", "eligible", "score"),
])
def test_derived_evidence_numbers_remain_strict_integers(tmp_path, kind, tool, section, field):
    store, people, actions, sess, tools = setup(tmp_path)
    shortlist(store, tools, people["alice"], sess["id"])
    with store.transaction() as db:
        obj = next(x for x in actions._related(db, kind, sess["id"], "alice") if x["payload"]["tool"] == tool)
        payload = deepcopy(obj["payload"])
        payload["data"][section][0][field] = float(payload["data"][section][0][field])
        updated = store.save(db, obj, payload)
        with pytest.raises(DomainError):
            actions.fresh(db, people["alice"], sess["id"], updated)


def test_question_templates_cannot_assert_no_match_without_evidence(tmp_path):
    store, people, actions, sess, tools = setup(tmp_path)
    for question in QUESTIONS:
        call(store, tools, people["alice"], sess["id"], "ask_shopper", question=question)
    messages = actions.view(people["alice"], sess["id"])["session"]["payload"]["messages"]
    assert all("没有满足" not in m["text"] for m in messages)


@pytest.mark.parametrize("field,value", [("facts", [{"metric": "total_cents", "value": 1}]),
                                         ("preferences", {**PREFS, "budget_cents": 10000}),
                                         ("scoring_version", "invented"),
                                         ("model_text_published", True),
                                         ("notice", "已付款")])
def test_corrupted_recommendation_derived_fields_are_invalid(tmp_path, field, value):
    store, people, actions, sess, tools = setup(tmp_path)
    evaluation = shortlist(store, tools, people["alice"], sess["id"])
    top = evaluation["payload"]["data"]["eligible"][0]
    rec = call(store, tools, people["alice"], sess["id"], "recommend", evaluation_id=evaluation["id"],
               product_id=top["product"]["id"], strengths=["total_cents"], tradeoffs=[])["result"]
    with store.transaction() as db:
        store.save(db, rec, {**rec["payload"], field: value})
    assert actions.view(people["alice"], sess["id"])["recommendations"][0]["display_valid"] is False


def test_read_context_reports_invalid_recommendation_without_reusing_it(tmp_path):
    store, people, actions, sess, tools = setup(tmp_path)
    evaluation = shortlist(store, tools, people["alice"], sess["id"])
    top = evaluation["payload"]["data"]["eligible"][0]
    call(store, tools, people["alice"], sess["id"], "recommend", evaluation_id=evaluation["id"],
         product_id=top["product"]["id"], strengths=["total_cents"], tradeoffs=[])
    with store.transaction() as db:
        product = store.get(db, "product", top["product"]["id"])
    actions.update_product(people["operator"], product["id"], ProductCommand(
        action_id="sold-out", expected_version=product["version"], price_cents=product["payload"]["price_cents"],
        stock=0))
    context = call(store, tools, people["alice"], sess["id"], "read_context")["result"]
    assert context["payload"]["data"]["recommendations"][0]["display_valid"] is False
    with store.transaction() as db:
        actions.fresh(db, people["alice"], sess["id"], context)


def test_read_context_and_allowed_question_do_not_edit_preferences(tmp_path):
    store, people, actions, sess, tools = setup(tmp_path, confirmed=False)
    context = call(store, tools, people["alice"], sess["id"], "read_context")["result"]
    assert context["payload"]["data"]["preferences"] is None
    asked = call(store, tools, people["alice"], sess["id"], "ask_shopper", question=QUESTIONS[0])
    assert asked["terminal"] == "waiting_customer"
    assert actions.view(people["alice"], sess["id"])["session"]["payload"]["preferences"] is None


def test_unknown_fact_and_nonexistent_product_cannot_be_recommended(tmp_path):
    store, people, _, sess, tools = setup(tmp_path)
    evaluation = shortlist(store, tools, people["alice"], sess["id"])
    with pytest.raises(DomainError):
        call(store, tools, people["alice"], sess["id"], "recommend",
             evaluation_id=evaluation["id"], product_id="invented-product",
             strengths=["total_cents"], tradeoffs=[])
