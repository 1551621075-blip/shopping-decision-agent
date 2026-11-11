"""Strict synthetic laptop filtering and deterministic, explainable demo scores."""
from __future__ import annotations

from .schemas import DomainError, Preferences, Product

NOTICE = (
    "本目录全部为原创合成笔记本与合成价格、库存、规格，仅用于决策流程演示，"
    "非真实品牌商品、报价或实测评测，不能据此推断真实产品表现。"
    "评分采用公开的 demo-v1 整数规则；未知软规格按 0 计分，不代表真实性能或能力为 0。"
)
SCORING_VERSION = "demo-v1"
SNAPSHOT_MAX_AGE_SECONDS = 86_400

REASON_LABELS = {
    "stale_snapshot": "商品快照已满 24 小时或捕获时间位于未来",
    "out_of_stock": "商品已售罄",
    "over_budget": "商品价格加运费超过预算",
    "unknown_ram_gb": "内存规格未知，不能确认满足最低要求",
    "insufficient_ram_gb": "内存低于最低要求",
    "unknown_storage_gb": "存储容量未知，不能确认满足最低要求",
    "insufficient_storage_gb": "存储容量低于最低要求",
    "unknown_weight_g": "重量未知，不能确认满足重量上限",
    "exceeds_max_weight_g": "重量超过用户设置的上限",
    "unknown_battery_minutes": "续航规格未知，不能确认满足最低要求",
    "insufficient_battery_minutes": "续航低于用户设置的最低要求",
    "unknown_discrete_gpu": "是否配备独立显卡未知",
    "discrete_gpu_required": "未配备用户要求的独立显卡",
    "unknown_os": "操作系统未知，不能确认满足用户要求",
    "os_mismatch": "操作系统与用户要求不一致",
}

# Absolute demo ranges, not candidate-relative ranks or real benchmark measurements.
# Values saturate at the endpoints; RAM/storage receive no extra credit above 32/1024 GB.
NORMALIZATION_RANGES = {
    "cpu_score": (0, 100),
    "gpu_score": (0, 100),
    "ram_gb": (4, 32),
    "storage_gb": (128, 1024),
    "weight_g": (500, 5000),
    "battery_minutes": (30, 1440),
}
PERFORMANCE_WEIGHTS = {"cpu_score": 50, "gpu_score": 30, "ram_gb": 15, "storage_gb": 5}
# Each use profile contributes 60 points; the explicit priority adds 40, totaling 100.
# Neither these profiles nor the performance formula introduce eligibility constraints.
USE_WEIGHTS = {
    "office": {"performance": 12, "portability": 15, "battery": 15, "value": 18},
    "coding": {"performance": 24, "portability": 12, "battery": 12, "value": 12},
    "creative": {"performance": 30, "portability": 9, "battery": 9, "value": 12},
    "gaming": {"performance": 36, "portability": 6, "battery": 6, "value": 12},
}
PRIORITY_BONUS = 40


def validate_product(product: dict) -> dict:
    """Return a fresh, complete strict Product dump, rejecting boolean/integer coercion."""
    # Pydantic's Literal[True] may accept integer 1 even in an otherwise strict model.
    if type(product) is not dict or product.get("synthetic") is not True:
        raise DomainError("invalid_product", "商品必须为完整字典，且 synthetic 必须严格为 true")
    try:
        return Product.model_validate(product, strict=True).model_dump()
    except ValueError:
        raise DomainError("invalid_product", "商品不符合严格 Product 结构") from None


def _preferences(prefs: dict | Preferences) -> Preferences:
    # Revalidate model dumps as well: mutable model instances must not bypass strict checks.
    if isinstance(prefs, Preferences):
        prefs = prefs.model_dump()
    if type(prefs) is not dict:
        raise DomainError("invalid_preferences", "偏好必须为 Preferences 或严格字典")
    try:
        return Preferences.model_validate(prefs, strict=True)
    except ValueError:
        raise DomainError("invalid_preferences", "偏好不符合严格 Preferences 结构") from None


def _validate_now(now: int) -> None:
    if type(now) is not int or now < 0:
        raise DomainError("invalid_now", "当前时间必须为非负整数秒")


def _cost(product: dict) -> int:
    return product["price_cents"] + product["shipping_cents"]


def total_cents(product: dict) -> int:
    """Return price plus shipping, never a coerced or shipping-exclusive amount."""
    return _cost(validate_product(product))


def _match(product: dict, prefs: Preferences) -> list[str]:
    reasons = []
    if product["stock"] == 0:
        reasons.append("out_of_stock")
    if _cost(product) > prefs.budget_cents:
        reasons.append("over_budget")

    for field, minimum in (("ram_gb", prefs.min_ram_gb), ("storage_gb", prefs.min_storage_gb)):
        value = product[field]
        if value is None:
            reasons.append("unknown_" + field)
        elif value < minimum:
            reasons.append("insufficient_" + field)

    if prefs.max_weight_g is not None:
        if product["weight_g"] is None:
            reasons.append("unknown_weight_g")
        elif product["weight_g"] > prefs.max_weight_g:
            reasons.append("exceeds_max_weight_g")

    if prefs.min_battery_minutes is not None:
        if product["battery_minutes"] is None:
            reasons.append("unknown_battery_minutes")
        elif product["battery_minutes"] < prefs.min_battery_minutes:
            reasons.append("insufficient_battery_minutes")

    if prefs.needs_discrete_gpu:
        if product["discrete_gpu"] is None:
            reasons.append("unknown_discrete_gpu")
        elif product["discrete_gpu"] is not True:
            reasons.append("discrete_gpu_required")

    if prefs.os != "any":
        if product["os"] is None:
            reasons.append("unknown_os")
        elif product["os"] != prefs.os:
            reasons.append("os_mismatch")
    return reasons


def match_product(product: dict, prefs: dict | Preferences) -> list[str]:
    """List hard-condition failures; freshness belongs to the time-aware catalog functions."""
    return _match(validate_product(product), _preferences(prefs))


def _filter(products: list[dict], prefs: Preferences, now: int) -> dict:
    _validate_now(now)
    if type(products) is not list:
        raise DomainError("invalid_catalog", "商品目录必须为完整列表")
    eligible, excluded = [], []
    seen = set()
    for raw in products:
        product = validate_product(raw)
        product_id = product["id"]
        if product_id in seen:
            raise DomainError("duplicate_product_id", "商品目录中存在重复商品 id")
        seen.add(product_id)
        reasons = _match(product, prefs)
        if not 0 <= now - product["captured_at"] < SNAPSHOT_MAX_AGE_SECONDS:
            reasons.insert(0, "stale_snapshot")
        if reasons:
            excluded.append({"product_id": product_id, "reasons": reasons})
        else:
            eligible.append(product)
    return {"eligible": eligible, "excluded": excluded, "notice": NOTICE}


def filter_products(products: list[dict], prefs: dict | Preferences, now: int) -> dict:
    """Partition the full catalog without truncation, hidden preferences, or input mutation."""
    return _filter(products, _preferences(prefs), now)


def _normalize(value: int | None, minimum: int, maximum: int, *, reverse: bool = False) -> int:
    if value is None:
        return 0
    bounded = max(minimum, min(maximum, value))
    numerator = maximum - bounded if reverse else bounded - minimum
    return numerator * 100 // (maximum - minimum)


def _components(product: dict, prefs: Preferences) -> dict:
    normalized = {
        field: _normalize(product[field], *limits, reverse=field == "weight_g")
        for field, limits in NORMALIZATION_RANGES.items()
    }
    weights = dict(USE_WEIGHTS[prefs.use])
    weights[prefs.priority] += PRIORITY_BONUS
    return {
        "performance": sum(normalized[field] * weight for field, weight in PERFORMANCE_WEIGHTS.items()) // 100,
        "portability": normalized["weight_g"],
        "battery": normalized["battery_minutes"],
        # Value means budget headroom, not a claim about market price or real-world quality.
        "value": (prefs.budget_cents - _cost(product)) * 100 // prefs.budget_cents,
        "weights": weights,
        "normalized_specs": normalized,
        "unknown_specs": [field for field in NORMALIZATION_RANGES if product[field] is None],
    }


def evaluate_products(products: list[dict], prefs: dict | Preferences, now: int) -> dict:
    """Score every eligible row with integer demo-v1 rules, then sort deterministically."""
    preferences = _preferences(prefs)
    filtered = _filter(products, preferences, now)
    eligible = []
    for product in filtered["eligible"]:
        components = _components(product, preferences)
        score = sum(components[field] * weight for field, weight in components["weights"].items()) // 100
        eligible.append({
            "product": product,
            "total_cents": _cost(product),
            "score": score,
            "components": components,
        })
    eligible.sort(key=lambda item: (-item["score"], item["total_cents"], item["product"]["id"]))
    return {
        "eligible": eligible,
        "excluded": filtered["excluded"],
        "scoring_version": SCORING_VERSION,
        "notice": NOTICE,
    }
