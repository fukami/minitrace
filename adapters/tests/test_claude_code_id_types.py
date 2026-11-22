"""claude-code session and agent ids count only as strings (ruled 2026-10-27).

The session log decides every value's type. A `sessionId` or `agentId` that is
a number, a boolean, a list or an object was formatted into the trace id
(`claude-code-subagent-s1-True`, a dict repr), so a value that is no id named a
trace. Such a value now reads as absent: the agent id falls back to the next
record's string `agentId`, else the file stem; the session id to the next
record's string `sessionId`, else the existing `no-session-id` failure.

Loaded via importlib because the adapter lives under a hyphenated directory.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADAPTERS))
import source_reader as sr  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "preprocess_claude_code", _ADAPTERS / "claude-code" / "preprocess_claude_code.py"
)
assert _spec is not None and _spec.loader is not None
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

NOT_IDS = [7, 1.5, True, [1], {"x": 1}]


def _user(uuid: str, **kw) -> dict:
    return {"type": "user", "uuid": uuid, "parentUuid": None,
            "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"role": "user", "content": f"hello {uuid}"}, **kw}


def _spawn(prompt: str, **kw) -> dict:
    """A parent Agent call whose prompt is a subagent's spawn prompt."""
    return {"type": "assistant", "uuid": "t-spawn", "parentUuid": "u1",
            "timestamp": "2026-01-01T00:00:01.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Agent",
                 "input": {"prompt": prompt, "subagent_type": "general"}}]}, **kw}


def _spawn_links(value) -> list:
    """Every sub_session_id the parent's Agent calls were linked to."""
    if isinstance(value, dict):
        out = [value["sub_session_id"]] if "sub_session_id" in value else []
        return out + [x for v in value.values() for x in _spawn_links(v)]
    if isinstance(value, list):
        return [x for v in value for x in _spawn_links(v)]
    return []


def _session(tmp_path: pathlib.Path, parent: list[dict],
             subagent: list[dict] | None = None) -> pathlib.Path:
    p = tmp_path / "parent-file.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in parent))
    if subagent is not None:
        d = tmp_path / "parent-file" / "subagents"
        d.mkdir(parents=True)
        (d / "agent-stem.jsonl").write_text("".join(json.dumps(r) + "\n" for r in subagent))
    return p


def _subagent(containers: list[dict]) -> dict:
    (sa,) = [c for c in containers if c["kind"] == "subagent"]
    return sa


@pytest.mark.parametrize("bad", NOT_IDS, ids=repr)
def test_non_string_agent_id_falls_back_to_the_file_stem(tmp_path, bad):
    path = _session(tmp_path, [_user("u1", sessionId="s1"), _spawn("hello a1", sessionId="s1")],
                    [_user("a1", sessionId="s1", isSidechain=True, agentId=bad)])
    containers, _ = pp.build_containers_with_fidelity(path)
    sa = _subagent(containers)
    assert sa["agent_id"] == "agent-stem"
    assert sa["trace_id"] == "claude-code-subagent-s1-agent-stem"
    (summary,) = containers[0]["subagents"]
    assert summary["trace_id"] == sa["trace_id"] and summary["agent_id"] == "agent-stem"
    assert _spawn_links(containers[0]["events"]) == [sa["trace_id"]]


@pytest.mark.parametrize("bad", NOT_IDS, ids=repr)
def test_non_string_agent_id_before_a_string_one_is_skipped(tmp_path, bad):
    path = _session(tmp_path, [_user("u1", sessionId="s1"), _spawn("hello a1", sessionId="s1")],
                    [_user("a1", sessionId="s1", isSidechain=True, agentId=bad),
                     _user("a2", sessionId="s1", isSidechain=True, agentId="real")])
    containers, _ = pp.build_containers_with_fidelity(path)
    sa = _subagent(containers)
    assert sa["agent_id"] == "real"
    assert sa["trace_id"] == "claude-code-subagent-s1-real"
    assert containers[0]["subagents"][0]["trace_id"] == sa["trace_id"]
    # The parent's Agent call links to the same trace the subagent container is.
    assert _spawn_links(containers[0]["events"]) == [sa["trace_id"]]


@pytest.mark.parametrize("slug_on", [None, 0, 1], ids=["no slug", "slug first", "slug second"])
def test_the_first_string_agent_id_wins_in_both_passes(tmp_path, slug_on):
    """Two different string agentIds in one subagent file: the container and
    the parent's Agent-call link both take the first, wherever the slug is."""
    recs = [_user("a1", sessionId="s1", isSidechain=True, agentId="A"),
            _user("a2", sessionId="s1", isSidechain=True, agentId="B")]
    if slug_on is not None:
        recs[slug_on]["slug"] = "the-slug"
    path = _session(tmp_path, [_user("u1", sessionId="s1"), _spawn("hello a1", sessionId="s1")], recs)
    containers, _ = pp.build_containers_with_fidelity(path)
    sa = _subagent(containers)
    assert sa["agent_id"] == "A"
    assert sa["trace_id"] == "claude-code-subagent-s1-A"
    assert _spawn_links(containers[0]["events"]) == [sa["trace_id"]]
    assert sa["slug"] == (None if slug_on is None else "the-slug")


def test_a_non_string_agent_id_does_not_end_the_slug_search(tmp_path):
    """A number is no agent id, so it does not stop the scan: the slug is the
    one seen up to the first string agentId, or the last one when none is
    (before, the loop stopped at the number with the first slug)."""
    path = _session(tmp_path, [_user("u1", sessionId="s1")],
                    [_user("a1", sessionId="s1", isSidechain=True, agentId=5, slug="x"),
                     _user("a2", sessionId="s1", isSidechain=True, slug="y")])
    containers, _ = pp.build_containers_with_fidelity(path)
    sa = _subagent(containers)
    assert sa["agent_id"] == "agent-stem"
    assert sa["slug"] == "y"


@pytest.mark.parametrize("bad", NOT_IDS, ids=repr)
def test_non_string_session_id_before_a_string_one_is_skipped(tmp_path, bad):
    path = _session(tmp_path, [_user("u1", sessionId=bad), _user("u2", sessionId="s1")],
                    [_user("a1", sessionId="s1", isSidechain=True, agentId="real")])
    containers, _ = pp.build_containers_with_fidelity(path)
    parent = containers[0]
    assert parent["session_id"] == "s1"
    assert parent["trace_id"] == "claude-code-agent-s1"
    assert _subagent(containers)["trace_id"] == "claude-code-subagent-s1-real"


@pytest.mark.parametrize("bad", NOT_IDS, ids=repr)
def test_only_non_string_session_ids_fail_closed(tmp_path, bad):
    path = _session(tmp_path, [_user("u1", sessionId=bad), _user("u2", sessionId=bad)])
    with pytest.raises(sr.SourceUnreadable) as exc:
        pp.build_containers_with_fidelity(path)
    assert exc.value.reason == "no-session-id"


def test_string_ids_are_used_as_written(tmp_path):
    path = _session(tmp_path, [_user("u1", sessionId="s1"), _spawn("hello a1", sessionId="s1")],
                    [_user("a1", sessionId="s1", isSidechain=True, agentId="a1")])
    containers, _ = pp.build_containers_with_fidelity(path)
    assert containers[0]["trace_id"] == "claude-code-agent-s1"
    assert _subagent(containers)["trace_id"] == "claude-code-subagent-s1-a1"
    assert _spawn_links(containers[0]["events"]) == ["claude-code-subagent-s1-a1"]
