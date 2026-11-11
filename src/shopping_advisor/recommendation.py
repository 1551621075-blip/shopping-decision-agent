"""Render and revalidate recommendation facts from a verified evaluation."""
from __future__ import annotations

from .schemas import DomainError, RecommendArgs

METRICS = {
    "total_cents": ("含运费总价（分）", -1), "ram_gb": ("内存（GB）", 1),
    "storage_gb": ("存储（GB）", 1), "weight_g": ("重量（克）", -1),
    "battery_minutes": ("合成续航指标（分钟）", 1), "cpu_score": ("合成CPU指标", 1),
    "gpu_score": ("合成GPU指标", 1),
}
NOTICE = "仅比较原创合成目录，不是实时价格、真实测评或全市场最优；未下单或付款。"


def _metric(row, metric):
    value = row["total_cents"] if metric == "total_cents" else row["product"][metric]
    if value is None:
        raise DomainError("unknown_metric", "未知规格不能作为推荐事实或取舍依据")
    return value


def render_recommendation(arguments, evaluation):
    try:
        args = RecommendArgs.model_validate(arguments).model_dump()
    except ValueError:
        raise DomainError("invalid_recommendation", "推荐选择结构异常") from None
    if args["evaluation_id"] != evaluation["id"]:
        raise DomainError("invalid_recommendation", "推荐引用与评估不一致")
    rows = evaluation["payload"]["data"]["eligible"]
    by_id = {row["product"]["id"]: row for row in rows}
    if args["product_id"] not in by_id:
        raise DomainError("ineligible_product", "只能推荐评估中满足全部硬条件的候选")
    selected = by_id[args["product_id"]]
    if len(set(args["strengths"])) != len(args["strengths"]):
        raise DomainError("duplicate_metric", "推荐指标不能重复")
    facts = [{"metric": metric, "label": METRICS[metric][0], "value": _metric(selected, metric)}
             for metric in args["strengths"]]
    comparisons, seen = [], set()
    for choice in args["tradeoffs"]:
        opponent_id, metric = choice["against_product_id"], choice["metric"]
        if opponent_id not in by_id or opponent_id == args["product_id"] or (opponent_id, metric) in seen:
            raise DomainError("invalid_tradeoff", "取舍必须引用本次评估的另一商品且不能重复")
        seen.add((opponent_id, metric))
        value, other = _metric(selected, metric), _metric(by_id[opponent_id], metric)
        direction = (value - other) * METRICS[metric][1]
        comparisons.append({"against_product_id": opponent_id, "metric": metric,
                            "label": METRICS[metric][0], "selected_value": value, "other_value": other,
                            "relation": "better" if direction > 0 else "worse" if direction < 0 else "equal"})
    top_id = rows[0]["product"]["id"]
    if args["product_id"] != top_id and not any(
            row["against_product_id"] == top_id and row["relation"] == "better" for row in comparisons):
        raise DomainError("unsupported_tradeoff", "选择非最高分候选需提供相对最高分候选更优的真实指标取舍")
    return {"decision": args, "evaluation_id": evaluation["id"],
            "preferences": evaluation["payload"]["preferences"], "selection": selected,
            "facts": facts, "tradeoffs": comparisons, "top_product_id": top_id,
            "scoring_version": evaluation["payload"]["data"]["scoring_version"],
            "model_text_published": False, "notice": NOTICE}
