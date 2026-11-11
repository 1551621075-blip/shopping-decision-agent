import pytest

from shopping_advisor.schemas import DomainError, Preferences, action_schema, validate_action


def test_all_action_schema_local_references_resolve_from_document_root():
    schema = action_schema()

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            if "$ref" in node:
                target = schema
                assert node["$ref"].startswith("#/")
                for piece in node["$ref"][2:].split("/"):
                    target = target[piece]
                assert isinstance(target, dict)
            for value in node.values():
                visit(value)

    visit(schema)


@pytest.mark.parametrize("value", [True, 600000.0, "600000", None, -1])
def test_budget_requires_strict_integer(value):
    with pytest.raises(ValueError):
        Preferences(budget_cents=value, use="coding")


@pytest.mark.parametrize("action", [None, [], {}, {"tool": "buy", "arguments": {}},
                                    {"tool": "read_context", "arguments": {"owner": "bob"}},
                                    {"tool": "read_context", "arguments": {}, "user": "bob"}])
def test_invalid_action_envelopes_rejected(action):
    with pytest.raises(DomainError):
        validate_action(action)
