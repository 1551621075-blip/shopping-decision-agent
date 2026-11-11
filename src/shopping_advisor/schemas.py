from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Version = Annotated[int, Field(strict=True, ge=1)]
Money = Annotated[int, Field(strict=True, ge=0, le=10000000)]
Use = Literal["office", "coding", "creative", "gaming"]
Priority = Literal["value", "performance", "portability", "battery"]
Metric = Literal["total_cents", "ram_gb", "storage_gb", "weight_g", "battery_minutes", "cpu_score", "gpu_score"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex


class Principal(StrictModel):
    user_id: Identifier
    role: Literal["customer", "operator"]


class Preferences(StrictModel):
    budget_cents: Annotated[int, Field(strict=True, ge=10000, le=10000000)]
    use: Use
    priority: Priority = "value"
    min_ram_gb: Annotated[int, Field(strict=True, ge=4, le=128)] = 8
    min_storage_gb: Annotated[int, Field(strict=True, ge=128, le=8192)] = 256
    max_weight_g: Annotated[int, Field(strict=True, ge=500, le=5000)] | None = None
    min_battery_minutes: Annotated[int, Field(strict=True, ge=30, le=1440)] | None = None
    needs_discrete_gpu: bool = False
    os: Literal["any", "windows", "linux", "macos"] = "any"


class Product(StrictModel):
    id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=100)]
    category: Literal["laptop"]
    synthetic: Literal[True]
    price_cents: Money
    shipping_cents: Money
    stock: Annotated[int, Field(strict=True, ge=0, le=10000)]
    ram_gb: Annotated[int, Field(strict=True, ge=4, le=128)] | None
    storage_gb: Annotated[int, Field(strict=True, ge=128, le=8192)] | None
    weight_g: Annotated[int, Field(strict=True, ge=500, le=5000)] | None
    battery_minutes: Annotated[int, Field(strict=True, ge=30, le=1440)] | None
    cpu_score: Annotated[int, Field(strict=True, ge=0, le=100)] | None
    gpu_score: Annotated[int, Field(strict=True, ge=0, le=100)] | None
    discrete_gpu: bool | None
    os: Literal["windows", "linux", "macos"] | None
    captured_at: Annotated[int, Field(strict=True, ge=0)]


class Command(StrictModel):
    action_id: Identifier


class VersionedCommand(Command):
    expected_version: Version


class MessageCommand(VersionedCommand):
    text: Text


class ProposalCommand(VersionedCommand):
    content_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    decision: Literal["confirm", "decline"]


class PreferencesCommand(VersionedCommand):
    preferences: Preferences


class ProductCommand(VersionedCommand):
    price_cents: Money
    stock: Annotated[int, Field(strict=True, ge=0, le=10000)]


class ReconcileCommand(Command):
    call_id: Identifier
    tokens: Annotated[int, Field(strict=True, ge=0, le=10000000)] | None = None
    note: Text
    outcome: Literal["no_usable_result"]


class EmptyArgs(StrictModel):
    pass


class AskArgs(StrictModel):
    question: Text


class PreferencesArgs(StrictModel):
    preferences: Preferences


class SearchArgs(StrictModel):
    preferences: Preferences | None = None


class CompareArgs(StrictModel):
    search_id: Identifier
    product_ids: list[Identifier] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.product_ids)) != len(self.product_ids):
            raise ValueError("商品不能重复")
        return self


class EvaluateArgs(StrictModel):
    comparison_id: Identifier


class Tradeoff(StrictModel):
    against_product_id: Identifier
    metric: Metric


class RecommendArgs(StrictModel):
    evaluation_id: Identifier
    product_id: Identifier
    strengths: list[Metric] = Field(min_length=1, max_length=4)
    tradeoffs: list[Tradeoff] = Field(max_length=4)


class RespondArgs(StrictModel):
    kind: Literal["facts", "no_match", "guidance"]
    text: Annotated[str, Field(max_length=2000)]
    evidence_ids: list[Identifier] = Field(max_length=10)


TOOL_ARGUMENTS = {
    "read_context": EmptyArgs, "ask_shopper": AskArgs, "propose_preferences": PreferencesArgs,
    "search_products": SearchArgs, "compare_products": CompareArgs,
    "evaluate_shortlist": EvaluateArgs, "recommend": RecommendArgs, "respond": RespondArgs,
}


def validate_action(value: object) -> tuple[str, dict]:
    if type(value) is not dict or set(value) != {"tool", "arguments"}:
        raise DomainError("invalid_action", "动作必须只包含 tool 和 arguments")
    name = value["tool"]
    if type(name) is not str or name not in TOOL_ARGUMENTS:
        raise DomainError("invalid_tool", "工具不在允许范围内")
    try:
        args = TOOL_ARGUMENTS[name].model_validate(value["arguments"]).model_dump()
    except ValueError:
        raise DomainError("invalid_arguments", "工具参数不符合严格结构") from None
    return name, args


def action_schema() -> dict:
    branches, definitions = [], {}
    for name, args in TOOL_ARGUMENTS.items():
        arguments = args.model_json_schema()
        for key, value in arguments.pop("$defs", {}).items():
            if key in definitions and definitions[key] != value:
                raise ValueError("Conflicting tool schema definitions")
            definitions[key] = value
        branches.append({
            "type": "object", "additionalProperties": False,
            "properties": {"tool": {"type": "string", "const": name}, "arguments": arguments},
            "required": ["tool", "arguments"],
        })
    return {"anyOf": branches, "$defs": definitions}
