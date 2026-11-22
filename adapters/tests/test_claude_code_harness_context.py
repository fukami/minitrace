"""Harness context from CC >= 2.1.260 attachments (prompt_snapshot & co).

Synthetic records only: real snapshots carry the operator's actual system
prompt and tool set and do not belong in this tree.

Loaded via importlib because the adapter lives under a hyphenated directory.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

_ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADAPTERS))
_spec = importlib.util.spec_from_file_location(
    "preprocess_claude_code", _ADAPTERS / "claude-code" / "preprocess_claude_code.py"
)
assert _spec is not None and _spec.loader is not None
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

SID = "3f0c1a2b-0000-4000-8000-000000000001"
BLOCKS = ["You are a coding agent.", "", "Use tools carefully.\n"]
SECRET_INSTRUCTIONS = "house rule: never deploy on Fridays"


def _att(sub: str, **kw) -> dict:
    return {"type": "attachment", "sessionId": SID, "uuid": f"a-{sub}-{len(kw)}",
            "timestamp": "2026-09-01T10:00:01.000Z", "attachment": {"type": sub, **kw}}


def _session(tmp_path: pathlib.Path, with_snapshots: bool) -> pathlib.Path:
    recs = [{"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": SID, "version": "2.1.263",
             "timestamp": "2026-09-01T10:00:00.000Z", "message": {"role": "user", "content": "hi"}}]
    if with_snapshots:
        recs += [
            _att("prompt_snapshot", systemPrompt=BLOCKS),
            _att("environment", snapshot={"platform": "darwin", "shell": "zsh"}),
            _att("instructions", files=[{"path": "/w/CLAUDE.md", "type": "project",
                                         "content": SECRET_INSTRUCTIONS}]),
            _att("prompt_snapshot", systemPrompt=BLOCKS, cliPrefix="cli",
                 tools=[{"name": "Read", "description": "read", "schema": {"type": "object"}},
                        {"name": "Bash", "description": "run", "schema": {"type": "object"}}]),
            _att("deferred_tools_record", entries=[
                {"name": "mcp__x__y", "description": "m", "input_schema": {"type": "object"}},
                {"name": "Read", "description": "dup", "input_schema": {}}]),
        ]
    p = tmp_path / f"{SID}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return p


def test_context_mapped_and_reversible(tmp_path):
    containers, fidelity = pp.build_containers_with_fidelity(_session(tmp_path, True))
    c = containers[0]
    assert c["system_prompt"] == "\n\n".join(BLOCKS)
    ext = c["ext_claude_code"]
    # the join is reversible from the recorded block lengths
    sp, out, pos = c["system_prompt"], [], 0
    for n in ext["system_prompt_block_lengths"]:
        out.append(sp[pos:pos + n]); pos += n + len(ext["system_prompt_join"])
    assert out == BLOCKS
    assert c["tools_enabled"] == ["Read", "Bash", "mcp__x__y"]
    assert [t["deferred"] for t in ext["tool_definitions"]] == [False, False, True]
    assert ext["snapshot_status"] == "recorded" and ext["cli_prefix"] == "cli"
    assert ext["environment_snapshots"] == [{"platform": "darwin", "shell": "zsh"}]
    assert ext["instruction_files"][0]["sha256"].startswith("sha256:")
    assert "system_prompt_variants" not in ext
    # hoisted records leave the event stream and never carry file content
    kinds = {e.get("attachment_subtype") for e in c["events"]}
    assert not kinds & pp.HOISTED_ATTACHMENTS
    assert SECRET_INSTRUCTIONS not in json.dumps(containers)


def test_older_session_declares_absence(tmp_path):
    containers, _ = pp.build_containers_with_fidelity(_session(tmp_path, False))
    c = containers[0]
    assert c["system_prompt"] is None and c["tools_enabled"] is None
    assert c["ext_claude_code"] == {"snapshot_status": "not_in_source"}


def test_differing_system_prompts_are_flagged_not_hidden():
    recs = [_att("prompt_snapshot", systemPrompt=["a"]), _att("prompt_snapshot", systemPrompt=["b"])]
    ctx = pp.collect_harness_context(recs)
    assert ctx["system_prompt"] == "a"
    assert ctx["ext_claude_code"]["system_prompt_variants"] == 2
