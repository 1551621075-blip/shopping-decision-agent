"""Deterministic local TEST keyword policy; 128 synthetic tokens are not model usage.

This is a test double, not an LLM or a natural-language understanding benchmark.
It performs no I/O, retains no session state and never confirms preferences.
The runtime remains responsible for authorization and checking evidence integrity.
"""

from __future__ import annotations

import re
from decimal import Decimal

from .provider import ProviderError, ProviderReply
from .schemas import Preferences, digest, validate_action
from .tools import QUESTIONS

SYNTHETIC_TOKENS = 128
POLICY_VERSION = "shopping-keywords-v1"
USE_WORDS = {
    "office": ("办公", "office"), "coding": ("编程", "开发", "coding"),
    "creative": ("创作", "剪辑", "creative"), "gaming": ("游戏", "gaming"),
}
PRIORITY_WORDS = {
    "value": ("价格", "性价比", "value"), "performance": ("性能", "performance"),
    "portability": ("便携", "轻便", "portability"), "battery": ("续航", "battery"),
}
MONEY = re.compile(r"(?<![\d.])([+-]?\d{1,9}(?:\.\d{1,4})?)\s*(万元|万|千元|千|k|元)")
SPEC_PATTERNS = {
    "min_ram_gb": r"(?:至少|最低)?\s*(\d+)\s*gb\s*内存|内存\s*(?:至少|最低)?\s*(\d+)\s*gb",
    "min_storage_gb": r"(?:至少|最低)?\s*(\d+)\s*gb\s*(?:存储|硬盘)|(?:存储|硬盘)\s*(?:至少|最低)?\s*(\d+)\s*gb",
    "max_weight_g": r"重量\s*(?:最多|不超过|至多|上限)\s*(\d+)\s*(?:克|g)",
    "min_battery_minutes": r"续航(?:指标)?\s*(?:至少|最低)\s*(\d+)\s*分钟",
}


def _keywords(text: str, options: dict) -> list[str]:
    return [key for key, words in options.items() if any(word in text for word in words)]


def _draft(messages: list[dict]) -> tuple[dict | None, int | None]:
    """A deliberately small syntax; unsupported/invalid values require a shopper answer."""
    values: dict = {}
    bad_budget, ambiguous_use = False, False
    for message in messages:
        if message.get("role") != "customer" or type(message.get("text")) is not str:
            continue
        text = message["text"].lower()
        amounts = MONEY.findall(text)
        if amounts:
            parsed = set()
            for number, unit in amounts:
                multiplier = 1000000 if unit.startswith("万") else 100000 if unit in {"千", "千元", "k"} else 100
                amount = Decimal(number) * multiplier
                if amount == int(amount) and 10000 <= amount <= 10000000:
                    parsed.add(int(amount))
            bad_budget = len(parsed) != 1 or len(amounts) != 1
            if not bad_budget:
                values["budget_cents"] = parsed.pop()
        uses = _keywords(text, USE_WORDS)
        if uses:
            ambiguous_use = len(uses) != 1
            if not ambiguous_use:
                values["use"] = uses[0]
        # A mentioned hard battery/weight threshold alone is not a soft priority.
        priority_text = re.search(r"(?:更看重|优先|注重|看重)\s*([^，。；;]+)", text)
        if priority_text:
            priorities = _keywords(priority_text[1], PRIORITY_WORDS)
            if len(priorities) == 1:
                values["priority"] = priorities[0]
        for field, pattern in SPEC_PATTERNS.items():
            matches = list(re.finditer(pattern, text))
            if matches:
                values[field] = int(next(part for part in matches[-1].groups() if part is not None))
        if re.search(r"(?:必须|需要|要有)\s*独显", text):
            values["needs_discrete_gpu"] = True
        if re.search(r"(?:不需要|无需)\s*独显", text):
            values["needs_discrete_gpu"] = False
        systems = [name for name in ("windows", "linux", "macos") if name in text]
        if len(systems) == 1:
            values["os"] = systems[0]
        elif len(systems) > 1:
            return None, 2
    if bad_budget or "budget_cents" not in values:
        return None, 0
    if ambiguous_use or "use" not in values:
        return None, 1
    try:
        return Preferences.model_validate(values).model_dump(), None
    except ValueError:
        return None, 2


def _fresh_observation(context: dict, item: dict, preferences: dict, confirmed: bool) -> dict | None:
    obj = item.get("observation", {})
    payload = obj.get("payload", {})
    now = context.get("now")
    captured, expires = payload.get("captured_at"), payload.get("expires_at")
    if (type(obj.get("id")) is not str or type(payload.get("data")) is not dict
            or payload.get("tool") != item.get("tool")
            or payload.get("session_id") != context.get("session_id")
            or payload.get("preferences") != preferences
            or payload.get("bindings", {}).get("preferences_revision") != context.get("preferences_revision")
            or type(now) is not int or type(captured) is not int or type(expires) is not int
            or not captured <= now < expires):
        return None
    if item.get("tool") in {"search_products", "compare_products"} and payload.get("confirmed") is not confirmed:
        return None
    return obj


class DemoProvider:
    """No-argument, stateless TEST replacement with explicitly synthetic token accounting."""

    is_test_provider = True
    model = "TEST"
    usage_kind = "synthetic"
    fingerprint = "test-demo-" + digest({
        "policy": POLICY_VERSION, "model": model, "usage_kind": usage_kind,
        "tokens": SYNTHETIC_TOKENS, "questions": QUESTIONS,
        "uses": USE_WORDS, "priorities": PRIORITY_WORDS, "spec_patterns": SPEC_PATTERNS,
    })

    def decide(self, context: dict, schema: dict) -> ProviderReply:
        if type(context) is not dict or type(schema) is not dict or not schema.get("anyOf"):
            raise ProviderError("model_context", sent=False)
        action = self._choose(context)
        # Fail closed if the caller supplies a different action vocabulary.
        try:
            allowed = {branch["properties"]["tool"]["const"] for branch in schema["anyOf"]}
            if action["tool"] not in allowed:
                raise ValueError
            name, arguments = validate_action(action)
        except (KeyError, TypeError, ValueError):
            raise ProviderError("model_context", sent=False) from None
        return ProviderReply({"tool": name, "arguments": arguments}, SYNTHETIC_TOKENS, self.model)

    @staticmethod
    def _action(tool: str, **arguments) -> dict:
        return {"tool": tool, "arguments": arguments}

    def _ask(self, index: int) -> dict:
        return self._action("ask_shopper", question=QUESTIONS[index])

    def _choose(self, context: dict) -> dict:
        confirmed = context.get("preferences") is not None and context.get("preferences_confirmed") is True
        if confirmed:
            try:
                preferences = Preferences.model_validate(context["preferences"]).model_dump()
            except ValueError:
                return self._ask(-1)
        else:
            preferences, question = _draft(context.get("messages", []))
            if question is not None:
                return self._ask(question)
        search = self._action("search_products", preferences=None if confirmed else preferences)
        observations = context.get("observations", [])
        if not observations:
            return search
        last = observations[-1]
        error = last.get("observation", {}).get("error")
        if error:
            return search if error in {"stale_evidence", "invalid_evidence", "not_in_search"} else self._ask(-1)
        obj = _fresh_observation(context, last, preferences, confirmed)
        if obj is None:
            return search
        tool, data = last["tool"], obj["payload"]["data"]
        if tool == "search_products":
            products = data["eligible"]
            if not products:
                return self._action("respond", kind="no_match", text=QUESTIONS[4], evidence_ids=[obj["id"]])
            if not confirmed:
                return self._action("propose_preferences", preferences=preferences)
            # This TEST policy takes the observed catalog order, not an oracle rank.
            # Formal scoring applies only to this <=6-item shortlist, not the whole market.
            return self._action("compare_products", search_id=obj["id"],
                                product_ids=[product["id"] for product in products[:6]])
        if not confirmed:
            return self._action("propose_preferences", preferences=preferences)
        if tool == "compare_products":
            return self._action("evaluate_shortlist", comparison_id=obj["id"])
        if tool == "evaluate_shortlist" and data.get("eligible"):
            row = data["eligible"][0]
            strengths = ["total_cents"]
            for metric in ("ram_gb", "storage_gb", "cpu_score"):
                if type(row["product"].get(metric)) is int:
                    strengths.append(metric)
            return self._action("recommend", evaluation_id=obj["id"], product_id=row["product"]["id"],
                                strengths=strengths, tradeoffs=[])
        return search
