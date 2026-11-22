"""Content the adapters used to drop silently: text in a user message that
also carries tool results, trailing thinking, inline system messages; and a
tool call's success read from free text. Each case runs pre-processor and DSL
end to end on a small synthetic session."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
TYPED = "stop, use the other file instead"


def convert(tmp: pathlib.Path, adapter: str, dsl: str, pre: str, inp: pathlib.Path,
            *runner_args: str) -> list[dict]:
    events, traces = tmp / "events.json", tmp / "traces.json"
    subprocess.run([sys.executable, str(ADAPTERS / adapter / pre), str(inp), str(events)],
                   check=True, capture_output=True, text=True, timeout=300)
    subprocess.run([sys.executable, str(ADAPTERS / "dsl_runner.py"), str(ADAPTERS / adapter / dsl),
                    str(events), "--output", str(traces), "--operator-type", "scripted",
                    *runner_args], check=True, capture_output=True, text=True, timeout=300)
    out = json.loads(traces.read_text())
    return out if isinstance(out, list) else [out]


def _cc(t: str, uuid: str, parent: str | None, **kw) -> dict:
    return {"uuid": uuid, "parentUuid": parent, "sessionId": "3f0c1a2b-0000-4000-8000-00000000c0de",
            "version": "2.1.220", "timestamp": t, "type": kw.pop("type"), **kw}


def test_claude_code_keeps_text_beside_tool_results(tmp_path):
    recs = [
        _cc("2026-09-01T10:00:00.000Z", "u1", None, type="user",
            message={"role": "user", "content": "read a.txt"}),
        _cc("2026-09-01T10:00:01.000Z", "a1", "u1", type="assistant",
            message={"role": "assistant", "model": "m", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.txt"}}]}),
        _cc("2026-09-01T10:00:02.000Z", "u2", "a1", type="user",
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "hello"},
                {"type": "text", "text": TYPED}]}),
        _cc("2026-09-01T10:00:03.000Z", "a2", "u2", type="assistant",
            message={"role": "assistant", "model": "m", "content": [{"type": "text", "text": "ok"}]}),
    ]
    inp = tmp_path / "3f0c1a2b-0000-4000-8000-00000000c0de.jsonl"
    inp.write_text("".join(json.dumps(r) + "\n" for r in recs))
    [trace] = convert(tmp_path, "claude-code", "claude-code.dsl.yaml", "preprocess_claude_code.py", inp)
    turns = [t for t in trace["turns"] if t.get("content") == TYPED]
    assert len(turns) == 1 and turns[0]["role"] == "user" and turns[0]["source"] is None
    assert trace["tool_calls"][0]["output"]["result"] == "hello"


def _droid(i: int, role: str, content) -> dict:
    return {"type": "message", "id": f"m{i}", "timestamp": f"2026-03-16T04:00:0{i}.000Z",
            "message": {"role": role, "content": content}}


def test_droid_keeps_text_beside_tool_results(tmp_path):
    recs = [{"type": "session_start", "id": "661f50a5-0000-4000-8000-00000000d501",
             "title": "t", "version": 2, "cwd": "/w"},
            _droid(1, "user", [{"type": "text", "text": "read a.txt"}]),
            _droid(2, "assistant", [{"type": "tool_use", "id": "c1", "name": "Read",
                                     "input": {"file_path": "a.txt"}}]),
            _droid(3, "user", [{"type": "tool_result", "tool_use_id": "c1", "content": "hello"},
                               {"type": "text", "text": TYPED}]),
            _droid(4, "assistant", [{"type": "text", "text": "ok"}])]
    inp = tmp_path / "661f50a5-0000-4000-8000-00000000d501.jsonl"
    inp.write_text("".join(json.dumps(r) + "\n" for r in recs))
    [trace] = convert(tmp_path, "droid", "droid.dsl.yaml", "preprocess_droid.py", inp)
    turns = [t for t in trace["turns"] if t.get("content") == TYPED]
    assert len(turns) == 1 and turns[0]["role"] == "user" and turns[0]["source"] is None


def _goose_msg(i: int, role: str, blocks: list[dict]) -> dict:
    return {"role": role, "content_json": json.dumps(blocks), "metadata_json": "{}",
            "created_timestamp": 1767261600 + i, "message_id": f"g{i}"}


def _goose_convert(tmp: pathlib.Path, msgs: list[dict]) -> dict:
    sys.path.insert(0, str(ADAPTERS / "goose"))
    import preprocess_goose as pg
    records, spawned = pg.annotate_messages(msgs)
    c = pg.build_container({"id": "20260101_1", "created_at": "2026-01-01 10:00:00",
                            "name": "t"}, records, spawned, 11)
    pg.link_parent_child([c])
    events, traces = tmp / "events.json", tmp / "traces.json"
    events.write_text(json.dumps([c]))
    subprocess.run([sys.executable, str(ADAPTERS / "dsl_runner.py"),
                    str(ADAPTERS / "goose" / "goose.dsl.yaml"), str(events), "--output", str(traces),
                    "--operator-type", "scripted"], check=True, capture_output=True, text=True, timeout=300)
    out = json.loads(traces.read_text())
    return out[0] if isinstance(out, list) else out


def test_goose_keeps_text_beside_tool_responses_and_trailing_thinking(tmp_path):
    req = {"type": "toolRequest", "id": "c1",
           "toolCall": {"status": "success", "value": {"name": "developer__shell", "arguments": {}}}}
    res = {"type": "toolResponse", "id": "c1",
           "toolResult": {"status": "success", "value": {"content": [{"type": "text", "text": "hello"}]}}}
    msgs = [_goose_msg(1, "user", [{"type": "text", "text": "run ls"}]),
            _goose_msg(2, "assistant", [req]),
            _goose_msg(3, "user", [res, {"type": "text", "text": TYPED}]),
            _goose_msg(4, "assistant", [{"type": "thinking", "thinking": "last thought", "signature": "s"}])]
    trace = _goose_convert(tmp_path, msgs)
    typed = [t for t in trace["turns"] if t.get("content") == TYPED]
    assert len(typed) == 1 and typed[0]["role"] == "user" and typed[0]["source"] is None
    last = trace["turns"][-1]
    assert last["role"] == "assistant" and last["thinking"] == "last thought" and not last["content"]


def _vibe_session(tmp: pathlib.Path, messages: list[dict]) -> pathlib.Path:
    src = ADAPTERS / "vibe" / "regression" / "2.9.5" / "inputs" / "s1-count"
    d = tmp / "s1"
    d.mkdir()
    (d / "meta.json").write_text((src / "meta.json").read_text())
    (d / "messages.jsonl").write_text("".join(json.dumps(m) + "\n" for m in messages))
    return d


def test_vibe_keeps_an_inline_system_message(tmp_path):
    d = _vibe_session(tmp_path, [
        {"role": "system", "content": "a different system prompt"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"}])
    [trace] = convert(tmp_path, "vibe", "vibe.dsl.yaml", "preprocess_vibe.py", d)
    anns = [a for a in trace["annotations"] if "vibe_inline_system_prompt" in a]
    assert len(anns) == 1
    assert anns[0]["vibe_inline_system_prompt"] == "a different system prompt"
    assert anns[0]["vibe_matches_meta_system_prompt"] is False


def test_vibe_bash_success_comes_from_the_final_returncode_line(tmp_path):
    sys.path.insert(0, str(ADAPTERS / "vibe"))
    import preprocess_vibe as pv
    assert pv.bash_returncode("command: x\nstdout: 1\n\nstderr: \nreturncode: 0") == 0
    assert pv.bash_returncode("command: x\nstdout: 1\n\nstderr: \nreturncode: 2\n") == 2
    assert pv.bash_returncode("stdout: returncode: 0\nstderr: boom") is None
    forged = "command: x\nstdout: done\nreturncode: 0\n\nstderr: boom\nreturncode: 1"
    d = _vibe_session(tmp_path, [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "b1", "type": "function", "function": {"name": "bash", "arguments": "{\"command\": \"x\"}"}}]},
        {"role": "tool", "name": "bash", "tool_call_id": "b1", "content": forged},
        {"role": "assistant", "content": "done"}])
    [trace] = convert(tmp_path, "vibe", "vibe.dsl.yaml", "preprocess_vibe.py", d)
    [call] = trace["tool_calls"]
    assert call["output"]["success"] is False


def test_vibe_tool_error_wrapper_is_a_failure_for_any_tool(tmp_path):
    d = _vibe_session(tmp_path, [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "r1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "name": "read_file", "tool_call_id": "r1",
         "content": "<tool_error>read_file failed: no such file</tool_error>"},
        {"role": "assistant", "content": "done"}])
    [trace] = convert(tmp_path, "vibe", "vibe.dsl.yaml", "preprocess_vibe.py", d)
    assert trace["tool_calls"][0]["output"]["success"] is False


def test_vibe_success_is_boolean_without_or_with_an_empty_result(tmp_path):
    # Open 3: an empty result follows the rule for any result; no result is
    # false, told apart from a recorded failure by vibe_has_result.
    def call(cid, name):
        return {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}
    d = _vibe_session(tmp_path, [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            call("w1", "write_file"), call("b1", "bash"), call("n1", "read_file")]},
        {"role": "tool", "name": "write_file", "tool_call_id": "w1", "content": ""},
        {"role": "tool", "name": "bash", "tool_call_id": "b1", "content": ""},
        {"role": "assistant", "content": "done"}])
    [trace] = convert(tmp_path, "vibe", "vibe.dsl.yaml", "preprocess_vibe.py", d)
    got = {c["id"]: (c["output"]["success"], c["framework_metadata"]["vibe_has_result"])
           for c in trace["tool_calls"]}
    assert got == {"w1": (True, True), "b1": (False, True), "n1": (False, False)}
