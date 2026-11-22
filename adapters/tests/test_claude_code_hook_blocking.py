"""Hook blocking errors reach the trace.

Claude Code records a blocking hook message (PostToolUse feedback such as the
truncation or locator checks, exit code 2) as an `attachment` record of type
`hook_blocking_error`, with hookName, hookEvent, toolUseID and
blockingError.{blockingError, command}. The adapter had no rule for that
subtype, so every such message was dropped and hook friction could not be
counted from converted traces. Synthetic session only.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from schema_gate import schema_errors  # noqa: E402
from test_content_loss import _cc, convert  # noqa: E402

SID = "3f0c1a2b-0000-4000-8000-00000000c0de"
TEXT = "[check.sh]: TRUNCATED OUTPUT — completeness is UNKNOWN"
CMD = "$HOME/repo/runbook/check.sh"


def _blocking(t: str, uuid: str, parent: str, tool_use_id: str | None) -> dict:
    att = {"type": "hook_blocking_error", "hookName": "PostToolUse:Bash",
           "hookEvent": "PostToolUse",
           "blockingError": {"blockingError": TEXT, "command": CMD}}
    if tool_use_id is not None:
        att["toolUseID"] = tool_use_id
    return _cc(t, uuid, parent, type="attachment", attachment=att)


def _trace(tmp_path: pathlib.Path) -> dict:
    recs = [
        _cc("2026-09-01T10:00:00.000Z", "u1", None, type="user",
            message={"role": "user", "content": "count the rows"}),
        _cc("2026-09-01T10:00:01.000Z", "a1", "u1", type="assistant",
            message={"role": "assistant", "model": "m", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                 "input": {"command": "seq 100 | head -5"}}]}),
        _cc("2026-09-01T10:00:02.000Z", "u2", "a1", type="user",
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "1\n2\n3\n4\n5"}]}),
        _blocking("2026-09-01T10:00:02.500Z", "b1", "u2", "toolu_1"),
        _blocking("2026-09-01T10:00:02.600Z", "b2", "b1", None),
        _cc("2026-09-01T10:00:03.000Z", "a2", "b2", type="assistant",
            message={"role": "assistant", "model": "m", "content": [{"type": "text", "text": "ok"}]}),
    ]
    inp = tmp_path / f"{SID}.jsonl"
    inp.write_text("".join(json.dumps(r) + "\n" for r in recs))
    [trace] = convert(tmp_path, "claude-code", "claude-code.dsl.yaml",
                      "preprocess_claude_code.py", inp)
    return trace


def _blocking_annotations(trace: dict) -> list[dict]:
    return [a for a in trace.get("annotations", [])
            if any("/hook-blocking-error/" in t for t in a.get("taxonomy", []))]


def test_blocking_error_is_annotated_on_its_tool_call(tmp_path):
    trace = _trace(tmp_path)
    anns = _blocking_annotations(trace)
    assert len(anns) == 2
    on_call, on_trace = anns
    assert on_call["scope"] == {"type": "tool_call", "target_id": "toolu_1"}
    assert on_call["content"]["text"] == TEXT
    assert on_call["ext_claude_code_hook_command"] == CMD
    assert "claude-code/2.1.220/hook-blocking-error/PostToolUse:Bash" in on_call["taxonomy"]
    assert "claude-code/2.1.220/hook/PostToolUse" in on_call["taxonomy"]
    assert on_call["timestamp"].startswith("2026-09-01T10:00:02.5")
    # without a toolUseID the annotation falls back to the trace
    assert on_trace["scope"]["type"] == "trace"
    assert on_trace["content"]["text"] == TEXT
    assert trace["tool_calls"][0]["id"] == "toolu_1"


def test_trace_with_blocking_errors_is_schema_valid(tmp_path):
    assert schema_errors(_trace(tmp_path)) == []
