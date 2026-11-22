"""claude.ai adapter, format snapshot 2026-09-26.

A synthetic conversation (tests/fixtures/claude-ai-2026-09-26-probe.json, probe
strings only, no real content) runs through the pre-processor and the runner
with the new DSL and, for contrast, with the unchanged 2026-05-03 DSL.

Checks the five drift items of the claude.ai export drift note (2026-09-26):
turn content from text blocks (no rendered tool material), all thinking blocks
on the turn, injected prompts as annotations, real tool ids with synthetic
fallback and positional pairing, tool_origin kept. Also a canary for the
runner's silent string-literal fallback: no output string may contain DSL
expression text.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

_ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
_FIXTURE = _ADAPTERS / "tests" / "fixtures" / "claude-ai-2026-09-26-probe.json"
# Scripted: the runner refuses the scrubbed mode it does not perform (ruled
# 2026-10-27), and these tests read content, not the operator.
_OPERATOR = ["--operator-type", "scripted"]


def _convert(tmp_path: pathlib.Path, dsl: str) -> dict:
    pre = tmp_path / f"pre-{dsl}.json"
    out = tmp_path / f"out-{dsl}.json"
    subprocess.run([sys.executable, str(_ADAPTERS / "claude-ai" / "preprocess_claude_ai.py"), str(_FIXTURE), str(pre)],
                   check=True, capture_output=True, timeout=300)
    subprocess.run([sys.executable, str(_ADAPTERS / "dsl_runner.py"), str(_ADAPTERS / "claude-ai" / dsl), str(pre),
                    "--output", str(out), *_OPERATOR], check=True, capture_output=True, timeout=300)
    traces = json.loads(out.read_text())
    assert isinstance(traces, list) and len(traces) == 1
    return traces[0]


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


@pytest.fixture(scope="module")
def new(tmp_path_factory):
    return _convert(tmp_path_factory.mktemp("new"), "2026-09-26.dsl.yaml")


@pytest.fixture(scope="module")
def old(tmp_path_factory):
    return _convert(tmp_path_factory.mktemp("old"), "2026-05-03.dsl.yaml")


def test_turn_content_from_text_blocks(new):
    contents = [t["content"] for t in new["turns"]]
    assert contents == ["HUMAN-TEXT", "ASSIST-A\nASSIST-B", "RENDERED-ONLY", "HUMAN-2"]
    assert not any("RENDERED-TOOL" in c for c in contents)


def test_thinking_all_blocks(new):
    assert [t.get("thinking") for t in new["turns"]] == [None, "THINK-1\nTHINK-2", None, None]


def test_injected_prompt_is_annotation(new):
    anns = new.get("annotations") or []
    assert len(anns) == 1
    a = anns[0]
    assert a["content"]["text"] == "INJECTED-PROMPT"
    assert a["content"]["injection_source"] == "memory_block_head"
    assert a["taxonomy"] == ["claude-ai/2026-09-26/injected-prompt/memory_block_head"]
    assert not any(t["content"] == "INJECTED-PROMPT" for t in new["turns"])


def test_tool_ids_pairing_and_origin(new):
    tcs = new["tool_calls"]
    assert [tc["id"] for tc in tcs] == ["toolu_REAL", "claude-ai-tu-2"]
    assert [tc["output"]["result"] for tc in tcs] == ["RESULT-1", "RESULT-2"]
    assert [tc["output"]["success"] for tc in tcs] == [True, False]
    assert [tc["emitting_turn_index"] for tc in tcs] == [1, 2]
    assert tcs[0]["framework_metadata"]["tool_origin"] == "ORIGIN-1"
    # a tool_use without start_timestamp takes its message's created_at
    assert [tc["timestamp"] for tc in tcs] == ["2026-09-20T10:01:01.000000Z", "2026-09-20T10:02:00.000000Z"]


def test_no_expression_text_leaks(new):
    leaked = [s for s in _strings(new) if "$block" in s or "$record" in s or "blocks_join" in s or "coalesce(" in s]
    assert leaked == []


def test_old_dsl_unchanged(old):
    # the 2026-05-03 DSL keeps its documented behaviour on the same input
    assert old["turns"][1]["content"].count("RENDERED-TOOL") == 1
    assert not (old.get("annotations") or [])
    # Ruled 2026-10-21: the call keeps the block's own id when it has one,
    # as in 2026-09-26, so a tool_result's tool_use_id can find it.
    assert [tc["id"] for tc in old["tool_calls"]] == ["toolu_REAL", "claude-ai-tu-2"]
