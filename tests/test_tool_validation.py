from typing import Any

from miniclaw.agent.tools.base import Tool
from miniclaw.agent.tools.registry import ToolRegistry
from miniclaw.config.schema import ToolApprovalConfig


class SampleTool(Tool):
    @property
    def name(self) -> str:
        return "sample"

    @property
    def description(self) -> str:
        return "sample tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2},
                "count": {"type": "integer", "minimum": 1, "maximum": 10},
                "mode": {"type": "string", "enum": ["fast", "full"]},
                "meta": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string"},
                        "flags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["tag"],
                },
            },
            "required": ["query", "count"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class ApplyPatchSampleTool(Tool):
    @property
    def name(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return "patch tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"patch": {"type": "string"}},
            "required": ["patch"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return "patched"


def test_validate_params_missing_required() -> None:
    tool = SampleTool()
    errors = tool.validate_params({"query": "hi"})
    assert "missing required count" in "; ".join(errors)


def test_validate_params_type_and_range() -> None:
    tool = SampleTool()
    errors = tool.validate_params({"query": "hi", "count": 0})
    assert any("count must be >= 1" in e for e in errors)

    errors = tool.validate_params({"query": "hi", "count": "2"})
    assert any("count should be integer" in e for e in errors)


def test_validate_params_enum_and_min_length() -> None:
    tool = SampleTool()
    errors = tool.validate_params({"query": "h", "count": 2, "mode": "slow"})
    assert any("query must be at least 2 chars" in e for e in errors)
    assert any("mode must be one of" in e for e in errors)


def test_validate_params_nested_object_and_array() -> None:
    tool = SampleTool()
    errors = tool.validate_params(
        {
            "query": "hi",
            "count": 2,
            "meta": {"flags": [1, "ok"]},
        }
    )
    assert any("missing required meta.tag" in e for e in errors)
    assert any("meta.flags[0] should be string" in e for e in errors)


def test_validate_params_ignores_unknown_fields() -> None:
    tool = SampleTool()
    errors = tool.validate_params({"query": "hi", "count": 2, "extra": "x"})
    assert errors == []


async def test_registry_returns_validation_error() -> None:
    reg = ToolRegistry()
    reg.register(SampleTool())
    result = await reg.execute("sample", {"query": "hi"})
    assert "Invalid parameters" in result


async def test_registry_unclassified_tool_defaults_to_allow() -> None:
    reg = ToolRegistry(
        approval_config=ToolApprovalConfig(
            exec="always_ask",
            browser="always_ask",
            web_fetch="always_ask",
            write_file="always_ask",
        )
    )
    reg.register(SampleTool())
    result = await reg.execute("sample", {"query": "hello", "count": 2})
    assert result == "ok"


async def test_registry_apply_patch_uses_write_file_approval_mode() -> None:
    reg = ToolRegistry(
        approval_config=ToolApprovalConfig(
            exec="always_allow",
            browser="always_allow",
            web_fetch="always_allow",
            write_file="always_ask",
        )
    )
    reg.register(ApplyPatchSampleTool())
    result = await reg.execute("apply_patch", {"patch": "*** Begin Patch\n*** End Patch"})
    assert "denied" in result
