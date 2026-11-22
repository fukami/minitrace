"""MCP tool-name classification in the claude-code pre-processor.

Coverage-contract cell 27: a native MCP invocation appears in the JSONL only as a
`tool_use` block named `mcp__<server>__<tool>`. Before this refinement the bare
name fell through `operation_types: default -> OTHER` with server/tool unparsed.
`mcp_framework_metadata` splits them into the ToolCall `framework_metadata` slot
without touching `tool_name` (stays the framework name) or the OTHER op (the name
carries no read/write/execute semantics).

Loaded via importlib because the adapter lives under a hyphenated directory
(`claude-code/`) that is not a valid Python module path.
"""
from __future__ import annotations

import importlib.util
import pathlib

_PP = (
    pathlib.Path(__file__).resolve().parent.parent
    / "claude-code"
    / "preprocess_claude_code.py"
)
_spec = importlib.util.spec_from_file_location("preprocess_claude_code", _PP)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
mcp_framework_metadata = _mod.mcp_framework_metadata


def test_plain_mcp_name_splits_server_and_tool():
    assert mcp_framework_metadata("mcp__github__list_issues") == {
        "mcp_server": "github",
        "mcp_tool": "list_issues",
    }


def test_tool_segment_keeps_trailing_double_underscore():
    # server is always the first segment after the prefix; the rest is the tool,
    # so a tool name that itself contains `__` is preserved intact.
    assert mcp_framework_metadata("mcp__linear__create__issue") == {
        "mcp_server": "linear",
        "mcp_tool": "create__issue",
    }


def test_non_mcp_tool_returns_none():
    for name in ("Read", "Bash", "WebFetch", "Skill"):
        assert mcp_framework_metadata(name) is None


def test_malformed_mcp_names_stay_unclassified():
    # No tool segment, empty segments, or non-string -> leave as OTHER (None).
    for name in ("mcp__github", "mcp__", "mcp____tool", "mcp__server__", None, 42):
        assert mcp_framework_metadata(name) is None
