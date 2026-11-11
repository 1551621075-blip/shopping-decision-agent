from __future__ import annotations

import importlib
from copy import deepcopy
from itertools import permutations
from itertools import product as combinations

import pytest

from shopping_advisor.schemas import DomainError, Preferences, Product

NOW = 1_800_000_000
DIMENSIONS = ("performance", "portability", "battery", "value")


def catalog():
    assert importlib.util.find_spec("shopping_advisor.catalog") is not None, "catalog is not implemented"
    return importlib.import_module("shopping_advisor.catalog")


def synthetic():
    assert importlib.util.find_spec("shopping_advisor.synthetic") is not None, "synthetic is not implemented"
    return importlib.import_module("shopping_advisor.synthetic")


def laptop(**updates):
    value = {
        "id": "demo-reference",
        "name": "合成参考本",
        "category": "laptop",
        "synthetic": True,
        "price_cents": 480_000,
        "shipping_cents": 2_000,
        "stock": 5,
        "ram_gb": 16,
        "storage_gb": 512,
        "weight_g": 1450,
        "battery_minutes": 540,
        "cpu_score": 70,
        "gpu_score": 30,
        "discrete_gpu": False,
        "os": "linux",
        "captured_at": NOW - 60,
    }
    value.update(updates)
    return value


def preferences(**updates):
    value = {"budget_cents": 600_000, "use": "office"}
    value.update(updates)
    return value


def assert_error(code, function, *args):
    with pytest.raises(DomainError) as exc:
        function(*args)
    assert exc.value.code == code


def test_validate_product_returns_complete_independent_model_dump():
    source = laptop()
    original = deepcopy(source)
    result = catalog().validate_product(source)
    assert result == Product.model_validate(source).model_dump()
    assert source == original
    assert result is not source
    result["stock"] = 99
    assert source == original


@pytest.mark.parametrize(
    "field",
    ["price_cents", "shipping_cents", "stock", "ram_gb", "storage_gb", "weight_g",
     "battery_minutes", "cpu_score", "gpu_score", "captured_at"],
)
@pytest.mark.parametrize("invalid", [True, False, "16", 16.0])
def test_product_integer_fields_do_not_coerce(field, invalid):
    assert_error("invalid_product", catalog().validate_product, laptop(**{field: invalid}))


@pytest.mark.parametrize("invalid", [False, 0, 1, "true", None])
def test_product_synthetic_must_be_literal_boolean_true(invalid):
    assert_error("invalid_product", catalog().validate_product, laptop(synthetic=invalid))


@pytest.mark.parametrize("invalid", [0, 1, "false", "true"])
def test_product_discrete_gpu_is_strict_boolean_or_none(invalid):
    assert_error("invalid_product", catalog().validate_product, laptop(discrete_gpu=invalid))


@pytest.mark.parametrize(
    "updates",
    [{"price_cents": -1}, {"shipping_cents": 10_000_001}, {"stock": -1}, {"stock": 10_001},
     {"ram_gb": 3}, {"storage_gb": 127}, {"weight_g": 499}, {"battery_minutes": 1441},
     {"cpu_score": 101}, {"gpu_score": -1}, {"captured_at": -1}, {"os": "unknown"},
     {"category": "phone"}, {"id": "bad/id"}, {"name": ""}, {"extra": "not allowed"}],
)
def test_product_rejects_out_of_schema_data(updates):
    assert_error("invalid_product", catalog().validate_product, laptop(**updates))


def test_product_requires_explicit_nullable_fields_and_dictionary():
    api = catalog()
    for field in Product.model_fields:
        incomplete = laptop()
        del incomplete[field]
        assert_error("invalid_product", api.validate_product, incomplete)
    for invalid in (None, [], "product", Product.model_validate(laptop())):
        assert_error("invalid_product", api.validate_product, invalid)


def test_product_accepts_unknown_specifications_without_inventing_values():
    source = laptop(**{field: None for field in (
        "ram_gb", "storage_gb", "weight_g", "battery_minutes", "cpu_score", "gpu_score",
        "discrete_gpu", "os",
    )})
    assert catalog().validate_product(source) == source


def test_total_cost_includes_shipping_and_is_integer():
    api = catalog()
    for price, shipping in combinations((0, 1, 599_999, 10_000_000), repeat=2):
        total = api.total_cents(laptop(price_cents=price, shipping_cents=shipping))
        assert type(total) is int
        assert total == price + shipping
    assert_error("invalid_product", api.total_cents, laptop(shipping_cents=True))


def test_budget_is_inclusive_and_shipping_can_exceed_it():
    api = catalog()
    assert api.match_product(laptop(price_cents=599_000, shipping_cents=1_000), preferences()) == []
    assert api.match_product(laptop(price_cents=599_000, shipping_cents=1_001), preferences()) == [
        "over_budget"
    ]


@pytest.mark.parametrize(
    ("changes", "prefs", "reason"),
    [({"stock": 0}, {}, "out_of_stock"),
     ({"ram_gb": None}, {}, "unknown_ram_gb"),
     ({"ram_gb": 4}, {}, "insufficient_ram_gb"),
     ({"storage_gb": None}, {}, "unknown_storage_gb"),
     ({"storage_gb": 128}, {}, "insufficient_storage_gb"),
     ({"weight_g": None}, {"max_weight_g": 1500}, "unknown_weight_g"),
     ({"weight_g": 1501}, {"max_weight_g": 1500}, "exceeds_max_weight_g"),
     ({"battery_minutes": None}, {"min_battery_minutes": 600}, "unknown_battery_minutes"),
     ({"battery_minutes": 599}, {"min_battery_minutes": 600}, "insufficient_battery_minutes"),
     ({"discrete_gpu": None}, {"needs_discrete_gpu": True}, "unknown_discrete_gpu"),
     ({"discrete_gpu": False}, {"needs_discrete_gpu": True}, "discrete_gpu_required"),
     ({"os": None}, {"os": "windows"}, "unknown_os"),
     ({"os": "linux"}, {"os": "windows"}, "os_mismatch")],
)
def test_explicit_hard_constraints_report_stable_reasons(changes, prefs, reason):
    api = catalog()
    assert api.match_product(laptop(**changes), preferences(**prefs)) == [reason]
    assert api.REASON_LABELS[reason]


def test_hard_constraints_accept_exact_thresholds_and_requested_os_gpu():
    row = laptop(ram_gb=8, storage_gb=256, weight_g=1500, battery_minutes=600,
                 discrete_gpu=True, os="windows")
    prefs = preferences(max_weight_g=1500, min_battery_minutes=600, needs_discrete_gpu=True, os="windows")
    assert catalog().match_product(row, prefs) == []


def test_optional_unknown_specs_do_not_become_implicit_hard_constraints():
    api = catalog()
    row = laptop(weight_g=None, battery_minutes=None, cpu_score=None, gpu_score=None,
                 discrete_gpu=None, os=None)
    for use, priority in combinations(("office", "coding", "creative", "gaming"), DIMENSIONS):
        prefs = preferences(use=use, priority=priority)
        assert api.match_product(row, prefs) == []
        assert api.filter_products([row], prefs, NOW)["eligible"] == [row]


def test_use_and_priority_never_change_eligibility():
    api = catalog()
    rows = [laptop(id=f"item-{index}", **changes) for index, changes in enumerate([
        {}, {"discrete_gpu": False, "ram_gb": 8, "storage_gb": 256}, {"stock": 0},
        {"ram_gb": None}, {"price_cents": 600_000}, {"weight_g": None, "os": None},
    ])]
    expected = api.filter_products(rows, preferences(), NOW)
    for use, priority in combinations(("office", "coding", "creative", "gaming"), DIMENSIONS):
        assert api.filter_products(rows, preferences(use=use, priority=priority), NOW) == expected


def test_all_hard_failures_are_reported_without_duplicates():
    api = catalog()
    row = laptop(stock=0, price_cents=600_001, ram_gb=None, storage_gb=None, weight_g=None,
                 battery_minutes=None, discrete_gpu=None, os=None)
    prefs = preferences(max_weight_g=1500, min_battery_minutes=600, needs_discrete_gpu=True, os="windows")
    reasons = api.match_product(row, prefs)
    assert set(reasons) == {
        "over_budget", "out_of_stock", "unknown_ram_gb", "unknown_storage_gb", "unknown_weight_g",
        "unknown_battery_minutes", "unknown_discrete_gpu", "unknown_os",
    }
    assert len(reasons) == len(set(reasons))
    assert reasons == api.match_product(row, prefs)


def test_preferences_models_are_supported_and_inputs_are_not_changed():
    row = laptop()
    prefs = Preferences.model_validate(preferences())
    original = prefs.model_dump()
    assert catalog().match_product(row, prefs) == []
    assert catalog().evaluate_products([row], prefs, NOW)["eligible"]
    assert prefs.model_dump() == original


@pytest.mark.parametrize(
    "invalid",
    [{"budget_cents": True}, {"budget_cents": 600_000.0}, {"min_ram_gb": "8"},
     {"needs_discrete_gpu": 1}, {"needs_discrete_gpu": "false"}, {"use": "benchmark"},
     {"priority": "fastest"}, {"extra": 1}],
)
def test_preferences_are_strict_at_all_matching_entry_points(invalid):
    api = catalog()
    prefs = preferences(**invalid)
    assert_error("invalid_preferences", api.match_product, laptop(), prefs)
    assert_error("invalid_preferences", api.filter_products, [], prefs, NOW)
    assert_error("invalid_preferences", api.evaluate_products, [], prefs, NOW)


@pytest.mark.parametrize(("age", "eligible"), [(-1, False), (0, True), (1, True),
                                               (86399, True), (86400, False), (86401, False)])
def test_snapshot_age_interval_is_half_open(age, eligible):
    api = catalog()
    row = laptop(captured_at=NOW - age)
    filtered = api.filter_products([row], preferences(), NOW)
    evaluated = api.evaluate_products([row], preferences(), NOW)
    assert bool(filtered["eligible"]) is eligible
    assert bool(evaluated["eligible"]) is eligible
    assert filtered["excluded"] == evaluated["excluded"]
    if not eligible:
        assert filtered["excluded"] == [{"product_id": row["id"], "reasons": ["stale_snapshot"]}]
        assert api.REASON_LABELS["stale_snapshot"]


def test_stale_snapshot_does_not_hide_other_failures():
    result = catalog().filter_products([laptop(captured_at=NOW + 1, stock=0)], preferences(), NOW)
    assert set(result["excluded"][0]["reasons"]) == {"stale_snapshot", "out_of_stock"}


@pytest.mark.parametrize("now", [True, False, -1, 1.5, "1800000000", None])
def test_current_time_is_strict_nonnegative_integer(now):
    api = catalog()
    assert_error("invalid_now", api.filter_products, [], preferences(), now)
    assert_error("invalid_now", api.evaluate_products, [], preferences(), now)


def test_epoch_zero_is_valid_for_filtering():
    row = laptop(captured_at=0)
    assert catalog().filter_products([row], preferences(), 0)["eligible"] == [row]


@pytest.mark.parametrize("function_name", ["filter_products", "evaluate_products"])
def test_duplicate_ids_rejected_even_when_one_or_all_rows_are_excluded(function_name):
    function = getattr(catalog(), function_name)
    for stock_a, stock_b in combinations((0, 1), repeat=2):
        rows = [laptop(stock=stock_a), laptop(stock=stock_b)]
        before = deepcopy(rows)
        assert_error("duplicate_product_id", function, rows, preferences(), NOW)
        assert rows == before


@pytest.mark.parametrize("function_name", ["filter_products", "evaluate_products"])
def test_catalog_rejects_invalid_rows_and_non_list_container(function_name):
    function = getattr(catalog(), function_name)
    assert_error("invalid_product", function, [laptop(stock=True)], preferences(), NOW)
    for rows in (None, {}, "catalog", (laptop(),)):
        assert_error("invalid_catalog", function, rows, preferences(), NOW)


def test_filter_returns_full_catalog_and_independent_complete_products():
    api = catalog()
    rows = [laptop(id=f"all-{index:03}") for index in range(40)]
    prefs = preferences()
    original = deepcopy((rows, prefs))
    result = api.filter_products(rows, prefs, NOW)
    assert set(result) == {"eligible", "excluded", "notice"}
    assert result["eligible"] == rows
    assert result["excluded"] == []
    assert (rows, prefs) == original
    result["eligible"][0]["stock"] = 999
    assert (rows, prefs) == original


def test_evaluation_contract_and_transparent_integer_components():
    api = catalog()
    row = laptop()
    result = api.evaluate_products([row, laptop(id="sold-out", stock=0)], preferences(), NOW)
    assert set(result) == {"eligible", "excluded", "scoring_version", "notice"}
    assert result["scoring_version"] == "demo-v1"
    assert result["excluded"] == [{"product_id": "sold-out", "reasons": ["out_of_stock"]}]
    item = result["eligible"][0]
    assert set(item) == {"product", "total_cents", "score", "components"}
    assert item["product"] == row
    assert item["total_cents"] == 482_000
    components = item["components"]
    assert set(components["weights"]) == set(DIMENSIONS)
    assert sum(components["weights"].values()) == 100
    assert all(type(weight) is int and weight > 0 for weight in components["weights"].values())
    assert type(item["score"]) is int
    assert item["score"] == sum(components[key] * components["weights"][key] for key in DIMENSIONS) // 100
    assert components["unknown_specs"] == []
    assert "合成" in result["notice"]
    assert "非" in result["notice"]


def test_unknown_soft_specs_contribute_zero_and_have_explicit_disclaimer():
    row = laptop(cpu_score=None, gpu_score=None, weight_g=None, battery_minutes=None,
                 discrete_gpu=None, os=None)
    result = catalog().evaluate_products([row], preferences(use="gaming"), NOW)
    components = result["eligible"][0]["components"]
    assert components["portability"] == 0
    assert components["battery"] == 0
    assert set(components["unknown_specs"]) == {"cpu_score", "gpu_score", "weight_g", "battery_minutes"}
    assert components["normalized_specs"]["cpu_score"] == 0
    assert components["normalized_specs"]["gpu_score"] == 0
    assert "未知" in result["notice"] and "0" in result["notice"]
    assert "不代表" in result["notice"]


def test_normalization_endpoints_are_integer_and_bounded():
    api = catalog()
    rows = [
        laptop(id="minimum", price_cents=600_000, shipping_cents=0, ram_gb=4, storage_gb=128,
               cpu_score=0, gpu_score=0, weight_g=5000, battery_minutes=30),
        laptop(id="maximum", price_cents=0, shipping_cents=0, ram_gb=128, storage_gb=8192,
               cpu_score=100, gpu_score=100, weight_g=500, battery_minutes=1440),
    ]
    result = api.evaluate_products(rows, preferences(min_ram_gb=4, min_storage_gb=128), NOW)
    by_id = {item["product"]["id"]: item["components"] for item in result["eligible"]}
    for dimension in DIMENSIONS:
        assert by_id["maximum"][dimension] == 100
    assert by_id["minimum"]["portability"] == 0
    assert by_id["minimum"]["battery"] == 0
    assert by_id["minimum"]["value"] == 0


def test_weights_respond_to_use_and_priority_only_as_soft_preferences():
    api = catalog()
    by_profile = {}
    for use, priority in combinations(("office", "coding", "creative", "gaming"), DIMENSIONS):
        result = api.evaluate_products([laptop()], preferences(use=use, priority=priority), NOW)
        components = result["eligible"][0]["components"]
        weights = components["weights"]
        assert weights[priority] == max(weights.values())
        by_profile[use, priority] = weights
    assert by_profile["office", "value"] != by_profile["gaming", "value"]
    assert by_profile["office", "battery"] != by_profile["office", "performance"]


def test_scoring_is_independent_of_other_candidates():
    api = catalog()
    row = laptop()
    single = api.evaluate_products([row], preferences(), NOW)["eligible"][0]
    combined = api.evaluate_products([laptop(id="cheap", price_cents=10_000), row], preferences(), NOW)
    assert next(item for item in combined["eligible"] if item["product"]["id"] == row["id"]) == single


def test_ties_sort_by_total_cost_then_id_for_every_input_permutation():
    api = catalog()
    rows = [laptop(id="tie-c", price_cents=400_002, shipping_cents=0),
            laptop(id="tie-b", price_cents=400_001, shipping_cents=0),
            laptop(id="tie-a", price_cents=400_001, shipping_cents=0)]
    for order in permutations(rows):
        result = api.evaluate_products(list(order), preferences(), NOW)
        assert len({item["score"] for item in result["eligible"]}) == 1
        assert [item["product"]["id"] for item in result["eligible"]] == ["tie-a", "tie-b", "tie-c"]


def test_score_order_dominates_cost_order():
    api = catalog()
    expensive = laptop(id="high-performance", price_cents=500_000, cpu_score=100, gpu_score=100,
                       ram_gb=32, storage_gb=1024)
    cheap = laptop(id="low-performance", price_cents=100_000, cpu_score=0, gpu_score=0,
                   ram_gb=8, storage_gb=256)
    result = api.evaluate_products([cheap, expensive], preferences(use="gaming", priority="performance"), NOW)
    assert [item["product"]["id"] for item in result["eligible"]] == ["high-performance", "low-performance"]
    assert result["eligible"][0]["score"] > result["eligible"][1]["score"]


def test_evaluation_never_truncates_or_mutates_inputs_and_outputs_do_not_alias():
    api = catalog()
    rows = [laptop(id=f"full-{index:03}") for index in range(40)]
    prefs = preferences()
    original = deepcopy((rows, prefs))
    first = api.evaluate_products(rows, prefs, NOW)
    second = api.evaluate_products(rows, prefs, NOW)
    assert first == second
    assert len(first["eligible"]) == 40
    assert (rows, prefs) == original
    first["eligible"][0]["product"]["stock"] = 999
    first["eligible"][0]["components"]["weights"]["value"] = 0
    assert (rows, prefs) == original
    assert api.evaluate_products(rows, prefs, NOW) == second


def test_empty_catalog_has_valid_result_and_notice():
    api = catalog()
    filtered = api.filter_products([], preferences(), NOW)
    evaluated = api.evaluate_products([], preferences(), NOW)
    assert filtered["eligible"] == filtered["excluded"] == []
    assert evaluated["eligible"] == evaluated["excluded"] == []
    assert filtered["notice"] == evaluated["notice"]
    assert "合成" in filtered["notice"]


def test_generated_property_grid_for_eligibility_normalization_and_order():
    api = catalog()
    rows = []
    for index, (cpu, weight, battery, price) in enumerate(combinations(
        (None, 0, 50, 100), (None, 500, 2250, 5000), (None, 30, 720, 1440), (0, 599_999, 600_000),
    )):
        rows.append(laptop(id=f"grid-{index:04}", cpu_score=cpu, weight_g=weight,
                           battery_minutes=battery, price_cents=price, shipping_cents=1))
    original = deepcopy(rows)
    for use, priority in combinations(("office", "coding", "creative", "gaming"), DIMENSIONS):
        prefs = preferences(use=use, priority=priority)
        result = api.evaluate_products(rows, prefs, NOW)
        filtered = api.filter_products(rows, prefs, NOW)
        assert result["excluded"] == filtered["excluded"]
        assert len(result["eligible"]) + len(result["excluded"]) == len(rows)
        order = []
        for item in result["eligible"]:
            assert api.match_product(item["product"], prefs) == []
            assert item["total_cents"] <= prefs["budget_cents"]
            assert type(item["score"]) is int and 0 <= item["score"] <= 100
            components = item["components"]
            for dimension in DIMENSIONS:
                assert type(components[dimension]) is int and 0 <= components[dimension] <= 100
            for value in components["normalized_specs"].values():
                assert type(value) is int and 0 <= value <= 100
            order.append((-item["score"], item["total_cents"], item["product"]["id"]))
        assert order == sorted(order)
    assert rows == original


def test_improved_known_specifications_never_reduce_score():
    api = catalog()
    for priority in DIMENSIONS:
        prefs = preferences(priority=priority)
        baseline = api.evaluate_products([laptop()], prefs, NOW)["eligible"][0]["score"]
        for improvement in ({"cpu_score": 100}, {"gpu_score": 100}, {"ram_gb": 32},
                            {"storage_gb": 1024}, {"weight_g": 500}, {"battery_minutes": 1440},
                            {"price_cents": 0}, {"shipping_cents": 0}):
            improved = api.evaluate_products([laptop(**improvement)], prefs, NOW)["eligible"][0]["score"]
            assert improved >= baseline


def test_seed_is_original_complete_synthetic_and_deterministic():
    source = synthetic()
    first = source.seed_data(NOW)
    second = source.seed_data(NOW)
    assert set(first) == {"products"}
    assert first == second
    rows = first["products"]
    assert len(rows) >= 12
    assert len({row["id"] for row in rows}) == len(rows)
    assert len({row["name"] for row in rows}) == len(rows)
    for row in rows:
        assert Product.model_validate(row).model_dump() == row
        assert row["synthetic"] is True
        assert row["category"] == "laptop"
        assert row["captured_at"] == NOW - 60
        assert "合成" in row["name"]
    rows[0]["stock"] = 9999
    assert source.seed_data(NOW) == second
    assert first is not second


def test_seed_covers_decision_tradeoffs_and_rejection_paths_without_truncation():
    rows = synthetic().seed_data(NOW)["products"]
    api = catalog()
    assert {row["os"] for row in rows} >= {"windows", "linux", "macos", None}
    assert {row["discrete_gpu"] for row in rows} >= {True, False, None}
    assert any(row["stock"] == 0 for row in rows)
    assert any(row["ram_gb"] is None or row["storage_gb"] is None for row in rows)
    assert any(row["weight_g"] is None or row["battery_minutes"] is None for row in rows)
    assert any(row["weight_g"] is not None and row["weight_g"] <= 1200 for row in rows)
    assert any(row["battery_minutes"] is not None and row["battery_minutes"] >= 800 for row in rows)
    assert any(row["cpu_score"] is not None and row["cpu_score"] >= 90 for row in rows)
    budget = preferences()["budget_cents"]
    assert any(row["price_cents"] <= budget < api.total_cents(row) for row in rows)
    assert any(api.total_cents(row) <= 300_000 for row in rows)
    assert any(api.total_cents(row) > budget for row in rows)
    filtered = api.filter_products(rows, preferences(), NOW)
    assert len(filtered["eligible"]) + len(filtered["excluded"]) == len(rows)
    assert {item["product_id"] for item in filtered["excluded"]} | {
        row["id"] for row in filtered["eligible"]
    } == {row["id"] for row in rows}


@pytest.mark.parametrize("now", [True, False, 0, 59, -1, 60.0, "60", None])
def test_seed_requires_strict_integer_time_at_least_sixty(now):
    assert_error("invalid_now", synthetic().seed_data, now)


def test_seed_captured_at_boundary_and_time_shift():
    source = synthetic()
    at_zero = source.seed_data(60)["products"]
    shifted = source.seed_data(61)["products"]
    assert all(row["captured_at"] == 0 for row in at_zero)
    assert all(row["captured_at"] == 1 for row in shifted)
    for before, after in zip(at_zero, shifted, strict=True):
        assert {key: value for key, value in before.items() if key != "captured_at"} == {
            key: value for key, value in after.items() if key != "captured_at"
        }
