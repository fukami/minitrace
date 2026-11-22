"""Regression tests for defects found by generative and adversarial testing
of the DSL runner, the readers and the cross-trace pass.

Each test pins one defect with the smallest input that reproduced it.
Self-running, so the fleet sweep executes it as a script.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# Pre-processor directories, once per module and appended, so they never
# shadow a module another test file imports.
for _sub in ("claude-ai", "claude-code", "codex", "cursor", "gemini-cli", "vibe"):
    _p = str(pathlib.Path(__file__).resolve().parent.parent / _sub)
    if _p not in sys.path:
        sys.path.append(_p)

import source_reader  # noqa: E402
import timestamps  # noqa: E402


# --- a lone surrogate inside nested JSON crashed the pre-processor -----

def test_nested_json_lone_surrogate_is_repaired():
    value = source_reader.nested_json_loads('{"cmd": "\\ud83d tail"}')
    assert value == {"cmd": "� tail"}, value
    # What the pre-processors do with it: must not raise.
    json.dumps(value, ensure_ascii=False).encode("utf-8")


def _pre_cli(rel: str, text: str, name: str) -> tuple[int, str, str]:
    """Run a pre-processor's CLI on `text` (ASCII JSON, escapes intact);
    return (exit code, stderr, output read as strict UTF-8)."""
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        src, dst = pathlib.Path(td) / name, pathlib.Path(td) / "out.json"
        src.write_text(text, encoding="ascii")
        p = subprocess.run([sys.executable, str(_ADAPTERS / rel), str(src), str(dst)],
                           capture_output=True, text=True, timeout=120)
        out = dst.read_bytes().decode("utf-8") if dst.exists() else ""
    return p.returncode, p.stderr, out


def test_claude_ai_lone_surrogate_escape_is_repaired_not_a_traceback():
    # A "\ud83d" escape (and a noncharacter) in a value and in a member name
    # raised UnicodeEncodeError at the UTF-8 write; now each is U+FFFD and
    # one warning line names the count.
    conv = ('[{"uuid": "c1", "name": "t \\ud83d", "chat_messages": [{"uuid": "m1", "sender": "human", '
            '"text": "hi \\ud83d \\uffff", "created_at": "2026-01-01T00:00:01.000000Z", '
            '"parent_message_uuid": null, "k\\udc00": 1}]}]')
    code, err, out = _pre_cli("claude-ai/preprocess_claude_ai.py", conv, "conversations.json")
    assert code == 0 and "Traceback" not in err, err
    assert "3 string(s) held an unpaired surrogate or a noncharacter, replaced with U+FFFD" in err, err
    [c] = json.loads(out)
    assert c["name"] == "t \ufffd" and c["chat_messages"][0]["text"] == "hi \ufffd \ufffd", c
    assert "k\ufffd" in c["chat_messages"][0], c


def test_cursor_lone_surrogate_escape_is_repaired_not_a_traceback():
    stream = ('{"type": "system", "subtype": "init", "session_id": "s1", "model": "m \\ud83d", "k\\udc00": "\\uffff"}\n'
              '{"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi \\ud83d"}]}}\n')
    code, err, out = _pre_cli("cursor/preprocess_cursor.py", stream, "in.jsonl")
    assert code == 0 and "Traceback" not in err, err
    assert "line 1: 3 string(s) held an unpaired surrogate or a noncharacter" in err, err   # value, name, nonchar
    assert "line 2: 1 string(s) held an unpaired surrogate or a noncharacter" in err, err
    assert "\\ud83d" not in out and "m \ufffd" in out, out


def test_claude_ai_repaired_names_that_collide_are_reported():
    conv = ('[{"uuid": "c1", "name": "t", "chat_messages": [{"uuid": "m1", "sender": "human", "text": "x", '
            '"created_at": "2026-01-01T00:00:01.000000Z", "parent_message_uuid": null, '
            '"k\\ud800": 1, "k\\udc00": 2}]}]')
    code, err, out = _pre_cli("claude-ai/preprocess_claude_ai.py", conv, "conversations.json")
    assert code == 0 and "1 member(s) lost: a repaired name equals another" in err, err
    assert json.loads(out)[0]["chat_messages"][0]["k\ufffd"] == 2


def test_a_surrogate_nested_past_the_repair_is_refused_not_a_traceback():
    # Parsed but too deep for the repair walk (or, on another interpreter,
    # too deep to parse): claude-ai refuses the export, cursor skips the
    # line, as the shared readers do; never the UTF-8 write's traceback.
    deep = "[" * 3000 + '"\\ud83d"' + "]" * 3000
    conv = ('[{"uuid": "c1", "name": "t", "chat_messages": [{"uuid": "m1", "sender": "human", "text": "x", '
            '"created_at": "2026-01-01T00:00:01.000000Z", "parent_message_uuid": null, "d": ' + deep + '}]}]')
    code, err, _ = _pre_cli("claude-ai/preprocess_claude_ai.py", conv, "conversations.json")
    assert code == 2 and "Traceback" not in err and "is not readable JSON" in err, (code, err[-500:])
    stream = ('{"type": "system", "subtype": "init", "session_id": "s1"}\n'
              '{"type": "user", "d": ' + deep + '}\n')
    code, err, _ = _pre_cli("cursor/preprocess_cursor.py", stream, "in.jsonl")
    assert code == 0 and "Traceback" not in err, (code, err[-500:])
    assert "line 2: unparseable JSON" in err and "skipped" in err, err[-500:]


def test_cursor_and_claude_ai_clean_input_has_no_repair_line():
    code, err, _ = _pre_cli("claude-ai/preprocess_claude_ai.py",
                            '[{"uuid": "c1", "name": "t", "chat_messages": []}]', "conversations.json")
    assert code == 0 and "U+FFFD" not in err, err
    code, err, _ = _pre_cli("cursor/preprocess_cursor.py",
                            '{"type": "system", "subtype": "init", "session_id": "s1"}\n', "in.jsonl")
    assert code == 0 and "U+FFFD" not in err, err


def test_goose_non_string_tool_id_reads_as_absent_and_is_counted():
    # An id that is a list raised TypeError (unhashable dict key). Ruled
    # 2026-11-06: a present non-string id is no id, counted as a shape loss;
    # an absent or null id is not counted.
    _gp = str(_ADAPTERS / "goose")
    if _gp not in sys.path:
        sys.path.append(_gp)
    import preprocess_goose as pg  # noqa: PLC0415

    def msg(i, role, blocks):
        return {"role": role, "content_json": json.dumps(blocks), "metadata_json": "{}",
                "created_timestamp": 1767261600 + i, "message_id": f"g{i}"}

    def req(i):
        return {"type": "toolRequest", "id": i,
                "toolCall": {"status": "success", "value": {"name": "developer__shell", "arguments": {}}}}

    def res(i):
        return {"type": "toolResponse", "id": i,
                "toolResult": {"status": "success", "value": {"content": [{"type": "text", "text": "ok"}]}}}
    msgs = [msg(1, "user", [{"type": "text", "text": "go"}])]
    for n, i in enumerate((["a"], {"x": 1}, 5, None, "c1")):
        msgs += [msg(2 + 2 * n, "assistant", [req(i)]), msg(3 + 2 * n, "user", [res(i)])]
    pg.take_shape_losses()
    records, _ = pg.annotate_messages(msgs)
    losses = pg.take_shape_losses()
    assert losses.get("tool id not a string") == 6, losses   # list, object, number; request and response each
    ids = [c["id"] for r in records for c in r.get("tool_calls") or []]
    assert ids == ["", "", "", "", "c1"], ids


def _cc_run(records: list[dict]) -> tuple[int, str, list[dict]]:
    """preprocess_claude_code's CLI on one session file; (exit, stderr, containers)."""
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        src, dst = pathlib.Path(td) / "s.jsonl", pathlib.Path(td) / "out.json"
        src.write_text("".join(json.dumps(r) + "\n" for r in records))
        p = subprocess.run([sys.executable, str(_ADAPTERS / "claude-code" / "preprocess_claude_code.py"),
                            str(src), str(dst)], capture_output=True, text=True, timeout=120)
        out = json.loads(dst.read_text()) if dst.exists() else []
    return p.returncode, p.stderr, out


def _cc_rec(i: int, kind: str, content: list, ts: object = "2026-11-04T10:00:00.000Z") -> dict:
    return {"uuid": f"u{i}", "parentUuid": f"u{i - 1}" if i else None, "sessionId": "s-cc",
            "version": "2.1.220", "type": kind, "timestamp": ts,
            "message": {"role": kind, "model": "m", "content": content}}


def test_claude_code_tool_input_is_an_object_or_null():
    # Trace §4.8.1 (ruled 2026-11-08): a JSON string holding an object is
    # parsed; any other value that is not an object is null; both counted in
    # one warning line for the file. Object, null and absent are unchanged.
    inputs = ['{"command": "x"}', "plain text", [1, 2], 42, True, {"a": 1}, None]
    blocks = [{"type": "tool_use", "id": f"c{i}", "name": "Bash", "input": v} for i, v in enumerate(inputs)]
    blocks.append({"type": "tool_use", "id": "c-absent", "name": "Bash"})
    code, err, out = _cc_run([_cc_rec(0, "user", [{"type": "text", "text": "go"}]), _cc_rec(1, "assistant", blocks)])
    assert code == 0, err
    got = [b.get("input") for e in out[0]["events"] for b in e.get("content_blocks") or []
           if b.get("type") == "tool_use"]
    assert got == [{"command": "x"}, None, None, None, None, {"a": 1}, None, None], got
    assert "1 JSON string(s) parsed to an object, 4 written as null (trace §4.8.1)" in err, err


def test_claude_code_timestamps_read_by_the_shared_reader():
    # Ruled 2026-11-08, as 758 for the other pre-processors: §3.5 form via
    # to_spec_utc; an invalid calendar date, a second 60 that is no leap
    # second and a non-string are null and counted; the empty string is null
    # and not counted.
    stamps = ["2026-02-30T10:00:00.000Z", "2026-11-04T23:59:60.000Z", "2026-11-04T10:00:60.000Z",
              "2026-11-04T10:00:00.000+01:00", "2026-11-04T10:00:00.123Z", 42, "", None]
    recs = [_cc_rec(i, "user", [{"type": "text", "text": f"t{i}"}], ts) for i, ts in enumerate(stamps)]
    code, err, out = _cc_run(recs)
    assert code == 0, err
    got = [e.get("timestamp") for e in out[0]["events"] if e.get("event_kind") == "user_prompt"]
    assert got == [None, "2026-11-04T23:59:59.999999Z", None, "2026-11-04T09:00:00.000000Z",
                   "2026-11-04T10:00:00.123000Z", None, None, None], got
    assert "3 timestamp(s) not convertible to the §3.5 form, written as null" in err, err


def _run_preprocessor(harness: str, name: str, text: str, positional_output: bool) -> tuple[int, str, str]:
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    pre = next((pathlib.Path(__file__).resolve().parent.parent / harness).glob("preprocess_*.py"))
    with tempfile.TemporaryDirectory() as td:
        src, dst = pathlib.Path(td) / name, pathlib.Path(td) / "out"
        src.write_text(text)
        out_args = [str(dst)] if positional_output else ["--output", str(dst)]
        r = subprocess.run([sys.executable, str(pre), str(src), *out_args],
                           capture_output=True, text=True, timeout=300)
        return r.returncode, r.stderr, dst.read_text() if dst.exists() else ""


def test_droid_timestamps_read_by_the_shared_reader():
    # As claude-code: the §3.5 form via to_spec_utc; an invalid calendar
    # date, a second 60 that is no leap second and a non-string are null and
    # counted in one warning line naming the input; "" and null are null and
    # not counted. The invalid date is not in the output.
    stamps = ["2026-02-30T10:00:00.000Z", "2026-11-04T23:59:60.000Z", "2026-11-04T10:00:60.000Z",
              "2026-11-04T10:00:00.000+01:00", "2026-11-04T10:00:00.123Z", 42, "", None]
    # Records that are not emitted (the session record, an unknown type)
    # carry bad values too; they are not read, so not counted.
    recs = [{"type": "session_start", "id": "s1", "version": 2, "cwd": "/w", "timestamp": "2026-02-31T00:00:00Z"}]
    recs += [{"type": "message", "id": f"m{i}", "timestamp": ts,
              "message": {"role": "user", "content": [{"type": "text", "text": f"t{i}"}]}}
             for i, ts in enumerate(stamps)]
    recs.append({"type": "not-a-droid-type", "id": "x", "timestamp": "2026-04-31T00:00:00Z"})
    code, err, out = _run_preprocessor("droid", "s1.jsonl", "".join(json.dumps(r) + "\n" for r in recs), True)
    assert code == 0, err
    msgs = json.loads(out)[0]["messages"]
    got = [m.get("timestamp") for m in msgs if m.get("record_id")]
    assert got == [None, "2026-11-04T23:59:59.999999Z", None, "2026-11-04T09:00:00.000000Z",
                   "2026-11-04T10:00:00.123000Z", None, None, None], got
    # The session record takes the first timestamp that could be read.
    assert msgs[0]["event_kind"] == "session_init" and msgs[0]["timestamp"] == "2026-11-04T23:59:59.999999Z", msgs[0]
    lines = [ln for ln in err.splitlines() if "not convertible" in ln]
    assert len(lines) == 1 and lines[0].startswith("warning: droid: ") and lines[0].endswith(
        "s1.jsonl: 3 timestamp(s) not convertible to the §3.5 form, written as null"), err
    assert "2026-02-30" not in out and "10:00:60" not in out, out


def test_minimcp_timestamps_read_by_the_shared_reader():
    # minimcp copied ts unread, so an invalid calendar date reached the
    # trace, and a capture mixing string and non-string ts raised TypeError
    # in min(). Each ts is read by to_spec_utc: null and counted when it
    # cannot be read, and the session's first and last ts come from the rest.
    events = [
        {"session_id": "s1", "transport": "stdio", "ts": "2026-03-01T10:00:00.000000Z", "direction": "in",
         "request_id": 1, "event_kind": "lifecycle:initialize:request", "method": "initialize",
         "payload": {"params": {"capabilities": {}, "clientInfo": {"name": "c", "version": "1"}}}},
        {"session_id": "s1", "transport": "stdio", "ts": "2026-03-01T10:00:01.000+01:00", "direction": "out",
         "request_id": 1, "event_kind": "lifecycle:initialize:response", "payload": {"result": {"capabilities": {}}}},
        {"session_id": "s1", "transport": "stdio", "ts": "2026-02-30T10:00:00Z", "direction": "in",
         "request_id": 2, "event_kind": "tool:call:request", "method": "tools/call",
         "payload": {"params": {"name": "echo", "arguments": {}}}},
        {"session_id": "s1", "transport": "stdio", "ts": 42, "direction": "out", "request_id": 2,
         "event_kind": "tool:call:response", "payload": {"result": {"content": []}}},
    ]
    code, err, out = _run_preprocessor("minimcp", "cap.ndjson", "".join(json.dumps(e) + "\n" for e in events), False)
    assert code == 0, err
    recs = [json.loads(ln) for ln in out.splitlines()]
    meta = next(r for r in recs if r["kind"] == "session_meta")
    assert (meta["first_ts"], meta["last_ts"]) == ("2026-03-01T09:00:01.000000Z", "2026-03-01T10:00:00.000000Z"), meta
    assert [r["ts"] for r in recs if r["kind"] == "tool_call"] == [None], recs
    lines = [ln for ln in err.splitlines() if "not convertible" in ln]
    assert len(lines) == 1 and lines[0].startswith("warning: minimcp: ") and lines[0].endswith(
        "cap.ndjson: 2 timestamp(s) not convertible to the §3.5 form, written as null"), err
    assert "2026-02-30" not in out, out


def test_claude_code_agent_input_as_json_string_still_links_its_subagent():
    # The spawn payload reads the normalised input: a delegation call whose
    # input is a JSON string holding the prompt finds its subagent.
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    prompt = "look into the thing"
    parent = [_cc_rec(0, "user", [{"type": "text", "text": "go"}]),
              _cc_rec(1, "assistant", [{"type": "tool_use", "id": "a1", "name": "Agent",
                                        "input": json.dumps({"prompt": prompt, "subagent_type": "t"})}])]
    sub = [{**_cc_rec(0, "user", []), "agentId": "agent-x", "message": {"role": "user", "content": prompt}}]
    with tempfile.TemporaryDirectory() as td:
        src, dst = pathlib.Path(td) / "s.jsonl", pathlib.Path(td) / "out.json"
        src.write_text("".join(json.dumps(r) + "\n" for r in parent))
        (pathlib.Path(td) / "s" / "subagents").mkdir(parents=True)
        (pathlib.Path(td) / "s" / "subagents" / "agent-x.jsonl").write_text(json.dumps(sub[0]) + "\n")
        p = subprocess.run([sys.executable, str(_ADAPTERS / "claude-code" / "preprocess_claude_code.py"),
                            str(src), str(dst)], capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, p.stderr
        out = json.loads(dst.read_text())
    [tu] = [b for e in out[0]["events"] for b in e.get("content_blocks") or [] if b.get("type") == "tool_use"]
    assert tu["spawned_agent_payload"]["sub_session_id"] == out[1]["trace_id"], (tu, out[1].get("trace_id"))
    assert tu["spawned_agent_payload"]["agent_type"] == "t", tu


def test_claude_code_clean_session_has_no_new_warning():
    code, err, _ = _cc_run([_cc_rec(0, "user", [{"type": "text", "text": "go"}]),
                            _cc_rec(1, "assistant", [{"type": "tool_use", "id": "c", "name": "Bash",
                                                      "input": {"command": "ls"}}])])
    assert code == 0 and "§4.8.1" not in err and "timestamp(s) not convertible" not in err, err


def test_agentdojo_trace_classification_is_in_the_retained_set():
    # Trace v0.3.0 §6.5 retains public, internal, confidential and
    # customer-confidential; the DSL wrote "shared". Ruled 2026-11-08: public.
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    run = {"suite_name": "workspace", "pipeline_name": "pipe", "user_task_id": "user_task_0",
           "injection_task_id": None, "attack_type": None, "injections": {},
           "messages": [{"role": "user", "content": "list my files"},
                        {"role": "assistant", "content": "here", "tool_calls": None}],
           "utility": True, "security": True, "duration": 1.0, "error": None}
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "runs"
        f = root / "pipe" / "workspace" / "user_task_0" / "none" / "none.json"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps(run))
        pre, out = pathlib.Path(td) / "pre.json", pathlib.Path(td) / "out.json"
        for argv in ([str(_ADAPTERS / "agentdojo" / "preprocess_agentdojo.py"), "--runs-root", str(root),
                      "--run", str(f), "--output", str(pre)],
                     [str(_ADAPTERS / "dsl_runner.py"), str(_ADAPTERS / "agentdojo" / "agentdojo.dsl.yaml"),
                      str(pre), "--operator-type", "scripted", "--output", str(out)]):
            p = subprocess.run([sys.executable, *argv], capture_output=True, text=True, timeout=120)
            assert p.returncode == 0, p.stderr[-800:]
        traces = json.loads(out.read_text())
    [t] = traces if isinstance(traces, list) else [traces]
    assert t["classification"] == "public", t["classification"]
    assert t["classification"] in {"public", "internal", "confidential", "customer-confidential"}


def test_claude_code_structured_tool_result_is_json_text_in_the_trace():
    # output.result is a string or null (v0.2.0); claude-code copied a
    # tool_result's list of content blocks through. Ruled 2026-11-08: JSON
    # text, counted in a warning line.
    blocks = [{"type": "text", "text": "fetched ö"}]
    recs = [_cc_rec(0, "user", [{"type": "text", "text": "go"}]),
            _cc_rec(1, "assistant", [{"type": "tool_use", "id": "c1", "name": "Bash", "input": {"command": "x"}}]),
            _cc_rec(2, "user", [{"type": "tool_result", "tool_use_id": "c1", "content": blocks}]),
            _cc_rec(3, "assistant", [{"type": "tool_use", "id": "c2", "name": "Bash", "input": {"command": "y"}}]),
            _cc_rec(4, "user", [{"type": "tool_result", "tool_use_id": "c2", "content": "plain"}])]
    code, err, containers = _cc_run(recs)
    assert code == 0, err
    assert "1 tool result(s) not a string, written as JSON text" in err, err
    [trace] = _run_dsl(_ADAPTERS / "claude-code" / "claude-code.dsl.yaml", containers)[:1]
    got = {tc["id"]: tc["output"]["result"] for tc in trace["tool_calls"]}
    assert got == {"c1": json.dumps(blocks, ensure_ascii=False), "c2": "plain"}, got


def _mutate_first(rel: str, key: str, value, where=lambda d: True) -> tuple[str, object]:
    """A shipped rung input with the first `key` (in a dict `where` accepts)
    set to `value`; returns (text, the value written)."""
    lines = (_ADAPTERS / rel).read_text().splitlines()
    done = False

    def walk(o):
        nonlocal done
        if done:
            return
        if isinstance(o, dict):
            if key in o and where(o):
                o[key] = value
                done = True
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    out = []
    for line in lines:
        if line.strip() and not done:
            rec = json.loads(line)
            walk(rec)
            line = json.dumps(rec)
        out.append(line)
    assert done, (rel, key)
    return "\n".join(out) + "\n", value


def _pre_out(rel_pre: str, text: str, name: str) -> tuple[int, str, str]:
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        src, dst = pathlib.Path(td) / name, pathlib.Path(td) / "out.json"
        src.write_text(text)
        p = subprocess.run([sys.executable, str(_ADAPTERS / rel_pre), str(src), str(dst)],
                           capture_output=True, text=True, timeout=120)
        return p.returncode, p.stderr, dst.read_text() if dst.exists() else ""


def test_droid_codex_cursor_structured_result_is_json_text():
    # The three pass-throughs a mutation probe found; each a shipped rung
    # input with one result made a structure.
    struct = [{"type": "text", "text": "out ö"}, {"k": 1}]
    cases = [
        ("droid/preprocess_droid.py", "droid/regression/0.178.0/inputs/s1-config.jsonl", "content",
         lambda d: d.get("type") == "tool_result", "s1-config.jsonl",
         "1 tool result(s) not a string, written as JSON text"),
        ("codex/preprocess_codex.py", "codex/regression/0.114.0/inputs/s2-todo-grep.jsonl", "aggregated_output",
         lambda d: d.get("status") == "completed",   # item.started records are not read
         "s2.jsonl", "1 command output(s) not a string, written as JSON text"),
        ("cursor/preprocess_cursor.py", "cursor/regression/2026.07.23-e383d2b/inputs/s1-config.stream.jsonl",
         "stdout", lambda d: True, "in.jsonl", "1 tool result(s) not a string, written as JSON text"),
    ]
    for pre, inp, key, where, name, line in cases:
        text, value = _mutate_first(inp, key, struct, where)
        code, err, out = _pre_out(pre, text, name)
        assert code == 0 and "Traceback" not in err, (pre, err[-800:])
        assert line in err, (pre, err[-800:])
        assert json.dumps(json.dumps(value, ensure_ascii=False), ensure_ascii=False) in out, (pre, out[:400])


def test_every_classification_literal_is_in_the_retained_set():
    # Trace v0.3.0 §6.5 retains public, internal, confidential and
    # customer-confidential. Every classification a DSL in this tree writes,
    # at any depth and in any YAML spelling, and every one the judge-session
    # emitter writes where it is present, must be one of them or null (three
    # wrote "shared"; ruled public 2026-11-08). Tree-relative: a tree without
    # an adapter checks what it holds. A value that is not a constant fails.
    import ast  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    allowed = {"public", "internal", "confidential", "customer-confidential", None}
    seen: dict = {}

    def walk(node, where):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "classification":
                    seen.setdefault(v if v is None or isinstance(v, str) else repr(v), []).append(where)
                walk(v, where)
        elif isinstance(node, list):
            for v in node:
                walk(v, where)

    dsls = sorted(_ADAPTERS.glob("**/*.dsl.yaml"))
    assert _ADAPTERS / "agentdojo" / "agentdojo.dsl.yaml" in dsls, "canary: agentdojo's DSL not found"
    for f in dsls:
        walk(yaml.safe_load(f.read_text()), str(f.relative_to(_ADAPTERS)))
    for f in sorted(_ADAPTERS.glob("*/emit_judge_sessions.py")):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "classification":
                        val = v.value if isinstance(v, ast.Constant) else ast.unparse(v)
                        seen.setdefault(val, []).append(str(f.relative_to(_ADAPTERS)))
    assert "public" in seen, seen   # canary: agentdojo writes it
    bad = {v: fs for v, fs in seen.items() if v not in allowed}
    assert not bad, bad


def test_nested_json_without_surrogates_unchanged():
    assert source_reader.nested_json_loads('{"a": [1, "x"]}') == {"a": [1, "x"]}
    assert source_reader.nested_json_loads("not json", default="d") == "d"


# --- year-edge offsets overflowed in astimezone() ----------------------

def test_to_spec_utc_year_edges_are_null():
    # Ruled 2026-10-29: leaving the years 0001-9999 in UTC is null, never
    # passed through.
    for v in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
        assert timestamps.to_spec_utc(v) is None, v


def test_naive_local_to_utc_year_edge_returns_unchanged():
    v = "0001-01-01 00:00:00"
    assert timestamps.naive_local_to_utc(v, "Europe/Berlin") == v


def test_uuid7_time_beyond_year_9999_is_none():
    assert timestamps.uuid7_time("ffffffff-ffff-7fff-bfff-ffffffffffff") is None


def test_to_spec_utc_ascii_digits_and_no_trailing_newline():
    # \d matched non-ASCII digits; $ matched before a trailing newline.
    for v in ("٢٠٢٦-10-03T10:00:00Z", "2026-10-03T10:00:00Z\n"):
        assert timestamps.to_spec_utc(v) is None, v
    assert timestamps.to_spec_utc("2026-10-03T10:00:00.5+02:00") == "2026-10-03T08:00:00.500000Z"


# --- DSL YAML is 1.2 core schema, strict (DSL spec rc3 §2) -------------

def _load(text):
    import dsl_runner  # noqa: PLC0415
    return dsl_runner.load_adapter_yaml(text)


def _rejected(text):
    try:
        _load(text)
    except ValueError:
        return True
    return False


def test_yaml_core_schema_typing():
    got = _load("a: no\nb: on\nc: 755\nd: 1:30\ne: 2026-10-03\nf: 2.10\ng: true\nh: ~\ni: '1.28'\n")
    assert got == {"a": "no", "b": "on", "c": 755, "d": "1:30", "e": "2026-10-03",
                   "f": 2.1, "g": True, "h": None, "i": "1.28"}, got
    # Ruled 2026-10-06, spec 37b78f9d: 0755, which the core schema
    # reads as decimal 755, is not a JSON number and is refused at load now.
    assert _rejected("c: 0755\n")


def test_yaml_duplicate_nonstring_keys_and_tags_rejected():
    for text in ("a: 1\na: 2\n", "1: x\n", "true: x\n", "null: x\n",
                 "v: !!binary aGk=\n", "v: !!set {a}\n", "a: &x 1\nb: *x\n"):
        assert _rejected(text), text


def test_minimcp_session_meta_accumulates_both_timestamps():
    # the duplicate key dropped first_ts; the DSL must load and keep both.
    dsl = _load((pathlib.Path(__file__).resolve().parent.parent
                 / "minimcp" / "minimcp.dsl.yaml").read_text())
    rule = next(r for r in dsl["emit"] if r.get("name") == "session_meta")
    sources = [v for a in rule["do"] if "accumulate" in a
               for k, v in a["accumulate"].items() if k == "state.all_ts"]
    assert sources == ["$record.first_ts", "$record.last_ts"], sources


# --- evaluator, DSL spec rc3 §8.3 / §9.1 ----------

def _dr():
    import dsl_runner  # noqa: PLC0415
    return dsl_runner


def _eval_collecting(expr, ctx):
    """Evaluate with the diagnostics sink installed. On a runner without the
    sink the result is returned with no diagnostics, so a test run against
    older code fails on the behaviour it pins, not on a missing attribute."""
    D = _dr()
    diags: list = []
    sink = getattr(D, "_EVAL_DIAGNOSTICS", None)
    if sink is None:
        try:
            return D.evaluate(expr, ctx), diags
        except Exception:  # noqa: BLE001
            return "<raised>", diags
    tok = sink.set(diags)
    try:
        return D.evaluate(expr, ctx), diags
    finally:
        sink.reset(tok)


def test_input_failure_is_null_with_diagnostic_never_source_text():
    # A .text that is not a string is an input failure (§9.1); content that
    # is not a list is not one: it gives null (below).
    value, diags = _eval_collecting("text_blocks_join($record.content_blocks)",
                                    {"record": {"content_blocks": [{"type": "text", "text": 5}]}})
    assert value is None, value
    assert len(diags) == 1 and diags[0]["kind"] == "input-failure", diags


def test_text_blocks_join_follows_its_definition():
    # DSL rc3 §9.1: null, a non-list (a number or true included) or no text
    # block gives null, unreported; an absent .text contributes ""; a .text
    # that is not a string, null included, is an input failure.
    for content in (None, 5, 1.5, True, False, 0, "text", {"type": "text", "text": "x"}, [],
                    [{"type": "thinking", "text": "x"}], ["text"]):
        value, diags = _eval_collecting("text_blocks_join($record.c)", {"record": {"c": content}})
        assert value is None and diags == [], (content, value, diags)
    for content, want in (([{"type": "text", "text": "a"}, {"type": "text"}, {"type": "text", "text": "b"}],
                           "a\n\nb"),
                          ([{"type": "text", "text": ""}], ""),
                          ([{"type": "tool_use"}, {"type": "text", "text": "only"}], "only"),
                          # Only a text block's .text is checked: another block's is not read.
                          ([{"type": "tool_use", "text": 5}, {"type": "text", "text": "a"}], "a"),
                          ([{"type": ["text"], "text": 5}, {"type": True, "text": None}], None)):
        value, diags = _eval_collecting("text_blocks_join($record.c)", {"record": {"c": content}})
        assert value == want and diags == [], (content, value, diags)
    for bad in (None, 0, False, ["a"], {"t": "a"}):
        value, diags = _eval_collecting("text_blocks_join($record.c)",
                                        {"record": {"c": [{"type": "text", "text": "a"}, {"type": "text", "text": bad}]}})
        assert value is None and [d["kind"] for d in diags] == ["input-failure"], (bad, value, diags)
        assert "text_blocks_join: text is a" in diags[0]["message"], diags
        assert diags[0].get("expression") == "text_blocks_join($record.c)", diags


def test_function_name_outside_call_is_structural():
    D = _dr()
    for expr in ("$len", "$max"):
        try:
            D.evaluate(expr, {})
        except D.ExpressionEvalError:
            continue
        raise AssertionError(f"{expr} did not fail structurally")
    assert D.evaluate("max", {}) == "max"   # bare word: a literal


def test_literals_are_used_exactly_as_written():
    D = _dr()
    for text in ("cost $5", "a?.b", "What? yes: no", "2026-10", "1-5", "1.28", "max",
                 "0x10", "null", "true", "synthetic-test-judge", "inspect_ai/span/agent", ""):
        assert D.evaluate(text, {}) == text, text


def test_pre_quoted_literal_is_used_as_written():
    # rc3 §8.3 uses a literal exactly as written, so the quotes stay; §8.4's
    # "MAY pre-quote" sentence contradicts that and is open for the spec
    # session. No shipped DSL pre-quotes in an evaluated position.
    D = _dr()
    assert D.evaluate("'first'", {}) == "'first'"


def test_legacy_ternary_stands_in_any_expression_position():
    # Ruled 2026-10-22: a legacy `p ? a : b` spans its innermost bracket
    # group, bounded by a comma at its depth, so parentheses, an operand and
    # a function argument all read as written; a nested legacy ternary or a
    # `?` without its `:` stays a structural failure (§8.3).
    D = _dr()
    ctx = {"record": {"a": True, "b": False, "n": 5, "s": "x?y:z"}}
    for expr, want in (("$record.a ? 1 : 2", 1), ("($record.a ? 1 : 2)", 1), ("((($record.a ? 1 : 2)))", 1),
                       ("($record.b ? 1 : 2) + 10", 12), ("10 + ($record.b ? 1 : 2)", 12),
                       ("($record.a ? 1 : 2) + 10", 11),
                       ("coalesce(null, $record.b ? 1 : 2)", 2), ("coalesce(null,   $record.a ? 1 : 2)", 1),
                       ("coalesce($record.b ? null : null, $record.a ? 7 : 8)", 7),
                       ("$record.a ? ($record.n + 1) : 0", 6), ("$record.a ? 'q?:' : 'r'", "q?:"),
                       ("$record.n == 5 ? 'five' : 'other'", "five"), ("$record.s", "x?y:z")):
        assert D.evaluate(expr, ctx) == want, expr
    for expr in ("$record.a ? ($record.b ? 1 : 2) : 3", "($record.a ? 1 : 2) ? 3 : 4",
                 "$record.a ? 1 : ($record.b ? 2 : 3)", "$record.a ? $record.b ? 1 : 2 : 3",
                 "coalesce($record.a ? 1 : 2, $record.b ? 3)", "$record.a ? 1", "coalesce($record.a ? 1)",
                 "coalesce($record.b ? 1, 2 : 3)"):
        # Left unrewritten, not rewritten into something else, and refused
        # at load as well as at evaluation.
        assert "?" in D._normalise(expr), (expr, D._normalise(expr))
        assert D.expression_problem(expr) is not None, expr
        try:
            D.evaluate(expr, ctx)
        except D.ExpressionEvalError:
            continue
        raise AssertionError(f"{expr} did not fail structurally")


def test_claude_ai_synthetic_id_template_matches_the_old_concatenation():
    # 2026-09-26.dsl.yaml builds the synthetic tool-use id with a template now
    # that + is numeric only; through the shipped DSL it must produce the
    # string the concatenation did, and a source id must still win.
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    blocks = [{"type": "tool_use", "name": "repl", "input": {}},
              {"type": "tool_use", "name": "repl", "input": {}},
              {"type": "tool_use", "id": "toolu_x", "name": "repl", "input": {}}]
    with contextlib.redirect_stderr(io.StringIO()):
        (trace,) = _claude_ai_run([_conv("c", blocks)])
    assert [c["id"] for c in trace["tool_calls"]] == ["claude-ai-tu-1", "claude-ai-tu-2", "toolu_x"]


def test_context_variable_alone_literal_without_dollar_reference_with():
    D = _dr()
    rec = {"a": 1}
    got = D.evaluate("$record", {"record": rec})
    # Equal but a copy: a whole-value reference must not
    # alias the context into the trace.
    assert got == rec and got is not rec
    trace = {"x": 1}
    snap = D.evaluate("$trace", {"trace": trace})
    trace["self"] = snap            # no cycle: the snapshot is a copy
    assert snap == {"x": 1}
    assert D.evaluate("trace", {"trace": {"x": 1}}) == "trace"


def test_interpolated_values_render_per_spec():
    D = _dr()
    r = {"t": True, "f": False, "one": 1.0, "half": 0.5, "n": None, "s": "x", "o": {"k": 1}}
    assert D.evaluate("${record.t}/${record.f}", {"record": r}) == "true/false"
    assert D.evaluate("v=${record.one} ${record.half}", {"record": r}) == "v=1 0.5"
    assert D.evaluate("[${record.n}]${record.s}", {"record": r}) == "[]x"
    value, diags = _eval_collecting("obj=${record.o}", {"record": r})
    assert value is None and diags, (value, diags)


def test_time_functions_are_strict():
    D = _dr()
    for expr, ctx in (("iso_normalise($record.v)", {"record": {"v": "2026-10-03"}}),
                      ("iso_normalise($record.v)", {"record": {"v": "2026-W40-6"}}),
                      ("iso_normalise($record.v)", {"record": {"v": "2026-10-03T10:00:00"}}),
                      ("iso_normalise($record.v)", {"record": {"v": "2026-10-03T10:00:00.Z"}}),
                      ("iso_from_ms($record.v)", {"record": {"v": True}}),
                      ("iso_from_ms($record.v)", {"record": {"v": "1000"}})):
        value, diags = _eval_collecting(expr, ctx)
        assert value is None and len(diags) == 1, (expr, ctx, value, diags)
    assert D.evaluate("iso_from_ms($record.v)", {"record": {"v": 1700000000123}}) == "2023-11-14T22:13:20.123000Z"
    # §9.1: any number of fraction digits, truncated (not rounded) to six.
    assert D.evaluate("iso_normalise($record.v)", {"record": {"v": "2026-10-03T10:00:00.1234567Z"}}) \
        == "2026-10-03T10:00:00.123456Z"


def test_duration_seconds_is_bit_exact_microseconds():
    D = _dr()
    ctx = {"record": {"a": "2026-01-01T00:00:00Z", "b": "2026-01-01T00:00:00.000001Z",
                      "c": "2026-01-01T01:00:00+01:00"}}
    assert D.evaluate("duration_seconds($record.a, $record.b)", ctx) == 1e-06
    assert D.evaluate("duration_seconds($record.a, $record.c)", ctx) == 0.0
    assert D.evaluate("duration_seconds($record.a, $record.missing)", ctx) is None


def test_failed_inner_interpolation_nulls_the_whole_string():
    value, _ = _eval_collecting("tu-${record.a + 1}", {"record": {"a": "x"}})
    assert value is None


def test_r4_lineage_edge_with_null_hash_is_omitted():
    D = _dr()
    trace: dict = {}
    diags: list = []
    action = {"emit_lineage": {"target_trace_id": "$block",
                               "target_content_hash": "${trace:${block}:content_hash}"}}
    # An object-valued block id is an interpolation input failure -> null hash.
    D._apply_actions([action], {"block": {"id": 1}}, trace, {}, {}, diags)
    assert trace.get("lineage", []) == [], trace
    assert any(d.get("kind") == "lineage-edge-omitted" for d in diags), diags


# --- one bad lineage reference aborted the whole paired set (R3/R4) ----

def _tr(tid, *targets):
    import cross_trace  # noqa: PLC0415
    return {"schema_version": "0.3.0", "trace_type": "x", "id": tid, "operator": {"type": "scripted"},
            "turns": [], "lineage": [{"type": "x", "target_trace_id": t,
                                      "target_content_hash": cross_trace.make_cross_trace_reference(t)}
                                     for t in targets]}


def test_three_cycle_omits_every_in_cycle_edge_and_keeps_edges_leaving_it():
    import copy  # noqa: PLC0415
    import cross_trace  # noqa: PLC0415
    # a -> b -> c -> a is a cycle; a -> e and d -> a leave it or enter it.
    traces = [_tr("a", "b", "e"), _tr("b", "c"), _tr("c", "a"), _tr("d", "a"), _tr("e")]
    again = copy.deepcopy(traces[::-1])
    diags: list = []
    cross_trace.back_fill_cross_trace_references(traces, diags)
    by = {t["id"]: t for t in traces}
    assert {k: [e["target_trace_id"] for e in t["lineage"]] for k, t in by.items()} == \
        {"a": ["e"], "b": [], "c": [], "d": ["a"], "e": []}
    assert len(diags) == 3 and all("R3" in d["message"] for d in diags), diags
    for t in traces:
        for e in t["lineage"]:
            assert e["target_content_hash"] == by[e["target_trace_id"]]["content_hash"]
        assert cross_trace.compute_content_hash(t) == t["content_hash"]  # R1 round trip
    cross_trace.back_fill_cross_trace_references(again, [])
    assert {t["id"]: t["content_hash"] for t in again} == {t["id"]: t["content_hash"] for t in traces}


def test_null_block_id_degrades_end_to_end():
    # The probe that aborted the set: a data-built placeholder with a null id.
    D = _dr()
    import cross_trace  # noqa: PLC0415
    parent = {"id": "parent", "lineage": []}
    diags: list = []
    D._apply_actions([{"emit_lineage": {"target_trace_id": "$block",
                                        "target_content_hash": "${trace:${block}:content_hash}"}}],
                     {"block": None}, parent, {}, {}, diags)
    cross_trace.back_fill_cross_trace_references([parent, {"id": "child-1"}], diags)
    assert parent["lineage"] == [] and parent["content_hash"].startswith("sha256:")
    assert any("R4" in d["message"] for d in diags), diags


def test_hostile_cycle_cannot_choose_which_trace_loses_its_edge():
    # A hostile container lists the honest parent as its subagent: the cycle
    # costs both traces their in-cycle edge, whatever the ids sort to.
    import cross_trace  # noqa: PLC0415
    for parent_id, hostile_id in (("aaa-parent", "mmm-sub"), ("zzz-parent", "mmm-sub")):
        traces = [_tr(parent_id, hostile_id), _tr(hostile_id, parent_id), _tr("other")]
        kept = cross_trace.back_fill_cross_trace_references(traces, [])
        assert [t["lineage"] for t in kept] == [[], [], []], (parent_id, kept)


# --- one unusable or shared trace id aborted the whole set ------------

_ID_DSL = """
adapter: {name: t, harness_version: "1", dsl_version: "0.1.0", source_format: x, converter_version: x, produces_schema: "0.3.0"}
input: {type: json, encoding: utf-8, container: {record_path: events}}
%s
emit:
  - target: agent
    name: lin
    when: {has: to}
    do:
      - emit_lineage:
          relation: derived_from
          target_trace_id: "$record.to"
          target_content_hash: "${trace:${record.to}:content_hash}"
"""
_MULTI = 'sessions:\n  agent:\n    static: {schema_version: "0.3.0", trace_type: agent-session}\n    derived: {id: "%s"}'
_SINGLE = 'session:\n  static: {schema_version: "0.3.0", trace_type: agent-session}\n  derived: {id: "%s"}'


def _ids_and_diags(dsl_text, data):
    import io  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = _run_dsl(dsl_text, data)
    out = out if isinstance(out, list) else ([] if out is None else [out])
    return [t["id"] for t in out], err.getvalue(), out


def test_unusable_trace_ids_are_dropped_not_fatal():
    good = {"trace_id": "good", "events": [{"x": 1}]}
    for bad_id in (None, "", 7, True, {"a": 1}, ["x"]):
        bad = {"trace_id": bad_id, "events": [{"x": 1}]}
        ids, err, _ = _ids_and_diags(_ID_DSL % (_MULTI % "$container.trace_id"), [good, bad])
        assert ids == ["good"], (bad_id, ids)
        assert "not emitted (R5)" in err, (bad_id, err)


def test_shared_trace_id_drops_every_holder_and_their_edges_degrade():
    # The first holder could be a hostile record shadowing the genuine trace.
    data = [{"trace_id": "dup", "events": [{"x": 1}]}, {"trace_id": "dup", "events": [{"x": 2}]},
            {"trace_id": "p", "events": [{"to": "dup"}]}]
    ids, err, out = _ids_and_diags(_ID_DSL % (_MULTI % "$container.trace_id"), data)
    assert ids == ["p"] and out[0]["lineage"] == [], out
    assert "shared by 2 traces" in err and "R4" in err, err


def test_one_and_true_ids_no_longer_collide_into_an_abort():
    data = [{"trace_id": 1, "events": []}, {"trace_id": True, "events": []},
            {"trace_id": "s", "events": []}]
    ids, _, _ = _ids_and_diags(_ID_DSL % (_MULTI % "$container.trace_id"), data)
    assert ids == ["s"], ids


def test_id_template_with_a_null_part_is_unusable():
    # "agent-${container.trace_id}" with no trace_id used to emit "agent-".
    data = [{"trace_id": "a", "events": []}, {"events": []}]
    ids, err, _ = _ids_and_diags(_ID_DSL % (_MULTI % "agent-${container.trace_id}"), data)
    assert ids == ["agent-a"], ids
    assert "None is not a non-empty string" in err, err


def test_empty_and_boolean_id_parts_are_unusable_and_numbers_are_not():
    # "agent-${container.trace_id}" with "" or true used to give "agent-" or
    # "agent-true"; a number still builds an id (container_index shapes).
    for bad in ("", True, {"a": 1}, [1]):
        data = [{"trace_id": "a", "events": []}, {"trace_id": bad, "events": []}]
        ids, err, _ = _ids_and_diags(_ID_DSL % (_MULTI % "agent-${container.trace_id}"), data)
        assert ids == ["agent-a"], (bad, ids)
        assert "not emitted (R5)" in err, (bad, err)
    ids, _, _ = _ids_and_diags(_ID_DSL % (_MULTI % "agent-${container.trace_id}"),
                               [{"trace_id": 7, "events": []}])
    assert ids == ["agent-7"], ids


def test_unusable_part_rule_covers_only_the_placeholder_trace_id_portion():
    # The rule is scoped to the trace-id portion (R4),
    # everything before the last ':' outside inner ${...}; a field portion
    # renders under §8.3 (a templated field is a load-time matter).
    D = _dr()
    ctx = {"record": {"to": "x", "f": True, "e": ""}}
    assert D._interpolate_template("${trace:agent-${record.e}:content_hash}", ctx) is None
    assert D._interpolate_template("${trace:agent-${record.to}:${record.f}}", ctx) \
        == "${trace:agent-x:true}"
    assert D._interpolate_template("${trace:a:b-${record.to}:content_hash}", ctx) \
        == "${trace:a:b-x:content_hash}"


def _lineage_dsl(edge=None, **extra):
    d = {"adapter": {"dsl_version": "0.1.0"},
         "sessions": {"agent": {"static": {}, "derived": {"id": "$container.trace_id"}}},
         "emit": [{"name": "r", "target": "agent", "when": {"has": "to"},
                   "do": [{"emit_lineage": {"target_trace_id": "$record.to", **(edge or {})}}]}]}
    for path, value in extra.items():
        node = d
        *head, last = path.split("/")
        for k in head:
            node = node[int(k)] if k.isdigit() else node.setdefault(k, {})
        node[int(last) if last.isdigit() else last] = value
    return d


def test_lineage_has_one_form_and_one_writer_at_load():
    # §13.1: a data value never decides whether a field holds a placeholder or
    # which hash it binds; the forms that let it are rejected before input.
    D = _dr()
    ph = "${trace:agent-${record.to}:content_hash}"
    for name, d in (
            ("reference", _lineage_dsl({"target_content_hash": "$record.h"})),
            ("literal hash", _lineage_dsl({"target_content_hash": "sha256:" + "0" * 64})),
            ("other field", _lineage_dsl({"target_content_hash": "${trace:agent-${record.to}:id}"})),
            ("templated field", _lineage_dsl({"target_content_hash": "${trace:a:${record.f}}"})),
            ("text outside", _lineage_dsl({"target_content_hash": "x" + ph})),
            ("not a string", _lineage_dsl({"target_content_hash": 5})),
            ("static", _lineage_dsl(**{"sessions/agent/static/lineage": []})),
            ("derived below", _lineage_dsl(**{"sessions/agent/derived/lineage.0": "$record.l"})),
            ("passthrough", _lineage_dsl(**{"sessions/agent/passthrough/lineage": "x"})),
            ("computed", _lineage_dsl(**{"finalize/computed/lineage": "$state.l"})),
            ("set session.lineage", _lineage_dsl(**{"emit/0/do/0": {"set": {"session.lineage": "$record.l"}}})),
            ("set bare lineage", _lineage_dsl(**{"emit/0/do/0": {"set": {"lineage": "$record.l"}}})),
            ("on_block", _lineage_dsl(**{"emit/0/on_block": [{"name": "b", "do": [
                {"emit_lineage": {"target_trace_id": "x", "target_content_hash": "$block.h"}}]}]}))):
        assert len(D.dsl_lineage_problems(d)) == 1, (name, D.dsl_lineage_problems(d))
    for name, d in (
            ("absent", _lineage_dsl()),
            ("null literal", _lineage_dsl({"target_content_hash": None})),
            ("whole template", _lineage_dsl({"target_content_hash": ph})),
            ("colon in trace-id", _lineage_dsl({"target_content_hash": "${trace:a:b-${record.to}:content_hash}"})),
            ("state.lineage", _lineage_dsl(**{"emit/0/do/0": {"set": {"state.lineage": "$record.l"}}})),
            ("lineage_note", _lineage_dsl(**{"sessions/agent/static/lineage_note": "x"}))):
        assert D.dsl_lineage_problems(d) == [], (name, D.dsl_lineage_problems(d))


def test_a_data_supplied_placeholder_is_rejected_before_input_is_read():
    # The reproducer: target_content_hash "$record.h" let data carry
    # ${trace:victim:content_hash} into a resolved integrity edge.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    d = _lineage_dsl({"target_content_hash": "$record.h"})
    d["adapter"].update({"name": "t", "harness_version": "1", "source_format": "x",
                         "converter_version": "x", "produces_schema": "0.3.0"})
    d["input"] = {"type": "json", "encoding": "utf-8", "container": {"record_path": "events"}}
    d["sessions"]["agent"]["static"] = {"schema_version": "0.3.0", "trace_type": "agent-session"}
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(d))
        try:
            D.run(td / "a.dsl.yaml", td / "absent-input.json", None, operator_override=_OP)
        except SystemExit as e:
            assert "§13.1" in str(e) and "$record.h" in str(e), e
            return
    raise AssertionError("a data-supplied placeholder form loaded")


def test_unusable_placeholder_part_does_not_resolve_to_the_bare_prefix():
    # A trace really named "agent-" exists; an edge built from
    # "${trace:agent-${record.to}:content_hash}" with to "" must not bind it.
    dsl = """
adapter: {name: t, harness_version: "1", dsl_version: "0.1.0", source_format: x, converter_version: x, produces_schema: "0.3.0"}
input: {type: json, encoding: utf-8, container: {record_path: events}}
sessions:
  agent:
    static: {schema_version: "0.3.0", trace_type: agent-session}
    derived: {id: "$container.trace_id"}
emit:
  - target: agent
    name: lin
    when: {has: to}
    do:
      - emit_lineage:
          relation: derived_from
          target_trace_id: "$record.to"
          target_content_hash: "${trace:agent-${record.to}:content_hash}"
"""
    for bad in ("", True):
        data = [{"trace_id": "agent-", "events": []}, {"trace_id": "p", "events": [{"to": bad}]}]
        ids, err, out = _ids_and_diags(dsl, data)
        p = next(t for t in out if t["id"] == "p")
        assert p["lineage"] == [], (bad, p["lineage"])
        assert "R4" in err, (bad, err)


def test_r5_applies_to_single_trace_containers():
    data = [{"trace_id": "a", "events": []}, {"trace_id": "a", "events": []},
            {"trace_id": None, "events": []}, {"trace_id": "b", "events": []}]
    dsl = _ID_DSL.replace("  - target: agent\n    name: lin", "  - name: lin") % (_SINGLE % "$container.trace_id")
    ids, err, _ = _ids_and_diags(dsl, data)
    assert ids == ["b"], ids
    assert err.count("(R5)") == 2, err


def test_r5_applies_to_a_single_plain_trace():
    dsl = ('adapter: {name: t, harness_version: "1", dsl_version: "0.1.0", source_format: x, '
           'converter_version: x, produces_schema: "0.3.0"}\ninput: {type: json, encoding: utf-8}\n'
           + _SINGLE % "$record_first.sid" + "\nemit: []\n")
    ids, err, _ = _ids_and_diags(dsl, [{"x": 1}])
    assert ids == [] and "not emitted (R5)" in err, (ids, err)
    ids, _, _ = _ids_and_diags(dsl, [{"sid": "s1"}])
    assert ids == ["s1"], ids


# --- claude-ai ordered raw timestamp strings ---------------------------

def test_claude_ai_orders_chronologically_after_normalising():
    import preprocess_claude_ai as P  # noqa: PLC0415
    conv = {"uuid": "c", "created_at": "2026-10-03T08:00:00+00:00", "chat_messages": [
        {"uuid": "late", "parent_message_uuid": None, "created_at": "2026-10-03T10:00:00.5Z"},
        {"uuid": "early", "parent_message_uuid": None, "created_at": "2026-10-03T10:00:00Z"},
        {"uuid": "earliest", "parent_message_uuid": None, "created_at": "2026-10-03T11:00:00+02:00"}]}
    counts: dict = {}
    out = P.preprocess_conversation(conv, counts)
    assert [m["uuid"] for m in out["chat_messages"]] == ["earliest", "early", "late"]
    assert out["created_at"] == "2026-10-03T08:00:00.000000Z"
    assert counts == {"normalised": 4}, counts


# --- §8.3 "one hostile record must not suppress the rest" -

_ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
_OP = {"type": "scripted"}


def _run_dsl(dsl_text_or_path, data):
    import tempfile  # noqa: PLC0415
    D = _dr()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        dsl = dsl_text_or_path
        if isinstance(dsl, str):
            (td / "a.dsl.yaml").write_text(dsl)
            dsl = td / "a.dsl.yaml"
        (td / "in.json").write_text(json.dumps(data))
        return D.run(dsl, td / "in.json", None, operator_override=_OP)


def _claude_ai_run(convs, dsl="2026-09-26.dsl.yaml"):
    import tempfile  # noqa: PLC0415
    import preprocess_claude_ai as P  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        src, dst = pathlib.Path(td) / "in.json", pathlib.Path(td) / "out.json"
        src.write_text(json.dumps(convs))
        assert P.main(["x", str(src), str(dst)]) == 0
        return _run_dsl(_ADAPTERS / "claude-ai" / dsl, json.loads(dst.read_text()))


def _conv(uuid, content, **msg_kw):
    m = {"uuid": f"{uuid}-m", "sender": "assistant", "created_at": "2026-01-01T00:00:01.000000Z",
         "text": "t", "content": content, **msg_kw}
    return {"uuid": uuid, "name": "n", "summary": "", "created_at": "2026-01-01T00:00:00.000000Z",
            "chat_messages": [m]}


def _claude_ai_results(dsl, blocks):
    """{call id: its result text, or None when nothing attached} through
    the shipped preprocessor and DSL."""
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    with contextlib.redirect_stderr(io.StringIO()):
        (trace,) = _claude_ai_run([_conv("c", blocks)], dsl)
    return {c["id"]: (c.get("output") or {}).get("result") for c in trace["tool_calls"]}


def _ca_tu(name, tid=None):
    return {"type": "tool_use", "name": name, "input": {}, "start_timestamp": "2026-01-01T00:00:01.000000Z",
            **({"id": tid} if tid is not None else {})}


def _ca_tr(text, tid=None):
    return {"type": "tool_result", "content": [{"type": "text", "text": text}], "is_error": False,
            **({"tool_use_id": tid} if tid is not None else {})}


def test_claude_ai_tool_results_attach_to_the_call_their_id_names():
    # Ruled 2026-10-21: a tool_result attaches to the call its tool_use_id
    # names, so parallel results land on their own calls in either order;
    # one without a tool_use_id attaches to the last call emitted; one whose
    # id names no emitted call stays unattached, never placed by position.
    # Both shipped DSLs; 2026-05-03 now emits the block's own id as
    # 2026-09-26 does, so the ids can match.
    for dsl in ("2026-09-26.dsl.yaml", "2026-05-03.dsl.yaml"):
        got = _claude_ai_results(dsl, [_ca_tu("a", "toolu_a"), _ca_tu("b", "toolu_b"),
                                       _ca_tr("ra", "toolu_a"), _ca_tr("rb", "toolu_b")])
        assert got == {"toolu_a": "ra", "toolu_b": "rb"}, (dsl, got)
        got = _claude_ai_results(dsl, [_ca_tu("a", "toolu_a"), _ca_tu("b", "toolu_b"),
                                       _ca_tr("rb", "toolu_b"), _ca_tr("ra", "toolu_a")])
        assert got == {"toolu_a": "ra", "toolu_b": "rb"}, (dsl, got)
        got = _claude_ai_results(dsl, [_ca_tu("a", "toolu_a"), _ca_tu("s"), _ca_tr("rs"), _ca_tr("ra", "toolu_a")])
        assert got == {"toolu_a": "ra", "claude-ai-tu-2": "rs"}, (dsl, got)
        got = _claude_ai_results(dsl, [_ca_tu("a", "toolu_a"), _ca_tr("rz", "toolu_z")])
        assert got == {"toolu_a": None}, (dsl, got)
        # Only an absent or null tool_use_id is positional; a falsy value is
        # an id that names no call.
        for falsy in ("", 0, False, [], {}):
            got = _claude_ai_results(dsl, [_ca_tu("a", "toolu_a"), _ca_tr("rf", falsy)])
            assert got == {"toolu_a": None}, (dsl, falsy, got)
        got = _claude_ai_results(dsl, [_ca_tu("a", "toolu_a"), {**_ca_tr("rn"), "tool_use_id": None}])
        assert got == {"toolu_a": "rn"}, (dsl, got)
        got = _claude_ai_results(dsl, [_ca_tu("s"), _ca_tu("t"), _ca_tu("x", "toolu_x")])
        assert list(got) == ["claude-ai-tu-1", "claude-ai-tu-2", "toolu_x"], (dsl, got)


def _cursor_shell(command, *names):
    return {"command": command, "parsingResult": {"parsingFailed": False, "executableCommands": [
        {"name": n, "args": [{"type": "word", "value": "log" if n == "git" else "a"}]} for n in names]}}


def test_cursor_keeps_results_whole_and_reads_a_failed_shell_as_a_failure():
    # A result and a structured error are kept whole (no length cut); a shell
    # command that exited non-zero is a failed call with its stderr as the
    # error, or "exit code N" when stderr is empty; a falsy stdout of another
    # type is JSON text and counted, while "" and null stay "" and uncounted.
    import preprocess_cursor as C  # noqa: PLC0415

    def call(kind, result, args=None):
        C._results_as_json = 0
        tc = C.build_tool_call(kind, None, {kind: {"args": args or {}, "result": result}, "toolCallId": "t"},
                               0, "s")
        return tc, C._results_as_json

    long_out = "x" * 5000 + "END"
    tc, n = call("shellToolCall", {"success": {"stdout": long_out, "stderr": "", "exitCode": 0}}, {"command": "cat f"})
    assert (tc["result"], tc["success"], tc["error"], n) == (long_out, True, None, 0), tc
    tc, n = call("readToolCall", {"success": {"content": "y" * 5000 + "END"}})
    assert tc["result"].endswith('END"}') and len(tc["result"]) > 5000 and n == 0, tc["result"][-20:]
    err = {"message": "z" * 3000 + "END"}
    tc, _ = call("readToolCall", {"error": err})
    assert tc["error"] == json.dumps(err, ensure_ascii=False) and tc["success"] is False, tc["error"][-20:]
    tc, _ = call("readToolCall", {"error": None})
    assert tc["error"] == "null", tc
    for exit_code, stderr, want in ((1, "Permission denied", "Permission denied"), (2, "", "exit code 2"),
                                    (127, None, "exit code 127")):
        tc, _ = call("shellToolCall", {"success": {"stdout": "part", "stderr": stderr, "exitCode": exit_code}},
                     {"command": "cat f"})
        assert (tc["success"], tc["error"], tc["result"]) == (False, want, "part"), (exit_code, tc)
    for exit_code in (0, None, True, "1"):   # only a non-zero integer is a failure
        tc, _ = call("shellToolCall", {"success": {"stdout": "ok", "stderr": "", "exitCode": exit_code}},
                     {"command": "cat f"})
        assert (tc["success"], tc["error"]) == (True, None), (exit_code, tc)
    for value, text in ((0, "0"), (False, "false"), ([], "[]"), ({}, "{}")):
        tc, n = call("shellToolCall", {"success": {"stdout": value, "stderr": "", "exitCode": 0}}, {"command": "cat f"})
        assert (tc["result"], n) == (text, 1), (value, tc["result"], n)
    for value in ("", None):
        tc, n = call("shellToolCall", {"success": {"stdout": value, "stderr": "", "exitCode": 0}}, {"command": "cat f"})
        assert (tc["result"], n) == ("", 0), (value, tc["result"], n)
    tc, n = call("editToolCall", {"success": {"diffString": "", "message": "edited"}}, {"path": "f"})
    assert (tc["result"], n) == ("edited", 0), tc
    # The same rule on the edit and other paths: a falsy value of another
    # type is selected, not passed over.
    tc, n = call("editToolCall", {"success": {"diffString": 0, "message": "edited"}}, {"path": "f"})
    assert (tc["result"], n) == ("0", 1), tc
    tc, n = call("readToolCall", {"success": {"message": []}})
    assert (tc["result"], n) == ("[]", 1), tc
    # A rejection or an error member names the failure before the exit code;
    # a float exit code is not an integer exit status.
    tc, _ = call("shellToolCall", {"rejected": {"reason": "no"}, "success": {"stdout": "", "stderr": "e", "exitCode": 1}},
                 {"command": "cat f"})
    assert (tc["success"], tc["error"]) == (False, "rejected: no"), tc
    tc, _ = call("shellToolCall", {"error": "boom", "success": {"stdout": "", "stderr": "e", "exitCode": 1}},
                 {"command": "cat f"})
    assert (tc["success"], tc["error"]) == (False, "boom"), tc
    tc, _ = call("shellToolCall", {"success": {"stdout": "ok", "stderr": "", "exitCode": 1.0}}, {"command": "cat f"})
    assert (tc["success"], tc["error"]) == (True, None), tc


def test_cursor_keeps_a_result_of_an_unexpected_shape_and_warns():
    # A result.success that is not an object still reports success (as an
    # empty object does) and a result that is not an object reports none, as
    # before; the value itself is kept in output.result (a string as written,
    # else JSON text) and counted for the stream's warning, not read as "".
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import preprocess_cursor as C  # noqa: PLC0415

    def call(result):
        C._results_odd_shape = C._results_as_json = 0
        tc = C.build_tool_call("readToolCall", None,
                               {"readToolCall": {"args": {}, "result": result}, "toolCallId": "t"}, 0, "s")
        return tc, C._results_odd_shape

    for result, success, text in (({"success": "done"}, True, "done"), ({"success": 0}, True, "0"),
                                  ({"success": []}, True, "[]"), ("oops", False, "oops"),
                                  ([1, 2], False, "[1, 2]"), (7, False, "7")):
        tc, n = call(result)
        assert (tc["success"], tc["result"], tc["error"], n) == (success, text, None, 1), (result, tc, n)
    for result in ({"success": {"content": "x"}}, {"success": {}}, {"success": None}, {"success": ""}, None, "", {}):
        tc, n = call(result)
        assert n == 0, (result, tc, n)
        if result in ({"success": ""}, ""):
            # nothing to keep, nothing counted; success reads as before
            assert (tc["result"], tc["success"]) == ("", result != ""), (result, tc)
    stream = [{"type": "system", "subtype": "init", "session_id": "s", "model": "m", "cwd": "/w"},
              {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
               "session_id": "s"},
              {"type": "tool_call", "subtype": "completed", "call_id": "c1", "session_id": "s",
               "tool_call": {"readToolCall": {"args": {"path": "f"}, "result": "oops"}}},
              {"type": "result", "subtype": "success", "session_id": "s", "result": "ok"}]
    pre = pathlib.Path(__file__).resolve().parent.parent / "cursor" / "preprocess_cursor.py"
    with tempfile.TemporaryDirectory() as td:
        src, dst = pathlib.Path(td) / "s.jsonl", pathlib.Path(td) / "out.json"
        src.write_text("".join(json.dumps(e) + "\n" for e in stream))
        r = subprocess.run([sys.executable, str(pre), str(src), str(dst)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "warning: 1 tool result(s) whose result or result.success is not an object" in r.stderr, r.stderr


def test_shell_commands_that_may_run_more_than_one_verb_are_never_read():
    # Ruled 2026-10-21: a separator (; & && | ||), a line break, command
    # substitution ($( or a backtick) or < anywhere in the command, quoted
    # text included, turns what would be READ into EXECUTE; an output
    # redirect and a NEW or MODIFY verb keep precedence. Codex, cursor's
    # fallback and cursor's parsed path alike; the parsed path is READ only
    # for exactly one executable command and a string command with none of
    # these.
    import preprocess_codex as X  # noqa: PLC0415
    import preprocess_cursor as C  # noqa: PLC0415
    classifiers = {"codex": X.classify_operation_from_command, "cursor": C._classify_shell_fallback}
    compound = ["cat README.md && rm -rf /", "cat foo; rm bar", "cat a | sh", "cat a || rm b", "cat a & rm b",
                "cat a\nrm b", "cat a\rrm b", "cat $(rm b)", "cat `rm b`", "cat < a", "cat <(rm b)",
                "git log; rm x", "git status && rm x", "grep ';' f", "rm a; cat b",
                "ls " + "a" * 1100 + "; rm x"]   # the separator past the first 1024 characters
    expected = [("cat README.md", "READ"), ("git log --oneline", "READ"), ("grep -n foo f", "READ"),
                ("find . > x", "NEW"), ("cat a >> b", "MODIFY"), ("sed -i s/a/b/ f; cat f", "MODIFY"),
                ("touch a; rm b", "NEW")] + [(c, "EXECUTE") for c in compound]
    for name, classify in classifiers.items():
        got = [(cmd, classify(cmd)) for cmd, _ in expected]
        assert got == expected, (name, [(g, e) for g, e in zip(got, expected) if g != e])
    assert X.classify_operation_from_command("python3 -c 'open(1).read()'") == "READ"
    assert X.classify_operation_from_command("python3 -c 'open(1).read()'; rm x") == "EXECUTE"
    parsed = [(_cursor_shell("cat a", "cat"), "READ"), (_cursor_shell("git log", "git"), "READ"),
              (_cursor_shell("cat $(rm b)", "cat"), "EXECUTE"), (_cursor_shell("git log | sh", "git"), "EXECUTE"),
              (_cursor_shell("cat a", "cat", "rm"), "EXECUTE"), (_cursor_shell("git log", "git", "rm"), "EXECUTE"),
              (_cursor_shell(5, "cat"), "EXECUTE"), (_cursor_shell(None, "cat"), "EXECUTE"),
              (_cursor_shell("", "cat"), "EXECUTE"), (_cursor_shell(0, "cat"), "EXECUTE"),
              (_cursor_shell([], "cat"), "EXECUTE"),
              ({k: v for k, v in _cursor_shell("cat a", "cat").items() if k != "command"}, "EXECUTE"),
              (_cursor_shell("touch a && rm b", "touch", "rm"), "NEW")]
    got = [(a.get("command"), [e["name"] for e in a["parsingResult"]["executableCommands"]], C.classify_shell(a))
           for a, _ in parsed]
    assert [g[2] for g in got] == [e for _, e in parsed], got
    # A command that is not a string is EXECUTE in every path, not a crash.
    for bad in (["cat", "a"], 5, {"x": 1}):
        assert X.classify_operation_from_command(bad) == "EXECUTE", bad
        assert C._classify_shell_fallback(bad) == "EXECUTE", bad
        assert C.classify_shell({"command": bad}) == "EXECUTE", bad
        record = X.build_tool_call_record("c", "exec_command", {"cmd": bad})
        assert (record["operation_type"], record["file_path"]) == ("EXECUTE", None), record


def test_non_list_content_does_not_lose_other_conversations():
    good = _conv("good", [{"type": "text", "text": "hi"}])
    for bad_content in ("plain string", ["x"], 5, {"a": 1}):
        traces = _claude_ai_run([good, _conv("bad", bad_content)])
        assert len(traces) == 2, (bad_content, traces)


def test_claude_ai_preprocessor_skips_non_object_shapes():
    # Each hostile shape is skipped and counted, or kept with its bad field
    # treated as absent, and the rest is ordered and converted (the
    # earlier version asserted only that "good" survived).
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import preprocess_claude_ai as P  # noqa: PLC0415
    good = _conv("good", [{"type": "text", "text": "hi"}])
    hostile = [5, {"uuid": "n1", "chat_messages": [None]}, {"uuid": "n2", "chat_messages": {"a": 1}},
               {"uuid": "u", "chat_messages": [{"uuid": {"x": 1}, "parent_message_uuid": ["p"],
                                                "created_at": "2026-01-01T00:00:01Z", "content": []}]},
               {"uuid": "mix", "created_at": "2026-01-01T00:00:00Z", "chat_messages": [
                   {"uuid": "b", "created_at": "x"}, {"uuid": "a", "created_at": 5}]}]
    with tempfile.TemporaryDirectory() as td:
        src, dst = pathlib.Path(td) / "in.json", pathlib.Path(td) / "out.json"
        src.write_text(json.dumps([good, *hostile]))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert P.main(["x", str(src), str(dst)]) == 0
        pre = {c["uuid"]: [m.get("uuid") for m in c["chat_messages"]]
               for c in json.loads(dst.read_text())}
    assert "skipped 1 non-object conversation(s), 2 non-object message(s)" in err.getvalue(), err
    # 5 skipped; n1's None and n2's non-list skipped; u's object uuid kept as
    # absent for ordering; mix sorted with a non-string created_at as "".
    assert pre == {"good": ["good-m"], "n1": [], "n2": [], "u": [{"x": 1}], "mix": ["a", "b"]}, pre
    with contextlib.redirect_stderr(io.StringIO()):
        traces = _claude_ai_run([good, *hostile])
    assert {t["id"]: len(t["turns"]) for t in traces} \
        == {"good": 1, "n1": 0, "n2": 0, "u": 1, "mix": 2}, traces


def test_unhashable_id_or_tool_name_is_null_not_an_abort():
    good = _conv("good", [{"type": "text", "text": "hi"}])
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    # DSL rc3 §8.2.3 (spec 146cdeb3): a list tool_name becomes null; a
    # list id identifies no call, so the call is not emitted. Neither aborts.
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        traces = _claude_ai_run([good, _conv("bad", [{"type": "tool_use", "name": ["repl"], "input": {}}])])
    by = {t["id"]: t for t in traces}
    assert sorted(by) == ["bad", "good"], sorted(by)
    (call,) = by["bad"]["tool_calls"]
    assert call["tool_name"] is None and call["id"] == "claude-ai-tu-1", call
    assert "emit_tool_call.tool_name is a list, not a string; null" in err.getvalue(), err.getvalue()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        traces = _claude_ai_run([good, _conv("bad", [{"type": "tool_use", "id": [1], "name": "repl", "input": {}}])])
    by = {t["id"]: t for t in traces}
    assert sorted(by) == ["bad", "good"] and by["bad"]["tool_calls"] == [], by["bad"]["tool_calls"]
    assert "is not a usable id; tool call not emitted" in err.getvalue(), err.getvalue()


_MT_DSL = """
adapter: {name: t, harness_version: "1", dsl_version: "0.1.0", source_format: x, converter_version: x, produces_schema: "0.3.0"}
input: {type: json, encoding: utf-8, container: {record_path: events}}
state: {total: 0}
sessions:
  agent:
    static: {schema_version: "0.3.0", trace_type: agent-session}
    derived: {id: "$container.trace_id"}
emit:
  - target: agent
    name: tok
    when: {has: tokens}
    do:
      - accumulate:
          state.total: "$record.tokens"
finalize:
  computed:
    metrics.total_tokens: "$state.total"
"""


def test_accumulator_overflow_keeps_prior_value_and_the_set():
    big = 9007199254740991
    traces = _run_dsl(_MT_DSL, [{"trace_id": "good", "events": [{"tokens": 5}]},
                                {"trace_id": "bad", "events": [{"tokens": big}, {"tokens": big}]}])
    by = {t["id"]: t for t in traces}
    assert by["good"]["metrics"]["total_tokens"] == 5
    assert by["bad"]["metrics"]["total_tokens"] == big        # prior value kept
    assert all(t["content_hash"].startswith("sha256:") for t in traces)


def test_arithmetic_out_of_domain_is_an_input_failure():
    value, diags = _eval_collecting("$record.a + $record.a", {"record": {"a": 9007199254740991}})
    assert value is None and len(diags) == 1, (value, diags)
    value, diags = _eval_collecting("$record.a + $record.a", {"record": {"a": 1.7e308}})
    assert value is None and len(diags) == 1, (value, diags)


# --- evaluator boundary ----

def _structural(expr, ctx=None, *, why):
    """True when evaluation fails structurally for the named reason: any
    structural failure would let a case pass on a cause it does not test
    (a parse error where an unknown root was meant)."""
    D = _dr()
    try:
        D.evaluate(expr, ctx or {})
    except (D.ExpressionEvalError, D.UnsafeExpressionError) as e:
        assert why in str(e), (why, str(e))
        return True
    return False


def test_text_decided_structural_failures_are_named_at_load():
    # §8.3: whatever the text alone decides is structural, reached or not.
    D = _dr()
    for expr, why in (
            ("$foo.a", "is not a context variable"),
            ("$record.a +", "does not parse"),
            ("$record.a * 2", "unsupported binop"),
            ("$record.a and $record.b", "unsupported AST node"),
            ("$record.a >= 1", "unsupported comparator"),
            ("+$record.a", "unsupported unary op"),
            ("len", "outside call position"),
            ("$record.a.upper()", "does not define"),
            ("st_block_text($record.a)", "does not define"),
            ("len($record.a, $record.b)", "called with 2 argument(s)"),
            ("max(x=$record.a)", "keyword arguments"),
            ("$record.__class__", "is not accessible"),
            ("coalesce($record.a, 1j)", "complex is not a JSON value"),
            ("parse_iso($record.a) - parse_iso($record.b)", "does not define"),   # §9.1 has no parse_iso
            ("$record.a ? len($record.a, 1) : 0", "called with 2 argument(s)"),   # untaken branch
            ("not " * 3000 + "$record.a", "nested too deeply")):
        problem = D.expression_problem(expr)
        assert problem and why in problem, (expr, problem)
    for expr in ("$record.a", "coalesce($record.a, 'x')", "$record.a ? 1 : 2", "len($record.l) > 0",
                 "$record.a == 'b'", "-1", "not $record.a", "$record.l[0]", "duration_seconds($record.a, $record.b)"):
        assert D.expression_problem(expr) is None, (expr, D.expression_problem(expr))


def test_keyword_led_template_bodies_are_evaluated_as_written_at_load_and_run():
    # §8.3: the optional $ applies only to a body that begins with a
    # non-keyword identifier; ${not record.ok} and ${true} are valid, and the
    # load pass and evaluation agree on every form, leading spaces included.
    D = _dr()
    ctx = {"record": {"ok": False, "id": "x"}}
    for tmpl, rendered in (("${not record.ok}", "true"), ("${ not record.ok}", "true"), ("${true}", "true"),
                           ("${False}", "false"), ("${null}", ""), ("${2}", "2"), ("${'q'}", "q"),
                           ("${record.id}", "x"), ("${ record.id}", "x"), ("${$record.id}", "x")):
        assert [D.expression_problem(b) for b in D._template_bodies(tmpl)] == [None], tmpl
        assert D.evaluate(tmpl, ctx) == rendered, (tmpl, D.evaluate(tmpl, ctx))
    for tmpl in ("${notes.x}", "${trueish}", "${not}"):   # a keyword only as the whole identifier
        assert D.expression_problem(D._template_bodies(tmpl)[0]), tmpl


def test_chained_comparisons_and_other_index_forms_are_structural():
    # DSL rc3 §8.3: a chained comparison and any index form other than [n]
    # (non-negative integer constant) and ['name'] (string constant) are
    # structural, at load and at evaluation alike, whatever the data.
    D = _dr()
    ctx = {"record": {"a": 5, "l": [10, 20], "s": "abc", "o": {"k": "v"}}, "state": {"i": 0}}
    for expr in ("1 < $record.a < 9", "$record.a == 5 == True", "$record.l[-1]", "$record.l[$state.i]",
                 "$record.l[true]", "$record.l[1:2]", "$record.l[1.0]", "$record.o[$record.s]"):
        assert D.expression_problem(expr), expr
        try:
            D._evaluate_expression(expr, ctx)
        except D.ExpressionEvalError:
            continue
        raise AssertionError(f"{expr} evaluated")
    for expr, want in (("$record.l[1]", 20), ("$record.l[5]", None), ("$record.o['k']", "v"),
                       ("$record.s[0]", None), ("$record.o[0]", None), ("$record.l['k']", None)):
        assert D.expression_problem(expr) is None, expr
        assert D._evaluate_expression(expr, ctx) == want, (expr, D._evaluate_expression(expr, ctx))


def test_index_constant_outside_the_domain_is_structural():
    # DSL rc3 §2: a number constant outside ±(2^53-1) is structural at load,
    # an index constant included (review #1 of 1ad64437..f4f7ba10: the
    # index-form check in 1a6a9dbe had let it through as a null read).
    D = _dr()
    ctx = {"record": {"l": [10, 20]}}
    for expr in ("$record.l[9007199254740992]", "$record.l[99999999999999999999]"):
        problem = D.expression_problem(expr)
        assert problem and "domain" in problem, (expr, problem)
        try:
            D._evaluate_expression(expr, ctx)
        except D.ExpressionEvalError:
            continue
        raise AssertionError(f"{expr} evaluated")
    assert D.expression_problem("$record.l[9007199254740991]") is None
    assert D._evaluate_expression("$record.l[9007199254740991]", ctx) is None


def _schema_number_nodes(node, path, out):
    """Every node of a JSON Schema that admits an integer or a number, by
    path, with its type and whether it admits null; type unions in `type`,
    anyOf and oneOf are read as one node."""
    if isinstance(node, list):
        for i, n in enumerate(node):
            _schema_number_nodes(n, f"{path}/{i}", out)
        return
    if not isinstance(node, dict):
        return
    ts = set()
    for alt in [node, *(node.get("anyOf") or []), *(node.get("oneOf") or [])]:
        t = alt.get("type") if isinstance(alt, dict) else None   # a property named "type" is a dict
        ts |= {x for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)}
    if ts & {"integer", "number"}:
        alts = [node, *(node.get("anyOf") or []), *(node.get("oneOf") or [])]
        bound = lambda k: next((a[k] for a in alts if isinstance(a, dict) and k in a), None)  # noqa: E731
        assert not any(k in a for a in alts if isinstance(a, dict)
                       for k in ("exclusiveMinimum", "exclusiveMaximum", "multipleOf", "const", "enum")), path
        out[path] = ("integer" if "integer" in ts else "number", "null" in ts, bound("minimum"), bound("maximum"))
    for k, v in node.items():
        if k in ("anyOf", "oneOf") and isinstance(v, list):
            for i, alt in enumerate(v):
                if isinstance(alt, dict) and not (set(alt) <= {"type"}):
                    _schema_number_nodes(alt, f"{path}/{k}/{i}", out)
        elif isinstance(v, (dict, list)) and k not in ("enum", "const", "examples", "default"):
            _schema_number_nodes(v, f"{path}/{k}", out)


def test_schema_number_table_matches_the_base_schema():
    # The runner's table of numeric fields is the schema's, field for field,
    # with type and nullability, found by walking every node of every v0.3.0
    # schema (F2 census: 29 fields, 10 not admitting null; adding
    # ordering on turns and tool calls: 31).
    D = _dr()
    schemas = pathlib.Path(__file__).resolve().parents[2] / "schemas"
    found = {}
    for f in sorted(schemas.glob("minitrace-v0.3.0.*.schema.json")):
        _schema_number_nodes(json.loads(f.read_text()), f.name.removeprefix("minitrace-v0.3.0.").removesuffix(".schema.json"), found)
    defs = "$defs" if any("/$defs/" in k for k in found) else "definitions"
    want = {f"base/properties/metrics/properties/{k}": v for k, v in D._METRIC_NUMBERS.items()}
    by_def = {"turns": "Turn", "tool_calls": "ToolCall", "segment_markers": "SegmentMarker",
              "policy_triggers": "PolicyTrigger", "annotations": "Annotation"}
    for arr, fields in D._ITEM_NUMBERS.items():
        for k, t in fields.items():
            want[f"base/{defs}/{by_def[arr]}/properties/{k}"] = (t[0], True, t[1], t[2])
    want[f"base/{defs}/Turn/properties/index"] = ("integer", False, 0, None)
    assert found == want, (sorted(k for k in set(found) | set(want) if found.get(k) != want.get(k)), found)
    assert len(found) == 31 and sum(not v[1] for v in found.values()) == 10
    assert sum(v[2] is not None or v[3] is not None for v in found.values()) == 23   # census


def test_a_non_integer_ordering_on_a_turn_or_tool_call_becomes_null():
    # Ruled 2026-10-10: the base schema types ordering on Turn and
    # ToolCall as integer or null, so DSL §4 (l.113-116) reaches them: a value
    # the field cannot hold is set to null and reported with the trace and
    # path; an integer (by value, 2.0 included) and null are kept.
    D = _dr()
    odd = ["x", True, 1.5, "1", 9007199254740993, [1], {"a": 1}]
    turns = [{"index": i, "ordering": v} for i, v in enumerate(odd + [2.0, None, 3])]
    calls = [{"ordering": v} for v in odd + [2.0, None, 3]]
    t = {"id": "o", "turns": turns, "tool_calls": calls}
    diags = []
    [kept] = D._enforce_schema_numbers([t], diags)
    n = len(odd)
    assert [x["ordering"] for x in kept["turns"]] == [None] * n + [2.0, None, 3], kept["turns"]
    assert [x["ordering"] for x in kept["tool_calls"]] == [None] * n + [2.0, None, 3], kept["tool_calls"]
    assert sorted(d["field"] for d in diags) == sorted(
        [f"/turns/{i}/ordering" for i in range(n)] + [f"/tool_calls/{i}/ordering" for i in range(n)]), diags
    assert all(d["kind"] == "input-failure" and "trace 'o'" in d["message"] for d in diags), diags
    assert not D.set_level_loss(diags)
    # An item without the member is left without it.
    t2 = {"id": "p", "turns": [{"index": 0}], "tool_calls": [{}]}
    [k2] = D._enforce_schema_numbers([t2], [])
    assert "ordering" not in k2["turns"][0] and "ordering" not in k2["tool_calls"][0]


def test_schema_number_walk_sees_nested_and_union_fields():
    # The walk above is not vacuous: a numeric field under another object or
    # given as a oneOf union is found.
    out = {}
    _schema_number_nodes({"properties": {"timing": {"properties": {"probe_ms": {"type": ["integer", "null"]}}}},
                          "$defs": {"ToolCall": {"properties": {"duration_ms": {
                              "oneOf": [{"type": "integer"}, {"type": "null"}]}}}}}, "x", out)
    assert out == {"x/properties/timing/properties/probe_ms": ("integer", True, None, None),
                   "x/$defs/ToolCall/properties/duration_ms": ("integer", True, None, None)}, out


def test_values_numeric_schema_fields_cannot_hold_are_null_omitted_or_drop_the_trace():
    # DSL spec §4 (F2, ruled 2026-10-04): null where the field admits null,
    # the key omitted in the nine metrics fields that do not, and a turn whose
    # index is not its position (rc3 §4.1) drops the trace (set-level).
    D = _dr()

    def trace(tid, turns, metrics=None, tool_calls=()):
        return {"id": tid, "turns": turns, "metrics": dict(metrics or {}), "tool_calls": list(tool_calls)}
    ok_turns = [{"index": 0}, {"index": 1}]
    traces = [
        trace("m", ok_turns, {"turn_count": "1e16", "subagent_tool_calls": 2.5, "tool_call_count": 3,
                              "read_count": None, "total_input_tokens": "9007199254740993",
                              "session_cost": "1e999", "read_ratio": 0.5, "max_response_tokens": None},
              [{"emitting_turn_index": "1e16"}, {"emitting_turn_index": 1}]),
        trace("gap", [{"index": 0}, {"index": 2}]),
        trace("text", [{"index": 0}, {"index": "1"}]),
        trace("bool", [{"index": 0}, {"index": True}]),
        trace("float", [{"index": 0.5}]),
        trace("whole", [{"index": 0.0}, {"index": 1e0}]),   # T1: integers by value
        trace("big", [{"index": 9007199254740993}]),
    ]
    diags = []
    kept = D._enforce_schema_numbers(traces, diags)
    assert [t["id"] for t in kept] == ["m", "whole"], kept
    m = kept[0]["metrics"]
    assert m == {"tool_call_count": 3, "total_input_tokens": None, "session_cost": None,
                 "read_ratio": 0.5, "max_response_tokens": None}, m
    assert [c["emitting_turn_index"] for c in kept[0]["tool_calls"]] == [None, 1]
    assert D.set_level_loss(diags)
    by_kind = {}
    for d in diags:
        by_kind.setdefault(d["kind"], []).append(d)
    assert sorted(d["field"] for d in by_kind["input-failure"]) == [
        "/metrics/read_count", "/metrics/session_cost", "/metrics/subagent_tool_calls",
        "/metrics/total_input_tokens",
        "/metrics/turn_count", "/tool_calls/0/emitting_turn_index"], diags
    assert all(d["loss"] == "value" for d in by_kind["input-failure"])
    assert [d["field"] for d in by_kind["trace-dropped"]] == [
        "/turns/1/index", "/turns/1/index", "/turns/1/index", "/turns/0/index", "/turns/0/index"], diags
    assert all(d["loss"] == "set" and d["level"] == "error" for d in by_kind["trace-dropped"])
    assert len(diags) == 11, diags   # a null in a non-null field is reported too


def test_turn_index_from_input_that_is_not_its_position_drops_the_trace_end_to_end():
    # Through run(): the fixture maps index from input. A record whose index
    # is "1e16" (kept as text by the reader) or skips a position drops its
    # agent trace (set-level), and the judge trace's lineage edge to it is
    # then omitted by cross-trace resolution; the other container converts.
    import tempfile  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    with tempfile.TemporaryDirectory() as td:
        _turn_index_end_to_end(D, fixtures, pathlib.Path(td))


def _turn_index_end_to_end(D, fixtures, tmp_path):
    for bad in ("1e16", "2"):
        data = json.loads((fixtures / "synthetic-trace-pair.input.json").read_text())
        src = json.dumps(data).replace('"index": 1, "role": "assistant", "content": "synthetic response one"',
                                       f'"index": {bad}, "role": "assistant", "content": "synthetic response one"')
        assert src.count(f'"index": {bad},') == 1
        (tmp_path / "in.json").write_text(src)
        diags = []
        traces = D.run(fixtures / "synthetic-trace-pair.dsl.yaml", tmp_path / "in.json", None,
                       diagnostics_out=diags, operator_override=_OP)
        ids = sorted(t["id"] for t in traces)
        assert ids == ["agent-rec-002", "judge-rec-001", "judge-rec-002"], (bad, ids)
        dropped = [d for d in diags if d["kind"] == "trace-dropped"]
        assert len(dropped) == 1 and "agent-rec-001" in dropped[0]["message"], (bad, diags)
        assert D.set_level_loss(diags)
        judge = next(t for t in traces if t["id"] == "judge-rec-001")
        assert judge["lineage"] == [], (bad, judge["lineage"])
    # The other two output paths: multi-trace without a
    # container, and single-trace. Same DSL rules, records given directly.
    import yaml  # noqa: PLC0415
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    del base["input"]["container"]
    for name in ("agent", "judge"):
        base["sessions"][name]["derived"]["id"] = f"{name}-x"
        base["sessions"][name]["derived"]["title"] = f"{name}-x"
    judge_rule = base["emit"][1]["do"][0]["emit_lineage"]
    judge_rule["target_trace_id"] = "agent-x"
    judge_rule["target_content_hash"] = "${trace:agent-x:content_hash}"
    base["emit"][1]["do"][1]["emit_annotation"]["id"] = "ann-x"
    base["emit"][1]["do"][1]["emit_annotation"]["scope"]["target_id"] = "agent-x"
    single = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    single["session"] = base["sessions"]["agent"]
    single["emit"] = [{k: v for k, v in base["emit"][0].items() if k != "target"}]
    events = [{"index": 0, "role": "user", "content": "p"}, {"index": 1, "role": "assistant", "content": "r"}]
    for label, dsl, want_ids in (("multi", base, {"judge-x"}), ("single", single, None)):
        (tmp_path / f"{label}.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        for idx, emitted in ((1, True), ("1e16", False), (2, False)):
            (tmp_path / "ev.json").write_text(
                json.dumps(events).replace('"index": 1,', f'"index": {json.dumps(idx) if idx != "1e16" else idx},'))
            diags = []
            out = D.run(tmp_path / f"{label}.dsl.yaml", tmp_path / "ev.json", None, diagnostics_out=diags, operator_override=_OP)
            dropped = [d for d in diags if d["kind"] == "trace-dropped"]
            if label == "single":
                assert (out is not None) == emitted and len(dropped) == (not emitted), (label, idx, out, diags)
            else:
                ids = {t["id"] for t in out}
                assert ids == (want_ids | {"agent-x"} if emitted else want_ids), (label, idx, ids, diags)
                assert len(dropped) == (not emitted), (label, idx, diags)
            assert D.set_level_loss(diags) == (not emitted), (label, idx, diags)


def test_values_outside_the_schema_bounds_are_null_or_omitted():
    # Ruled 2026-10-05, option i; DSL rc3 §4 rule 2: a value outside
    # the bounds the base schema gives a numeric field is one it cannot hold:
    # null where null is admitted, the key omitted in the nine counts. The
    # bounds themselves are inclusive, and unbounded fields keep negatives.
    D = _dr()
    t = {"id": "b", "turns": [], "metrics": {
            "turn_count": -1, "tool_call_count": 0, "total_input_tokens": -5,
            "max_response_tokens": 0, "session_cost": -0.5, "read_ratio": -1},
         "tool_calls": [{"emitting_turn_index": -1}, {"emitting_turn_index": 0}],
         "policy_triggers": [{"threshold_proximity": 1.5, "ordering": -3}, {"threshold_proximity": 1}],
         "annotations": [{"near_miss_proximity": -0.1}, {"near_miss_proximity": 0}]}
    diags = []
    [kept] = D._enforce_schema_numbers([t], diags)
    assert kept["metrics"] == {"tool_call_count": 0, "total_input_tokens": None, "max_response_tokens": 0,
                               "session_cost": -0.5, "read_ratio": -1}, kept["metrics"]
    assert [c["emitting_turn_index"] for c in kept["tool_calls"]] == [None, 0]
    assert kept["policy_triggers"] == [{"threshold_proximity": None, "ordering": -3}, {"threshold_proximity": 1}]
    assert [a["near_miss_proximity"] for a in kept["annotations"]] == [None, 0]
    assert sorted(d["field"] for d in diags) == [
        "/annotations/0/near_miss_proximity", "/metrics/total_input_tokens", "/metrics/turn_count",
        "/policy_triggers/0/threshold_proximity", "/tool_calls/0/emitting_turn_index"], diags
    assert not D.set_level_loss(diags) and {d["loss"] for d in diags} == {"value"}, diags


def test_a_trace_dropped_for_its_index_still_counts_for_r5():
    # Ruled 2026-10-05, option a; DSL rc3 §4 rule 1, §13.2 R5: R5
    # decides on the traces as built, so the twin of a trace dropped for its
    # turn index is not emitted either, and edges naming the id fall under R4.
    import tempfile  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    good = [{"index": 0, "role": "user", "content": "p"}, {"index": 1, "role": "assistant", "content": "r"}]
    bad = [{"index": 0, "role": "user", "content": "p"}, {"index": 2, "role": "assistant", "content": "r"}]
    data = [{"id": "rec-001", "events": bad}, {"id": "rec-001", "events": good},
            {"id": "rec-002", "events": good}]
    with tempfile.TemporaryDirectory() as td:
        (pathlib.Path(td) / "in.json").write_text(json.dumps(data))
        diags = []
        traces = D.run(fixtures / "synthetic-trace-pair.dsl.yaml", pathlib.Path(td) / "in.json", None,
                       diagnostics_out=diags, operator_override=_OP)
    assert sorted(t["id"] for t in traces) == ["agent-rec-002", "judge-rec-002"], traces
    msgs = sorted(d["message"] for d in diags if d["kind"] == "trace-dropped")
    assert len(msgs) == 3, diags
    assert any("agent-rec-001" in m and "is shared by 2 traces" in m for m in msgs), msgs
    assert any("judge-rec-001" in m and "is shared by 2 traces" in m for m in msgs), msgs
    assert any("agent-rec-001" in m and "turn at position 1" in m for m in msgs), msgs
    assert next(t for t in traces if t["id"] == "judge-rec-002")["lineage"], "edge to a kept trace stays"


def test_operator_capture_mode_and_purpose_are_checked_before_emitting():
    # T10 (ruled 2026-10-05, option i; minitrace-spec §5.2, §5.3, DSL rc3
    # §7.1): human and delegated operators need a capture mode, and a
    # capture_purpose the runner writes is well-formed, or nothing is emitted.
    # Every accepted operator validates against the base schema.
    import types  # noqa: PLC0415
    import jsonschema  # noqa: PLC0415
    D = _dr()
    schemas = pathlib.Path(__file__).resolve().parents[2] / "schemas"
    base = json.loads((schemas / "minitrace-v0.3.0.base.schema.json").read_text())
    op_schema = {**base["$defs"]["Operator"], "$defs": base["$defs"]}
    bases = next(a["enum"] for a in base["$defs"]["Operator"]["allOf"][2]["then"]["properties"]
                 ["capture_purpose"]["then"]["properties"]["legal_basis"]["anyOf"] if "enum" in a)
    assert tuple(bases) == D._LEGAL_BASES

    def build(**kw):
        a = dict(operator_type=None, operator_aware=None, operator_capture_mode=None,
                 operator_capture_purpose_legal_basis=None, operator_capture_purpose_reference=None,
                 operator_capture_purpose_authorised_at=None)
        a.update(kw)
        return D._build_operator_from_args(types.SimpleNamespace(**a))

    def refused(**kw):
        try:
            build(**kw)
        except SystemExit as e:
            return str(e)
        raise AssertionError(f"accepted {kw}")
    full = dict(operator_type="human", operator_aware=True, operator_capture_mode="full",
                operator_capture_purpose_legal_basis="gdpr_art6_consent",
                operator_capture_purpose_reference="DPIA-7",
                operator_capture_purpose_authorised_at="2026-10-05T00:00:00.000000Z")
    accepted = [build(operator_type="delegated", operator_capture_mode="full"),
                build(operator_type="scripted"), build(**full),
                build(**{**full, "operator_capture_purpose_legal_basis": "x-local-policy"}),
                build(**{**full, "operator_aware": False})]
    assert accepted[0] == {"type": "delegated", "capture_mode": "full"}
    for op in accepted:
        jsonschema.validate(op, op_schema)
    assert "REQUIRED when operator-type is 'delegated'" in refused(operator_type="delegated")
    assert "REQUIRED when operator-type is 'human'" in refused(operator_type="human", operator_aware=True)
    assert "legal basis 'gdpr'" in refused(**{**full, "operator_capture_purpose_legal_basis": "gdpr"})
    msg = refused(**{**full, "operator_capture_purpose_authorised_at": "2026-10-05T00:00:00Z"})
    assert "not in the §3.5 form" in msg and "did you mean 2026-10-05T00:00:00.000000Z" in msg, msg
    assert "not in the §3.5 form" in refused(**{**full, "operator_capture_purpose_authorised_at":
                                                "2026-13-05T00:00:00.000000Z"})
    # CLI strings reach the trace, so they
    # are I-JSON too; a non-UTF-8 argv byte arrives as an unpaired surrogate.
    assert "noncharacter U+FDD0" in refused(**{**full, "operator_capture_purpose_legal_basis": "x-\ufdd0"})
    assert "noncharacter U+FFFF" in refused(**{**full, "operator_capture_purpose_reference": "r\uffff"})
    argv = b"r\xff".decode("utf-8", "surrogateescape")
    assert "unpaired surrogate U+DCFF" in refused(**{**full, "operator_capture_purpose_reference": argv})
    # The library path (run(operator_override=...)) applies the same rules:
    # every operator the CLI builds passes, and the refused shapes are refused.
    assert all(D._operator_problems(op) == [] for op in accepted)
    loose = {"type": "ai_overseer", "capture_purpose": {"legal_basis": "anything", "reference": "",
                                                        "authorised_at": "2026-10-05T00:00:00.000000Z"}}
    assert D._operator_problems(loose) == []   # §5.3 well-formedness binds human operators only
    jsonschema.validate(loose, op_schema)
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    for bad in ({"type": "delegated"}, {"type": "human", "operator_aware": True, "capture_mode": "full",
                                        "capture_purpose": None},
                # Malformed purposes, in full mode: the other modes are
                # refused on their own (ruled 2026-10-27).
                {"type": "human", "operator_aware": True, "capture_mode": "full",
                 "capture_purpose": {"legal_basis": "gdpr", "reference": "r",
                                     "authorised_at": "2026-10-05T00:00:00.000000Z"}},
                {"type": "robot"}, "scripted",
                # The schema's typing for every operator type.
                {"type": "scripted", "capture_purpose": "x"},
                {"type": "scripted", "capture_purpose": {"legal_basis": "a", "reference": "b",
                                                         "authorised_at": "junk"}},
                {"type": "delegated", "capture_mode": "full", "capture_purpose": {"legal_basis": 5}},
                {"type": "human", "operator_aware": True, "capture_mode": "full",
                 "capture_purpose": "x"},
                {"type": "scripted", "capture_mode": "weird"}, {"type": "ai_overseer", "operator_aware": "yes"},
                {"type": "scripted", "note": "\ud800"},
                # Spec lane verifier on cb443e77: runtime values also keep the
                # §3.4 numeric domain (DSL §7.1).
                {"type": "scripted", "n": 1e300}, {"type": "scripted", "n": [9007199254740992]},
                {"type": "ai_overseer", "capture_mode": "bogus", "capture_purpose": "x", "n": 1e300},
                # A Python value that is not a
                # JSON value hid what it held from the walk (dicts and lists only).
                {"type": "scripted", "x": ("\ud800",)}, {"type": "scripted", "x": (2**60,)},
                {"type": "scripted", 1: "a", "1": "b"}, {"type": "scripted", "x": b"1"},
                {"type": "scripted", "x": {1}}):
        try:
            D.run(fixtures / "synthetic-trace-pair.dsl.yaml", fixtures / "synthetic-trace-pair.input.json",
                  None, operator_override=bad)
        except SystemExit as e:
            assert "operator not conformant" in str(e), (bad, str(e))
        else:
            raise AssertionError(f"run accepted operator {bad}")


def test_dsl_strings_that_are_not_i_json_are_refused_and_nothing_non_i_json_is_written():
    # An emitted trace is I-JSON
    # (minitrace-spec §3.4). A DSL string or key holding an unpaired surrogate
    # or a noncharacter is structural at load; a raw U+FFFF in the YAML file
    # is a clean refusal, not a traceback; and a last guard drops (set-level,
    # reported) any trace that still holds one rather than writing it.
    import tempfile  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    src = (fixtures / "synthetic-trace-pair.dsl.yaml").read_text()
    with tempfile.TemporaryDirectory() as td:
        for esc, needle in (("\\uFFFF", "noncharacter U+FFFF"), ("\\uD800", "unpaired surrogate U+D800")):
            text = src.replace('title: "synthetic-pair-agent-${container.id}"', f'title: "a{esc}b-${{container.id}}"')
            assert text != src
            f = pathlib.Path(td) / "a.dsl.yaml"
            f.write_text(text)
            try:
                D.run(f, fixtures / "synthetic-trace-pair.input.json", None, diagnostics_out=[], operator_override=_OP)
            except SystemExit as e:
                assert "rejected at load" in str(e) and needle in str(e), str(e)
            else:
                raise AssertionError(f"{esc} loaded")
        assert D.dsl_string_problems({"a": {"k\ufdd0": "x", "l": ["ok", "\U0010ffff"]}}) == [
            "a.k\\ufdd0: key holds noncharacter U+FDD0", "a.l[1]: noncharacter U+10FFFF"]
    try:
        D.load_adapter_yaml("a: \uffff\n")
    except ValueError as e:
        assert "adapter YAML invalid" in str(e)
    else:
        raise AssertionError("raw U+FFFF loaded")
    diags = []
    ok = {"id": "ok", "turns": [{"content": "fine \ufffd"}]}
    kept = D._refuse_non_ijson([ok, {"id": "v", "x": ["\ud800"]}, {"id": "k", "y": {"\ufffe": 1}}], diags)
    assert kept == [ok] and [d["reason"] for d in diags] == ["unpaired-surrogate", "noncharacter"], diags
    assert D.set_level_loss(diags)
    diags = []
    assert D._refuse_non_ijson([ok, {"id": "t", "x": ("\ud800",)}, {"id": "n", "y": {1: "a", "1": "b"}}],
                               diags) == [ok]
    assert [d["reason"] for d in diags] == ["not-a-json-value"] * 2, diags
    diags = []   # Numbers the hashing cannot take
    assert D._refuse_non_ijson([ok, {"id": "i", "x": 2**60}, {"id": "f", "x": [float("nan")]}], diags) == [ok]
    assert [d["reason"] for d in diags] == ["number-outside-safe-range"] * 2, diags


def test_the_i_json_guard_runs_before_cross_trace_hashing_on_every_path():
    # The backstop ran after cross-trace
    # resolution, whose hashing raised on a surrogate first, so multi-trace
    # runs aborted. It now runs before, on every path, and R5 counts the id.
    # The operator check would refuse this input first; it is bypassed here
    # to reach the guard.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    real = D._operator_problems
    D._operator_problems = lambda op: []
    try:
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
            single = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
            single["input"] = {"type": "json"}
            single["session"] = base["sessions"]["agent"]
            single["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
            single["emit"] = [{k: v for k, v in base["emit"][0].items() if k != "target"}]
            (td / "single.dsl.yaml").write_text(yaml.safe_dump(single, sort_keys=False))
            (td / "ev.json").write_text(json.dumps([{"index": 0, "role": "user", "content": "p"}]))
            # The non-container multi-trace path too.
            src = (fixtures / "synthetic-trace-pair.dsl.yaml").read_text()
            flat = src.replace("  container:\n    record_path: events\n", "").replace("${container.id}", "x")
            assert "container" not in flat
            (td / "flat.dsl.yaml").write_text(flat)
            for dsl, inp, out in ((td / "flat.dsl.yaml", td / "ev.json", None),
                                  (fixtures / "synthetic-trace-pair.dsl.yaml",
                                   fixtures / "synthetic-trace-pair.input.json", None),
                                  (fixtures / "synthetic-trace-pair.dsl.yaml",
                                   fixtures / "synthetic-trace-pair.input.json", td / "dir/"),
                                  (td / "single.dsl.yaml", td / "ev.json", None)):
                diags = []
                got = D.run(dsl, inp, out, operator_override={"type": "scripted", "note": "\ud800"},
                            diagnostics_out=diags)
                assert not got, (dsl.name, out, got)
                dropped = [d for d in diags if d.get("kind") == "trace-dropped"]
                assert dropped and all(d.get("reason") == "unpaired-surrogate" for d in dropped), diags
                assert D.set_level_loss(diags)
    finally:
        D._operator_problems = real


def test_a_trace_dropped_as_not_i_json_still_counts_for_r5():
    # Per DSL rc3 §15 and §13.2 R5 (as already ruled for the
    # turn index): one trace that is not I-JSON is dropped, and its clean twin
    # with the same id is not emitted either; edges into the id fall under R4.
    # A number outside the domain is dropped the same
    # way, not an abort in the cross-trace hashing.
    import tempfile  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    good = [{"index": 0, "role": "user", "content": "p"}, {"index": 1, "role": "assistant", "content": "r"}]
    data = [{"id": "rec-001", "events": good}, {"id": "rec-001", "events": good},
            {"id": "rec-002", "events": good}]
    real = D._build_trace
    for spoil, needle in (("x\ud800", "unpaired surrogate"), (2**60, "outside the §3.4 domain"),
                          (float("nan"), "outside the §3.4 domain")):
        spoiled = []

        def build(*a, **kw):
            t, diag = real(*a, **kw)
            if t.get("id") == "agent-rec-001" and not spoiled:
                t["title"] = spoil   # past every earlier check, as a runner defect would be
                spoiled.append(t)
            return t, diag
        D._build_trace = build
        try:
            with tempfile.TemporaryDirectory() as td:
                (pathlib.Path(td) / "in.json").write_text(json.dumps(data))
                diags = []
                traces = D.run(fixtures / "synthetic-trace-pair.dsl.yaml", pathlib.Path(td) / "in.json", None,
                               diagnostics_out=diags, operator_override=_OP)
        finally:
            D._build_trace = real
        assert spoiled
        assert sorted(t["id"] for t in traces) == ["agent-rec-002", "judge-rec-002"], (spoil, traces)
        msgs = sorted(d["message"] for d in diags if d["kind"] == "trace-dropped")
        assert len(msgs) == 3, (spoil, diags)
        assert any("agent-rec-001" in m and "is shared by 2 traces" in m for m in msgs), msgs
        assert any("judge-rec-001" in m and "is shared by 2 traces" in m for m in msgs), msgs
        assert any("agent-rec-001" in m and needle in m for m in msgs), msgs


def test_ordering_comparisons_take_two_numbers_or_two_spec_timestamps():
    # Review #3 of 1ad64437..f4f7ba10; DSL rc3 §8.3: `<` and `>` take
    # two numbers (never booleans) or two timestamps already in the §3.5 form,
    # compared chronologically; any other pair, null included, is an input
    # failure. Before, null read as 0, so `$missing < 1` was true. (A
    # parenthesised chain is structural at load since 49647777, below.)
    ctx = {"record": {"a": 5, "f": 2.5, "s": "x", "b": True,
                      "t1": "2026-10-05T00:00:00.000000Z", "t2": "2026-10-05T00:00:01.000000Z",
                      "tz": "2026-10-05T00:00:00Z"}}
    for expr, want in (("$record.a < 9", True), ("$record.a > 9", False), ("$record.f > $record.a", False),
                       ("$record.t1 < $record.t2", True), ("$record.t2 > $record.t1", True),
                       ("-$record.a < 0", True)):
        value, diags = _eval_collecting(expr, ctx)
        assert value is want and diags == [], (expr, value, diags)
    for expr in ("$record.missing < 1", "$record.missing > -1", "$record.s < 1",
                 "$record.b < 2", "$record.a > $record.b", "$record.tz < $record.t2", "$record.s < $record.t1"):
        value, diags = _eval_collecting(expr, ctx)
        assert value is None and len(diags) == 1, (expr, value, diags)


def test_a_comparison_or_not_as_an_operand_of_ordering_is_structural():
    # DSL rc3 §8.3 (ruled 2026-10-05, option i; spec 49647777): an operand
    # of < or > that is a comparison or a `not` expression, parenthesised or
    # not, is structural, at load and at evaluation alike; under == and != it
    # stays valid; `not x > 0` is `not (x > 0)`; a boolean from data stays an
    # input failure.
    D = _dr()
    ctx = {"record": {"x": 5, "y": 1, "t": True}}
    for expr in ("(1 < $record.x) < 9", "(not $record.x) > 0", "9 > ($record.x > 1)",
                 "($record.x == 5) < 2", "(not $record.t) < (not $record.t)"):
        assert D.expression_problem(expr), expr
        try:
            D._evaluate_expression(expr, ctx)
        except D.ExpressionEvalError:
            continue
        raise AssertionError(f"{expr} evaluated")
    for expr, want in (("(1 < $record.x) == ($record.y < 2)", True), ("not $record.x > 0", False),
                       ("($record.x > 1) != ($record.y > 1)", True)):
        assert D.expression_problem(expr) is None, expr
        value, diags = _eval_collecting(expr, ctx)
        assert value is want and diags == [], (expr, value, diags)
    assert D.expression_problem("$record.t > 0") is None
    value, diags = _eval_collecting("$record.t > 0", ctx)
    assert value is None and len(diags) == 1, (value, diags)


def test_builtin_domains_of_min_max_count_where_and_first_block_text():
    # DSL rc3 §9.1: each builtin
    # keeps its domain with an explicit check, an input failure outside it.
    D = _dr()
    assert [D.fn_min(x) for x in ([3, 1.5, None], ["b", "a"], [], [None, None], None)] == [1.5, "a", None, None, None]
    assert D.fn_max([3, 1.5, None]) == 3 and D.fn_max(["2026-01-02", "2026-01-01"]) == "2026-01-02"
    for bad in ([True, 2], [True, False], [1, "a"], [{"a": 1}], [[1], [0]], "abc", 5, {}):
        for fn in ("min", "max"):
            value, diags = _eval_collecting(f"{fn}($record.v)", {"record": {"v": bad}})
            assert value is None and len(diags) == 1, (fn, bad, value, diags)
    items = [{"k": 1}, {"k": 1.0}, {"k": True}, "not an object", 7]
    assert D.fn_count_where(items, "k", 1) == 2 and D.fn_count_where(None, "k", 1) == 0
    for bad in ("abc", {}, 0, True):
        value, diags = _eval_collecting("count_where($record.v, k, 1)", {"record": {"v": bad}})
        assert value is None and len(diags) == 1, (bad, value, diags)
    blocks = [{"type": "text", "text": "hi"}, {"type": "thinking", "thinking": "hm"}]
    assert D.fn_first_block_text_of_type(blocks, "text") == "hi"
    assert D.fn_first_block_text_of_type(blocks, "thinking") == "hm"
    assert D.fn_first_block_text_of_type("abc", "text") is None
    assert D.fn_first_block_text_of_type({"type": "text", "text": "x"}, "text") is None
    assert D.fn_first_block_text_of_type([{"type": "text"}], "text") is None
    for bad in (5, ["x"], {"a": 1}, True):
        value, diags = _eval_collecting("first_block_text_of_type($record.c, 'text')",
                                        {"record": {"c": [{"type": "text", "text": bad}]}})
        assert value is None and len(diags) == 1, (bad, value, diags)


def test_tool_name_and_target_id_lookups_use_strict_equality():
    # DSL rc3 §8.3, §8.2.4, §10 (ruled 2026-10-05 option i, spec 24a1ab2c):
    # table keys and emitted tool-call ids are strings, so a tool name or a
    # target_id that is not a string never matches; an unmatched target_id
    # is reported, an unmatched tool name falls to default, else OTHER.
    # Before, target_id true attached its result to the call with id 1.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    dsl["input"] = {"type": "json"}
    dsl["session"] = base["sessions"]["agent"]
    dsl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
    dsl["operation_types"] = {"Read": "READ", "default": "OTHER"}
    dsl["emit"] = [
        {"when": {"field": "kind", "value": "call"},
         "do": [{"emit_tool_call": {"id": "$record.id", "tool_name": "$record.name"}}]},
        {"when": {"field": "kind", "value": "result"},
         "do": [{"attach_tool_result": {"target_id": "$record.target", "output": {"result": "$record.out"}}}]}]
    recs = [{"kind": "call", "id": 1, "name": True}, {"kind": "result", "target": True, "out": "loose"},
            {"kind": "result", "target": 1.0, "out": "loose"},
            {"kind": "call", "id": "a", "name": "Read"}, {"kind": "result", "target": "a", "out": "ok"}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "k.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps(recs))
        diags = []
        t = D.run(td / "k.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"},
                  diagnostics_out=diags)
    # The lookup outcomes only: whether a non-string id may be emitted at all
    # is an open spec question, not pinned here.
    got = [(c.get("operation_type"), (c.get("output") or {}).get("result")) for c in t["tool_calls"]]
    # Ruled 2026-10-05: target_id 1.0 is rendered "1" and finds the
    # call emitted as "1"; target_id true still matches nothing.
    assert got == [("OTHER", "loose"), ("READ", "ok")], got
    assert [d["kind"] for d in diags if d["kind"].startswith("tool-result")] == ["tool-result-unattached"], diags


def test_a_tool_call_id_is_classified_as_r5_classifies_a_trace_id_part():
    # Ruled 2026-10-05, R5 throughout; DSL rc3 §8.2.3, spec 146cdeb3:
    # a non-empty string is used as is, a number is rendered (1, 1.0 -> "1");
    # null, "", a boolean, an object, a list, or a template with such a part,
    # is unusable: the call is not emitted (value-level, reported) and
    # last_tool_call_id becomes null, so a later attach through it cannot
    # land on the previous call; a tool_name neither string nor null is null.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    dsl["input"] = {"type": "json"}
    dsl["session"] = base["sessions"]["agent"]
    dsl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
    dsl["emit"] = [
        {"when": {"field": "kind", "value": "call"},
         "do": [{"emit_tool_call": {"id": "$record.id", "tool_name": "$record.name"}}]},
        {"when": {"field": "kind", "value": "tcall"},
         "do": [{"emit_tool_call": {"id": "tc-${record.id}", "tool_name": "t"}}]},
        {"when": {"field": "kind", "value": "result"},
         "do": [{"attach_tool_result": {"target_id": "$state.last_tool_call_id",
                                        "output": {"result": "$record.out"}}}]}]
    recs = [{"kind": "call", "id": "a", "name": "t"}, {"kind": "result", "out": "for a"},
            {"kind": "call", "id": 1, "name": True}, {"kind": "result", "out": "for 1"},
            {"kind": "call", "id": 2.0, "name": "t"}]
    for bad in (None, "", True, False, [1], {"k": 1}):
        recs += [{"kind": "call", "id": bad, "name": "t"}, {"kind": "result", "out": "lost"}]
    recs += [{"kind": "tcall", "id": True}, {"kind": "result", "out": "lost"},
             {"kind": "tcall", "id": 7}, {"kind": "result", "out": "for tc-7"}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "k.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps(recs))
        diags = []
        t = D.run(td / "k.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"},
                  diagnostics_out=diags)
    got = [(c["id"], c["tool_name"], (c.get("output") or {}).get("result")) for c in t["tool_calls"]]
    assert got == [("a", "t", "for a"), ("1", None, "for 1"), ("2", "t", None), ("tc-7", "t", "for tc-7")], got
    dropped = [d for d in diags if "tool call not emitted" in str(d.get("message"))]
    assert len(dropped) == 7 and all(d["loss"] == "value" for d in dropped), diags
    # A template id names its template and the cause.
    assert sum("template 'tc-${record.id}' has an unusable ${...} part" in d["message"] for d in dropped) == 1, dropped
    assert sum("(bool) is not a usable id" in d["message"] for d in dropped) == 2, dropped
    # Each 'lost' result found last_tool_call_id null: reported, never attached.
    assert sum(d["kind"] == "tool-result-unattached" for d in diags) == 7, diags
    assert sum("tool_name is a bool" in str(d.get("message")) for d in diags) == 1, diags
    assert not D.set_level_loss(diags)


def test_a_numeric_target_id_is_rendered_before_matching():
    # Ruled 2026-10-05: a numeric target_id is rendered per §8.3 as
    # emit_tool_call renders a numeric id, so a source that numbers its calls
    # and their results keeps its results; a boolean or an unknown id stays
    # unattached and reported.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    dsl["input"] = {"type": "json"}
    dsl["session"] = base["sessions"]["agent"]
    dsl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
    dsl["emit"] = [
        {"when": {"field": "kind", "value": "call"},
         "do": [{"emit_tool_call": {"id": "$record.id", "tool_name": "t"}}]},
        {"when": {"field": "kind", "value": "result"},
         "do": [{"attach_tool_result": {"target_id": "$record.target", "output": {"result": "$record.out"}}}]}]
    recs = [{"kind": "call", "id": 5}, {"kind": "call", "id": 2.0}, {"kind": "call", "id": "s"},
            {"kind": "result", "target": 5, "out": "r5"}, {"kind": "result", "target": 2, "out": "r2"},
            {"kind": "result", "target": True, "out": "lost"}, {"kind": "result", "target": 9, "out": "lost"}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "k.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps(recs))
        diags = []
        t = D.run(td / "k.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"},
                  diagnostics_out=diags)
    got = [(c["id"], (c.get("output") or {}).get("result")) for c in t["tool_calls"]]
    assert got == [("5", "r5"), ("2", "r2"), ("s", None)], got
    unattached = [d["message"] for d in diags if d["kind"] == "tool-result-unattached"]
    assert len(unattached) == 2 and any("True (bool)" in m for m in unattached), unattached
    assert any("unknown target_id 9 (int)" in m for m in unattached), unattached


def test_tool_calls_are_written_only_by_the_emit_actions_at_load():
    # Ruled 2026-10-05, as §13.1 for lineage: every tool call comes
    # from emit_tool_call (attach_tool_result writes into one), so the
    # §8.2.3 id and tool_name rules hold for all of them.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    for name, d in (
            ("static", _lineage_dsl(**{"sessions/agent/static/tool_calls": []})),
            ("derived below", _lineage_dsl(**{"sessions/agent/derived/tool_calls.0": "$record.c"})),
            ("passthrough", _lineage_dsl(**{"sessions/agent/passthrough/tool_calls": "x"})),
            ("computed", _lineage_dsl(**{"finalize/computed/tool_calls": "$state.c"})),
            ("set session.tool_calls", _lineage_dsl(**{"emit/0/do/0": {"set": {"session.tool_calls": "$record.c"}}})),
            ("set bare tool_calls", _lineage_dsl(**{"emit/0/do/0": {"set": {"tool_calls.0.id": 1}}})),
            ("on_block", _lineage_dsl(**{"emit/0/on_block": [{"name": "b", "do": [
                {"set": {"session.tool_calls": "$block.c"}}]}]})),
            # Ruled 2026-10-05, option 1: the alias.
            ("set through the alias", _lineage_dsl(**{"emit/0/do/0": {"set": {"state.tool_calls_by_id.5.id": True}}})),
            ("set the alias", _lineage_dsl(**{"emit/0/do/0": {"set": {"state.tool_calls_by_id": {}}}})),
            ("accumulate through the alias", _lineage_dsl(**{"emit/0/do/0": {
                "accumulate": {"state.tool_calls_by_id.5.notes": "$record.n"}}})),
            ("alias in on_block", _lineage_dsl(**{"emit/0/on_block": [{"name": "b", "do": [
                {"set": {"state.tool_calls_by_id.x.tool_name": 1}}]}]})),
            ("alias in initial state", _lineage_dsl(**{"state/tool_calls_by_id": {"5": {"id": True}}})),
            # Aligned with the DSL schema's state-block propertyNames (ruled 2026-10-05: align).
            ("dotted alias key in initial state", _lineage_dsl(**{"state": {"tool_calls_by_id.5": {}}}))):
        assert len(D.dsl_tool_calls_problems(d)) == 1, (name, D.dsl_tool_calls_problems(d))
        assert D.dsl_lineage_problems(d) == [], name
    for name, d in (
            ("none", _lineage_dsl()),
            ("state.tool_calls", _lineage_dsl(**{"emit/0/do/0": {"set": {"state.tool_calls": "$record.c"}}})),
            ("tool_calls_note", _lineage_dsl(**{"sessions/agent/static/tool_calls_note": "x"})),
            ("state.tool_calls_by_idx", _lineage_dsl(**{"emit/0/do/0": {"set": {"state.tool_calls_by_idx": 1}}})),
            ("tool_calls_by_idx in initial state", _lineage_dsl(**{"state": {"tool_calls_by_idx": {}}})),
            ("alias read", _lineage_dsl(**{"emit/0/do/0": {"set": {"state.seen": "$state.tool_calls_by_id"}}})),
            ("emit actions", _lineage_dsl(**{"emit/0/do": [
                {"emit_tool_call": {"id": "$record.id", "tool_name": "t"}},
                {"attach_tool_result": {"target_id": "$record.id", "output": {}}}]}))):
        assert D.dsl_tool_calls_problems(d) == [], (name, D.dsl_tool_calls_problems(d))
    # The original probe is refused before any input is read.
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    dsl = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl["sessions"]["agent"]["static"]["tool_calls"] = [{"id": 1, "tool_name": True}]
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "a.dsl.yaml"
        f.write_text(yaml.safe_dump(dsl, sort_keys=False))
        try:
            D.run(f, fixtures / "synthetic-trace-pair.input.json", None, diagnostics_out=[], operator_override=_OP)
        except SystemExit as e:
            assert "tool_calls outside the emit actions" in str(e) and "sessions.agent.static.tool_calls" in str(e), e
        else:
            raise AssertionError("tool_calls in static loaded")
    # The alias wrote {"id": true, "tool_name": {"x": 1}}
    # into the trace's call with no diagnostic; refused at load now. One call
    # is emitted, then written through the alias (the pair fixture emits
    # no calls, so it could not show the rewrite).
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    dsl["input"] = {"type": "json"}
    dsl["session"] = base["sessions"]["agent"]
    dsl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
    dsl["emit"] = [
        {"when": {"field": "kind", "value": "call"},
         "do": [{"emit_tool_call": {"id": "$record.id", "tool_name": "t"}}]},
        {"when": {"field": "kind", "value": "poke"},
         "do": [{"set": {"state.tool_calls_by_id.5.id": True, "state.tool_calls_by_id.5.tool_name": {"x": 1}}}]}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps([{"kind": "call", "id": 5}, {"kind": "poke"}]))
        try:
            t = D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"},
                      diagnostics_out=[])
        except SystemExit as e:
            assert str(e).count("state.tool_calls_by_id is the emitted calls themselves") == 2, e
        else:
            raise AssertionError(f"a write through state.tool_calls_by_id loaded: {t['tool_calls']}")


def test_a_derived_field_report_names_the_record_or_container_it_read():
    # Ruled 2026-10-05: a session.derived report named "record None".
    # It now names what the field read: the trace's first record
    # ($record_first) as record 0, the container ($container, container mode)
    # as container N, and neither as no record.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    dsl = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl["state"] = {"d": {"k": 1}}
    dsl["sessions"]["agent"]["derived"].update({   # a container-reading field last, so the
        "environment.from_state": "s-${state.d}",      # reset after the derived fields is tested
        "environment.from_record": "r-${record_first.obj}",
        "environment.from_container": "c-${container.obj}"})
    dsl["emit"][0]["do"].append({"set": {"state.rec": "x-${record.obj}"}})   # a record-phase failure
    good = [{"index": 0, "role": "user", "content": "p", "obj": {"x": 1}}]
    data = [{"id": "rec-001", "obj": {"a": 1}, "events": good}, {"id": "rec-002", "obj": {"a": 2}, "events": good}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "p.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps(data))
        diags = []
        D.run(td / "p.dsl.yaml", td / "in.json", None, diagnostics_out=diags, operator_override=_OP)
    got = sorted((d["field"].rsplit(".", 1)[1], d.get("record_index"), d.get("container_index"))
                 for d in diags if str(d.get("field", "")).startswith("session.derived."))
    # Spec 5fc0192d (ruled 2026-10-06): in container mode $record_first is
    # named with its container.
    assert got == [("from_container", None, 0), ("from_container", None, 1), ("from_record", 0, 0),
                   ("from_record", 0, 1), ("from_state", None, None), ("from_state", None, None)], got
    msgs = [d["message"] for d in diags if str(d.get("field", "")).startswith("session.derived.")]
    assert sum("(container 1)" in m for m in msgs) == 1 and sum("(record 0, container 1)" in m for m in msgs) == 1, msgs
    assert sum("(no input)" in m for m in msgs) == 2 and not any("record None" in m for m in msgs), msgs
    # A record-phase report keeps its record and, in container mode, names
    # its own container (ruled 2026-10-06; it named none before, so two
    # containers' reports could not be told apart).
    rec = [d for d in diags if d.get("field") == "set.state.rec"]
    assert sorted((d["record_index"], d["container_index"]) for d in rec) == [(0, 0), (0, 1)], rec
    assert sum("(record 0, container 1)" in d["message"] for d in rec) == 1, rec


def test_report_sites_without_the_data_they_name_and_in_finalize():
    # The guards. Outside container mode
    # $container is {} and with no records $record_first is {}: their reports
    # name no container and no record. $container_first is container 0, and
    # finalize.computed names the container it read like session.derived.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())

    last: list = []

    def sites(dsl, data):
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
            (td / "in.json").write_text(json.dumps(data))
            diags = []
            D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"},
                  diagnostics_out=diags)
        last[:] = diags
        rows = [(d["field"].rsplit(".", 1)[1], d.get("record_index"), d.get("container_index"),
                 "(no input)" in d["message"]) for d in diags
                if str(d.get("field", "")).startswith(("session.derived.env", "finalize.computed."))]
        return sorted(rows, key=repr)

    flat = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    flat["input"] = {"type": "json"}
    flat["session"] = dict(base["sessions"]["agent"], derived={
        "id": "agent-x", "title": "agent-x", "environment.c": "c-${container}", "environment.r": "r-${record_first.obj}"})
    flat["emit"] = [{"when": {"field": "role", "value": "user"}, "do": [{"emit_turn": {"index": "$record.index", "role": "user", "content": "p"}}]}]
    assert sites(flat, [{"index": 0, "role": "user", "obj": {"a": 1}}]) == [
        ("c", None, None, True), ("r", 0, None, False)]                      # no container input
    flat["emit"][0]["do"].append({"set": {"state.f": "x-${record.obj}"}})
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(flat, sort_keys=False))
        (td / "in.json").write_text(json.dumps([{"index": 0, "role": "user", "obj": {"a": 1}}]))
        diags = []
        D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"}, diagnostics_out=diags)
    [rec] = [d for d in diags if d.get("field") == "set.state.f"]
    assert rec["record_index"] == 0 and "container_index" not in rec, rec   # no container input: none named
    pair = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    pair["state"] = {"d": {"k": 1}}
    pair["sessions"]["agent"]["derived"].update({"environment.f": "f-${container_first.obj}",
                                                 "environment.r": "r-${record_first}",
                                                 "environment.i": "i-${container_index}-${state.d}",
                                                 "environment.two": "t-${container.obj}${container_first.obj}"})
    pair["finalize"] = {"computed": {"environment.fin": "x-${container.obj}",
                                     # Finalize binds $container alone, so these
                                     # read null and name nothing (no container 0, no record 0).
                                     "environment.fcf": "$container_first.obj.a + 1",
                                     "environment.frf": "$record_first.obj.a + 1"}}
    got = sites(pair, [{"id": "a", "obj": {"a": 1}, "events": []},
                       {"id": "b", "obj": {"b": 1}, "events": [{"index": 0, "role": "user", "content": "p"}]}])
    assert ("r", None, None, True) in got and ("r", 0, 1, False) in got, got      # empty records: no record 0
    assert [x for x in got if x[0] == "f"] == [("f", None, 0, False)] * 2, got   # agent session, both containers
    assert sorted(x[2] for x in got if x[0] == "fin") == [0, 0, 1, 1], got        # two sessions, two containers
    # Spec 5fc0192d: $container_index references the container.
    assert sorted(x[2] for x in got if x[0] == "i") == [0, 1] and all(not x[3] for x in got if x[0] == "i"), got
    # Two roots name both (container 1 and container 0); in container 0 they
    # are the same container, named once.
    two = sorted(((d.get("container_index"), d.get("inputs"), d["message"].split("(", 1)[1].split(")", 1)[0])
                  for d in last if d.get("field") == "session.derived.environment.two"), key=repr)
    assert two == [(0, None, "container 0"),
                   (None, [{"record_index": None, "container_index": 1}, {"record_index": None, "container_index": 0}],
                    "container 1; container 0")], two
    assert [x for x in got if x[0] in ("fcf", "frf")] == [("fcf", None, None, True)] * 4 + [("frf", None, None, True)] * 4, got


def test_number_constants_are_written_as_json_numbers():
    # (number forms; DSL rc3 §8.3: an unsigned JSON number, RFC 8259 §6;
    # hexadecimal, octal and binary forms and digit separators are not part
    # of the language). Python's parser took them as ordinary numbers, at
    # load and at evaluation alike; both refuse them now.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    for form in ("0x10", "0X1F", "0o7", "0b1", "1_000", "1_0.5", ".5", "1.", "01.5", "1.e3", "0.5e"):
        problem = D.expression_problem(f"$record.n + {form}")
        assert problem is not None and (form in problem or "does not parse" in problem), (form, problem)
    for form in ("0", "10", "1.5", "0.0", "1e5", "2E-3", "1.5e+2"):
        assert D.expression_problem(f"$record.n + {form}") is None, form
    assert "'0x10'" in D.expression_problem("-0x10")
    # The ?. and $ rewrites no longer join digits into a
    # number the adapter never wrote (1?.5 was 1.5, 1$5 was 15).
    for text in ("$record.n + 1?.5", "$record.n + 1$5", "0$.5"):
        assert D.expression_problem(text) == "does not parse as an expression", text
    for text in ("1?.5 + 0", "1$5"):
        try:
            D._evaluate_expression(text, {})
        except D.ExpressionEvalError as e:
            assert "does not parse" in str(e), e
        else:
            raise AssertionError(f"{text} evaluated")
    assert D.expression_problem("$record?.a") is None and D.expression_problem("$record.x?.y") is None
    # Four forms the anchored rewrites also refuse now,
    # each accepted before (§8.3: `$` + ASCII letter or `_` is a reference,
    # `$é` and `$ x` are not; safe navigation is written ?.name). Whether a
    # space after ?. is allowed is a spec-lane question.
    for text in ("$record ?. a", "$$record", "$ record", "$é"):
        assert D.expression_problem(text) == "does not parse as an expression", text
    # A template body is checked the same way, and run() refuses before any input.
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    dsl = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl["sessions"]["agent"]["derived"]["environment.n"] = "n-${container_index + 0x10}"
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "a.dsl.yaml"
        f.write_text(yaml.safe_dump(dsl, sort_keys=False))
        try:
            D.run(f, fixtures / "synthetic-trace-pair.input.json", None, diagnostics_out=[], operator_override=_OP)
        except SystemExit as e:
            assert "rejected at load" in str(e) and "'0x10' is not in the JSON number form" in str(e), e
        else:
            raise AssertionError("0x10 loaded")
    # Evaluation, reached without the load pass, refuses the same text.
    try:
        D._evaluate_expression("1_000 + 1", {})
    except D.ExpressionEvalError as e:
        assert "'1_000' is not in the JSON number form" in str(e), e
    else:
        raise AssertionError("1_000 evaluated")
    assert D._evaluate_expression("1e3 + 0.5", {}) == 1000.5


def test_years_before_1000_are_written_with_four_digits():
    # strftime's %Y does not pad years below 1000 on glibc
    # ("999"), so the §3.5 form depended on the platform. The year is padded
    # by the runner now. (macOS pads, so this cannot fail there on the old
    # code; it pins the form on every platform.)
    import datetime as dt  # noqa: PLC0415
    D = _dr()
    assert D.fn_iso_normalise("0099-01-02T03:04:05.5+00:00") == "0099-01-02T03:04:05.500000Z"
    t = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=-60_000_000_000)
    assert t.year == 68 and D.fn_iso_from_seconds(-60_000_000_000) == \
        f"0068-{t.month:02d}-{t.day:02d}T{t.hour:02d}:{t.minute:02d}:{t.second:02d}.000000Z"
    t2 = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(milliseconds=-61_000_000_000_000)
    assert t2.year < 1000 and D.fn_iso_from_ms(-61_000_000_000_000).startswith(f"{t2.year:04d}-")
    assert D._spec_ts(dt.datetime(999, 12, 31, 23, 59, 59, 999999, tzinfo=dt.timezone.utc)) \
        == "0999-12-31T23:59:59.999999Z"
    import inspect  # noqa: PLC0415
    assert ".strftime(" not in inspect.getsource(D), "a strftime %Y would bring the platform dependence back"


def test_no_writer_in_adapters_formats_a_year_with_strftime():
    # The year was first padded in the runner only; timestamps.py,
    # source_reader.py and the goose, agentdojo, opencode, mcpuniverse,
    # cursor and judge-trace writers still used strftime's %Y, which glibc
    # leaves unpadded below year 1000. They all write through
    # timestamps.spec_ts now. (macOS pads, so only the source check can
    # fail here on the old code; the values pin the form on every platform.)
    import datetime as dt  # noqa: PLC0415
    import importlib.util  # noqa: PLC0415
    adapters = pathlib.Path(__file__).resolve().parent.parent
    writers = [p for p in adapters.rglob("*.py")
               if not {"tests", "fuzz", ".venv", "__pycache__"} & set(p.relative_to(adapters).parts)]
    assert len(writers) > 20, writers
    # strftime itself, and the format-spec forms that call it (f"{d:%Y...}",
    # format(d, "%Y...")); strptime patterns parse and are not matched.
    import re  # noqa: PLC0415
    via_strftime = re.compile(r"\.strftime\(|:%Y|format\([^)]*%Y")
    assert [str(p.relative_to(adapters)) for p in writers if via_strftime.search(p.read_text())] == []
    # ... and by call, so a format held in a constant (format(d, FMT)) is caught
    # too; no writer calls format(), strftime or __format__.
    import ast  # noqa: PLC0415
    calls = [f"{p.relative_to(adapters)}:{n.lineno}" for p in writers for n in ast.walk(ast.parse(p.read_text()))
             if isinstance(n, ast.Call) and ((isinstance(n.func, ast.Name) and n.func.id == "format")
                                             or (isinstance(n.func, ast.Attribute) and n.func.attr in ("strftime", "__format__")))]
    assert calls == [], calls
    assert timestamps.spec_ts(dt.datetime(999, 12, 31, 23, 59, 59, 999999)) == "0999-12-31T23:59:59.999999Z"
    assert timestamps.to_spec_utc("0999-01-02T03:04:05+01:00") == "0999-01-02T02:04:05.000000Z"
    assert timestamps.naive_local_to_utc("0099-01-02 03:04:05") == "0099-01-02T03:04:05.000000Z"

    def load(rel):
        spec = importlib.util.spec_from_file_location("_t800_" + rel.replace("/", "_")[:-3], adapters / rel)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    assert load("goose/preprocess_goose.py").normalise_timestamp("0999-01-02 03:04:05") \
        == "0999-01-02T03:04:05.000000Z"
    assert load("agentdojo/preprocess_agentdojo.py").normalize_iso_microseconds("0999-01-02 03:04:05.5") \
        == "0999-01-02T03:04:05.500000Z"
    # Every other replaced writer runs once, so a missing import fails here
    # (cursor's was missing in the first edit; the judge writer's main() is not run).
    # Input epochs reach years below 1000 too: -4e10 s is year 702.
    y702 = "0702-06-15T00:53:20.000000Z"
    assert load("cursor/preprocess_cursor.py")._iso(-40_000_000_000_000) == y702
    assert load("opencode/preprocess_opencode.py").normalise_timestamp(-40_000_000_000_000) == y702
    assert load("mcpuniverse/preprocess_mcpuniverse.py").iso_from_epoch(-40_000_000_000) == y702
    assert load("goose/preprocess_goose.py").normalise_timestamp(-40_000_000_000) == y702
    assert timestamps.uuid7_time("00000000-0000-7000-8000-000000000000") == "1970-01-01T00:00:00.000000Z"
    import os  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415
    with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "0"}):
        assert source_reader.asserted_at_now() == "1970-01-01T00:00:00.000000Z"
    with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": ""}):
        assert len(source_reader.asserted_at_now()) == len("1970-01-01T00:00:00.000000Z")


def test_string_constants_and_dollar_boundaries_follow_section_8_3():
    # Strings: a string constant is one single- or
    # double-quoted run with neither its own quote nor a backslash: no
    # escapes, no r/u prefixes, no triple quotes, no adjacent strings Python
    # joins. Point 2: `$` after an identifier character or `.` is not a
    # reference start, so x$record.id is two operands (no parse), not xrecord.
    D = _dr()
    for text in (r"'a\nb'", "'a' 'b'", "\"a\" 'b'", "r'a'", "u'a'", "'''a'''", '"""a"""', "'it''s'"):
        problem = D.expression_problem(f"{text} + $record.id")
        assert problem is not None and problem.startswith("string constant"), (text, problem)
    for text in ("'a'", '"a"', "''", "'say \"hi\"'", "'$5 ?. x'"):
        assert D.expression_problem(f"{text} + $record.id") is None, text
    assert D.expression_problem("$record['k']") is None
    for text in ("x$record.id", "a.$record", "1+x$record.n"):
        assert D.expression_problem(text) == "does not parse as an expression", text
    for text in ("x + $record.n", "($record.n)", "len($record.a)"):
        assert D.expression_problem(text) is None, text
    # The evaluator refuses the same text.
    for text in (r"'a\nb'", "x$record.id"):
        try:
            D._evaluate_expression(text, {"record": {"id": 1}})
        except D.ExpressionEvalError:
            pass
        else:
            raise AssertionError(f"{text} evaluated")
    assert D._evaluate_expression("'a' == \"a\"", {}) is True


def test_runner_built_input_failures_name_record_field_and_expression():
    # (DSL rc3 §8.3: an input failure is reported with record,
    # field and expression). Five failures are found by a runner rule after
    # evaluation rather than by the evaluator, and carried no expression
    # (four no record either): a foreach_block source that is not a list, an
    # unusable emit_tool_call id, a tool_name that is not a string, a
    # target_id that is not a scalar, and an accumulate leaving the domain.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    dsl["input"] = {"type": "json"}
    dsl["state"] = {"n": 9007199254740990}
    dsl["session"] = base["sessions"]["agent"]
    dsl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
    dsl["emit"] = [
        {"name": "blocks", "when": {"field": "kind", "value": "b"}, "foreach_block": "$record.s",
         "on_block": [{"when": {"has_any": True}, "do": []}], "do": []},
        {"name": "calls", "when": {"field": "kind", "value": "c"},
         "do": [{"emit_tool_call": {"id": "$record.bad", "tool_name": "t"}},
                {"emit_tool_call": {"id": "c-1", "tool_name": "$record.tn"}},
                {"attach_tool_result": {"target_id": "$record.obj", "output": {}}},
                {"accumulate": {"state.n": "$record.big"}}]}]
    recs = [{"kind": "b", "s": "not a list"},
            {"kind": "c", "bad": True, "tn": 5, "obj": {"a": 1}, "big": 5}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps(recs))
        diags = []
        D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"},
              diagnostics_out=diags)
    got = {d["field"]: (d.get("record_index"), d.get("expression")) for d in diags
           if d.get("kind") == "input-failure"}
    assert got == {"blocks.foreach_block": (0, "$record.s"),
                   "emit_tool_call.id": (1, "$record.bad"),
                   "emit_tool_call.tool_name": (1, "$record.tn"),
                   "attach_tool_result.target_id": (1, "$record.obj"),
                   "accumulate.state.n": (1, "$record.big")}, got


def test_a_write_through_a_non_object_is_an_input_failure_keeping_the_prior_value():
    # Ruled 2026-10-09: input failure, prior value kept; state. and
    # session.: set or accumulate on a.b where a holds a string, array,
    # number or boolean replaced a with {"b": ...} and reported nothing.
    # It is an input failure naming the path in the way and its JSON type;
    # nothing is written. A null or absent parent is created, as before.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    dsl["input"] = {"type": "json"}
    dsl["state"] = {"lst": [1], "num": 5, "t": True, "z": None, "d": {"e": "s"}}
    dsl["session"] = base["sessions"]["agent"]
    dsl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
    dsl["emit"] = [{"name": "w", "when": {"has_any": True}, "do": [
        {"set": {"state.a": "$record.s", "session.environment": "$record.s"}},
        {"accumulate": {"state.a.n": 1}},
        {"set": {"state.a.m": 2, "state.lst.x": 1, "state.num.x": 1, "state.t.x": 1,
                 "session.environment.model": "m"}},
        {"accumulate": {"state.z.n": 2, "state.q.n": 3, "state.lst.n": 1}},
        # §8.2.1 (spec 8dbbc465; ruled 2026-10-09: path first): a blocked
        # path is reported, not the RHS, which is not a number either.
        {"accumulate": {"state.num.y": "$record.s"}},
        # ... and a RHS that fails to evaluate adds no second report (review).
        {"accumulate": {"state.num.z": "$record.s + 1"}},
        {"set": {"state.num.w": "$record.s + 1"}},
        {"set": {"state.d.e.f": 1}},
        {"set": {"session.operational_context.seen": {
            k: f"$state.{k}" for k in ("a", "num", "t", "z", "q", "lst", "d")}}}]}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps([{"s": "s"}]))
        diags = []
        trace = D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"},
                      diagnostics_out=diags)
    if isinstance(trace, list):   # single-trace mode returns the trace itself
        [trace] = trace
    got = {d["field"]: d["message"].split(": ", 1)[1] for d in diags if d.get("kind") == "input-failure"}
    # Each message ends with the place, here the record.
    assert got == {k: f"{v}; prior value kept (record 0)" for k, v in {
        "accumulate.state.a.n": "state.a holds a string, not an object",
        "set.state.a.m": "state.a holds a string, not an object",
        "set.state.lst.x": "state.lst holds an array, not an object",
        "set.state.num.x": "state.num holds a number, not an object",
        "set.state.t.x": "state.t holds a boolean, not an object",
        "set.session.environment.model": "session.environment holds a string, not an object",
        "accumulate.state.lst.n": "state.lst holds an array, not an object",
        "accumulate.state.num.y": "state.num holds a number, not an object",
        "accumulate.state.num.z": "state.num holds a number, not an object",
        "set.state.num.w": "state.num holds a number, not an object",
        "set.state.d.e.f": "state.d.e holds a string, not an object",   # depth 2
    }.items()}, got
    assert all(d["record_index"] == 0 for d in diags if d.get("kind") == "input-failure"), diags
    fields = [d.get("field") for d in diags if d.get("kind") == "input-failure"]
    assert len(fields) == len(set(fields)) == len(got), fields   # one report per key
    assert trace["environment"] == "s", trace["environment"]
    assert trace["operational_context"]["seen"] == {
        "a": "s", "num": 5, "t": True, "z": {"n": 2}, "q": {"n": 3}, "lst": [1], "d": {"e": "s"}}, \
        trace["operational_context"]


def test_an_accumulate_key_outside_state_is_refused_at_load():
    # Ruled 2026-10-09, fix: DSL rc3 §15 lists "an accumulate key
    # that is not a state. path" among the structural failures at load, and
    # §8.2.5 says the LHS is a state. path; the runner loaded such a key and
    # then dropped it (session.metrics.n: no write, no report). Refused at
    # load now, in a rule's do and in an on_block's do.
    import tempfile  # noqa: PLC0415
    import pytest  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())

    def run(emit):
        dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
        dsl["input"] = {"type": "json"}
        dsl["state"] = {"n": 0}
        dsl["session"] = base["sessions"]["agent"]
        dsl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
        dsl["emit"] = emit
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
            (td / "in.json").write_text(json.dumps([{"s": [{"type": "t"}]}]))
            return D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"})

    for key in ("session.metrics.n", "metrics.n", "n", "stat.n", "statex.n", "state", "session.state.n"):
        with pytest.raises(SystemExit) as e:
            run([{"name": "r", "when": {"has_any": True}, "do": [{"accumulate": {key: 1}}]}])
        assert f"emit.r.accumulate.{key}: an accumulate key is a state. path" in str(e.value), (key, e.value)
    with pytest.raises(SystemExit) as e:
        run([{"name": "r", "when": {"has_any": True}, "foreach_block": "$record.s",
              "on_block": [{"name": "b", "when": {"has_any": True}, "do": [{"accumulate": {"session.x": 1}}]}],
              "do": []}])
    assert "emit.r.on_block.b.accumulate.session.x: an accumulate key is a state. path" in str(e.value), e.value
    run([{"name": "r", "when": {"has_any": True}, "do": [{"accumulate": {"state.n": 1}}]}])   # loads


def test_a_whole_document_finding_names_the_input_file_not_record_0():
    # A decode error or a duplicate key in a json input belongs to the
    # whole document (read_json cannot place it in a record), but the
    # reader gave it record_index 0, so reports read "input record 0" or, in
    # container mode, "input container 0", and the reporting-bound summary
    # fell back to record 0 the same way. It has no record_index now and is
    # named "the input file".
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    container_dsl = fixtures / "synthetic-trace-pair.dsl.yaml"
    single = yaml.safe_load(container_dsl.read_text())
    single["input"] = {"type": "json"}
    dup = b'[{"id": "c0", "events": [{"index": 0, "role": "user", "content": "p", "role": "user"}]}]'
    bad_utf8 = b'[{"id": "c0", "events": [{"index": 0, "role": "user", "content": "p\xff"}]}]'
    many = b'[{"id": "c0", "events": [{"index": 0, "role": "user", "content": "' + b"\xffa" * 1100 + b'"}]}]'
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "s.dsl.yaml").write_text(yaml.safe_dump(single, sort_keys=False))
        for dsl in (container_dsl, td / "s.dsl.yaml"):
            for raw, reason in ((dup, "duplicate-key"), (bad_utf8, None), (many, None)):
                (td / "in.json").write_bytes(raw)
                diags = []
                D.run(dsl, td / "in.json", None, diagnostics_out=diags, operator_override=_OP)
                whole = [d for d in diags if d.get("kind") in ("input-decode-error", "input-findings-truncated")
                         or d.get("reason") == "duplicate-key"]
                want = 1 if raw is not many else 1001   # the bound: 1,000 listed, then one summary
                assert len(whole) == want, (dsl.name, raw[:40], len(whole), diags[:3])
                for d in whole:
                    assert d.get("record_index") is None and d.get("container_index") is None, (dsl.name, d)
                    if dsl == container_dsl:   # every container-mode finding carries the key
                        assert "container_index" in d, d
                    if raw is bad_utf8:        # a decode error keeps its byte offset
                        assert d["message"].endswith(f" at byte {raw.index(bytes([0xff]))}"), d["message"]
                        assert d["byte_offset"] == raw.index(bytes([0xff])), d
                    assert d["message"].startswith("the input file: ") \
                        or "the first at the input file" in d["message"], (dsl.name, d["message"])
                    assert "record 0" not in d["message"] and "container 0" not in d["message"], d["message"]
    # Only a whole-document finding gets a byte offset: a container-level
    # repair (no record after _input_place, but one the reader placed) gave a
    # false "at byte 0", the placeholder read_json passes.
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "in.json"
        p.write_text('[{"id": "c\\ud800", "events": [{"index": 0, "role": "user", "content": "p"}]}]')
        diags = []
        D.run(container_dsl, p, None, diagnostics_out=diags, operator_override=_OP)
        [rep] = [d for d in diags if d.get("reason") == "lone-surrogate"]
        assert (rep["container_index"], rep["record_index"]) == (0, None), rep
        assert "byte_offset" not in rep and "at byte" not in rep["message"], rep
    # JSONL (ruled 2026-10-10: keep real offsets): a decode error in a line
    # shows its real file offset in both modes; a repair in a container
    # field shows none (it carried the line start before the fix).
    line0 = b'{"id": "c0", "events": [{"index": 0, "role": "user", "content": "p"}]}\n'
    single_jsonl = yaml.safe_load((td_dsl := container_dsl).read_text())
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        cont = yaml.safe_load(td_dsl.read_text())
        cont["input"] = {"type": "jsonl", "container": {"record_path": "events"}}
        (td / "c.dsl.yaml").write_text(yaml.safe_dump(cont, sort_keys=False))
        single_jsonl["input"] = {"type": "jsonl"}
        single_jsonl["session"] = single_jsonl.pop("sessions")["agent"]
        single_jsonl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
        single_jsonl["emit"] = [r for r in single_jsonl["emit"] if r.get("target") == "agent"]
        for r in single_jsonl["emit"]:
            r.pop("target", None)
        (td / "s.dsl.yaml").write_text(yaml.safe_dump(single_jsonl, sort_keys=False))
        bad_line = b'{"id": "c1", "events": [{"index": 0, "role": "user", "content": "q\xff"}]}\n'
        rec_line = b'{"index": 0, "role": "user", "content": "q\xff"}\n'
        for dsl_name, raw in (("c.dsl.yaml", line0 + bad_line),
                              ("s.dsl.yaml", b'{"index": 0, "role": "user", "content": "p"}\n' + rec_line)):
            (td / "in.jsonl").write_bytes(raw)
            diags = []
            D.run(td / dsl_name, td / "in.jsonl", None, diagnostics_out=diags, operator_override=_OP)
            [rep] = [d for d in diags if d.get("kind") == "input-decode-error"]
            assert rep.get("byte_offset") == raw.index(b"\xff"), (dsl_name, rep)
            assert rep["message"].endswith(f" at byte {raw.index(bytes([0xff]))}"), (dsl_name, rep["message"])
        (td / "in.jsonl").write_bytes(line0 + b'{"id": "c\\ud800", "events": []}\n')
        diags = []
        D.run(td / "c.dsl.yaml", td / "in.jsonl", None, diagnostics_out=diags, operator_override=_OP)
        [rep] = [d for d in diags if d.get("reason") == "lone-surrogate"]
        assert "byte_offset" not in rep and "at byte" not in rep["message"], rep
        # A repair of a top-level string record had the path "" (falsy), so
        # the runner tests the key, not its value. Since
        # §15 (spec 9c801194): a record that is not an object is skipped
        # before any repair, in json as in jsonl, so no runner input reaches
        # that case: the string is now a set-level skip, not a record.
        single_jsonl["input"] = {"type": "json"}
        (td / "sj.dsl.yaml").write_text(yaml.safe_dump(single_jsonl, sort_keys=False))
        (td / "in.json").write_text('["\\ud800"]')
        diags = []
        D.run(td / "sj.dsl.yaml", td / "in.json", None, diagnostics_out=diags, operator_override=_OP)
        assert not [d for d in diags if d.get("reason") == "lone-surrogate"], diags
        [skip] = [d for d in diags if d.get("reason") == "not-an-object"]
        assert skip["loss"] == "set" and skip["record_index"] == 0, skip
    # The reader side. The summary names the FIRST suppressed finding even
    # when it lies in no record: tracking keyed on "index still None" let a
    # later finding's record overwrite it.
    src = source_reader.SourceRead(path=pathlib.Path("x"), _limits=source_reader.ReadLimits(max_findings=1))
    for ri in (None, None, 5):
        src.add_finding({"kind": "decode-error", "byte_offset": 0, "record_index": ri})
    src._seal_findings()
    [summary] = [f for f in src.findings if f["kind"] == "findings-truncated"]
    assert summary["record_index"] is None and summary["suppressed_count"] == 2, summary
    # A whole-document finding sorts beside record-level ones (no None < int).
    mixed = [{"byte_offset": 0, "record_index": 3, "kind": "truncated"},
             {"byte_offset": 0, "record_index": None, "kind": "parse-error"}]
    assert [f["record_index"] for f in sorted(mixed, key=source_reader._finding_sort_key)] == [None, 3]
    assert [f["record_index"] for f in sorted(mixed, key=source_reader._chain_sort_key)] == [None, 3]


def test_an_identifier_is_written_in_the_section_8_3_form():
    # Ruled 2026-10-09, §8.3's full form: an identifier is an ASCII
    # letter or _, then Unicode letters or digits (L, N) or _, and is read as
    # written. Python identifiers also take combining marks and connector
    # punctuation, a non-ASCII first letter, and NFKC-normalise, so x́$record
    # (the $ stripped after a mark, which \w does not cover) parsed as the
    # one name x́record, and ﬁeld evaluated to "field". Each such name or
    # path segment is a structural failure, at load and at evaluation.
    import pytest  # noqa: PLC0415
    D = _dr()
    ctx = {"record": {"a": 1, "field": 2}}
    bad = {
        "x́$record": "x́record",          # a combining mark (Mn) glued to a stripped $
        "á$record.a": "árecord",        # the todo's á$record.b, decomposed
        "$record.á": "á",               # a mark in a path segment
        "coalesce(x‿y, 1)": "x‿y",        # connector punctuation (Pc)
        "coalesce(éa, 1)": "éa",          # a non-ASCII first letter
        "$record.名": "名",                 # a path segment starting with a non-ASCII letter
        "coalesce(ﬁeld, 1)": "ﬁeld",      # NFKC would read it as "field"
        "$record.ﬁeld": "ﬁeld",
        "coalesce(aﬁ, 1)": "aﬁ",          # L throughout, but NFKC reads it as "afi"
        "$record.a１": "a１",               # a fullwidth digit (N), NFKC "a1"
        "coalesce(é, 1)": "é",            # inside a call
    }
    for expr, name in bad.items():
        problem = D.expression_problem(expr)
        assert problem == f"identifier {name!r} is not in the §8.3 form", (expr, problem)
        with pytest.raises(D.ExpressionEvalError) as e:
            D.evaluate(expr, ctx)
        assert f"identifier {name!r} is not in the §8.3 form" in str(e.value), (expr, e.value)
    # A string with no marker is a literal (§8.3), never an identifier; one
    # exists only inside an expression or a ${...} body.
    assert D.evaluate("éa", ctx) == "éa" and D.evaluate("ﬁeld", ctx) == "ﬁeld"
    with pytest.raises(D.ExpressionEvalError, match="is not in the §8.3 form"):
        D.evaluate("v-${coalesce(x‿y, 1)}", ctx)
    for expr, want in (("$record.a", 1), ("coalesce(x_1, 1)", "x_1"), ("coalesce(_a名1, 1)", "_a名1"),
                       ("$record.field", 2), ("coalesce(A١, 1)", "A١"), ("coalesce($record.zz, 'é')", "é")):
        assert D.expression_problem(expr) is None, expr
        assert D.evaluate(expr, ctx) == want, expr


def test_a_name_nfkc_reads_as_true_false_or_none_is_refused_not_a_crash():
    # Found by the spec lane's check of 7c04e821: ast.parse raises
    # ValueError, not SyntaxError, for a name that NFKC-normalises to True,
    # False or None, and both parse sites caught only SyntaxError, so an
    # adapter with coalesce(Ｔｒｕｅ, 1) crashed at load. It is not a §8.3
    # identifier (NFKC changes it): a structural failure, named as any non-identifier
    # is, at load and at evaluation.
    import tempfile  # noqa: PLC0415
    import pytest  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    ctx = {"record": {"a": 1}}
    bad = {
        "coalesce(Ｔｒｕｅ, 1)": "Ｔｒｕｅ",
        "coalesce(Ｆａｌｓｅ, 1)": "Ｆａｌｓｅ",
        "coalesce(Ｎｏｎｅ, 1)": "Ｎｏｎｅ",
        "$record.Ｔｒｕｅ": "Ｔｒｕｅ",               # a path segment
        "coalesce('Ｎｏｎｅ', Ｆａｌｓｅ)": "Ｆａｌｓｅ",  # the quoted one is text, not the name
        "coalesce($record.a, Ｎone)": "Ｎone",        # partly fullwidth
        # The first as written: _normalise's ternary rewrite puts the
        # then-branch first.
        "Ｔｒｕｅ == $record.a ? Ｎｏｎｅ : 1": "Ｔｒｕｅ",
    }
    for expr, name in bad.items():
        assert D.expression_problem(expr) == f"identifier {name!r} is not in the §8.3 form", (expr, D.expression_problem(expr))
        with pytest.raises(D.ExpressionEvalError) as e:
            D.evaluate(expr, ctx)
        assert f"identifier {name!r} is not in the §8.3 form" in str(e.value), (expr, e.value)
    with pytest.raises(D.ExpressionEvalError, match="'Ｔｒｕｅ' is not in the §8.3 form"):
        D.evaluate("v-${coalesce(Ｔｒｕｅ, 1)}", ctx)
    # Any other ValueError from ast.parse is refused too: a lone surrogate
    # raised UnicodeEncodeError (a ValueError) uncaught on the parent.
    surrogate = "coalesce('\ud800', 1)"
    assert D.expression_problem(surrogate) == "does not parse as an expression"
    with pytest.raises(D.ExpressionEvalError, match="does not parse as an expression"):
        D.evaluate(surrogate, ctx)
    # Inside a string constant it is text; ASCII constants are unchanged.
    for expr, want in (("coalesce($record.zz, 'Ｔｒｕｅ')", "Ｔｒｕｅ"), ("coalesce(None, True)", True)):
        assert D.expression_problem(expr) is None, expr
        assert D.evaluate(expr, ctx) == want, expr
    dsl = {"adapter": {"dsl_version": "0.1.0"}, "input": {"type": "json"},
           "session": {"static": {}, "derived": {"id": "x", "t": "coalesce(Ｔｒｕｅ, 1)"}}, "emit": []}
    assert D.dsl_expression_problems(dsl) and "'Ｔｒｕｅ' is not in the §8.3 form" in D.dsl_expression_problems(dsl)[0]
    with tempfile.TemporaryDirectory() as td:   # refused by run() before any input is read, not a ValueError
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False, allow_unicode=True))
        with pytest.raises(SystemExit, match="'Ｔｒｕｅ' is not in the §8.3 form"):
            D.run(td / "a.dsl.yaml", td / "missing.json", None, operator_override=_OP)


def test_only_space_tab_line_breaks_and_form_feeds_are_removed_at_the_ends():
    # DSL rc3 §8.3, spec badea515, ruling B; ruled 2026-10-10: only
    # space, tab, line breaks and form feeds are removed at either end of an
    # expression or a ${...} body; any other White_Space character outside a
    # string constant is a structural failure. str.strip() removed them all
    # (and U+001C-U+001F, which str.isspace counts), and the ternary rewrite
    # stripped its condition and branches, so `$record.a` with a trailing
    # U+00A0, or U+00A0 after the `?`, loaded and evaluated.
    import tempfile  # noqa: PLC0415
    import pytest  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    ctx = {"record": {"a": 1}}
    # White_Space is str.isspace() less U+001C-U+001F; built here, not read
    # from the runner, so a character missing from its set is caught.
    other = sorted({chr(c) for c in range(0x110000) if chr(c).isspace()} - set(map(chr, range(0x1C, 0x20)))
                   - set(" \t\n\r\f"))
    assert len(other) == 20, [hex(ord(c)) for c in other]

    def refused(expr, ws):
        want = f"whitespace U+{ord(ws):04X} outside a string constant (§8.3: space and tab only)"
        assert D.expression_problem(expr) == want, (ascii(expr), D.expression_problem(expr))
        with pytest.raises(D.ExpressionEvalError) as e:
            D.evaluate(expr, ctx)
        assert want in str(e.value), (ascii(expr), e.value)

    for ws in other:
        for expr in (f"$record.a{ws}", f"{ws}$record.a", f"$record.a ?{ws}'y' : 'n'", f"$record.a ? 'y'{ws}: 'n'",
                     f"$record.a{ws}? 'y' : 'n'", f"coalesce($record.a,{ws}1)"):
            refused(expr, ws)
        for tmpl in (f"v-${{record.a{ws}}}", f"v-${{{ws}record.a}}", f"v-${{{ws}}}"):
            with pytest.raises(D.ExpressionEvalError, match=f"whitespace U\\+{ord(ws):04X}"):
                D.evaluate(tmpl, ctx)
    # Not White_Space, so not removed either: it no longer vanishes at an end.
    for c in map(chr, range(0x1C, 0x20)):
        assert D.expression_problem(f"$record.a{c}") == "does not parse as an expression", hex(ord(c))
        with pytest.raises(D.ExpressionEvalError, match="does not parse as an expression"):
            D.evaluate(f"$record.a{c}", ctx)
        # Beside the ternary's ? and : too: its rewrite stripped the
        # condition and branches with str.strip().
        for expr in (f"$record.a ?{c}'y' : 'n'", f"$record.a ? 'y' :{c}'n'", f"$record.a{c}? 'y' : 'n'",
                     f"$record.a ? 'y'{c}: 'n'", f"{c}$record.a ? 'y' : 'n'", f"$record.a ? 'y' : 'n'{c}"):
            assert D.expression_problem(expr) == "does not parse as an expression", ascii(expr)
            with pytest.raises(D.ExpressionEvalError, match="does not parse as an expression"):
                D.evaluate(expr, ctx)
    # Still removed at the ends; text inside a string constant is text.
    for expr in ("$record.a\n", "\n$record.a\r\n", "\t$record.a \f", " $record.a ? 'y' : 'n' "):
        assert D.expression_problem(expr) is None, ascii(expr)
        assert D.evaluate(expr, ctx) in (1, "y"), ascii(expr)
    assert D.evaluate("$record.a ? 'a\u00a0b\u3000' : 'n'", ctx) == "a\u00a0b\u3000"
    assert D.evaluate("v-${ record.a\t}", ctx) == "v-1"
    # At load, in an expression and in a template body, and through run().
    dsl = {"adapter": {"dsl_version": "0.1.0"}, "input": {"type": "json"}, "session": {"static": {}, "derived": {
        "id": "x", "t": "$record.a\u2028", "u": "v-${record.a\u00a0}"}}, "emit": []}
    probs = D.dsl_expression_problems(dsl)
    assert any("U+2028" in p for p in probs) and any("U+00A0" in p for p in probs), probs
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False, allow_unicode=True), encoding="utf-8")
        with pytest.raises(SystemExit, match="whitespace U\\+2028"):
            D.run(td / "a.dsl.yaml", td / "missing.json", None, operator_override=_OP)


def test_unreadable_empty_and_non_object_input_through_the_cli():
    # (DSL rc3 §15 Unreadable and empty input, spec 9c801194;
    # ruled 2026-10-10: exit status 4). Through the CLI, as a caller sees it:
    # an input unreadable as a whole exits 4 with one line (it was 1 and a
    # traceback); nothing to convert exits 0 with a warning and no trace (it
    # was 1, "no records"); a json root-array element that is not an object,
    # and in single-trace mode a root that is neither array nor object, is
    # skipped as a set-level loss (exit 3) where it was a record (single-trace)
    # or two misleading R5 errors (container mode).
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    adapters = pathlib.Path(__file__).resolve().parent.parent
    runner = adapters / "dsl_runner.py"
    adapter = {"name": "probe", "harness_version": "0", "dsl_version": "0.1.0", "source_format": "test",
               "converter_version": "test", "produces_schema": "0.3.0"}
    rule = {"name": "t", "when": {"has": "content"},
            "do": [{"emit_turn": {"index": "$state.n", "content": "$record.content"}}, {"accumulate": {"state.n": 1}}]}

    def single(kind):
        return {"adapter": adapter, "input": {"type": kind}, "state": {"n": 0},
                "session": {"static": {}, "derived": {"id": "s1"}}, "emit": [rule]}
    container = yaml.safe_load((adapters / "tests" / "fixtures" / "synthetic-trace-pair.dsl.yaml").read_text())
    rec = {"index": 0, "role": "user", "content": "p"}
    cont = {"id": "c0", "events": [rec]}
    # (dsl, input suffix, input text, exit, traces written, diagnostic kinds, stderr fragment)
    skip = ["input-parse-error/set"]
    unreadable = ["input-unreadable"]   # the report says why
    cases = [
        (single("json"), "json", "[]", 0, None, ["input-empty"], "nothing to convert (no records)"),
        (single("json"), "json", json.dumps([rec, 5]), 3, 1, skip, "input record 1: parse-error (not-an-object)"),
        (single("json"), "json", "[5]", 3, None, skip, "input record 0: parse-error (not-an-object)"),
        (single("json"), "json", "5", 3, None, skip, "input record 0: parse-error (not-an-object)"),
        (single("json"), "json", "null", 3, None, skip, "input record 0: parse-error (not-an-object)"),
        (single("json"), "json", '"x"', 3, None, skip, "input record 0: parse-error (not-an-object)"),
        (single("json"), "json", json.dumps(rec), 0, 1, [], None),
        (single("json"), "json", "{bad", 4, None, unreadable, "error: input unreadable: "),
        (single("jsonl"), "jsonl", "", 0, None, ["input-empty"], "nothing to convert (no records)"),
        (single("jsonl"), "jsonl", "\n  \n", 0, None, ["input-empty"], "nothing to convert (no records)"),
        (single("jsonl"), "jsonl", json.dumps(rec) + "\n5\n", 3, 1, skip, "input record 1: parse-error (not-an-object)"),
        (container, "json", "[]", 0, None, ["input-empty"], "nothing to convert (no top-level elements)"),
        (container, "json", json.dumps([cont, 5]), 3, 2, skip, "input container 1: parse-error (not-an-object)"),
        (container, "json", json.dumps([None, cont]), 3, 2, skip, "input container 0: parse-error (not-an-object)"),
        (container, "json", json.dumps(cont), 4, None, unreadable, "container input is an object, not an array"),
        (container, "json", "{bad", 4, None, unreadable, "error: input unreadable: "),
        # Whitespace only is not well-formed JSON; a
        # single-trace root that is no array and deeper than the reading
        # bound (§4: 512) is a skipped record, not unreadable; container mode
        # with jsonl input, one container per line.
        (single("json"), "json", " \n\t", 4, None, unreadable, "error: input unreadable: "),
        (single("json"), "json", '{"a":' * 600 + "1" + "}" * 600, 3, None, skip,
         "input record 0: parse-error (nesting-too-deep)"),
        ({**container, "input": {**container["input"], "type": "jsonl"}}, "jsonl", "5\n" + json.dumps(cont) + "\n",
         3, 2, skip, "input container 0: parse-error (not-an-object)"),
    ]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        for dsl, suffix, text, code, n, kinds, frag in cases:
            label = (dsl["input"]["type"], "container" if "sessions" in dsl else "single", text[:30])
            a, i, out = td / "a.dsl.yaml", td / f"in.{suffix}", td / "out.json"
            diag = td / "out.json.diagnostics.json"
            for f in (out, diag):
                f.unlink(missing_ok=True)
            a.write_text(yaml.safe_dump(dsl, sort_keys=False))
            i.write_text(text)
            r = subprocess.run([sys.executable, str(runner), str(a), str(i), "--output", str(out),
                                "--operator-type", "scripted"], capture_output=True, text=True, timeout=300)
            assert r.returncode == code and "Traceback" not in r.stderr, (label, r.returncode, r.stderr[-300:])
            got = json.loads(out.read_text()) if out.exists() else None
            assert (None if got is None else len(got) if isinstance(got, list) else 1) == n, (label, got)
            report = json.loads(diag.read_text())
            assert sorted({d["kind"] + (f"/{d['loss']}" if d.get("loss") else "") for d in report["diagnostics"]}) \
                == kinds and report["set_level_loss"] == (code == 3), (label, report)
            if code == 4:   # the caller's line is the last one
                assert r.stderr.strip().splitlines()[-1].startswith("error: input unreadable: "), (label, r.stderr)
            if frag:
                assert frag in r.stderr, (label, r.stderr)
        # A missing input file is unreadable too, in both input types.
        for dsl, suffix in ((single("json"), "json"), (single("jsonl"), "jsonl")):
            a = td / "a.dsl.yaml"
            a.write_text(yaml.safe_dump(dsl, sort_keys=False))
            r = subprocess.run([sys.executable, str(runner), str(a), str(td / f"missing.{suffix}"),
                                "--operator-type", "scripted"], capture_output=True, text=True, timeout=300)
            assert r.returncode == 4 and "Traceback" not in r.stderr \
                and r.stderr.strip().splitlines()[-1].startswith("error: input unreadable: "), (suffix, r.stderr)
    # run() raises InputUnreadable, still a ValueError for its callers.
    D = _dr()
    assert issubclass(D.InputUnreadable, ValueError)
    # The reader's switch is off by default: other readers (gemini-cli's
    # pre-processor) keep a record that is not an object.
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "x.json"
        p.write_text("[5, {}]")
        assert source_reader.read_json(p).records == [5, {}]
        src = source_reader.read_json(p, require_object=True)
        assert src.records == [{}] and src.record_indices == [1], src.records
        [(f, loss)] = src.findings_with_loss()
        assert (f["reason"], f["record_index"], loss) == ("not-an-object", 0, "set"), (f, loss)


def test_no_earlier_output_passes_for_this_runs():
    # (DSL rc3 §15, spec 9c801194; ruled 2026-10-10: replace a file,
    # refuse a directory, never delete in a directory). An earlier run's
    # single-file trace and report stayed when this run wrote none (an []
    # run, an unreadable input, a refused adapter); a rerun into an output
    # directory failed with a FileExistsError traceback.
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    runner = pathlib.Path(__file__).resolve().parent.parent / "dsl_runner.py"
    adapter = {"name": "probe", "harness_version": "0", "dsl_version": "0.1.0", "source_format": "test",
               "converter_version": "test", "produces_schema": "0.3.0"}
    dsl = {"adapter": adapter, "input": {"type": "json"}, "state": {"n": 0},
           "session": {"static": {}, "derived": {"id": "s1"}},
           "emit": [{"name": "t", "when": {"has": "content"},
                     "do": [{"emit_turn": {"index": 0, "content": "$record.content"}}]}]}
    rec = json.dumps([{"index": 0, "role": "user", "content": "p"}])

    def cli(a, i, out):
        return subprocess.run([sys.executable, str(runner), str(a), str(i), "--output", str(out),
                               "--operator-type", "scripted"], capture_output=True, text=True, timeout=300)
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, bad_a, i = td / "a.dsl.yaml", td / "bad.dsl.yaml", td / "in.json"
        a.write_text(yaml.safe_dump(dsl, sort_keys=False))
        bad_a.write_text(yaml.safe_dump({**dsl, "emit": [{"name": "t", "when": {"has": "content"}, "do": [{"set": 5}]}]},
                                        sort_keys=False))
        out = td / "out.json"
        report = td / "out.json.diagnostics.json"
        for text, adapter_file, code, kinds in (("[]", a, 0, ["input-empty"]), ("{bad", a, 4, ["input-unreadable"]),
                                                (rec, bad_a, 1, None)):
            i.write_text(rec)
            assert cli(a, i, out).returncode == 0 and out.exists() and report.exists()
            i.write_text(text)
            r = cli(adapter_file, i, out)
            assert r.returncode == code and "Traceback" not in r.stderr, (text, r.returncode, r.stderr[-300:])
            assert not out.exists(), (text, "an earlier run's trace was left in place")
            if kinds is None:   # refused at load: no report of this run, and none of the earlier one
                assert not report.exists(), text
            else:
                assert [d["kind"] for d in json.loads(report.read_text())["diagnostics"]] == kinds, text
        # Directory output: refused before writing when it already holds a
        # trace or the report; the files there are left as they are.
        # Container mode only: directory output is a container-mode form.
        d = td / "out"
        d.mkdir()   # the CLI's Path drops a trailing "/", so only an existing directory is one
        ca = pathlib.Path(__file__).resolve().parent / "fixtures" / "synthetic-trace-pair.dsl.yaml"
        i.write_text(json.dumps([{"id": "c0", "events": [{"index": 0, "role": "user", "content": "p"}]}]))
        assert cli(ca, i, d).returncode == 0
        before = {p.name: p.read_bytes() for p in d.iterdir()}
        assert sorted(before) == ["agent-c0.minitrace.json", "conversion.diagnostics.json", "judge-c0.minitrace.json"], \
            sorted(before)
        for held in (sorted(before), ["judge-c0.minitrace.json"], ["conversion.diagnostics.json"]):
            for p in d.iterdir():
                p.unlink()
            for name in held:
                (d / name).write_bytes(before[name])
            r = cli(ca, i, d)
            assert r.returncode == 4 and "Traceback" not in r.stderr and "refused before writing" in r.stderr, \
                (held, r.stderr[-300:])
            assert {p.name: p.read_bytes() for p in d.iterdir()} == {n: before[n] for n in held}, held
        # A directory holding other files only is written as before, and
        # they are left alone.
        for p in d.iterdir():
            p.unlink()
        (d / "notes.txt").write_text("x")
        assert cli(ca, i, d).returncode == 0
        assert sorted(p.name for p in d.iterdir()) == ["agent-c0.minitrace.json", "conversion.diagnostics.json",
                                                       "judge-c0.minitrace.json", "notes.txt"]


def test_output_clearing_removes_a_link_not_its_target_and_a_lone_earlier_report():
    # (a) a link at the single-file output
    # or at its .diagnostics.json is removed, never its target; (b) an
    # earlier report is removed when no earlier trace is there. A load
    # refusal writes no report of its own, so a report left there is the
    # earlier one; a written run follows no link to a target either.
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    adapter = {"name": "probe", "harness_version": "0", "dsl_version": "0.1.0", "source_format": "test",
               "converter_version": "test", "produces_schema": "0.3.0"}
    dsl = {"adapter": adapter, "input": {"type": "json"}, "state": {"n": 0},
           "session": {"static": {}, "derived": {"id": "s1"}},
           "emit": [{"name": "t", "when": {"has": "content"},
                     "do": [{"emit_turn": {"index": 0, "content": "$record.content"}}]}]}
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, bad_a, i = td / "a.dsl.yaml", td / "bad.dsl.yaml", td / "in.json"
        a.write_text(yaml.safe_dump(dsl, sort_keys=False))
        bad_a.write_text(yaml.safe_dump({**dsl, "emit": [{"name": "t", "when": {"has": "content"},
                                                          "do": [{"set": 5}]}]}, sort_keys=False))
        i.write_text(json.dumps([{"index": 0, "role": "user", "content": "p"}]))
        out, report = td / "out.json", td / "out.json.diagnostics.json"
        keep_t, keep_r = td / "keep-trace.txt", td / "keep-report.txt"
        # (a) Both paths links to unrelated files: refused at load, and written.
        for adapter_file, refused in ((bad_a, True), (a, False)):
            for p in (out, report):
                if os.path.lexists(p):
                    p.unlink()
            keep_t.write_text("trace target")
            keep_r.write_text("report target")
            out.symlink_to(keep_t)
            report.symlink_to(keep_r)
            try:
                D.run(adapter_file, i, out, operator_override=_OP)
            except SystemExit:
                assert refused, "a valid adapter was refused"
            else:
                assert not refused, "the bad adapter was not refused"
            assert keep_t.read_text() == "trace target" and keep_r.read_text() == "report target", adapter_file.name
            assert not out.is_symlink() and not report.is_symlink(), adapter_file.name
            # Gone after the refusal, this run's own after the written run.
            assert os.path.lexists(out) is not refused, adapter_file.name
            assert os.path.lexists(report) is not refused, adapter_file.name
        # (b) A lone earlier report, no earlier trace: removed all the same.
        for p in (out, report):
            if os.path.lexists(p):
                p.unlink()
        report.write_text('{"diagnostics": []}')
        try:
            D.run(bad_a, i, out, operator_override=_OP)
        except SystemExit:
            pass
        else:
            raise AssertionError("the bad adapter was not refused")
        assert not os.path.lexists(report) and not os.path.lexists(out)


def test_output_clearing_never_touches_the_input_adapter_or_a_directory_entry():
    # The removal deleted the input when --output
    # was the input file, and the adapter when it was the adapter; the CLI's
    # own operator refusal ran before the clearing; a directory entry that is
    # a subdirectory, a dangling link or differs only in letter case got past
    # the refusal, and the run crashed after writing its report; a report
    # sibling that is a directory crashed the run. Every case is refused (or
    # cleared) before anything is written, with exit 4 and no traceback.
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    runner = pathlib.Path(__file__).resolve().parent.parent / "dsl_runner.py"
    adapter = {"name": "probe", "harness_version": "0", "dsl_version": "0.1.0", "source_format": "test",
               "converter_version": "test", "produces_schema": "0.3.0"}
    dsl = {"adapter": adapter, "input": {"type": "json"}, "state": {"n": 0},
           "session": {"static": {}, "derived": {"id": "s1"}},
           "emit": [{"name": "t", "when": {"has": "content"},
                     "do": [{"emit_turn": {"index": 0, "content": "$record.content"}}]}]}
    rec = json.dumps([{"index": 0, "role": "user", "content": "p"}])

    def cli(a, i, out, op="scripted"):
        return subprocess.run([sys.executable, str(runner), str(a), str(i), "--output", str(out),
                               "--operator-type", op], capture_output=True, text=True, timeout=300)
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = td / "a.dsl.yaml", td / "in.json"
        a.write_text(yaml.safe_dump(dsl, sort_keys=False))
        i.write_text(rec)
        a_bytes, i_bytes = a.read_bytes(), i.read_bytes()
        link = td / "link.json"
        link.symlink_to(i)
        hard = td / "hard.json"
        os.link(i, hard)
        # the input, the adapter, a link and a hard link to the input
        # (samefile, not path equality)
        for out in (i, a, link, hard):
            r = cli(a, i, out)
            assert r.returncode == 4 and "is the same file as" in r.stderr and "Traceback" not in r.stderr, \
                (out, r.returncode, r.stderr[-300:])
            assert a.read_bytes() == a_bytes and i.read_bytes() == i_bytes, out
        assert not (td / "in.json.diagnostics.json").exists()
        # The report sibling is the input: x.json.diagnostics.json as input,
        # x.json (not there yet) as output.
        sib_in = td / "x.json.diagnostics.json"
        sib_in.write_text(rec)
        r = cli(a, sib_in, td / "x.json")
        assert r.returncode == 4 and "is the same file as" in r.stderr and sib_in.read_text() == rec, r.stderr
        # A directory output never writes that sibling, so it is not checked
        # there: an adapter named k.diagnostics.json beside an output dir k.
        k = td / "k"
        k.mkdir()
        ka = td / "k.diagnostics.json"
        ka.write_text((pathlib.Path(__file__).resolve().parent / "fixtures" / "synthetic-trace-pair.dsl.yaml").read_text())
        (td / "c.json").write_text(json.dumps([{"id": "c0", "events": [{"index": 0, "role": "user", "content": "p"}]}]))
        assert cli(ka, td / "c.json", k).returncode == 0 and (k / "agent-c0.minitrace.json").exists()
        # A location that cannot be cleared is refused, not a traceback: an
        # unreadable output directory, an earlier output in a read-only one.
        locked = td / "locked"
        locked.mkdir()
        ro = td / "ro"
        ro.mkdir()
        i.write_text(rec)
        assert cli(a, i, ro / "o.json").returncode == 0
        # Root clears and removes regardless of the modes, so this block is
        # not exercised as root (ruled 2026-10-22); the rest of the test is.
        if not (hasattr(os, "geteuid") and os.geteuid() == 0):
            locked.chmod(0o000)
            ro.chmod(0o555)
            try:
                r = cli(ka, td / "c.json", locked)
                assert r.returncode == 4 and "cannot be cleared" in r.stderr and "Traceback" not in r.stderr, \
                    r.stderr[-300:]
                r = cli(a, i, ro / "o.json")
                assert r.returncode == 4 and "cannot be removed" in r.stderr and "left in place from an earlier run" \
                    in r.stderr and "Traceback" not in r.stderr, r.stderr[-300:]
            finally:
                locked.chmod(0o755)
                ro.chmod(0o755)
            # Both paths are checked before either is
            # removed, and what was removed or left is named.
            assert (ro / "o.json").exists() and (ro / "o.json.diagnostics.json").exists()   # left, as the message says
        (ro / "o.json.diagnostics.json").unlink()
        (ro / "o.json.diagnostics.json").mkdir()
        r = cli(a, i, ro / "o.json")
        assert r.returncode == 4 and "nothing removed" in r.stderr and (ro / "o.json").exists(), r.stderr[-300:]
        (ro / "o.json.diagnostics.json").rmdir()
        if hasattr(os, "chflags") and hasattr(stat_mod := __import__("stat"), "UF_IMMUTABLE"):
            assert cli(a, i, ro / "o.json").returncode == 0
            rep = ro / "o.json.diagnostics.json"
            os.chflags(rep, stat_mod.UF_IMMUTABLE)
            try:
                r = cli(a, i, ro / "o.json")
                assert r.returncode == 4 and f"removed: {ro / 'o.json'}" in r.stderr \
                    and f"left in place from an earlier run: {rep}" in r.stderr, r.stderr[-300:]
                assert not (ro / "o.json").exists() and rep.exists()
            finally:
                os.chflags(rep, 0)
        # A name the file system cannot hold for the report sibling is
        # refused before anything is written (the trace was written first).
        longo = td / ("n" * 240 + ".json")
        r = cli(a, i, longo)
        assert r.returncode == 4 and "cannot be cleared" in r.stderr and "Traceback" not in r.stderr \
            and not longo.exists(), (r.returncode, r.stderr[-300:])
        # The CLI's operator refusal (human without capture_mode) comes after
        # the clearing now, as every other outcome does; and a single-file run
        # whose every record was skipped leaves no earlier trace either.
        out, report = td / "out.json", td / "out.json.diagnostics.json"
        for op, text, code in (("human", rec, 1), ("scripted", "[5]", 3)):
            i.write_text(rec)
            assert cli(a, i, out).returncode == 0 and out.exists() and report.exists()
            i.write_text(text)
            r = cli(a, i, out, op)
            assert r.returncode == code and "Traceback" not in r.stderr, (op, r.returncode, r.stderr[-300:])
            assert not out.exists() and (report.exists() == (code == 3)), (op, sorted(p.name for p in td.iterdir()))
        # A report sibling that is a directory is refused, not removed.
        i.write_text(rec)
        report.unlink()   # the all-skipped run's report
        report.mkdir()
        r = cli(a, i, out)
        assert r.returncode == 4 and "is not a file" in r.stderr and report.is_dir() and not out.exists(), r.stderr
        report.rmdir()
        # Directory output: any entry type, any letter case, counts; nothing
        # in the directory is changed; refused before the input is read.
        ca = pathlib.Path(__file__).resolve().parent / "fixtures" / "synthetic-trace-pair.dsl.yaml"
        d = td / "out"
        d.mkdir()
        i.write_text(json.dumps([{"id": "c0", "events": [{"index": 0, "role": "user", "content": "p"}]}]))
        makers = {"subdirectory": lambda p: p.mkdir(), "dangling link": lambda p: p.symlink_to(td / "nowhere"),
                  "file": lambda p: p.write_text("old")}
        for name, kind in (("agent-c0.minitrace.json", "subdirectory"), ("agent-c0.minitrace.json", "dangling link"),
                           ("AGENT-C0.MINITRACE.JSON", "file"), ("Conversion.Diagnostics.json", "file")):
            makers[kind](d / name)
            for text in (i.read_text(), "{bad"):   # refused before the input is read
                r = cli(ca, td / ("bad.json" if text == "{bad" else "in.json"), d) if text == "{bad" else cli(ca, i, d)
                assert r.returncode == 4 and "refused before writing" in r.stderr and "Traceback" not in r.stderr, \
                    (name, kind, r.stderr[-300:])
                assert sorted(os.listdir(d)) == [name], (name, kind, os.listdir(d))
            p = d / name
            p.rmdir() if kind == "subdirectory" else p.unlink()


def test_coverage_never_overwrites_the_input_adapter_trace_or_report():
    # Ruled 2026-10-10: --coverage overwrote the input, the adapter,
    # the trace just written or its report, with exit 0, and a directory
    # there crashed after the trace was written. Each is refused with exit 4
    # before anything is removed or run; an earlier coverage file is removed
    # at the start, as output clearing removes an earlier single-file trace.
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    runner = pathlib.Path(__file__).resolve().parent.parent / "dsl_runner.py"
    adapter = {"name": "probe", "harness_version": "0", "dsl_version": "0.1.0", "source_format": "test",
               "converter_version": "test", "produces_schema": "0.3.0"}
    dsl = {"adapter": adapter, "input": {"type": "json"}, "state": {"n": 0},
           "session": {"static": {}, "derived": {"id": "s1"}},
           "emit": [{"name": "t", "when": {"has": "content"},
                     "do": [{"emit_turn": {"index": 0, "content": "$record.content"}}]}]}
    rec = json.dumps([{"index": 0, "role": "user", "content": "p"}])

    def cli(a, i, out, cov):
        return subprocess.run([sys.executable, str(runner), str(a), str(i), "--output", str(out),
                               "--coverage", str(cov), "--operator-type", "scripted"],
                              capture_output=True, text=True, timeout=60)   # a fifo not refused blocks

    def refused(r, what):
        assert r.returncode == 4 and what in r.stderr and "refused before writing" in r.stderr \
            and "Traceback" not in r.stderr, (r.returncode, r.stderr[-300:])
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i, out = td / "a.dsl.yaml", td / "in.json", td / "out.json"
        report = td / "out.json.diagnostics.json"
        a.write_text(yaml.safe_dump(dsl, sort_keys=False))
        i.write_text(rec)
        assert cli(a, i, out, td / "cov.json").returncode == 0 and (td / "cov.json").exists()
        snap = {p: p.read_bytes() for p in (a, i, out, report)}
        link, hard, dangling = td / "link.json", td / "hard.json", td / "dangling.json"
        link.symlink_to(i)
        os.link(i, hard)
        # The input, the adapter, the trace, its report, a link and a hard
        # link to the input: refused, and the earlier trace and report are
        # left (refused before anything is removed).
        for cov in (i, a, out, report, link, hard):
            refused(cli(a, i, out, cov), "is the same file as")
            assert {p: p.read_bytes() for p in snap} == snap, cov
        # Not there yet: the same path as the trace or its report, and a
        # dangling link to where the trace will be written.
        fresh = td / "fresh.json"
        dangling.symlink_to(fresh)
        for cov in (fresh, td / "fresh.json.diagnostics.json", dangling, td / "." / "fresh.json"):
            refused(cli(a, i, fresh, cov), "is the same file as")
            assert not fresh.exists() and not (td / "fresh.json.diagnostics.json").exists(), cov
        # A directory, or a link to one: refused before the trace is written
        # (it was an IsADirectoryError after it).
        sub = td / "sub"
        sub.mkdir()
        sublink = td / "sublink"
        sublink.symlink_to(sub)
        for cov in (sub, sublink):
            refused(cli(a, i, fresh, cov), "is a directory")
            assert not fresh.exists() and sub.is_dir() and sublink.is_symlink(), cov
        if hasattr(os, "mkfifo"):   # anything else that is not a file is refused, not removed
            fifo = td / "fifo"
            os.mkfifo(fifo)
            refused(cli(a, i, fresh, fifo), "is not a file")
            assert fifo.exists() and not fresh.exists()
        # An earlier coverage file is removed at the start: an unreadable
        # input and a refused adapter leave none; a link is removed, never
        # its target.
        bad_a = td / "bad.dsl.yaml"
        bad_a.write_text(yaml.safe_dump({**dsl, "emit": [{"name": "t", "when": {"has": "content"}, "do": [{"set": 5}]}]},
                                        sort_keys=False))
        cov = td / "cov.json"
        for text, adapter_file, code in (("{bad", a, 4), (rec, bad_a, 1)):
            i.write_text(rec)
            assert cli(a, i, out, cov).returncode == 0 and cov.exists()
            i.write_text(text)
            r = cli(adapter_file, i, out, cov)
            assert r.returncode == code and "Traceback" not in r.stderr, (text, r.stderr[-300:])
            assert not cov.exists(), (text, "an earlier run's coverage was left in place")
        i.write_text(rec)
        target = td / "keep.json"
        target.write_text("keep")
        covlink = td / "covlink.json"
        covlink.symlink_to(target)
        assert cli(a, i, out, covlink).returncode == 0
        assert target.read_text() == "keep" and not covlink.is_symlink() and "unmapped" in covlink.read_text()
        # Directory output: a coverage path inside it with a trace's or the
        # report's name, in any letter case, is refused; any other name is
        # written beside the traces.
        ca = pathlib.Path(__file__).resolve().parent / "fixtures" / "synthetic-trace-pair.dsl.yaml"
        c = td / "c.json"
        c.write_text(json.dumps([{"id": "c0", "events": [{"index": 0, "role": "user", "content": "p"}]}]))
        d = td / "out"
        d.mkdir()
        for name in ("agent-c0.minitrace.json", "X.MINITRACE.JSON", "Conversion.Diagnostics.json"):
            refused(cli(ca, c, d, d / name), "inside the output directory")
            assert os.listdir(d) == [], name
        assert cli(ca, c, d, d / "cov.json").returncode == 0
        assert sorted(os.listdir(d)) == ["agent-c0.minitrace.json", "conversion.diagnostics.json", "cov.json",
                                         "judge-c0.minitrace.json"]
        # Ruled 2026-10-10: an earlier coverage file
        # inside a directory output is refused, never removed, a link
        # there too; outside it, a coverage path whose parent is missing is
        # refused before the trace is written (it was a traceback after it).
        for p in d.iterdir():
            if p.name != "cov.json":
                p.unlink()
        (d / "covlink.json").symlink_to(td / "nowhere")
        for name in ("cov.json", "covlink.json"):
            before = (d / "cov.json").read_bytes()
            refused(cli(ca, c, d, d / name), "already exists inside the output directory")
            assert sorted(os.listdir(d)) == ["cov.json", "covlink.json"] and (d / "cov.json").read_bytes() == before
        refused(cli(a, i, fresh, td / "nodir" / "cov.json"), "is not an existing directory")
        assert not fresh.exists() and not (td / "nodir").exists()
        # ... and an earlier trace and report are left.
        assert cli(a, i, out, td / "cov.json").returncode == 0
        earlier = {p: p.read_bytes() for p in (out, report)}
        refused(cli(a, i, out, td / "nodir" / "cov.json"), "is not an existing directory")
        assert {p: p.read_bytes() for p in earlier} == earlier
        # A case-insensitive file system: a path differing only in letter
        # case is the same file, which only shows once the trace is written.
        # The coverage is then refused, and the trace kept.
        probe = td / "case-probe"
        probe.write_text("")
        if (td / "CASE-PROBE").exists():
            for cov, hit in ((td / "OUT.JSON", out), (td / "OUT.JSON.DIAGNOSTICS.JSON", report)):
                for p in (out, report):
                    p.unlink(missing_ok=True)
                r = cli(a, i, out, cov)
                assert r.returncode == 4 and "is the same file as" in r.stderr and "found only after the run" \
                    in r.stderr and "the trace was written and kept, the coverage not written" in r.stderr \
                    and "refused before writing" not in r.stderr and "Traceback" not in r.stderr, r.stderr[-300:]
                kept = json.loads(out.read_text())
                assert kept.get("turns") == [{"index": 0, "content": "p"}] and "input_summary" not in kept, \
                    (cov, "the trace was overwritten")
                assert "diagnostics" in json.loads(report.read_text()), (cov, "the report was overwritten")


def test_an_input_or_adapter_the_runner_may_not_read_is_no_traceback():
    # DSL rc3 §15, spec a3bf9aee; ruled 2026-10-10: an input that
    # cannot be opened or read, not permitted included, is unreadable as a
    # whole: its report and exit 4 (it was a PermissionError traceback from
    # source_reader._hash_file, exit 1). An adapter that cannot be read
    # (missing, not permitted, a directory) is exit 1 in one line, as an
    # adapter refused at load (it was a traceback). A process that reads a
    # file without read permission (root, unless it lacks the capability to
    # bypass file permissions; a probe decides, not the user id) checks only
    # that it reads the input and the shared reader's files; the hash pass,
    # the CLI on a file under a shut directory, the re-hash and the two locked
    # adapters are not checked for it. The other cases run for every user.
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import pytest  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        probe = pathlib.Path(td) / "probe"
        probe.write_text("x")
        probe.chmod(0o000)
        try:
            probe.read_bytes()
            reads_locked = True
        except PermissionError:
            reads_locked = False
        finally:
            probe.chmod(0o600)
    runner = pathlib.Path(__file__).resolve().parent.parent / "dsl_runner.py"
    adapter = {"name": "probe", "harness_version": "0", "dsl_version": "0.1.0", "source_format": "test",
               "converter_version": "test", "produces_schema": "0.3.0"}

    def dsl(kind):
        return {"adapter": adapter, "input": {"type": kind}, "state": {"n": 0},
                "session": {"static": {}, "derived": {"id": "s1"}},
                "emit": [{"name": "t", "when": {"has": "content"},
                          "do": [{"emit_turn": {"index": 0, "content": "$record.content"}}]}]}

    def cli(a, i, out):
        return subprocess.run([sys.executable, str(runner), str(a), str(i), "--output", str(out),
                               "--operator-type", "scripted"], capture_output=True, text=True, timeout=60)
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        out, report = td / "out.json", td / "out.json.diagnostics.json"
        for kind, text in (("json", json.dumps([{"index": 0, "role": "user", "content": "p"}])),
                           ("jsonl", json.dumps({"index": 0, "role": "user", "content": "p"}) + "\n")):
            a, i = td / f"{kind}.dsl.yaml", td / f"in.{kind}"
            a.write_text(yaml.safe_dump(dsl(kind), sort_keys=False))
            i.write_text(text)
            assert cli(a, i, out).returncode == 0 and out.exists()
            if reads_locked:
                out.unlink()   # so the run below must write it
            i.chmod(0o000)
            try:
                r = cli(a, i, out)
            finally:
                i.chmod(0o644)
            if reads_locked:
                assert r.returncode == 0 and out.exists(), (kind, "the input was read", r.stderr[-300:])
                continue
            assert r.returncode == 4 and "Traceback" not in r.stderr \
                and r.stderr.strip().splitlines()[-1].startswith("error: input unreadable: "), (kind, r.stderr[-300:])
            assert not out.exists(), kind
            assert [d["kind"] for d in json.loads(report.read_text())["diagnostics"]] == ["input-unreadable"], kind
        # The shared reader: the hash pass is the first read; a file under a
        # parent without search permission is read-error too (Python 3.12's
        # is_file raised there, 3.14 said not-a-file).
        p = td / "locked.jsonl"
        p.write_text("{}\n")
        shut = td / "shut"
        shut.mkdir()
        q = shut / "x.jsonl"
        q.write_text("{}\n")
        p.chmod(0o000)
        shut.chmod(0o000)
        try:
            for read in (source_reader.read_jsonl, source_reader.read_json):
                for path in (p, q):
                    if reads_locked:
                        assert read(path).size_bytes == 3, (read.__name__, path, "the file was read")
                        continue
                    with pytest.raises(source_reader.SourceUnreadable) as e:
                        read(path)
                    assert e.value.reason == "read-error", (read.__name__, path, e.value.reason)
            if not reads_locked:
                with pytest.raises(source_reader.SourceUnreadable):
                    source_reader._hash_file(p)
            loop = td / "loop.jsonl"
            loop.symlink_to(loop)   # not there to read: not-a-file, as a dangling link
            with pytest.raises(source_reader.SourceUnreadable) as e:
                source_reader.read_jsonl(loop)
            assert e.value.reason == "not-a-file", e.value.reason
            # A NUL in the path was a ValueError
            # traceback; a name too long stays not-a-file.
            for odd in (td / "a\0b.jsonl", td / ("n" * 300 + ".jsonl")):
                with pytest.raises(source_reader.SourceUnreadable) as e:
                    source_reader.read_jsonl(odd)
                assert e.value.reason == "not-a-file", (len(str(odd)), e.value.reason)
            if not reads_locked:
                a = td / "jsonl.dsl.yaml"
                r = cli(a, q, out)
                assert r.returncode == 4 and "Traceback" not in r.stderr and "read-error" in r.stderr, \
                    r.stderr[-300:]
        finally:
            p.chmod(0o644)
            shut.chmod(0o755)
        # The second call site: the changed-during-read re-hash, with the
        # file no longer readable by then (not checked when it is read anyway).
        real_hash = source_reader._hash_file
        calls = []

        def hash_then_lock(path):
            calls.append(path)
            if len(calls) == 2:
                path.chmod(0o000)
            return real_hash(path)
        source_reader._hash_file = hash_then_lock
        try:
            if not reads_locked:
                with pytest.raises(source_reader.SourceUnreadable) as e:
                    source_reader.read_jsonl(p)
                assert e.value.reason == "read-error" and len(calls) == 2, (e.value.reason, len(calls))
        finally:
            source_reader._hash_file = real_hash
            p.chmod(0o644)
        # An adapter that cannot be read: exit 1, one line, no traceback,
        # the earlier output removed first; anything that is not a
        # regular file is refused before reading (a FIFO blocked for ever).
        i = td / "in.json"
        good = td / "json.dsl.yaml"
        locked_a = td / "locked.dsl.yaml"
        locked_a.write_text(yaml.safe_dump(dsl("json"), sort_keys=False))
        locked_a.chmod(0o000)
        adir = td / "adir"
        adir.mkdir()
        dangling_a, dirlink_a = td / "dangling.dsl.yaml", td / "dirlink.dsl.yaml"
        dangling_a.symlink_to(td / "nowhere")
        dirlink_a.symlink_to(adir)
        shut.chmod(0o755)
        (shut / "a.dsl.yaml").write_text(locked_a.name)
        bads = [td / "missing.dsl.yaml", adir, dangling_a, dirlink_a] + ([] if reads_locked else [locked_a, shut / "a.dsl.yaml"])
        if hasattr(os, "mkfifo"):
            os.mkfifo(td / "fifo.dsl.yaml")
            bads.append(td / "fifo.dsl.yaml")
        try:
            for bad in bads:
                assert cli(good, i, out).returncode == 0 and out.exists()
                if bad.parent == shut:
                    shut.chmod(0o000)
                try:
                    r = cli(bad, i, out)
                finally:
                    shut.chmod(0o755)
                last = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ""
                assert r.returncode == 1 and "Traceback" not in r.stderr and last.startswith(f"error: adapter {bad} ") \
                    and "cannot be read" in last, (bad, r.returncode, r.stderr[-300:])
                assert not out.exists() and not report.exists(), (bad, "the earlier output was left")
        finally:
            locked_a.chmod(0o644)
            shut.chmod(0o755)


def test_the_findings_summary_counts_every_replacement_past_the_bound():
    # DSL rc3 §4 (Bytes in the input; f80de236): each
    # replacement is an input failure and the summary past the reporting
    # bound names their count. Past 1,000 runs in one line or document the
    # decoder folds the rest into one run, which counted as one: 5,000
    # isolated FF bytes gave 1,000 reports and "1 further input finding".
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415

    def summary(src):
        tr = [f for f in src.findings if f["kind"] == "findings-truncated"]
        return (sum(f["kind"] == "decode-error" for f in src.findings), tr[0]["suppressed_count"] if tr else 0)
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        doc, lines = td / "d.json", td / "l.jsonl"
        doc.write_bytes(b'["' + b"a\xff" * 5000 + b'"]')
        # Two lines of 1,500 runs each: 1,000 listed, 2,000 further.
        lines.write_bytes((b'"' + b"a\xff" * 1500 + b'"\n') * 2)
        assert summary(source_reader.read_json(doc)) == (1000, 4000)
        assert summary(source_reader.read_jsonl(lines, require_object=False)) == (1000, 2000)
        # A bound above the decoder's: the summarising run is listed, and the
        # replacements it stands for beyond itself are still counted.
        wide = source_reader.ReadLimits(max_findings=5000)
        assert summary(source_reader.read_json(doc, limits=wide)) == (1001, 3999)
        # Pinned: the boundary (1,000 runs: no summary;
        # 1,001: one further), a multi-byte run (ED A0 80: three replacements
        # each), and skipped lines before the folded run: the summary names
        # the first affected record and stays set-level.
        edge, mixed = td / "edge.json", td / "mixed.jsonl"
        for n, want in ((1000, (1000, 0)), (1001, (1000, 1)), (1002, (1000, 2))):
            edge.write_bytes(b'["' + b"a\xff" * n + b'"]')
            assert summary(source_reader.read_json(edge)) == want, n
        edge.write_bytes(b'["' + b"\xed\xa0\x80" * 1500 + b'"]')
        assert summary(source_reader.read_json(edge)) == (1000, 3500)
        mixed.write_bytes(b"{bad\n" * 900 + b'"' + b"a\xff" * 1500 + b'"\n' + b"{bad\n" * 5)
        src = source_reader.read_jsonl(mixed, require_object=False)
        # 900 skips and 100 decode errors listed (1,000), the other 1,405
        # (1,400 runs and 5 skips) in the summary: 2,405 in all.
        assert summary(src) == (100, 1405) and len(src.findings) == 1001, summary(src)
        [(trunc, loss)] = [(f, l) for f, l in src.findings_with_loss() if f["kind"] == "findings-truncated"]
        assert trunc["record_index"] == 900 and loss == "set", (trunc, loss)
        # The pre-processors' whole-file decode reports the total too.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            source_reader.decode_reporting(doc)
        assert "5000 undecodable byte run(s)" in err.getvalue(), err.getvalue()
        # Through the CLI: the report's summary names 4,000.
        runner = pathlib.Path(__file__).resolve().parent.parent / "dsl_runner.py"
        adapter = {"name": "probe", "harness_version": "0", "dsl_version": "0.1.0", "source_format": "test",
                   "converter_version": "test", "produces_schema": "0.3.0"}
        dsl = {"adapter": adapter, "input": {"type": "json"}, "state": {"n": 0},
               "session": {"static": {}, "derived": {"id": "s1"}},
               "emit": [{"name": "t", "when": {"has": "content"},
                         "do": [{"emit_turn": {"index": 0, "content": "$record.content"}}]}]}
        a = td / "a.dsl.yaml"
        a.write_text(yaml.safe_dump(dsl, sort_keys=False))
        i = td / "in.json"
        i.write_bytes(b'[{"index": 0, "role": "user", "content": "' + b"a\xff" * 5000 + b'"}]')
        out = td / "out.json"
        r = subprocess.run([sys.executable, str(runner), str(a), str(i), "--output", str(out),
                            "--operator-type", "scripted"], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and "Traceback" not in r.stderr, r.stderr[-300:]
        diags = json.loads((td / "out.json.diagnostics.json").read_text())["diagnostics"]
        trunc = [d for d in diags if d.get("kind") == "input-findings-truncated"]
        assert len(trunc) == 1 and trunc[0]["message"].startswith("4000 further input finding(s)"), trunc


def test_a_record_path_item_that_is_not_an_object_is_skipped_as_unreadable():
    # DSL rc3 §4.1.1, §15, spec f405b67e; ruled 2026-10-10: an
    # item of a container's record_path list that is not an object is
    # skipped as unreadable (set-level, exit 3); the other items are the
    # records; a container whose every item was skipped still has its trace,
    # built from none. A record keeps its list position in every report, and
    # the skips count toward the §4 reporting bound with the reader's
    # findings. Before, every item was a record (7 turns, exit 0).
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    runner = pathlib.Path(__file__).resolve().parent.parent / "dsl_runner.py"
    adapter = {"name": "probe", "harness_version": "0", "dsl_version": "0.1.0", "source_format": "test",
               "converter_version": "test", "produces_schema": "0.3.0"}

    def dsl(kind, record_path="items"):
        return {"adapter": adapter, "input": {"type": kind, "container": {"record_path": record_path}},
                "state": {"n": 0},
                "session": {"static": {}, "derived": {"id": "c-${container.id}", "title": "first-${record_first.t}"}},
                "emit": [{"name": "t", "when": {"has": "t"},
                          "do": [{"emit_turn": {"index": "$state.n", "role": "user", "content": "$record.t"}},
                                 {"set": {"state.n": "$state.n + 1"}},
                                 {"note": {"level": "info", "message": "${record.v}"}}]}]}

    def run(kind, text, record_path="items"):
        a, i, out = td / f"{kind}.dsl.yaml", td / f"in.{kind}", td / "out.json"
        a.write_text(yaml.safe_dump(dsl(kind, record_path), sort_keys=False))
        i.write_bytes(text) if isinstance(text, bytes) else i.write_text(text)
        r = subprocess.run([sys.executable, str(runner), str(a), str(i), "--output", str(out),
                            "--operator-type", "scripted"], capture_output=True, text=True, timeout=120)
        assert "Traceback" not in r.stderr, r.stderr[-300:]
        traces = json.loads(out.read_text()) if out.exists() else None
        return r.returncode, traces, json.loads((td / "out.json.diagnostics.json").read_text())["diagnostics"]

    def skips(diags):
        return [(d["container_index"], d["record_index"]) for d in diags
                if d.get("reason") == "not-an-object" and d.get("kind") == "input-parse-error"]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        mixed = [5, {"t": "A", "v": "ok"}, "x", None, [1], True, {"t": "B", "v": {"d": 1}}]
        conts = [{"id": "a", "items": mixed}, {"id": "b", "items": [1, "y"]}]
        for kind, text in (("json", json.dumps(conts)), ("jsonl", "".join(json.dumps(c) + "\n" for c in conts))):
            code, traces, diags = run(kind, text)
            assert code == 3, (kind, code)
            got = {t["id"]: ([u["content"] for u in t["turns"]], t.get("title")) for t in traces}
            # $record_first is the first object; container b's trace is built from none.
            assert got == {"c-a": (["A", "B"], "first-A"), "c-b": ([], "first-")}, (kind, got)
            assert skips(diags) == [(0, 0), (0, 2), (0, 3), (0, 4), (0, 5), (1, 0), (1, 1)], (kind, skips(diags))
            assert all(d["loss"] == "set" for d in diags if d.get("reason") == "not-an-object"), kind
            # A later report names the record by its list position: 6, not 1.
            [late] = [d for d in diags if d.get("kind") == "input-failure"]
            assert (late["record_index"], late["container_index"]) == (6, 0) and "(record 6, container 0)" \
                in late["message"], late
        # A session-level report reading $record_first names its list
        # position too (record 1 here, not 0).
        code, traces, diags = run("json", json.dumps([{"id": "e", "items": [7, {"t": {"d": 2}}]}]))
        [first] = [d for d in diags if d.get("kind") == "input-failure" and "session.derived.title" in d["message"]]
        assert (first["record_index"], first["container_index"]) == (1, 0), first
        # A deep path, an index path, and $self untouched.
        code, traces, diags = run("json", json.dumps([{"id": "d", "p": {"q": [{"t": "Z"}, 7]}}]), "p.q")
        assert code == 3 and [u["content"] for u in traces[0]["turns"]] == ["Z"] and skips(diags) == [(0, 1)]
        code, traces, diags = run("json", json.dumps([{"id": "s", "t": "S"}]), "$self")
        assert code == 0 and [u["content"] for u in traces[0]["turns"]] == ["S"] and skips(diags) == []
        # The bound: 1,200 skipped items, 1,000 reported, the rest summarised
        # with their number and the first (record 1000, container 0).
        code, traces, diags = run("json", json.dumps([{"id": "m", "items": list(range(1200))}]))
        assert code == 3 and len(skips(diags)) == 1000, len(skips(diags))
        [summ] = [d for d in diags if d.get("kind") == "input-findings-truncated"]
        assert (summ["suppressed_count"], summ["record_index"], summ["container_index"], summ["loss"]) \
            == (200, 1000, 0, "set") and summ["message"].startswith("200 further input finding(s)"), summ
        # Shared with the reader: 998 skipped lines leave room for 2 of 5.
        good = json.dumps({"id": "g", "items": [1, 2, 3, 4, 5]}) + "\n"
        code, traces, diags = run("jsonl", "{bad\n" * 998 + good)
        [summ] = [d for d in diags if d.get("kind") == "input-findings-truncated"]
        assert len(skips(diags)) == 2 and summ["suppressed_count"] == 3 and (summ["record_index"],
                                                                             summ["container_index"]) == (2, 998), summ
        # The reader already summarised (1,001 skipped lines): its count takes all 5.
        code, traces, diags = run("jsonl", "{bad\n" * 1001 + good)
        [summ] = [d for d in diags if d.get("kind") == "input-findings-truncated"]
        assert skips(diags) == [] and summ["suppressed_count"] == 6 and summ["loss"] == "set" \
            and summ["message"].startswith("6 further input finding(s)") \
            and summ["message"].count("; at least one skipped a record") == 1, summ
        # Pinned: a value-level reader summary (one line
        # of 1,005 bad bytes) becomes set-level, its suffix added once;
        # 1,000 skipped lines leave a budget of 0 with no reader summary, so
        # a summary of their own; a container after one the reader skipped
        # is named by its input position (2, not 1).
        code, traces, diags = run("jsonl", b'{"id": "f", "items": [1, 2], "z": "' + b"a\xff" * 1005 + b'"}\n')
        [summ] = [d for d in diags if d.get("kind") == "input-findings-truncated"]
        assert summ["suppressed_count"] == 7 and summ["loss"] == "set" \
            and summ["message"].count("; at least one skipped a record") == 1, summ
        code, traces, diags = run("jsonl", "{bad\n" * 1000 + good)
        [summ] = [d for d in diags if d.get("kind") == "input-findings-truncated"]
        assert skips(diags) == [] and summ["suppressed_count"] == 5 and summ["container_index"] == 1000, summ
        code, traces, diags = run("json", json.dumps([{"id": "h", "items": [{"t": "H"}]}, 7, {"id": "k", "items": ["q"]}]))
        # The reader skips the top-level 7 (container 1, no record); the
        # item skip names container 2.
        assert code == 3 and skips(diags) == [(1, None), (2, 0)], skips(diags)
        # DSL rc3 §4, spec e03c3454; ruled 2026-10-10: the
        # summary names the earliest input in input order among all it
        # stands for, not the first finding left unreported. A skipped item
        # in container 0 folded into the reader's summary of 1,001 bad lines
        # (first unreported: line 1001) is the first: record 0, container 0.
        code, traces, diags = run("jsonl", json.dumps({"id": "z", "items": ["x", {"t": "Z"}]}) + "\n"
                                  + "{bad\n" * 1001)
        [summ] = [d for d in diags if d.get("kind") == "input-findings-truncated"]
        assert (summ["suppressed_count"], summ["record_index"], summ["container_index"]) == (2, 0, 0) \
            and "the first at record 0, container 0; at least one skipped a record" in summ["message"] \
            and summ.get("path") is None, summ
        # A reader summary whose first carries a path (a string repair) drops
        # it when a folded skip is earlier: the path named the later input.
        rep = "".join('{"id": "r%d", "items": [{"t": "R"}], "s": "\\ud800"}\n' % n for n in range(1001))
        code, traces, diags = run("jsonl", json.dumps({"id": "z", "items": ["x", {"t": "Z"}]}) + "\n" + rep)
        [summ] = [d for d in diags if d.get("kind") == "input-findings-truncated"]
        assert (summ["record_index"], summ["container_index"]) == (0, 0) and summ.get("path") is None, summ
        assert any(d.get("path") for d in diags if d.get("kind") != "input-findings-truncated"), \
            "the repairs carry paths, so the summary had one to drop"
        # The reader's own first is the earliest too
        # (within a line it added the depth truncations before the repairs),
        # by container, then list position; a finding on the container as a
        # whole, or on the input file, has no position and stays first.
        fill = "{bad\n" * 1000
        deep = "[" * 70 + "]" * 70

        def first(text, kind="jsonl"):
            code, traces, diags = run(kind, text)
            [summ] = [d for d in diags if d.get("kind") == "input-findings-truncated"]
            return summ["record_index"], summ["container_index"], summ["message"]
        rep0 = '{"t": "A", "s": "\\ud800"}'
        assert first(fill + '{"id": "q", "items": [%s, {"t": "B", "d": %s}]}\n' % (rep0, deep))[:2] == (0, 1000)
        assert first(fill + '{"id": "q", "items": [%s, "x", {"t": "B", "d": %s}]}\n' % (rep0, deep))[:2] == (0, 1000)
        assert first("[" + "5, " * 1000 + '{"id": "q", "items": [%s, {"t": "B", "d": %s}]}]' % (rep0, deep),
                     "json")[:2] == (0, 1000)
        assert first(fill + '{"id": "q", "items": ["x", {"t": "B", "d": %s}]}\n' % deep)[:2] == (0, 1000)
        ri, ci, msg = first(fill.encode() + b'{"id": "q", "z": "\xff", "items": ["x", {"t": "B"}]}\n')
        assert (ri, ci) == (None, 1000) and "the first at container 1000;" in msg, (ri, ci, msg)
        # The convention stated in _reader_order_key (the spec fixes no order):
        # a finding with no position precedes the positioned ones
        # of its line, so it stays first beside a repair at record 0.
        ri, ci, msg = first(fill.encode() + b'{"id": "q", "z": "\xff", "items": [{"t": "A", "s": "\\ud800"}]}\n')
        assert (ri, ci) == (None, 1000), (ri, ci, msg)
        ri, ci, msg = first(b'[{"id": "q", "z": "' + b"a\xff" * 1001 + b'", "items": ["x", {"t": "B"}]}]', "json")
        assert ci is None and "the first at the input file" in msg, (ri, ci, msg)
        # The reader's first stays when it is earlier (its lines come first).
        code, traces, diags = run("jsonl", "{bad\n" * 1001 + json.dumps({"id": "y", "items": ["x"]}) + "\n")
        [summ] = [d for d in diags if d.get("kind") == "input-findings-truncated"]
        assert (summ["suppressed_count"], summ["container_index"]) == (2, 1000) \
            and "the first at container 1000" in summ["message"], summ


# verify_run outcomes (ruled 2026-10-20): complete, incomplete (a set-level
# loss, or nothing converted) or failed, through the library and the CLI;
# the runner's diagnostics outlive the temporary workdir. Wrapper level, so
# a host without minitrace-verify skips these only.

def _verify_run_probe_dsl(derived_id: str) -> dict:
    adapter = {"name": "probe", "harness_version": "0", "dsl_version": "0.1.0", "source_format": "test",
               "converter_version": "test", "produces_schema": "0.3.0"}
    return {"adapter": adapter, "input": {"type": "json"}, "state": {"n": 0},
            "session": {"static": {}, "derived": {"id": derived_id}},
            "emit": [{"name": "t", "when": {"has": "content"},
                      "do": [{"emit_turn": {"index": 0, "content": "$record.content"}}]}]}


def _verify_run_case(tmp: pathlib.Path, dsl: dict | None, records: str):
    """(adapter path, input path) for a probe DSL, or for pi with its
    fixture plus `records` appended when dsl is None."""
    import yaml  # noqa: PLC0415
    adapters = pathlib.Path(__file__).resolve().parent.parent
    if dsl is None:
        fixture = (adapters / "pi" / "format-reference" / "session-v0.58.1.jsonl").read_text(encoding="utf-8")
        (tmp / "in.jsonl").write_text(fixture + records, encoding="utf-8")
        return adapters / "pi" / "0.58.1.dsl.yaml", tmp / "in.jsonl"
    (tmp / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
    (tmp / "in.json").write_text(records)
    return tmp / "a.dsl.yaml", tmp / "in.json"


_verify_built = False


def _verify_run_built():
    """verify_run, with minitrace-verify built from this tree's source before
    its first use, as test_verify.py does: a binary left in target/ from an
    earlier source would otherwise answer for the current one, and a missing
    toolchain or a failed build fails the test; it does not skip."""
    global _verify_built
    import subprocess  # noqa: PLC0415
    import verify_run  # noqa: PLC0415
    if not _verify_built:
        manifest = _ADAPTERS / "minitrace-verify" / "Cargo.toml"
        cmd = ["cargo", "build", "--release", "--offline", "--locked", "--quiet", "--manifest-path", str(manifest)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)
        except FileNotFoundError as e:
            raise AssertionError("cargo not found; these tests build minitrace-verify from source") from e
        except subprocess.TimeoutExpired as e:
            raise AssertionError(f"`{' '.join(cmd)}` did not finish in 600 s") from e
        if proc.returncode != 0 or not verify_run.VERIFY.is_file():
            raise AssertionError(f"`{' '.join(cmd)}` failed (exit {proc.returncode}):\n{proc.stderr}")
        _verify_built = True
    return verify_run


def _quiet(fn, *args, **kwargs):
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return fn(*args, **kwargs), buf.getvalue()


def test_verify_run_complete_conversion_is_complete():
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        a, i = _verify_run_case(pathlib.Path(td), None, "")
        result, out = _quiet(verify_run.run, a, i, operator_type="scripted")
    assert result.outcome == "complete" and bool(result) is True, out
    assert result.diagnostics is None or result.diagnostics.get("set_level_loss") is not True, out


def _minimcp_capture(tool_ts: str) -> str:
    events = [
        {"session_id": "s1", "transport": "stdio", "ts": "2026-03-01T10:00:00.000000Z", "direction": "in",
         "request_id": 1, "event_kind": "lifecycle:initialize:request", "method": "initialize",
         "payload": {"params": {"capabilities": {}, "clientInfo": {"name": "c", "version": "1"}}}},
        {"session_id": "s1", "transport": "stdio", "ts": "2026-03-01T10:00:01.000000Z", "direction": "out",
         "request_id": 1, "event_kind": "lifecycle:initialize:response", "payload": {"result": {"capabilities": {}}}},
        {"session_id": "s1", "transport": "stdio", "ts": tool_ts, "direction": "in", "request_id": 2,
         "event_kind": "tool:call:request", "method": "tools/call", "payload": {"params": {"name": "echo", "arguments": {}}}},
        {"session_id": "s1", "transport": "stdio", "ts": "2026-03-01T10:00:03.000000Z", "direction": "out",
         "request_id": 2, "event_kind": "tool:call:response", "payload": {"result": {"isError": False}}},
    ]
    return "".join(json.dumps(e) + "\n" for e in events)


def _agentdojo_runs(root: pathlib.Path, unreadable: bool = False) -> pathlib.Path:
    run = {"suite_name": "workspace", "pipeline_name": "pipe", "user_task_id": "user_task_0",
           "injection_task_id": None, "attack_type": None, "injections": {},
           "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "do it"},
                        {"role": "assistant", "content": "done", "tool_calls": None}],
           "utility": True, "security": True, "duration": 1.0, "error": None}
    d = root / "pipe" / "workspace" / "user_task_0" / "none"
    d.mkdir(parents=True)
    (d / "none.json").write_text(json.dumps(run))
    if unreadable:
        (d / "bad.json").write_text("{not json")
    return root


def test_agentdojo_evaluation_timestamp_is_read_or_warned():
    # A zoneless value, AgentDojo's own form, is read as UTC as before when
    # it is a real date and time; Z or an offset is read by the shared parser
    # (it was read as no timestamp); anything else is no timestamp with a
    # warning line for the run file (it was silent).
    import importlib.util  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("_agentdojo_timestamps", _ADAPTERS / "agentdojo" / "preprocess_agentdojo.py")
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
    table = [("2025-07-25 11:09:09", "2025-07-25T11:09:09.000000Z", 0),
             ("2025-07-25T11:09:09.123456", "2025-07-25T11:09:09.123456Z", 0),
             ("2026-03-01T10:00:00Z", "2026-03-01T10:00:00.000000Z", 0),
             ("2026-03-01T10:00:00+01:00", "2026-03-01T09:00:00.000000Z", 0),
             ("2026-02-30 10:00:00", None, 1), ("junk", None, 1), (42, None, 1),
             (None, None, 0), ("", None, 0)]
    for value, want, counted in table:
        P._ts_refused = 0
        assert (P.normalize_iso_microseconds(value), P._ts_refused) == (want, counted), value
    # build_container counts per run file whoever calls it: a count left from
    # an earlier call does not carry into the next.
    P._ts_refused = 5
    msgs = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y", "tool_calls": None}]
    P.build_container({"messages": msgs, "evaluation_timestamp": "2025-07-25 11:09:09"},
                      pathlib.Path("p/workspace/user_task_0/none/none.json"))
    assert P._ts_refused == 0, P._ts_refused
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        root = _agentdojo_runs(td / "runs")
        good = next(root.rglob("none.json"))
        bad = good.with_name("bad_ts.json")
        run = json.loads(good.read_text())
        bad.write_text(json.dumps({**run, "evaluation_timestamp": "2026-02-30 10:00:00"}))
        good.write_text(json.dumps({**run, "evaluation_timestamp": "2025-07-25 11:09:09"}))
        pre = _ADAPTERS / "agentdojo" / "preprocess_agentdojo.py"
        r = subprocess.run([sys.executable, str(pre), str(root), str(td / "out.json")],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        lines = [ln for ln in r.stderr.splitlines() if "evaluation_timestamp not convertible" in ln]
        assert len(lines) == 1 and "bad_ts.json" in lines[0] and lines[0].startswith("warning: agentdojo: "), r.stderr
        out = json.loads((td / "out.json").read_text())
        assert sorted(c["event_ts_present"] for c in out) == [False, True], out


def _minimcp_roots_capture(caps: dict, answered: bool) -> str:
    def ev(kind, d, rid, **kw):
        return {"session_id": "s1", "transport": "stdio", "ts": "2026-03-01T10:00:00.000000Z",
                "direction": d, "request_id": rid, "event_kind": kind, **kw}
    events = [ev("lifecycle:initialize:request", "in", 1, method="initialize",
                 payload={"params": {"capabilities": caps, "clientInfo": {"name": "c", "version": "1"}}}),
              ev("lifecycle:initialize:response", "out", 1, payload={"result": {"capabilities": {}}}),
              ev("roots:list:request", "out", 9, method="roots/list", payload={})]
    if answered:
        events.append(ev("roots:list:response", "in", 9, payload={"result": {"roots": []}}))
    return "".join(json.dumps(e) + "\n" for e in events)


def test_minimcp_advertised_vs_serviced_annotation_has_its_title_and_detail():
    # The title and detail interpolated list members, which the DSL refuses:
    # both were null and every capture with the summary converted incomplete.
    # The pre-processor now supplies each list as text ("none" when empty).
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    m = _ADAPTERS / "minimcp"
    cases = {
        "conformant": ({"roots": {}}, True,
                       "advertised-vs-serviced (conformant): unserviced=none; serviced-unadvertised=none",
                       "Host advertised roots; server fired roots; host serviced roots. Advertised-and-fired but "
                       "unserviced: none. Serviced but never advertised: none."),
        "divergence": ({"roots": {}, "sampling": {}}, False,
                       "advertised-vs-serviced (divergence): unserviced=roots; serviced-unadvertised=none",
                       "Host advertised roots, sampling; server fired roots; host serviced none. "
                       "Advertised-and-fired but unserviced: roots. Serviced but never advertised: none."),
    }
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        for name, (caps, answered, title, detail) in cases.items():
            cap = td / f"{name}.ndjson"
            cap.write_text(_minimcp_roots_capture(caps, answered))
            pre, out = td / f"{name}.pre.jsonl", td / f"{name}.traces.json"
            for argv in ([str(m / "preprocess_minimcp.py"), str(cap), str(pre)],
                         [str(_ADAPTERS / "dsl_runner.py"), str(m / "minimcp.dsl.yaml"), str(pre),
                          "--operator-type", "scripted", "--output", str(out)]):
                r = subprocess.run([sys.executable, *argv], capture_output=True, text=True, timeout=120)
                assert r.returncode == 0, (name, r.stderr[-500:])
            traces = json.loads(out.read_text())
            [t] = traces if isinstance(traces, list) else [traces]
            [ann] = [a for a in t["annotations"]
                     if a["content"].get("ext_namespace") == "ext:mcp:advertised_vs_serviced"]
            assert ann["content"]["title"] == title, (name, ann["content"])
            assert ann["content"]["detail"].startswith(detail), (name, ann["content"])
            assert ann["content"]["unserviced_capabilities"] == (["roots"] if name == "divergence" else []), ann
            result, log = _quiet(verify_run.run, m / "minimcp.dsl.yaml", cap, m / "preprocess_minimcp.py",
                                 operator_type="scripted")
            assert result.outcome == "complete", (name, log)


def test_verify_run_drives_the_minimcp_and_agentdojo_preprocessors():
    # verify_run passes every pre-processor <input> <output>; minimcp took only
    # --output and agentdojo only --runs-root/--output, so both runs failed
    # with exit 2 before any conversion. A warning of either is now a loss in
    # the outcome; AgentDojo's input is its runs directory.
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    ad = _ADAPTERS
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        for name, ts, warned in (("ok", "2026-03-01T10:00:02.000000Z", False), ("bad", "2026-02-30T10:00:02Z", True)):
            cap = td / f"{name}.ndjson"
            cap.write_text(_minimcp_capture(ts))
            result, out = _quiet(verify_run.run, ad / "minimcp" / "minimcp.dsl.yaml", cap,
                                 ad / "minimcp" / "preprocess_minimcp.py", operator_type="scripted")
            assert result.outcome in ("complete", "incomplete") and "preprocessor exited" not in out, out
            warns = (result.diagnostics or {}).get("preprocessor_warnings") or []
            assert any("timestamp(s) not convertible" in w for w in warns) is warned, (name, warns)
            assert (result.outcome == "incomplete") if warned else True, out
        for name, unreadable, want in (("clean", False, "complete"), ("unreadable", True, "incomplete")):
            root = _agentdojo_runs(td / f"runs-{name}", unreadable)
            result, out = _quiet(verify_run.run, ad / "agentdojo" / "agentdojo.dsl.yaml", root,
                                 ad / "agentdojo" / "preprocess_agentdojo.py", operator_type="scripted")
            assert result.outcome == want, (name, out)
            if unreadable:
                assert any(w.startswith("warning: skip (unreadable): ")
                           for w in result.diagnostics["preprocessor_warnings"]), result.diagnostics


def test_minimcp_and_agentdojo_take_the_positional_form_and_keep_their_flags():
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    mcp, adj = _ADAPTERS / "minimcp" / "preprocess_minimcp.py", _ADAPTERS / "agentdojo" / "preprocess_agentdojo.py"
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        cap = td / "cap.ndjson"
        cap.write_text(_minimcp_capture("2026-03-01T10:00:02.000000Z"))
        root = _agentdojo_runs(td / "runs")
        run_file = next(root.rglob("none.json"))

        def go(*argv):
            return subprocess.run([sys.executable, *map(str, argv)], capture_output=True, text=True, timeout=120)

        assert go(mcp, cap, "--output", td / "m1").returncode == 0 and go(mcp, cap, td / "m2").returncode == 0
        assert (td / "m1").read_bytes() == (td / "m2").read_bytes() and (td / "m1").stat().st_size > 0
        r = go(mcp, cap, td / "m3", "--output", td / "m4")
        assert r.returncode == 2 and "give the output once" in r.stderr and not (td / "m3").exists(), r.stderr
        assert go(adj, "--runs-root", root, "--output", td / "a1").returncode == 0
        assert go(adj, root, td / "a2").returncode == 0
        assert (td / "a1").read_bytes() == (td / "a2").read_bytes()
        assert go(adj, "--runs-root", root, "--run", run_file.relative_to(root), "--output", td / "a3").returncode == 0
        r = go(adj, run_file, td / "a4")
        assert r.returncode == 2 and "--run" in r.stderr and not (td / "a4").exists(), r.stderr
        r = go(adj, root, td / "a5", "--runs-root", root)
        assert r.returncode == 2 and "give the runs directory once" in r.stderr, r.stderr
        r = go(adj, root)
        assert r.returncode == 2 and "an output are required" in r.stderr, r.stderr
        r = go(adj, "--runs-root", root, td / "a6")
        assert r.returncode == 2 and "give the output as --output" in r.stderr, r.stderr
        # An output that is the input is refused before anything is written.
        before = cap.read_bytes()
        for argv in ((mcp, cap, cap), (mcp, cap, "--output", cap)):
            r = go(*argv)
            assert r.returncode == 2 and "is the input" in r.stderr and cap.read_bytes() == before, r.stderr
        before = run_file.read_bytes()
        for argv in ((adj, "--runs-root", root, "--run", run_file.relative_to(root), "--output", run_file),
                     (adj, root, run_file),                  # the positional form, every run file read
                     (adj, "--runs-root", root, "--output", run_file)):
            r = go(*argv)
            assert r.returncode == 2 and "is one of the run files read" in r.stderr \
                and run_file.read_bytes() == before, (argv, r.stderr)


def test_verify_run_a_skipped_record_is_incomplete_and_its_loss_is_kept():
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        a, i = _verify_run_case(pathlib.Path(td), None, "{not json\n")
        result, out = _quiet(verify_run.run, a, i, operator_type="scripted")
    # The workdir was temporary and is gone; the loss is still in hand.
    assert result.outcome == "incomplete" and bool(result) is False, out
    assert result.diagnostics["set_level_loss"] is True, result.diagnostics
    assert [d["kind"] for d in result.diagnostics["diagnostics"] if d.get("loss") == "set"] \
        == ["input-parse-error"], result.diagnostics
    assert "outcome: incomplete" in out, out


def test_verify_run_a_dropped_trace_is_incomplete():
    import json as _json  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        a, i = _verify_run_case(pathlib.Path(td), _verify_run_probe_dsl("$container.sid"),
                                _json.dumps([{"content": "hi"}]))
        result, out = _quiet(verify_run.run, a, i, operator_type="scripted")
    assert result.outcome == "incomplete" and bool(result) is False, out
    assert [d["kind"] for d in result.diagnostics["diagnostics"] if d.get("loss") == "set"] \
        == ["trace-dropped"], result.diagnostics


def test_verify_run_every_trace_dropped_in_container_mode_is_incomplete():
    # Container mode writes [] when every trace is dropped: incomplete like a missing output, not a failed verify of
    # an empty directory.
    import json as _json  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    dsl = _verify_run_probe_dsl("$container.sid")
    dsl["input"]["container"] = {"record_path": "items"}
    dsl["emit"][0]["when"] = {"has": "t"}
    dsl["emit"][0]["do"][0]["emit_turn"]["content"] = "$record.t"
    with tempfile.TemporaryDirectory() as td:
        a, i = _verify_run_case(pathlib.Path(td), dsl, _json.dumps([{"items": [{"t": "hi"}]}]))
        result, out = _quiet(verify_run.run, a, i, operator_type="scripted")
    assert result.outcome == "incomplete" and "no trace was emitted" in result.reasons, out
    assert [d["kind"] for d in result.diagnostics["diagnostics"] if d.get("loss") == "set"] \
        == ["trace-dropped"], result.diagnostics


def test_verify_run_a_reused_workdir_verifies_only_this_runs_traces():
    # An earlier run's extra trace in split/ is not verified with this one.
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        (td / "work" / "split").mkdir(parents=True)
        (td / "work" / "split" / "trace-7.minitrace.json").write_text('{"stale": true}')
        result, out = _quiet(verify_run.run, a, i, operator_type="scripted", workdir=td / "work")
    assert result.outcome == "complete", out


def test_verify_run_unreadable_input_is_failed():
    # The runner's exit 4 (input unreadable as a whole) is a failure.
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        a, i = _verify_run_case(pathlib.Path(td), _verify_run_probe_dsl("s1"), "{not json")
        result, out = _quiet(verify_run.run, a, i, operator_type="scripted")
    assert result.outcome == "failed" and "dsl_runner exited 4" in result.reasons, out


def test_verify_run_an_empty_conversion_is_incomplete():
    # Nothing converted, so nothing verified (once a crash).
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        a, i = _verify_run_case(pathlib.Path(td), _verify_run_probe_dsl("s1"), "[]")
        result, out = _quiet(verify_run.run, a, i, operator_type="scripted")
    assert result.outcome == "incomplete" and bool(result) is False, out
    assert "nothing to verify" in out, out


def test_verify_run_a_failing_preprocessor_is_failed():
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, _verify_run_probe_dsl("s1"), "[]")
        (td / "pre.py").write_text("import sys\nsys.exit(1)\n")
        result, out = _quiet(verify_run.run, a, i, td / "pre.py", operator_type="scripted")
    assert result.outcome == "failed" and bool(result) is False, out


def test_verify_run_a_verify_failure_is_failed_even_with_a_loss():
    # minitrace-verify exiting non-zero fails the run, and outranks a loss:
    # a lossy conversion whose traces do not verify is failed, not incomplete.
    import stat  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    real = verify_run.VERIFY
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        stub = td / "verify-stub"
        stub.write_text("#!/bin/sh\necho stub mismatch\nexit 1\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        verify_run.VERIFY = stub
        try:
            for name, records in (("clean", ""), ("lossy", "{not json\n")):
                (td / name).mkdir()
                a, i = _verify_run_case(td / name, None, records)
                result, out = _quiet(verify_run.run, a, i, operator_type="scripted")
                assert result.outcome == "failed" and bool(result) is False, (name, out)
                # The lossy arm is lossy: the failure outranks a real loss.
                assert (result.diagnostics or {}).get("set_level_loss") is (name == "lossy"), (name, out)
                assert "minitrace-verify exited 1" in result.reasons, (name, result.reasons)
        finally:
            verify_run.VERIFY = real


def test_verify_run_requires_an_operator():
    # Ruled 2026-10-20: no default operator; a human session must not
    # become scripted by omission. Library and CLI both.
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import pytest  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        a, i = _verify_run_case(pathlib.Path(td), None, "")
        with pytest.raises(TypeError):
            verify_run.run(a, i)
        # A consent assertion is a bool, never coerced ("false" is truthy).
        with pytest.raises(TypeError):
            verify_run.run(a, i, operator_type="human", operator_aware="false", capture_mode="scrubbed")
        script = pathlib.Path(verify_run.__file__)

        def cli(*args):
            r = subprocess.run([sys.executable, str(script), str(a), str(i), *args],
                               capture_output=True, text=True, timeout=300)
            return r.returncode, r.stdout + r.stderr

        for args in ([], ["--operator-type", "nobody"]):
            assert cli("-", *args)[0] == 2, args
        # The old positional, with --operator-type given: an extra positional.
        assert cli("-", "scripted", "--operator-type", "scripted")[0] == 2
        # Without "-", it lands in the preprocess slot: failed, and named.
        code, out = cli("scripted", "--operator-type", "scripted")
        assert code == 1 and "preprocessor not found: scripted" in out, out


def test_verify_run_passes_the_full_operator_object():
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    purpose = {"legal_basis": "gdpr_art6_consent", "reference": "probe-consent-1",
               "authorised_at": "2026-10-20T00:00:00.000000Z"}
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        result, out = _quiet(verify_run.run, a, i, operator_type="human", operator_aware=True,
                             capture_mode="full", capture_purpose=purpose, workdir=td / "work")
        traces = json.loads((td / "work" / "traces.json").read_text())
    traces = traces if isinstance(traces, list) else [traces]   # pi is single-trace
    assert result.outcome == "complete", out
    assert [t["operator"] for t in traces] == [{"type": "human", "operator_aware": True,
                                                "capture_mode": "full", "capture_purpose": purpose}], traces


def test_verify_run_a_human_operator_without_awareness_is_refused():
    # The runner requires operator_aware for a human operator (§5.4);
    # verify_run passes the assertion through and does not invent it.
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        a, i = _verify_run_case(pathlib.Path(td), None, "")
        result, out = _quiet(verify_run.run, a, i, operator_type="human", capture_mode="full")
    assert result.outcome == "failed" and any(r.startswith("dsl_runner exited") for r in result.reasons), out
    assert "--operator-aware is REQUIRED" in out, out


def test_verify_run_cli_exit_status_per_outcome():
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    script = pathlib.Path(verify_run.__file__)
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        complete = _verify_run_case(td, None, "")
        (td / "lossy").mkdir()
        lossy = _verify_run_case(td / "lossy", None, "{not json\n")

        def cli(*args):
            r = subprocess.run([sys.executable, str(script), *map(str, args)],
                               capture_output=True, text=True, timeout=300)
            return r.returncode, r.stdout + r.stderr

        assert cli(*complete, "-", "--operator-type", "scripted")[0] == 0
        code, out = cli(*lossy, "-", "--operator-type", "scripted")
        assert code == 3 and "outcome: incomplete" in out, out
        kept = td / "kept.json"
        code, out = cli(*lossy, "-", "--operator-type", "scripted", "--allow-incomplete", "--diagnostics-out", kept)
        assert code == 0 and "INCOMPLETE" in out, out
        assert json.loads(kept.read_text())["set_level_loss"] is True
        (td / "pre.py").write_text("import sys\nsys.exit(1)\n")
        assert cli(*lossy, td / "pre.py", "--operator-type", "scripted", "--allow-incomplete")[0] == 1
        # A run with no diagnostics overwrites an earlier file with null.
        assert cli(*lossy, td / "pre.py", "--operator-type", "scripted", "--diagnostics-out", kept)[0] == 1
        assert json.loads(kept.read_text()) is None


def _verify_run_cli(*args):
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    script = pathlib.Path(__file__).resolve().parent.parent / "verify_run.py"
    env = {**os.environ, "COLUMNS": "80", "NO_COLOR": "1", "PYTHON_COLORS": "0", "FORCE_COLOR": ""}
    r = subprocess.run([sys.executable, str(script), *map(str, args)],
                       capture_output=True, text=True, timeout=300, env=env)
    return r.returncode, r.stdout + r.stderr


def test_verify_run_diagnostics_out_refuses_the_sources():
    # Ruled 2026-10-21: the input, the adapter and the preprocessor, by
    # path, symlink or hardlink, and any path inside an input directory, are
    # a usage error before anything runs; nothing is written.
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        adapter, inp = _verify_run_case(td, None, "")
        (td / "pre.py").write_text("import sys\n")
        (td / "corpus").mkdir()
        (td / "corpus" / "a.jsonl").write_bytes(inp.read_bytes())
        os.symlink(inp, td / "link")
        os.link(inp, td / "hard")
        os.symlink(td / "corpus", td / "corpus-link")
        cases = [(inp, "-", inp, "is the input"), (inp, "-", td / "link", "is the input"),
                 (inp, "-", td / "hard", "is the input"), (inp, "-", adapter, "is the adapter"),
                 (inp, td / "pre.py", td / "pre.py", "is the preprocessor"),
                 (td / "corpus", "-", td / "corpus" / "d.json", "inside the input directory"),
                 (td / "corpus", "-", td / "corpus-link" / "d.json", "inside the input directory"),
                 (td / "corpus-link", "-", td / "corpus" / "d.json", "inside the input directory")]
        if (td / "CORPUS").exists():   # a case-insensitive file system: the same directory
            cases.append((td / "corpus", "-", td / "CORPUS" / "d.json", "inside the input directory"))
        sources = [inp, adapter, td / "pre.py", td / "corpus" / "a.jsonl"]
        before = {p: p.read_bytes() for p in sources}
        for i, pre, dest, why in cases:
            code, out = _verify_run_cli(adapter, i, pre, "--operator-type", "scripted", "--diagnostics-out", dest)
            assert code == 2 and why in out and "error: --diagnostics-out" in out, (dest, out)
            assert "outcome:" not in out, out   # refused before anything ran
            assert {p: p.read_bytes() for p in sources} == before, dest
        assert sorted(p.name for p in (td / "corpus").iterdir()) == ["a.jsonl"]


def test_verify_run_diagnostics_out_failed_write_fails_the_run():
    # Ruled 2026-10-21: a write that fails makes the run failed (exit 1),
    # whatever the conversion's outcome, --allow-incomplete included.
    import tempfile  # noqa: PLC0415
    _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        adapter, inp = _verify_run_case(td, None, "")
        ok = td / "d.json"
        code, out = _verify_run_cli(adapter, inp, "-", "--operator-type", "scripted", "--diagnostics-out", ok)
        assert code == 0 and json.loads(ok.read_text())["set_level_loss"] is False, out
        # The CLI writes through write_atomically: a symlink at the
        # destination is replaced, and the file it named is left alone.
        import os  # noqa: PLC0415
        (td / "other").write_text("keep\n")
        os.symlink(td / "other", td / "l.json")
        code, out = _verify_run_cli(adapter, inp, "-", "--operator-type", "scripted", "--diagnostics-out", td / "l.json")
        assert code == 0 and (td / "other").read_text() == "keep\n" and not (td / "l.json").is_symlink(), out
        for dest in (td / "missing" / "d.json", td):   # no such directory; a directory
            code, out = _verify_run_cli(adapter, inp, "-", "--operator-type", "scripted",
                                        "--allow-incomplete", "--diagnostics-out", dest)
            assert code == 1 and "outcome: complete" in out and "could not write --diagnostics-out" in out, out
            assert "outcome: failed (the diagnostics were not written)" in out, out
        assert sorted(p.name for p in td.iterdir() if p.name.startswith(".")) == [], list(td.iterdir())


def test_verify_run_diagnostics_out_is_written_atomically():
    # Ruled 2026-10-21: a temporary file in the destination's directory,
    # then os.replace. A failure leaves the old content and no temporary
    # file; a symlink at the destination is replaced, its target untouched.
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415
    import verify_run  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        verify_run.write_atomically(td / "fresh.json", "x\n")   # a new file: write_text's mode
        assert ((td / "fresh.json").stat().st_mode & 0o777) == 0o666 & ~_current_umask()
        (td / "fresh.json").unlink()
        dest = td / "d.json"
        dest.write_text("old\n")
        dest.chmod(0o1600)
        verify_run.write_atomically(dest, "new\n")   # permission bits kept, sticky dropped
        assert dest.read_text() == "new\n" and sorted(p.name for p in td.iterdir()) == ["d.json"]
        assert (dest.stat().st_mode & 0o7777) == 0o600, oct(dest.stat().st_mode)
        # Root may write a 0444 file (os.access allows it), so as root the
        # read-only refusal is not checked.
        if os.geteuid() != 0:
            dest.chmod(0o444)                         # read-only: fails as write_text did
            refused = False
            try:
                verify_run.write_atomically(dest, "x\n")
            except PermissionError:
                refused = True
            assert refused and dest.read_text() == "new\n" and sorted(p.name for p in td.iterdir()) == ["d.json"]
            dest.chmod(0o600)

        raised = False
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            try:
                verify_run.write_atomically(dest, "partial\n")
            except OSError:
                raised = True
        assert raised and dest.read_text() == "new\n" and sorted(p.name for p in td.iterdir()) == ["d.json"]
        (td / "other").write_text("keep\n")
        os.symlink(td / "other", td / "l.json")
        verify_run.write_atomically(td / "l.json", "x\n")
        assert (td / "other").read_text() == "keep\n" and not (td / "l.json").is_symlink()


def _pi_with_shared_tool_call_id(td: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """pi's s2 rung input with its second tool-call id replaced by the
    first: two calls share one id (DSL rc3 §8.2.3), a value-level loss."""
    adapters = pathlib.Path(__file__).resolve().parent.parent
    text = (adapters / "pi" / "regression" / "0.80.3" / "inputs" / "s2-todo-grep.jsonl").read_text(encoding="utf-8")
    ids = []
    for line in text.splitlines():
        rec = json.loads(line)
        for b in (rec.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "toolCall":
                ids.append(b["id"])
    assert len(ids) >= 2 and ids[0] != ids[1], ids
    (td / "dup.jsonl").write_text(text.replace(ids[1], ids[0]), encoding="utf-8")
    return adapters / "pi" / "0.58.1.dsl.yaml", td / "dup.jsonl"


def test_verify_run_value_level_loss_from_the_runner_is_incomplete():
    # Ruled 2026-10-21: a dsl_runner diagnostic with loss "value" makes the
    # run incomplete (exit 3; 0 with --allow-incomplete), naming level,
    # source and count; before, it was complete with exit 0.
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _pi_with_shared_tool_call_id(td)
        result, out = _quiet(verify_run.run, a, i, operator_type="scripted")
        value = [d for d in result.diagnostics["diagnostics"] if d.get("loss") == "value"]
        assert value and result.diagnostics["set_level_loss"] is False, result.diagnostics
        assert result.outcome == "incomplete", out
        assert result.reasons == [f"value-level loss: {len(value)} diagnostic(s) from dsl_runner"], result.reasons
        assert result.diagnostics["preprocessor_warnings"] == [], result.diagnostics
        kept = td / "d.json"
        code, out = _verify_run_cli(a, i, "-", "--operator-type", "scripted", "--diagnostics-out", kept)
        assert code == 3 and "outcome: incomplete (value-level loss:" in out, out
        assert json.loads(kept.read_text())["preprocessor_warnings"] == []
        code, out = _verify_run_cli(a, i, "-", "--operator-type", "scripted", "--allow-incomplete")
        assert code == 0 and "INCOMPLETE" in out, out


def test_verify_run_a_preprocessor_warning_is_value_level_loss():
    # Ruled 2026-10-21: any preprocessor output line starting, after
    # whitespace, with "warning:" is a loss; the lines go into the
    # diagnostics under preprocessor_warnings. cursor's inputTokens 1e16
    # is not a usable count and was complete before.
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    adapters = pathlib.Path(__file__).resolve().parent.parent
    text = (adapters / "cursor/regression/2026.06.04-5fd875e/inputs/s1-config.stream.jsonl").read_text()
    assert text.count('"inputTokens":14') == 1
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "s.jsonl").write_text(text.replace('"inputTokens":14', '"inputTokens":1e16'))
        result, out = _quiet(verify_run.run, adapters / "cursor/cursor.dsl.yaml", td / "s.jsonl",
                             adapters / "cursor/preprocess_cursor.py", operator_type="scripted")
        warns = result.diagnostics["preprocessor_warnings"]
        assert any("inputTokens" in w and "not a usable count" in w for w in warns), warns
        assert all(w.startswith("warning:") for w in warns), warns
        assert result.outcome == "incomplete", out
        assert f"loss: {len(warns)} preprocessor warning(s)" in result.reasons, result.reasons
        # The same input unchanged converts complete, with no warning.
        (td / "ok.jsonl").write_text(text)
        result, out = _quiet(verify_run.run, adapters / "cursor/cursor.dsl.yaml", td / "ok.jsonl",
                             adapters / "cursor/preprocess_cursor.py", operator_type="scripted")
        assert result.outcome == "complete" and result.diagnostics["preprocessor_warnings"] == [], out


def test_verify_run_preprocessor_warning_prefix_and_failure():
    # Only a line that starts with "warning:" counts; a failed preprocessor
    # stays failed, and its warnings are kept although the runner wrote no
    # diagnostics (the object then holds nulls for the runner's keys).
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        copy = "import shutil, sys\nshutil.copyfile(sys.argv[1], sys.argv[2])\n"
        (td / "quiet.py").write_text(copy + "print('fidelity: warnings=0, note: warning: none')\n")
        result, out = _quiet(verify_run.run, a, i, td / "quiet.py", operator_type="scripted")
        assert result.outcome == "complete" and result.diagnostics["preprocessor_warnings"] == [], out
        (td / "warns.py").write_text(copy + "print('warning: one field unusable')\n"
                                     "print('  warning: dropped 2 records', file=sys.stderr)\n")
        result, out = _quiet(verify_run.run, a, i, td / "warns.py", operator_type="scripted")
        assert result.outcome == "incomplete", out
        assert result.diagnostics["preprocessor_warnings"] == [   # stdout, then stderr
            "warning: one field unusable", "warning: dropped 2 records"], result.diagnostics
        assert result.reasons == ["loss: 2 preprocessor warning(s)"], result.reasons
        (td / "fails.py").write_text("import sys\nprint('warning: half read', file=sys.stderr)\nsys.exit(1)\n")
        result, out = _quiet(verify_run.run, a, i, td / "fails.py", operator_type="scripted")
        assert result.outcome == "failed" and result.reasons == ["preprocessor exited 1"], result.reasons
        assert result.diagnostics == {"set_level_loss": None, "diagnostics": None,
                                      "preprocessor_warnings": ["warning: half read"],
                                      "preprocessor_fidelity": None}, result.diagnostics


def test_verify_run_set_and_value_loss_reasons_name_source_and_count():
    # Ruled 2026-10-21: the set-level reason names source and count too,
    # and comes first; a record that fails to parse is a set-level loss.
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _pi_with_shared_tool_call_id(td)
        i.write_text(i.read_text(encoding="utf-8") + "{not json\n{nor this\n", encoding="utf-8")
        result, out = _quiet(verify_run.run, a, i, operator_type="scripted")
        entries = result.diagnostics["diagnostics"]
        sets = sum(1 for d in entries if d.get("loss") == "set")
        values = sum(1 for d in entries if d.get("loss") == "value")
        assert sets and values and sets != values and result.outcome == "incomplete", (sets, values, out)
        assert result.reasons == [f"set-level loss: {sets} diagnostic(s) from dsl_runner",
                                  f"value-level loss: {values} diagnostic(s) from dsl_runner"], result.reasons


def test_verify_run_object_without_runner_diagnostics_keeps_the_set_level_loss():
    # Ruled 2026-10-21: when the runner's file is unreadable and the
    # preprocessor warned, set_level_loss is true if the runner exited 3.
    import verify_run  # noqa: PLC0415
    for runner_exit, expected in ((3, True), (0, None), (None, None)):
        r = verify_run.VerifyResult("incomplete", None, ["x"])
        verify_run._account_value_loss(r, ["warning: w"], runner_exit)
        assert r.diagnostics == {"set_level_loss": expected, "diagnostics": None,
                                 "preprocessor_warnings": ["warning: w"],
                                 "preprocessor_fidelity": None}, (runner_exit, r.diagnostics)
        assert r.reasons == ["x", "loss: 1 preprocessor warning(s)"], r.reasons
    # End to end: the runner exits 3 (a record that does not parse) and its
    # file reads as unreadable; the preprocessor warns.
    import tempfile  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415
    _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "{not json\n")
        (td / "warns.py").write_text("import shutil, sys\nshutil.copyfile(sys.argv[1], sys.argv[2])\n"
                                     "print('warning: one field unusable')\n")
        with mock.patch.object(verify_run, "_read_diagnostics", return_value=None):
            result, out = _quiet(verify_run.run, a, i, td / "warns.py", operator_type="scripted")
        assert result.diagnostics == {"set_level_loss": True, "diagnostics": None,
                                      "preprocessor_warnings": ["warning: one field unusable"],
                                      "preprocessor_fidelity": None}, out
        assert result.reasons == ["set-level loss from dsl_runner (its diagnostics file unreadable)",
                                  "loss: 1 preprocessor warning(s)"], result.reasons


def test_claude_ai_skips_are_a_preprocessor_warning_only_when_something_was_skipped():
    # Ruled 2026-10-21: claude-ai's skipped non-object conversations and
    # messages are a "warning:" line, printed only when there are any, so a
    # clean export converts complete and a skip makes the run incomplete.
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    adapters = pathlib.Path(__file__).resolve().parent.parent
    probe = json.loads((adapters / "tests/fixtures/claude-ai-2026-09-26-probe.json").read_text(encoding="utf-8"))
    conv = probe[0] if isinstance(probe, list) else probe
    assert isinstance(conv.get("chat_messages"), list) and conv["chat_messages"], conv.keys()
    dsl, pre = adapters / "claude-ai/2026-09-26.dsl.yaml", adapters / "claude-ai/preprocess_claude_ai.py"
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "ok.json").write_text(json.dumps(probe), encoding="utf-8")
        result, out = _quiet(verify_run.run, dsl, td / "ok.json", pre, operator_type="scripted")
        assert result.outcome == "complete" and result.diagnostics["preprocessor_warnings"] == [], out
        conv["chat_messages"] = conv["chat_messages"] + [42, "x"]
        (td / "skip.json").write_text(json.dumps(probe), encoding="utf-8")
        result, out = _quiet(verify_run.run, dsl, td / "skip.json", pre, operator_type="scripted")
        assert result.diagnostics["preprocessor_warnings"] == [
            "warning: skipped 0 non-object conversation(s), 2 non-object message(s)"], result.diagnostics
        assert result.outcome == "incomplete" and "loss: 1 preprocessor warning(s)" in result.reasons, out


def _fidelity_record(*skipped: int, discarded: int = 0, findings=(), chain=()) -> dict:
    """A fidelity record of the shape source_reader writes, one source per
    `skipped` count (one source with none skipped when none is given)."""
    sources = [{"role": "primary", "content_hash": "sha256:" + f"{n:064x}", "size_bytes": 1,
                "record_count_read": 10, "record_count_skipped": s, "record_count_discarded": discarded,
                "record_count_emitted": 10 - s - discarded} for n, s in enumerate(skipped or (0,))]
    return {"asserted_by": "test", "converter_version": "test", "sources": sources, "checks": [],
            "read_findings": list(findings), "chain_findings": list(chain)}


def _fidelity_preprocessor(td: pathlib.Path, name: str, body=None, make: str = "", exit_code: int = 0):
    """A preprocessor that copies its input and leaves a fidelity record
    beside its output: `body` (a JSON value, or bytes as they are) written
    there, or the Python statements `make`, which see the record's path as
    `side`."""
    lines = ["import os, pathlib, shutil, sys", "shutil.copyfile(sys.argv[1], sys.argv[2])",
             "side = pathlib.Path(sys.argv[2]).with_suffix('.fidelity.json')"]
    if body is not None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        (td / f"{name}.rec").write_bytes(raw)
        lines.append(f"side.write_bytes(pathlib.Path({str(td / (name + '.rec'))!r}).read_bytes())")
    lines += [make, f"sys.exit({exit_code})"]
    (td / f"{name}.py").write_text("\n".join(lines) + "\n")
    return td / f"{name}.py"


def test_verify_run_a_record_skipped_in_the_fidelity_record_is_incomplete():
    # Ruled 2026-10-21: claude-code reports a damaged line in its fidelity
    # record (pre.fidelity.json beside its output), not on a "warning:"
    # line; a skipped record or a read finding there makes the run
    # incomplete, and the record goes into the diagnostics. Discarded
    # records are adapter policy, not loss: the format reference
    # (discarded 3) stays complete.
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    adapters = pathlib.Path(__file__).resolve().parent.parent
    dsl, pre = adapters / "claude-code/claude-code.dsl.yaml", adapters / "claude-code/preprocess_claude_code.py"
    lines = (adapters / "claude-code/format-reference/session-v2.1.76.jsonl").read_text(
        encoding="utf-8").splitlines(True)
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "ok.jsonl").write_text("".join(lines), encoding="utf-8")
        result, out = _quiet(verify_run.run, dsl, td / "ok.jsonl", pre, operator_type="scripted")
        record = result.diagnostics["preprocessor_fidelity"]
        assert [s["record_count_discarded"] for s in record["sources"]] == [3], record
        assert result.outcome == "complete" and result.reasons == [], out
        (td / "bad.jsonl").write_text("".join(lines[:2] + ['{"type": "user", "broken\n'] + lines[2:]),
                                      encoding="utf-8")
        result, out = _quiet(verify_run.run, dsl, td / "bad.jsonl", pre, operator_type="scripted")
        record = result.diagnostics["preprocessor_fidelity"]
        assert [s["record_count_skipped"] for s in record["sources"]] == [1], record
        assert len(record["read_findings"]) == 1, record
        assert result.diagnostics["preprocessor_warnings"] == [], result.diagnostics
        assert result.outcome == "incomplete", out
        assert result.reasons == ["set-level loss: 1 record(s) skipped in the preprocessor's fidelity record",
                                  "loss: 1 read finding(s) in the preprocessor's fidelity record"], result.reasons
        kept = td / "d.json"
        code, out = _verify_run_cli(dsl, td / "bad.jsonl", pre, "--operator-type", "scripted",
                                    "--diagnostics-out", kept)
        assert code == 3 and "outcome: incomplete (set-level loss: 1 record(s) skipped" in out, out
        assert json.loads(kept.read_text())["preprocessor_fidelity"]["sources"] == record["sources"]


def test_verify_run_fidelity_record_loss_classes():
    # Ruled 2026-10-21: any read finding is a loss and a skipped record a
    # set-level loss, each counted over every source; discarded records and
    # chain findings are not loss (chain findings are carried in the record).
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    finding = {"kind": "decode-error", "record_index": 0, "source_index": 0}
    cases = [
        ("policy", _fidelity_record(0, 0, discarded=4, chain=[{"kind": "x"}]), "complete", []),
        ("finding", _fidelity_record(findings=[finding]), "incomplete",
         ["loss: 1 read finding(s) in the preprocessor's fidelity record"]),
        ("skips", _fidelity_record(0, 1, 2), "incomplete",
         ["set-level loss: 3 record(s) skipped in the preprocessor's fidelity record"]),
        ("maxcount", _fidelity_record(2 ** 63 - 1, 2 ** 63 - 1), "incomplete",
         [f"set-level loss: {2 ** 64 - 2} record(s) skipped in the preprocessor's fidelity record"]),
    ]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        for name, record, outcome, reasons in cases:
            p = _fidelity_preprocessor(td, name, record)
            result, out = _quiet(verify_run.run, a, i, p, operator_type="scripted")
            assert (result.outcome, result.reasons) == (outcome, reasons), (name, out)
            assert result.diagnostics["preprocessor_fidelity"] == record, (name, result.diagnostics)


def test_verify_run_an_unreadable_fidelity_record_is_incomplete():
    # Ruled 2026-10-21: a fidelity record that cannot be read, or lacks the
    # counts, leaves the loss unknown: incomplete, the record null, the
    # reason named; a FIFO there does not block the run.
    import tempfile  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415
    verify_run = _verify_run_built()
    good = _fidelity_record()
    lacks = "lacks the counts it must carry"
    cases = [
        ("notjson", b"{not json", "is not JSON"),
        ("undecodable", b"\xff\xfe", "is not JSON"),
        ("nan", json.dumps(good)[:-1].encode() + b', "x": [NaN]}', "is not JSON"),
        ("infinity", json.dumps(good).replace('"size_bytes": 1', '"size_bytes": -Infinity').encode(),
         "is not JSON"),
        ("overflow", json.dumps(good)[:-1].encode() + b', "x": {"y": -1e999}}', "is not JSON"),
        ("emptysources", {**good, "sources": []}, lacks),
        ("array", [good], lacks),
        ("nofindings", {k: v for k, v in good.items() if k != "read_findings"}, lacks),
        ("nosources", {k: v for k, v in good.items() if k != "sources"}, lacks),
        ("sourcenotobject", {**good, "sources": [1]}, lacks),
        ("boolcount", _fidelity_record(True), lacks),
        ("negative", _fidelity_record(-1), lacks),
        ("int64", _fidelity_record(0, 2 ** 63), lacks),
        ("huge", _fidelity_record(10 ** 4299, 10 ** 4299), lacks),
        ("floatcount", {**good, "sources": [{**good["sources"][0], "record_count_skipped": 1.0}]}, lacks),
        ("secondbad", {**good, "sources": good["sources"] + [{"record_count_skipped": "1"}]}, lacks),
    ]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        target = td / "elsewhere.json"
        target.write_text(json.dumps(good))
        made = [(name, _fidelity_preprocessor(td, name, body), problem) for name, body, problem in cases] + [
            ("directory", _fidelity_preprocessor(td, "directory", make="side.mkdir()"), "is not a regular file"),
            ("fifo", _fidelity_preprocessor(td, "fifo", make="os.mkfifo(side)"), "is not a regular file"),
            ("symlink", _fidelity_preprocessor(td, "symlink", make=f"side.symlink_to({str(target)!r})"),
             "could not be opened ("),
        ]
        for name, p, problem in made:
            result, out = _quiet(verify_run.run, a, i, p, operator_type="scripted", timeout=60)
            assert result.outcome == "incomplete", (name, out)
            assert len(result.reasons) == 1 and result.reasons[0].startswith(
                f"loss unknown: the preprocessor's fidelity record {problem}"), (name, result.reasons)
            assert result.diagnostics["preprocessor_fidelity"] is None, (name, result.diagnostics)
        p = _fidelity_preprocessor(td, "large", good)
        size = len(json.dumps(good).encode())
        with mock.patch.object(verify_run, "FIDELITY_MAX_BYTES", size - 1):
            result, out = _quiet(verify_run.run, a, i, p, operator_type="scripted")
        assert result.reasons == [f"loss unknown: the preprocessor's fidelity record is larger than "
                                  f"{size - 1} bytes"], out
        with mock.patch.object(verify_run, "FIDELITY_MAX_BYTES", size):
            result, out = _quiet(verify_run.run, a, i, p, operator_type="scripted")
        assert result.outcome == "complete", out


def test_verify_run_diagnostics_text_is_standard_json():
    # Ruled 2026-10-21: --diagnostics-out holds standard JSON only; a
    # non-finite value is a ValueError, which the CLI reports as a failed
    # write (exit 1), never NaN or Infinity in the file.
    import pytest  # noqa: PLC0415
    import verify_run  # noqa: PLC0415
    assert json.loads(verify_run.diagnostics_text({"a": [1.5, None]})) == {"a": [1.5, None]}
    for bad in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError):
            verify_run.diagnostics_text({"preprocessor_fidelity": {"x": [bad]}})


def test_verify_run_fidelity_record_reused_workdir_and_failed_preprocessor():
    # An earlier run's record in a reused workdir is removed before the
    # preprocessor runs, so it is never read as this run's; one that cannot
    # be removed fails the run. A failed preprocessor stays failed, and the
    # record it left is kept in the diagnostics.
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        copy = _fidelity_preprocessor(td, "none")
        work = td / "work"
        work.mkdir()
        (work / "pre.fidelity.json").write_text(json.dumps(_fidelity_record(5)))
        result, out = _quiet(verify_run.run, a, i, copy, operator_type="scripted", workdir=work)
        assert result.outcome == "complete", out
        assert result.diagnostics is None or result.diagnostics["preprocessor_fidelity"] is None, out
        assert not (work / "pre.fidelity.json").exists()
        (work / "pre.fidelity.json").mkdir()
        result, out = _quiet(verify_run.run, a, i, copy, operator_type="scripted", workdir=work)
        assert result.outcome == "failed", out
        assert result.reasons[0].startswith("an earlier fidelity record could not be removed"), result.reasons
        record = _fidelity_record(1)
        failing = _fidelity_preprocessor(td, "failing", record, exit_code=1)
        result, out = _quiet(verify_run.run, a, i, failing, operator_type="scripted")
        assert (result.outcome, result.reasons) == ("failed", ["preprocessor exited 1"]), out
        assert result.diagnostics == {"set_level_loss": None, "diagnostics": None, "preprocessor_warnings": [],
                                      "preprocessor_fidelity": record}, result.diagnostics


def test_agentdojo_skips_are_warning_lines():
    # Ruled 2026-10-21: an unreadable run file and a run with no messages
    # are each a "warning:" line; the summary line is not, so nothing is
    # counted twice.
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import verify_run  # noqa: PLC0415
    script = pathlib.Path(__file__).resolve().parent.parent / "agentdojo" / "preprocess_agentdojo.py"
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "runs").mkdir()
        (td / "runs" / "bad.json").write_text("{not json")
        (td / "runs" / "empty.json").write_text(json.dumps({"messages": []}))
        r = subprocess.run([sys.executable, str(script), "--runs-root", str(td / "runs"),
                            "--output", str(td / "out.json")], capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr
        warns = verify_run._preprocessor_warnings(r.stdout + "\n" + r.stderr)
        assert len(warns) == 2 and warns[0].startswith("warning: skip (unreadable): "), warns
        assert warns[1] == "warning: skip (no messages): empty.json", warns
        assert "skipped: {" in r.stderr, r.stderr


def test_verify_run_a_step_that_times_out_fails_the_run():
    # Ruled 2026-10-21: each step (preprocessor, dsl_runner, minitrace-verify)
    # gets the timeout (600 s by default); a step that times out is killed
    # and the run is failed, naming the step. Diagnostics already read are
    # kept.
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415
    verify_run = _verify_run_built()
    assert verify_run.STEP_TIMEOUT == 600
    real_popen = subprocess.Popen

    def popen_timing_out(marker, seen):
        class P(real_popen):
            """The real process; its first wait raises TimeoutExpired when
            its command names `marker`, as if the step had run too long."""
            fired = False

            def communicate(self, input=None, timeout=None):
                if not self.fired:
                    self.fired = True
                    seen.append(timeout)
                    if any(marker in str(c) for c in self.args[:2]):
                        raise subprocess.TimeoutExpired(self.args, timeout)
                return super().communicate(input, timeout)
        return P

    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        (td / "copy.py").write_text("import shutil, sys\nshutil.copyfile(sys.argv[1], sys.argv[2])\n")
        for step, marker in (("the preprocessor", "copy.py"), ("dsl_runner", "dsl_runner.py"),
                             ("minitrace-verify", "minitrace-verify")):
            seen = []
            with mock.patch.object(verify_run.subprocess, "Popen", popen_timing_out(marker, seen)):
                result, out = _quiet(verify_run.run, a, i, td / "copy.py", operator_type="scripted", timeout=7)
            assert result.outcome == "failed" and result.reasons == [f"{step} timed out after 7 s"], (step, out)
            assert seen and set(seen) == {7}, (step, seen)
            if step == "minitrace-verify":   # read before the verify step
                assert result.diagnostics["set_level_loss"] is False, result.diagnostics
        # Default: every step waited on with 600.
        seen = []
        with mock.patch.object(verify_run.subprocess, "Popen", popen_timing_out("no step has this", seen)):
            result, out = _quiet(verify_run.run, a, i, td / "copy.py", operator_type="scripted")
        assert result.outcome == "complete" and seen == [600, 600, 600], (seen, out)


def _group_has_a_live_member(pgid):
    """Whether a process group still has a member that is not a zombie. On
    Linux killpg(pgid, 0) also succeeds on a group of zombies (macOS raises
    PermissionError, which propagates), and a killed group's orphans stay
    zombies when PID 1 does not reap (a container running `sleep infinity`).
    killpg goes first; when it finds a group and /proc is there, /proc decides:
    every member seen a zombie is gone, and none seen is asked of killpg again
    (the last one may just have been reaped). Elsewhere killpg decides."""
    import os  # noqa: PLC0415
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    if not pathlib.Path("/proc/self/stat").exists():
        return True
    seen = False
    for stat in pathlib.Path("/proc").glob("[0-9]*/stat"):
        try:
            state, _ppid, pgrp = stat.read_bytes().rsplit(b")", 1)[1].split()[:3]
        except (OSError, IndexError, ValueError):   # it exited while being read
            continue
        if int(pgrp) == pgid:
            if state != b"Z":
                return True
            seen = True
    if seen:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_group_of_zombies_is_not_a_live_group():
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415
    if os.name != "posix":
        return

    def state(pid):
        return pathlib.Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]

    def until(check):
        deadline = time.monotonic() + 10
        while not check() and time.monotonic() < deadline:
            time.sleep(0.01)
        return check()
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        assert _group_has_a_live_member(p.pid), "a running group read as gone"
        if pathlib.Path("/proc/self/stat").exists():
            p.kill()   # not waited for: a zombie, this process its parent
            assert until(lambda: state(p.pid) == b"Z"), "the probe process is no zombie"
            assert not _group_has_a_live_member(p.pid), "a group of zombies read as live"
    finally:
        p.kill()
        p.wait()
    assert not _group_has_a_live_member(p.pid), "a reaped group read as live"
    if not pathlib.Path("/proc/self/stat").exists():
        return
    # A leader that exited and is not yet reaped, a child alive in its group,
    # and another process of its session in a group of its own.
    script = ("import os, subprocess, sys\n"
              "sleep = {'args': [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
              "         'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}\n"
              "child = subprocess.Popen(**sleep)\n"
              "other = subprocess.Popen(preexec_fn=os.setpgrp, **sleep)\n"
              "print(child.pid, other.pid, flush=True)\n"
              "os._exit(0)\n")
    p = subprocess.Popen([sys.executable, "-c", script], start_new_session=True, stdout=subprocess.PIPE, text=True)
    child = other = None
    try:
        child, other = (int(n) for n in p.stdout.readline().split())
        assert until(lambda: state(p.pid) == b"Z"), "the leader is no zombie"
        assert _group_has_a_live_member(p.pid), "a live child under a zombie leader read as gone"
        os.kill(child, signal.SIGKILL)
        # Its orphan is reaped by PID 1 or stays a zombie; the other group
        # shares the session and stays alive, and does not count.
        assert until(lambda: not _group_has_a_live_member(p.pid)), "another group of the session read as this one"
    finally:
        for pid in (child, other):
            try:
                if pid is not None:
                    os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        p.kill()
        p.stdout.close()
        p.wait()
    # What the interrupt test relies on: the leader reaped, its orphans gone
    # or zombies.
    assert until(lambda: not _group_has_a_live_member(p.pid)), "a reaped leader's killed group read as live"


def test_verify_run_an_interrupt_while_waiting_kills_the_step_group():
    # The step runs in its own session, so Ctrl-C at the terminal no longer
    # reaches it; any exception while waiting kills its group, then
    # propagates.
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import time  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415
    verify_run = _verify_run_built()
    if os.name != "posix":
        return
    real_popen = subprocess.Popen
    started = []

    class P(real_popen):
        def communicate(self, input=None, timeout=None):
            started.append(self.pid)
            time.sleep(0.5)   # let it start its child
            raise KeyboardInterrupt

    with tempfile.TemporaryDirectory() as td:
        beat = pathlib.Path(td) / "beat"
        script = ("import subprocess, time\n"
                  f"subprocess.Popen(['/bin/sh', '-c', 'while :; do echo x >> {beat}; sleep 0.2; done'])\n"
                  "time.sleep(30)\n")
        raised = False
        with mock.patch.object(verify_run.subprocess, "Popen", P):
            try:
                verify_run._step({"timeout": 60}, "the preprocessor", [sys.executable, "-c", script])
            except KeyboardInterrupt:
                raised = True
        assert raised and started
        size = beat.stat().st_size if beat.exists() else 0
        time.sleep(1)
        assert (beat.stat().st_size if beat.exists() else 0) == size, "the step's child kept running"
        assert not _group_has_a_live_member(started[0]), "the step's process group outlived the interrupt"


def test_verify_run_a_descendant_holding_the_pipes_still_gives_the_partial_output():
    # A descendant in a session of its own survives the group kill and
    # holds the pipes; what the step printed before is kept, and the run
    # does not wait for the descendant.
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import time  # noqa: PLC0415
    verify_run = _verify_run_built()
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as td:
        pidfile = pathlib.Path(td) / "pid"
        script = ("import subprocess, sys, time\n"
                  "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
                  " start_new_session=True)\n"
                  f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
                  "print('warning: early line', flush=True)\n"
                  "time.sleep(30)\n")
        t0 = time.monotonic()
        try:
            verify_run._step({"timeout": 1}, "the preprocessor", [sys.executable, "-c", script])
            raised = None
        except verify_run._StepTimedOut as e:
            raised = e
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
        assert raised is not None and time.monotonic() - t0 < 15
        assert verify_run._preprocessor_warnings(raised.output) == ["warning: early line"], raised.output
    # The step itself exits at once: its group holds only an unreaped
    # zombie, and killpg gives EPERM on macOS; still a timeout, not a crash.
    with tempfile.TemporaryDirectory() as td:
        pidfile = pathlib.Path(td) / "pid"
        script = ("import subprocess, sys\n"
                  "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
                  " start_new_session=True)\n"
                  f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
                  "print('warning: early line', flush=True)\n")
        try:
            verify_run._step({"timeout": 1}, "the preprocessor", [sys.executable, "-c", script])
            raised = None
        except verify_run._StepTimedOut as e:
            raised = e
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
        assert raised is not None, "a step whose descendant holds the pipes reads as timed out"
        assert verify_run._preprocessor_warnings(raised.output) == ["warning: early line"], raised.output


def test_verify_run_a_timeout_kills_what_the_step_started_and_keeps_its_warnings():
    # Ruled 2026-10-21: the step's process group is killed, so a process it
    # started does not outlive the run; the warning: lines the preprocessor
    # printed before it was killed are kept as preprocessor warnings.
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import time  # noqa: PLC0415
    verify_run = _verify_run_built()
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        marker = td / "outlived"
        (td / "spawns.py").write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen(['/bin/sh', '-c', 'sleep 2 && touch {marker}'])\n"
            "print('warning: dropped 1 record', flush=True)\n"
            "time.sleep(30)\n")
        t0 = time.monotonic()
        result, out = _quiet(verify_run.run, a, i, td / "spawns.py", operator_type="scripted", timeout=1)
        assert time.monotonic() - t0 < 10, out
        assert result.outcome == "failed" and result.reasons == ["the preprocessor timed out after 1 s"], out
        assert result.diagnostics == {"set_level_loss": None, "diagnostics": None,
                                      "preprocessor_warnings": ["warning: dropped 1 record"],
                                      "preprocessor_fidelity": None}, result.diagnostics
        time.sleep(3)
        assert not marker.exists(), "a process the timed-out step started outlived the run"


def test_verify_run_timeout_end_to_end_and_its_argument():
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        (td / "slow.py").write_text("import time\ntime.sleep(30)\n")
        result, out = _quiet(verify_run.run, a, i, td / "slow.py", operator_type="scripted", timeout=0.5)
        assert result.outcome == "failed" and result.reasons == ["the preprocessor timed out after 0.5 s"], out
        code, out = _verify_run_cli(a, i, td / "slow.py", "--operator-type", "scripted", "--timeout", "0.5")
        assert code == 1 and "TIMED OUT: the preprocessor timed out after 0.5 s" in out, out
        for bad in ("0", "-1", "nan", "inf", "x"):
            code, out = _verify_run_cli(a, i, "-", "--operator-type", "scripted", "--timeout", bad)
            assert code == 2 and "--timeout" in out, (bad, out)
        for bad in (0, -1, True, "5", float("nan")):
            raised = False
            try:
                verify_run.run(a, i, operator_type="scripted", timeout=bad)
            except ValueError:
                raised = True
            assert raised, bad


def test_verify_run_a_killed_step_leaves_no_sqlite_copy_in_tmpdir():
    # sqlite_source copies a goose or opencode store into a temporary
    # directory and removes it at exit, which a step killed on timeout never
    # reaches. Every step gets a temporary directory (TMPDIR) that the run
    # owns and removes on any outcome, with or without a workdir, which it
    # is not inside (ruled 2026-10-28).
    import os  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import sqlite3  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        store = td / "sessions.db"
        con = sqlite3.connect(store)
        con.execute("CREATE TABLE sessions (id TEXT)")
        con.commit()
        con.close()
        record = td / "copied-to"
        stub = ("import pathlib, shutil, sys, tempfile, time\n"
                f"sys.path.insert(0, {str(verify_run.ADAPTERS_DIR)!r})\n"
                "import sqlite_source\n"
                f"db = sqlite_source.open_evidence_db(pathlib.Path({str(store)!r}))\n"
                "copy = pathlib.Path(db.execute('PRAGMA database_list').fetchone()[2]).parent\n"
                f"pathlib.Path({str(record)!r}).write_text(str(copy) + '\\n' + tempfile.gettempdir())\n"
                "shutil.copyfile(sys.argv[1], sys.argv[2])\n")
        for hang in (True, False):
            for workdir in (None, td / f"work-{hang}"):
                case = (hang, workdir)
                if workdir is not None:
                    workdir.mkdir()
                record.unlink(missing_ok=True)
                (td / "stub.py").write_text(stub + ("time.sleep(30)\n" if hang else ""))
                result, out = _quiet(verify_run.run, a, i, td / "stub.py", operator_type="scripted",
                                     workdir=workdir, timeout=3 if hang else 60)
                copy, step_tmp = (pathlib.Path(p) for p in record.read_text().split("\n"))
                try:
                    if hang:
                        assert result.outcome == "failed" and result.reasons == [
                            "the preprocessor timed out after 3 s"], (case, out)
                    else:
                        assert result.outcome == "complete", (case, out)
                    assert copy.name.startswith("minitrace-sqlite-"), (case, copy)
                    assert os.path.realpath(copy.parent) == os.path.realpath(step_tmp), (case, copy, step_tmp)
                    assert not copy.exists(), (case, "the store copy outlived the run")
                    assert not step_tmp.exists(), (case, "the step's temporary directory outlived the run")
                    if workdir is not None:
                        assert workdir.is_dir() and not os.path.realpath(step_tmp).startswith(
                            os.path.realpath(workdir) + os.sep), (case, step_tmp)
                finally:
                    shutil.rmtree(copy, ignore_errors=True)
        # Every step, not only the preprocessor, gets the same directory.
        envs = []

        class Recording(subprocess.Popen):
            def __init__(self, *args, **kwargs):
                envs.append((kwargs.get("env") or {}).get("TMPDIR"))
                super().__init__(*args, **kwargs)
        (td / "copy.py").write_text("import shutil, sys\nshutil.copyfile(sys.argv[1], sys.argv[2])\n")
        with mock.patch.object(verify_run.subprocess, "Popen", Recording):
            result, out = _quiet(verify_run.run, a, i, td / "copy.py", operator_type="scripted")
        assert result.outcome == "complete" and len(envs) == 3 and envs[0] and len(set(envs)) == 1, (envs, out)
        assert not pathlib.Path(envs[0]).exists(), envs


def test_verify_run_a_descendant_writing_into_the_step_directory_does_not_fail_the_run():
    # A step that exits, or times out, while a process it started keeps
    # writing into its TMPDIR: the run still returns its result. Its group
    # is killed after every step, so a descendant in the step's session is
    # gone and the directory is removed; one in a session of its own cannot
    # be killed, and a directory that cannot be removed is a warning line,
    # the outcome unchanged (ruled 2026-10-28).
    import os  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import signal  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import time  # noqa: PLC0415
    verify_run = _verify_run_built()
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        (td / "child.py").write_text(
            "import pathlib, sys, tempfile, time\n"
            "d, beat, end = tempfile.gettempdir(), pathlib.Path(sys.argv[1]), time.time() + 60\n"
            "while time.time() < end:\n"
            "    with beat.open('a') as fh:\n"
            "        fh.write('x')\n"
            "    try:\n"
            "        tempfile.mkdtemp(dir=d)\n"
            "    except OSError:\n"
            "        pass\n"
            "    time.sleep(0.05)\n")
        # "same": a descendant in the step's session, the step exits;
        # "own": one in a session of its own, the step times out;
        # "locked": the step leaves a directory it cannot be removed from;
        # root removes it regardless, so as root "locked" is not checked.
        cases = ["same", "own"] + ([] if os.geteuid() == 0 else ["locked"])
        for case in cases:
            beat, pidfile, record = (td / f"{n}-{case}" for n in ("beat", "pid", "step-tmp"))
            stub = ("import os, pathlib, shutil, subprocess, sys, tempfile, time\n"
                    f"pathlib.Path({str(record)!r}).write_text(tempfile.gettempdir())\n")
            if case == "locked":
                stub += ("locked = pathlib.Path(tempfile.gettempdir()) / 'locked'\n"
                         "locked.mkdir()\n"
                         "(locked / 'f').write_text('x')\n"
                         "locked.chmod(0o500)\n")
            else:
                stub += (f"c = subprocess.Popen([sys.executable, {str(td / 'child.py')!r}, {str(beat)!r}],"
                         " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,"
                         f" start_new_session={case == 'own'})\n"
                         f"pathlib.Path({str(pidfile)!r}).write_text(str(c.pid))\n"
                         "time.sleep(0.5)\n")
            if case == "own":
                stub += "time.sleep(30)\n"
            stub += "shutil.copyfile(sys.argv[1], sys.argv[2])\n"
            (td / "stub.py").write_text(stub)
            step_tmp = None
            try:
                result, out = _quiet(verify_run.run, a, i, td / "stub.py", operator_type="scripted",
                                     timeout=2 if case == "own" else 60)
                step_tmp = record.read_text()
                warned = f"warning: step directory {step_tmp} not removed (" in out
                if case == "own":
                    assert result.outcome == "failed" and result.reasons == [
                        "the preprocessor timed out after 2 s"], out
                    # rmtree may win the race with the descendant or not;
                    # the directory is left exactly when the warning says so.
                    assert warned == os.path.lexists(step_tmp), (warned, out)
                elif case == "same":
                    assert result.outcome == "complete", out
                    assert not warned and not os.path.lexists(step_tmp), out
                    size = beat.stat().st_size
                    time.sleep(1)
                    assert beat.stat().st_size == size, "the step's descendant outlived the step"
                else:
                    assert result.outcome == "complete", out
                    assert warned and os.path.isdir(step_tmp), out
                    assert out.count("not removed") == 1, out
            finally:
                # Kill the descendant only while its heartbeat still grows,
                # so a pid reused after the run killed and reaped it is
                # never signalled.
                if pidfile.exists() and beat.exists():
                    size = beat.stat().st_size
                    time.sleep(0.3)
                    if beat.stat().st_size != size:
                        try:
                            os.kill(int(pidfile.read_text()), signal.SIGKILL)
                        except (OSError, ValueError):
                            pass
                if step_tmp is None and record.exists():
                    step_tmp = record.read_text()
                if step_tmp:
                    time.sleep(0.2)
                    if os.path.isdir(os.path.join(step_tmp, "locked")):
                        os.chmod(os.path.join(step_tmp, "locked"), 0o700)
                    shutil.rmtree(step_tmp, ignore_errors=True)


def test_verify_run_step_directory_removal_retries_then_warns():
    # The step directory is removed with retries while something still
    # writes into it; after the last failure it is one warning line, not an
    # exception (ruled 2026-10-28).
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415
    import verify_run  # noqa: PLC0415
    real = verify_run.shutil.rmtree
    with tempfile.TemporaryDirectory() as td:
        # A FileNotFoundError for an entry that vanished under rmtree (older
        # Pythons raise it) while the directory is still there is retried.
        for fail_first, removed, exc in ((2, True, OSError), (2, True, FileNotFoundError),
                                         (99, False, OSError)):
            d = os.path.join(td, f"steps-{fail_first}-{exc.__name__}")
            os.mkdir(d)
            calls = []

            def flaky(p, *args, **kwargs):
                calls.append(p)
                if len(calls) <= fail_first:
                    raise exc(66, "Directory not empty", p)
                return real(p, *args, **kwargs)
            with mock.patch.object(verify_run.shutil, "rmtree", flaky), \
                    mock.patch.object(verify_run.time, "sleep", lambda s: None):
                _, out = _quiet(verify_run._remove_step_tmp, d)
            if removed:
                assert len(calls) == 3 and not os.path.exists(d) and out == "", (calls, out)
            else:
                assert len(calls) == 5 and os.path.isdir(d), calls
                assert out == f"  warning: step directory {d} not removed ([Errno 66] Directory not empty: {d!r})\n", out


def _partial_output_case(td: pathlib.Path):
    """(runner, adapter, input) for output that fails part-way: two
    containers of synthetic-trace-pair, the second one's agent trace larger
    than the file-size limit the failing run gets."""
    adapters = pathlib.Path(__file__).resolve().parent.parent
    i = td / "in.json"
    i.write_text(json.dumps([{"id": "c0", "events": [{"index": 0, "role": "user", "content": "p"}]},
                             {"id": "c1", "events": [{"index": 0, "role": "user", "content": "x" * 20000}]}]))
    return (adapters / "dsl_runner.py", adapters / "tests" / "fixtures" / "synthetic-trace-pair.dsl.yaml", i)


def test_output_that_fails_part_way_leaves_nothing_of_this_run():
    # Ruled 2026-10-29: traces and the report are written under temporary
    # names, then put in place, the report last; an I/O failure part-way
    # (here a file-size limit, File too large) removes what this run wrote,
    # exits 1 with one error line and no traceback, and a rerun into the
    # same directory is accepted. Single-file output the same.
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    if os.name != "posix":
        return
    import resource  # noqa: PLC0415

    def limited():
        resource.setrlimit(resource.RLIMIT_FSIZE, (8000, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))

    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        runner, a, i = _partial_output_case(td)

        def cli(out, limit):
            return subprocess.run([sys.executable, str(runner), str(a), str(i), "--output", str(out),
                                   "--operator-type", "scripted"], capture_output=True, text=True,
                                  timeout=300, preexec_fn=limited if limit else None)
        d = td / "out"
        d.mkdir()
        (d / "notes.txt").write_text("x")
        r = cli(d, True)
        assert r.returncode == 1 and "Traceback" not in r.stderr, r.stderr[-600:]
        assert r.stderr.strip().splitlines()[-1].startswith(f"error: output {d} could not be written ("), r.stderr
        assert sorted(p.name for p in d.iterdir()) == ["notes.txt"], sorted(p.name for p in d.iterdir())
        r = cli(d, False)
        assert r.returncode == 0, r.stderr[-600:]
        names = sorted(p.name for p in d.iterdir())
        assert names == ["agent-c0.minitrace.json", "agent-c1.minitrace.json", "conversion.diagnostics.json",
                         "judge-c0.minitrace.json", "judge-c1.minitrace.json", "notes.txt"], names
        for n in names:
            if n.endswith(".json"):
                json.loads((d / n).read_text())
            assert ((d / n).stat().st_mode & 0o777) == 0o666 & ~_current_umask(), n
        # Single-file output: neither the trace file nor its report, and no
        # temporary file beside them.
        s = td / "single"
        s.mkdir()
        out = s / "all.json"
        r = cli(out, True)
        assert r.returncode == 1 and "Traceback" not in r.stderr, r.stderr[-600:]
        assert r.stderr.strip().splitlines()[-1].startswith(f"error: output {out} could not be written ("), r.stderr
        assert sorted(p.name for p in s.iterdir()) == [], sorted(p.name for p in s.iterdir())
        r = cli(out, False)
        assert r.returncode == 0 and sorted(p.name for p in s.iterdir()) == ["all.json", "all.json.diagnostics.json"]
        assert [t["id"] for t in json.loads(out.read_text())] == ["agent-c0", "judge-c0", "agent-c1", "judge-c1"]
        for n in ("all.json", "all.json.diagnostics.json"):
            assert ((s / n).stat().st_mode & 0o777) == 0o666 & ~_current_umask(), n


def test_output_put_in_place_part_way_is_removed_on_failure():
    # Ruled 2026-10-29: a failure while the files are put in place (after
    # every temporary file was written) removes the traces this run already
    # put in place and its temporary files, never a file it did not write;
    # the report is put in place last, so it never stands beside a partial
    # set. run() raises OutputFailed.
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415
    D = _dr()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        _, a, i = _partial_output_case(td)
        for fail_at in ("agent-c1.minitrace.json", "conversion.diagnostics.json"):
            d = td / f"out-{fail_at}"
            d.mkdir()
            (d / "notes.txt").write_text("x")
            real_link, real_replace = os.link, os.replace

            def failing(src, dst, *args, **kwargs):
                if os.path.basename(dst) == fail_at:
                    raise OSError(28, "No space left on device", str(dst))
                return (real_link if failing.kind == "link" else real_replace)(src, dst, *args, **kwargs)
            for kind in ("link", "replace"):
                failing.kind = kind
                with mock.patch.object(D.os, kind, failing):
                    raised = None
                    try:
                        D.run(a, i, d, operator_override=_OP)
                    except D.OutputFailed as e:
                        raised = e
                if raised is not None:
                    break
            assert raised is not None and "No space left on device" in str(raised), (fail_at, raised)
            assert sorted(p.name for p in d.iterdir()) == ["notes.txt"], (fail_at, sorted(p.name for p in d.iterdir()))
        # Single-file output, the report failing after the trace was put in place.
        s = td / "single"
        s.mkdir()
        real_replace = os.replace

        def failing_report(src, dst, *args, **kwargs):
            if str(dst).endswith(".diagnostics.json"):
                raise OSError(28, "No space left on device", str(dst))
            return real_replace(src, dst, *args, **kwargs)
        raised = None
        with mock.patch.object(D.os, "replace", failing_report):
            try:
                D.run(a, i, s / "all.json", operator_override=_OP)
            except D.OutputFailed as e:
                raised = e
        assert raised is not None and sorted(p.name for p in s.iterdir()) == [], sorted(p.name for p in s.iterdir())
        # Directory output never overwrites: a file that appeared at a
        # trace's name is kept, the run fails; temporary names are hidden
        # and never *.minitrace.json.
        w = td / "appeared"
        w.mkdir()
        (w / "a.minitrace.json").write_text("someone else's\n")
        opened = []
        real_open = os.open

        def recording(p, *args, **kwargs):
            opened.append(os.path.basename(p))
            return real_open(p, *args, **kwargs)
        raised = None
        with mock.patch.object(D.os, "open", recording):
            try:
                D._write_output_files([(w / "b.minitrace.json", "b\n"), (w / "a.minitrace.json", "a\n"),
                                       (w / "conversion.diagnostics.json", "{}\n")], replace=False, label=w)
            except D.OutputFailed as e:
                raised = e
        assert raised is not None, "an existing file was overwritten"
        assert sorted(p.name for p in w.iterdir()) == ["a.minitrace.json"], sorted(p.name for p in w.iterdir())
        assert (w / "a.minitrace.json").read_text() == "someone else's\n"
        assert len(opened) == 3 and all(n.startswith(".") and not n.endswith(".minitrace.json") for n in opened), opened
        # The report is put in place last, after every trace, in both forms.
        for out, kind in ((td / "order-dir", "link"), (td / "order-single.json", "replace")):
            if kind == "link":
                out.mkdir()
            order = []
            real = getattr(os, kind)

            def recording_place(src, dst, *args, real=real, **kwargs):
                order.append(os.path.basename(dst))
                return real(src, dst, *args, **kwargs)
            with mock.patch.object(D.os, kind, recording_place):
                D.run(a, i, out, operator_override=_OP)
            report = "conversion.diagnostics.json" if kind == "link" else "order-single.json.diagnostics.json"
            assert order and order[-1] == report and order.count(report) == 1, order
            assert len(order) == (5 if kind == "link" else 2), order


def test_output_cleanup_covers_any_exception_and_a_failed_unlink_and_long_names():
    # Ruled 2026-10-29: Ctrl-C while the files are put
    # in place still removes what this run wrote, and propagates as itself;
    # a trace linked into place whose temporary file then cannot be removed
    # is removed too; a final name that fits the file system's limit never
    # fails for its temporary name (single-file output's report adds 18
    # characters to the output's name).
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415
    D = _dr()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        _, a, i = _partial_output_case(td)
        d = td / "interrupted"
        d.mkdir()
        real_link = os.link
        links = []

        def interrupting(src, dst, *args, **kwargs):
            links.append(os.path.basename(dst))
            if len(links) == 2:
                raise KeyboardInterrupt
            return real_link(src, dst, *args, **kwargs)
        raised = None
        with mock.patch.object(D.os, "link", interrupting):
            try:
                D.run(a, i, d, operator_override=_OP)
            except KeyboardInterrupt as e:
                raised = e
        assert raised is not None and sorted(p.name for p in d.iterdir()) == [], sorted(p.name for p in d.iterdir())
        d = td / "unlink-fails"
        d.mkdir()
        real_unlink = os.unlink
        failed = []

        def failing_once(p, *args, **kwargs):
            if not failed and os.path.basename(p).endswith(".partial"):
                failed.append(p)
                raise OSError(5, "Input/output error", str(p))
            return real_unlink(p, *args, **kwargs)
        raised = None
        with mock.patch.object(D.os, "unlink", failing_once):
            try:
                D.run(a, i, d, operator_override=_OP)
            except D.OutputFailed as e:
                raised = e
        assert raised is not None and failed and sorted(p.name for p in d.iterdir()) == [], \
            sorted(p.name for p in d.iterdir())
        # Where the file system has no hard links, a rename puts each file
        # in place: all of them on success, none left when the report fails,
        # and a file already at a name is kept and the run fails.
        import errno  # noqa: PLC0415

        def no_links(src, dst, *args, **kwargs):
            raise OSError(errno.EPERM, "Operation not permitted", str(dst))
        real_rename = os.rename
        for case in ("ok", "report fails", "name taken"):
            d = td / f"no-links-{case}"
            d.mkdir()

            def renaming(src, dst, *args, **kwargs):
                if case == "report fails" and os.path.basename(dst) == "conversion.diagnostics.json":
                    raise OSError(28, "No space left on device", str(dst))
                if case == "name taken" and not (d / "judge-c1.minitrace.json").exists():
                    # It appears after the run's check of the directory.
                    (d / "judge-c1.minitrace.json").write_text("someone else's\n")
                return real_rename(src, dst, *args, **kwargs)
            raised = None
            with mock.patch.object(D.os, "link", no_links), mock.patch.object(D.os, "rename", renaming):
                try:
                    D.run(a, i, d, operator_override=_OP)
                except D.OutputFailed as e:
                    raised = e
            names = sorted(p.name for p in d.iterdir())
            if case == "ok":
                assert raised is None and names == ["agent-c0.minitrace.json", "agent-c1.minitrace.json",
                                                    "conversion.diagnostics.json", "judge-c0.minitrace.json",
                                                    "judge-c1.minitrace.json"], (raised, names)
            elif case == "report fails":
                assert raised is not None and names == [], names
            else:
                assert raised is not None and names == ["judge-c1.minitrace.json"], names
                assert (d / "judge-c1.minitrace.json").read_text() == "someone else's\n"
        # Single-file output: a file at the output's name that this run did
        # not put there is not removed when putting the trace in place fails.
        s = td / "foreign"
        s.mkdir()
        real_replace = os.replace

        def failing_trace(src, dst, *args, **kwargs):
            if os.path.basename(dst) == "all.json":
                # It appears after the run cleared the output's name.
                (s / "all.json").write_text("someone else's\n")
                raise OSError(28, "No space left on device", str(dst))
            return real_replace(src, dst, *args, **kwargs)
        raised = None
        with mock.patch.object(D.os, "replace", failing_trace):
            try:
                D.run(a, i, s / "all.json", operator_override=_OP)
            except D.OutputFailed as e:
                raised = e
        assert raised is not None and sorted(p.name for p in s.iterdir()) == ["all.json"]
        assert (s / "all.json").read_text() == "someone else's\n"
        # The longest output name whose report name still fits.
        limit = os.pathconf(td, "PC_NAME_MAX") if hasattr(os, "pathconf") else 255
        s = td / "long"
        s.mkdir()
        out = s / ("o" * (limit - len(".diagnostics.json")))
        D.run(a, i, out, operator_override=_OP)
        assert sorted(p.name for p in s.iterdir()) == sorted([out.name, out.name + ".diagnostics.json"])


def test_verify_run_a_reused_workdir_never_converts_an_earlier_preprocessor_output():
    # Ruled 2026-10-29: in a reused workdir the preprocessor's earlier
    # output is removed before it runs, so one that exits 0 without writing
    # fails the run (it converted the previous input, complete).
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        work = td / "work"
        (td / "copy.py").write_text("import shutil, sys\nshutil.copyfile(sys.argv[1], sys.argv[2])\n")
        (td / "silent.py").write_text("print('done')\n")
        result, out = _quiet(verify_run.run, a, i, td / "copy.py", operator_type="scripted", workdir=work)
        assert result.outcome == "complete" and (work / "pre.json").exists(), out
        result, out = _quiet(verify_run.run, a, i, td / "silent.py", operator_type="scripted", workdir=work)
        assert result.outcome == "failed" and result.reasons == ["the preprocessor exited 0 and wrote no output"], \
            (result.outcome, result.reasons, out)
        assert not (work / "pre.json").exists(), "an earlier run's preprocessor output was left in the workdir"


def test_verify_run_never_removes_a_callers_file_kept_at_the_workdirs_names():
    # An input or a preprocessor kept in the workdir as
    # pre.json or pre.fidelity.json (by name or through a symlink) is the
    # caller's file; the run is refused before anything is removed, and the
    # file is kept.
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        copy = "import shutil, sys\nshutil.copyfile(sys.argv[1], sys.argv[2])\n"
        for kept in ("pre.json", "pre.fidelity.json"):
            for role in ("input", "input via a symlink", "preprocessor"):
                work = td / f"work-{kept}-{role.replace(' ', '-')}"
                work.mkdir()
                if role == "preprocessor":
                    (work / kept).write_text(copy)
                    args, body = (i, work / kept), copy
                else:
                    (work / kept).write_bytes(i.read_bytes())
                    body = i.read_text()
                    given = work / kept
                    if role == "input via a symlink":
                        given = td / f"link-{kept}"
                        os.symlink(work / kept, given)
                    (td / "copy.py").write_text(copy)
                    args = (given, td / "copy.py")
                result, out = _quiet(verify_run.run, a, args[0], args[1], operator_type="scripted", workdir=work)
                name = "preprocessor" if role == "preprocessor" else "input"
                assert result.outcome == "failed" and result.reasons == [
                    f"the {name} is the workdir's {kept}, which a run replaces; nothing was removed"], \
                    (kept, role, result.reasons, out)
                assert (work / kept).read_text() == body, (kept, role, "the caller's file was changed or removed")


def test_verify_run_never_writes_into_an_input_directory_nor_removes_the_adapter():
    # A workdir that is the input directory, or lies in
    # it, is refused before anything is written, and the corpus files keep
    # their bytes; an adapter DSL kept at the workdir's pre.json or
    # pre.fidelity.json is refused as the input and the preprocessor are.
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        corpus = td / "corpus"
        corpus.mkdir()
        shutil.copy(i, corpus / "session.jsonl")
        (corpus / "pre.json").write_text("corpus file\n")
        (corpus / "sub").mkdir()
        (corpus / "sub" / "pre.fidelity.json").write_text("corpus file\n")
        (td / "copy.py").write_text("import shutil, sys\nshutil.copyfile(sys.argv[1], sys.argv[2])\n")
        for work, inside in ((corpus, "the same as"), (corpus / "sub", "inside"), (corpus / "new", "inside")):
            result, out = _quiet(verify_run.run, a, corpus, td / "copy.py", operator_type="scripted", workdir=work)
            assert result.outcome == "failed" and result.reasons == [
                f"the workdir is {inside} the input directory, whose files a run would replace; nothing was written"], \
                (work, result.reasons, out)
            assert (corpus / "pre.json").read_text() == "corpus file\n"
            assert (corpus / "sub" / "pre.fidelity.json").read_text() == "corpus file\n"
            assert not (corpus / "new").exists(), "the workdir was created inside the input directory"
        # A workdir beside an input file (its directory is not an input) runs.
        result, out = _quiet(verify_run.run, a, i, td / "copy.py", operator_type="scripted", workdir=td / "beside")
        assert result.outcome == "complete", out
        for kept in ("pre.json", "pre.fidelity.json"):
            work = td / f"adapter-{kept}"
            work.mkdir()
            (work / kept).write_bytes(a.read_bytes())
            result, out = _quiet(verify_run.run, work / kept, i, td / "copy.py", operator_type="scripted",
                                 workdir=work)
            assert result.outcome == "failed" and result.reasons == [
                f"the adapter is the workdir's {kept}, which a run replaces; nothing was removed"], (kept, out)
            assert (work / kept).read_bytes() == a.read_bytes(), kept


def test_verify_run_own_temporary_workdir_is_not_refused_inside_an_input_directory(monkeypatch):
    # Only a workdir the caller gave is refused inside an
    # input directory; the run's own temporary one is new and empty, so an
    # input directory that holds TMPDIR still converts.
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        corpus = td / "corpus"
        (corpus / "tmp").mkdir(parents=True)
        (td / "stub.py").write_text(f"import shutil, sys\nshutil.copyfile({str(i)!r}, sys.argv[2])\n")
        monkeypatch.setattr(tempfile, "tempdir", str(corpus / "tmp"))
        result, out = _quiet(verify_run.run, a, corpus, td / "stub.py", operator_type="scripted")
        monkeypatch.undo()
        assert result.outcome == "complete", (result.reasons, out)
        result, out = _quiet(verify_run.run, a, corpus, td / "stub.py", operator_type="scripted",
                             workdir=corpus / "work")
        assert result.reasons == ["the workdir is inside the input directory, whose files a run would replace; "
                                  "nothing was written"], result.reasons


def test_to_spec_utc_reads_as_the_dsl_does_and_counts_what_it_nulls():
    # Ruled 2026-10-29: to_spec_utc never passes a value through. It reads
    # a timestamp as DSL rc3 §9.1 iso_normalise does (one parser), a leap
    # second included; anything else, a non-string too, is null and counted
    # for the caller's warning line; null and "" are null and not counted.
    timestamps.take_refused()
    cases = [
        ("2016-12-31T23:59:60Z", "2016-12-31T23:59:59.999999Z"),
        ("2016-12-31T23:59:60.5Z", "2016-12-31T23:59:59.999999Z"),
        ("2017-01-01T00:59:60+01:00", "2016-12-31T23:59:59.999999Z"),   # 23:59:60 in UTC
        ("9999-12-31T23:59:60Z", "9999-12-31T23:59:59.999999Z"),
        ("2026-01-01T00:00:00.1234567Z", "2026-01-01T00:00:00.123456Z"),   # truncated
        ("2026-10-03T10:00:00.5+02:00", "2026-10-03T08:00:00.500000Z"),
        (None, None), ("", None),
    ]
    for v, want in cases:
        assert timestamps.to_spec_utc(v) == want, v
    assert timestamps.take_refused() == 0
    nulled = ["2026-01-01T12:00:60Z", "2017-01-01T00:59:60Z", "2026-02-30T10:00:00Z", "garbage",
              "2026-01-01T10:00:00", "0001-01-01T00:00:00+01:00", 5, [], {"a": 1}, True]
    for v in nulled:
        assert timestamps.to_spec_utc(v) is None, v
    assert timestamps.take_refused() == len(nulled)
    assert timestamps.take_refused() == 0, "take_refused resets the count"


def test_iso_normalise_maps_a_leap_second_and_refuses_any_other_second_60():
    # Spec lane 62a1d701 (DSL rc3 §9.1): seconds 60 only when the UTC value
    # is 23:59:60, giving 23:59:59.999999 of that UTC day; the offset is
    # applied to hours and minutes, the seconds carried; any other 60 is an
    # input failure, and duration_seconds reads a leap second the same way.
    D = _dr()
    assert D.fn_iso_normalise("2016-12-31T23:59:60Z") == "2016-12-31T23:59:59.999999Z"
    assert D.fn_iso_normalise("2017-01-01T00:59:60.25+01:00") == "2016-12-31T23:59:59.999999Z"
    assert D.fn_iso_normalise("9999-12-31T23:59:60Z") == "9999-12-31T23:59:59.999999Z"
    for bad in ("2026-01-01T12:00:60Z", "2017-01-01T00:59:60Z", "2016-12-31T23:58:60Z"):
        raised = False
        try:
            D.fn_iso_normalise(bad)
        except ValueError:
            raised = True
        assert raised, bad
    assert D.fn_duration_seconds("2016-12-31T23:59:59Z", "2016-12-31T23:59:60Z") == 0.999999


def test_preprocessors_warn_once_per_input_for_timestamps_they_null():
    # Ruled 2026-10-29: codex, claude-ai, gemini-cli and vibe print one
    # "warning:" line per input counting the timestamps written as null
    # (verify_run reads it as a loss), and the bad value is not in the output.
    import re  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    adapters = pathlib.Path(__file__).resolve().parent.parent
    iso = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)")
    for harness in ("codex", "claude-ai", "gemini-cli", "vibe"):
        manifests = sorted((adapters / harness).glob("regression/*/manifest.json"))
        if manifests:   # the first regression scenario's input
            spec = next(iter(json.loads(manifests[0].read_text())["scenarios"].values()))
            src, cwd = manifests[0].parent / spec["input"], manifests[0].parent
        else:           # no rung: claude-ai's probe fixture; gemini-cli a session of its own
            src = adapters / "tests" / "fixtures" / "claude-ai-2026-09-26-probe.json" if harness == "claude-ai" else None
            cwd = adapters
        pre = next((adapters / harness).glob("preprocess_*.py"))
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            if src is None:   # the release tree ships no gemini-cli capture
                (td / "src").mkdir()
                src = td / "src" / "session.json"
                src.write_text(json.dumps({
                    "sessionId": "s1", "startTime": "2026-01-01T00:00:00.000Z",
                    "lastUpdated": "2026-01-01T00:00:01.000Z",
                    "messages": [{"id": "m1", "timestamp": "2026-01-01T00:00:00.000Z", "type": "user",
                                  "content": [{"text": "hi"}]},
                                 {"id": "m2", "timestamp": "2026-01-01T00:00:01.000Z", "type": "gemini",
                                  "content": "hello"}]}))
            inp = td / src.name
            if src.is_dir():
                shutil.copytree(src, inp)
                files = sorted(p for p in inp.rglob("*") if p.is_file())
            else:
                shutil.copy(src, inp)
                files = [inp]
            for f in files:   # the first timestamp of the input made invalid
                text = f.read_text()
                if iso.search(text):
                    f.write_text(iso.sub("2026-02-30T10:00:00Z", text, count=1))
                    break
            else:
                raise AssertionError(f"{harness}: no timestamp in {src}")
            out = td / "out.json"
            r = subprocess.run([sys.executable, str(pre), str(inp), str(out)], capture_output=True, text=True,
                               timeout=300, cwd=cwd)
            lines = [ln for ln in r.stderr.splitlines() if "not convertible to the §3.5 form" in ln]
            assert len(lines) == 1 and re.fullmatch(
                rf"warning: {harness}: .+: [1-9][0-9]* timestamp\(s\) not convertible to the §3\.5 form, "
                r"written as null", lines[0]), (harness, r.returncode, r.stderr[-500:])
            assert r.returncode == 0 and out.exists(), (harness, r.returncode, r.stderr[-300:])
            assert "2026-02-30" not in out.read_text(), harness


def _current_umask() -> int:
    import os  # noqa: PLC0415
    m = os.umask(0)
    os.umask(m)
    return m


def test_a_literal_map_or_list_is_a_fresh_object_in_every_trace():
    # Ruled 2026-10-09, evaluate() and static/passthrough: a YAML map
    # or list in session.derived, finalize.computed (both through evaluate())
    # or session.static/passthrough went into each trace as the adapter's own
    # object, so a write beneath it in one trace changed the adapter text and
    # every other trace sharing it (in container mode, the last container's
    # value showed in all). Each trace now gets its own copy.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    dsl = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    agent = dsl["sessions"]["agent"]
    agent.setdefault("derived", {})["handover"] = {"received": None}
    agent.setdefault("static", {})["operational_context"] = {"git_ref": None}
    agent.setdefault("passthrough", {})["coordination"] = {"project_id": None}
    dsl.setdefault("finalize", {}).setdefault("computed", {})["redactions"] = []
    # A named tool and the default. Map values, whose sharing
    # this tested, are refused at load since the 2026-10-10 shape ruling (operation_types values are
    # §10's enumeration), so only the lookup is left to pin here.
    dsl["operation_types"] = {"Bash": "EXECUTE", "default": "OTHER"}
    dsl["emit"][0]["do"] += [{"emit_tool_call": {"id": "t1", "tool_name": "Bash"}},
                             {"emit_tool_call": {"id": "t2", "tool_name": "Other"}}]
    dsl["emit"].append({"name": "mark", "target": "agent", "when": {"has_any": True}, "do": [{"set": {
        "session.handover.produced": "$record.content",
        "session.operational_context.sandbox": "$record.content",
        "session.coordination.predecessor_session": "$record.content"}}]})
    recs = [[{"index": 0, "role": "user", "content": f"c{i}"}] for i in range(2)]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps([{"id": f"c{i}", "events": r} for i, r in enumerate(recs)]))
        traces = D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override=_OP)
    t0, t1 = (t for t in sorted(traces, key=lambda t: t["id"]) if t["id"].startswith("agent-"))
    for t, want in ((t0, "c0"), (t1, "c1")):
        assert (t["handover"]["produced"], t["operational_context"]["sandbox"],
                t["coordination"]["predecessor_session"]) == (want, want, want), (t["id"], t)
    for field in ("handover", "operational_context", "coordination", "redactions"):
        assert t0[field] is not t1[field], field
    for t in (t0, t1):
        assert [tc["operation_type"] for tc in t["tool_calls"]] == ["EXECUTE", "OTHER"], t["tool_calls"]
    lit = {"a": [1, {"b": 2}]}
    got = D.evaluate(lit, {})
    assert got == lit and got is not lit and got["a"] is not lit["a"] and got["a"][1] is not lit["a"][1]


def test_container_input_that_is_not_an_array_is_unreadable():
    # Ruled 2026-10-09, input unreadable: DSL rc3 §4.1 "Top-level
    # input MUST be a JSON array". In container mode an object was converted
    # quietly as one container, and a scalar became a container whose traces
    # R5 then dropped with a misleading "trace id None" report. Either is now
    # the input unreadable, as malformed JSON is: no traces. Single-trace
    # mode is unchanged (§4: a non-array root is one record).
    import tempfile  # noqa: PLC0415
    import pytest  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    container_dsl = fixtures / "synthetic-trace-pair.dsl.yaml"
    obj = {"id": "c0", "events": [{"index": 0, "role": "user", "content": "p"}]}
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        p = td / "in.json"
        for root, kind in ((obj, "an object"), (5, "a number"), ("s", "a string"), (True, "a boolean"), (None, "null"),
                           (12345678901234567890, "a number"), (1e400, "a number")):   # read as strings (§4)
            p.write_text(json.dumps(root) if root != 1e400 else "1e400")
            with pytest.raises(ValueError, match=rf"input unreadable: .*: container input is {kind}, not an array \(§4\.1\)"):
                D.run(container_dsl, p, None, operator_override=_OP)
        p.write_text("[]")   # nothing to convert: a warning, no trace (spec 9c801194)
        diags = []
        assert D.run(container_dsl, p, None, diagnostics_out=diags, operator_override=_OP) == []
        assert [d["kind"] for d in diags] == ["input-empty"] and diags[0]["loss"] is None, diags
        single = yaml.safe_load(container_dsl.read_text())
        single["input"] = {"type": "json"}
        single["session"] = single.pop("sessions")["agent"]
        single["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
        single["emit"] = [r for r in single["emit"] if r.get("target") == "agent"]
        for r in single["emit"]:
            r.pop("target", None)
        (td / "s.dsl.yaml").write_text(yaml.safe_dump(single, sort_keys=False))
        p.write_text(json.dumps({"index": 0, "role": "user", "content": "p"}))
        t = D.run(td / "s.dsl.yaml", p, None, operator_override=_OP)
        assert (t[0] if isinstance(t, list) else t)["turns"], t


def test_a_key_with_an_empty_segment_is_refused_at_load():
    # DSL rc3 §15, spec f2430aed; ruled 2026-10-09: a key with an
    # empty segment (state.a..b, state.c., state., flags..x) in a set or
    # accumulate (on_block included), finalize.computed, or a static, derived
    # or passthrough map of session or of a sessions shape is a structural
    # failure at load. The runner wrote keys named "" and reported nothing.
    import copy  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import pytest  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    multi = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    assert D.dsl_empty_segment_problems(multi) == []
    rule = {"name": "r", "target": "agent", "when": {"has_any": True}}

    def add(where, key):
        d = copy.deepcopy(multi)
        if where in ("set", "accumulate"):
            d["emit"].append({**rule, "do": [{where: {key: 1}}]})
        elif where == "on_block":
            d["emit"].append({**rule, "foreach_block": "$record.s", "do": [],
                              "on_block": [{"name": "b", "when": {"has_any": True}, "do": [{"accumulate": {key: 1}}]}]})
        elif where == "computed":
            d.setdefault("finalize", {}).setdefault("computed", {})[key] = "1"
        else:
            d["sessions"]["agent"].setdefault(where, {})[key] = "x"
        return d

    cases = [("set", "state.a..b", "emit.r.set"), ("set", "state.", "emit.r.set"), ("set", "session.", "emit.r.set"),
             ("accumulate", "state.c.", "emit.r.accumulate"), ("on_block", "state.a..b", "emit.r.on_block.b.accumulate"),
             ("computed", "metrics..n", "finalize.computed"), ("static", "flags..x", "sessions.agent.static"),
             ("derived", "a.", "sessions.agent.derived"), ("passthrough", ".a", "sessions.agent.passthrough")]
    for where, key, place in cases:
        assert D.dsl_empty_segment_problems(add(where, key)) == [
            f"{place}.{key}: a key with an empty segment (§15)"], (where, key, D.dsl_empty_segment_problems(add(where, key)))
    single = copy.deepcopy(multi)
    single["session"] = single.pop("sessions")["agent"]
    single["session"].setdefault("static", {})["flags..x"] = 1
    assert D.dsl_empty_segment_problems(single) == ["session.static.flags..x: a key with an empty segment (§15)"]
    with tempfile.TemporaryDirectory() as td:   # wired into run(), before any input is read
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(add("set", "state.a..b"), sort_keys=False))
        with pytest.raises(SystemExit, match=r"emit\.r\.set\.state\.a\.\.b: a key with an empty segment"):
            D.run(td / "a.dsl.yaml", td / "missing.json", None, operator_override=_OP)


def test_a_shape_error_or_operation_type_outside_the_enum_is_refused_at_load():
    # DSL rc3 §15, §10; ruled 2026-10-10: shape errors loaded.
    # A body of the wrong type (set: 5, accumulate: 5, do as a mapping)
    # crashed only when its rule fired (AttributeError, TypeError), an
    # unknown action failed at run time, an unknown rule key was ignored, a
    # predicate outside §8.1 was a warning, and an operation_types value
    # outside §10's enumeration went into tool_calls unchecked. Each is now a
    # structural failure at load, named by place; a rule with no when is
    # still only a warning.
    import copy  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import pytest  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    adapters = pathlib.Path(__file__).resolve().parent.parent
    shipped = [p for p in adapters.rglob("*.dsl.yaml") if not {"tests", "fuzz"} & set(p.relative_to(adapters).parts)]
    assert len(shipped) >= 10, len(shipped)   # holds in the dev and the release tree
    for p in shipped:
        assert D.dsl_shape_problems(D.load_adapter_yaml(p.read_text(encoding="utf-8"))) == [], p
    multi = yaml.safe_load((pathlib.Path(__file__).resolve().parent / "fixtures" / "synthetic-trace-pair.dsl.yaml").read_text())
    assert D.dsl_shape_problems(multi) == []
    rule = {"name": "r", "target": "agent", "when": {"has_any": True}}

    def with_rule(**kw):
        d = copy.deepcopy(multi)
        d["emit"].append({**rule, **kw})
        return d

    def block(**kw):   # a rule with foreach_block needs no do
        return with_rule(foreach_block="$record.s", on_block=[{"name": "b", "when": {"has_any": True}, "do": [], **kw}])

    cases = [
        (with_rule(do=[{"set": 5}]), "emit.r.do[0].set: the action body is a mapping (§8.2)"),
        (with_rule(do=[{"accumulate": 5}]), "emit.r.do[0].accumulate: the action body is a mapping (§8.2)"),
        (with_rule(do=[{"emit_turn": "x"}]), "emit.r.do[0].emit_turn: the action body is a mapping (§8.2)"),
        (with_rule(do=[{"note": None}]), "emit.r.do[0].note: the action body is a mapping (§8.2)"),
        (with_rule(do={"set": {"state.a": 1}}), "emit.r.do: a list of actions (§8.2)"),
        (with_rule(do=[5]), "emit.r.do[0]: an action is a mapping (§8.2)"),
        (with_rule(do=[{"emit_thing": {}}]), "emit.r.do[0]: unknown action ['emit_thing'] (§15)"),
        (with_rule(do=[{"set": {"state.a": 1}, "note": {"message": "x"}}]),
         "emit.r.do[0]: unknown key 'note' beside the action set (§15)"),
        (with_rule(do=[{"emit_tool_call": {}, "omit_if_none": ["a"]}]),
         "emit.r.do[0]: unknown key 'omit_if_none' beside the action emit_tool_call (§15)"),
        (with_rule(do=[{"emit_turn": {}, "omit_if_none": "a"}]), "emit.r.do[0].omit_if_none: a list of field names (§8.2.2)"),
        (with_rule(do=[], bogus=1), "emit.r: unknown key 'bogus' in a rule (§15)"),
        (with_rule(do=[], when={"has_any": False}), "emit.r.when: has_any is falsy, never matches"),
        (with_rule(do=[], when={"block_type": "text"}), "emit.r.when: block_type outside on_block never matches"),
        (with_rule(do=[], when={"any_of": []}), "emit.r.when.any_of: must be a non-empty list"),
        (with_rule(do=[], when=5), "emit.r.when: empty or non-mapping predicate never matches"),
        (block(target="agent"), "emit.r.on_block.b: unknown key 'target' in a rule (§15)"),
        (block(foreach_block="y"), "emit.r.on_block.b: unknown key 'foreach_block' in a rule (§15)"),
        (block(do=[{"set": 5}]), "emit.r.on_block.b.do[0].set: the action body is a mapping (§8.2)"),
        (block(when={"has_any": False}), "emit.r.on_block.b.when: has_any is falsy, never matches"),
        # The rest of predicate_problems' forms, and an
        # empty all_of, which matched every record (all([])) under a "never
        # fires" warning.
        (with_rule(do=[], when={"all_of": []}), "emit.r.when.all_of: must be a non-empty list"),
        (with_rule(do=[], when={"field": "a"}),
         "emit.r.when: field without value: or present: never matches (unrecognised [])"),
        (with_rule(do=[], when={"all_of": [{"has": "a"}], "has": "b"}), "emit.r.when: keys ['has'] ignored beside all_of"),
        (with_rule(do=[], when={"block_field": "a", "value": 1}), "emit.r.when: block_field outside on_block never matches"),
        # A key that is present but null is no list or mapping: YAML's bare
        # `do:` is null, and it loaded and was ignored.
        (with_rule(do=None), "emit.r.do: a list of actions (§8.2)"),
        (block(do=None), "emit.r.on_block.b.do: a list of actions (§8.2)"),
        (with_rule(foreach_block="$record.s", on_block=None), "emit.r.on_block: a list of rules (§8)"),
        (with_rule(do=[{"emit_turn": {}, "omit_if_none": None}]), "emit.r.do[0].omit_if_none: a list of field names (§8.2.2)"),
    ]
    for d, want in cases:
        assert D.dsl_shape_problems(d) == [want], (want, D.dsl_shape_problems(d))
    unnamed = copy.deepcopy(multi)
    unnamed["emit"].append({"target": "agent", "when": {"has_any": True}, "do": [{"set": 5}]})
    assert D.dsl_shape_problems(unnamed) == [f"emit[{len(unnamed['emit']) - 1}].do[0].set: the action body is a mapping (§8.2)"]
    for emit, want in ((5, "emit: a list of rules (§8)"), ([5], "emit[0]: a rule is a mapping (§8)"),
                       (None, "emit: a list of rules (§8)")):
        assert D.dsl_shape_problems({**multi, "emit": emit}) == [want], emit
    # A missing emit crashed run() (KeyError) after loading.
    assert D.dsl_shape_problems({k: v for k, v in multi.items() if k != "emit"}) == ["emit: a list of rules (§8)"]
    for ops, want in (({"bash": "WRITE"}, "operation_types.bash: 'WRITE' is not one of READ, MODIFY, NEW, EXECUTE, DELEGATE, OTHER (§10)"),
                      ({"x": {"k": 1}}, "operation_types.x: {'k': 1} is not one of READ, MODIFY, NEW, EXECUTE, DELEGATE, OTHER (§10)"),
                      ({"x": 5}, "operation_types.x: 5 is not one of READ, MODIFY, NEW, EXECUTE, DELEGATE, OTHER (§10)"),
                      (["READ"], "operation_types: a mapping of tool name to operation type (§10)"),
                      (None, "operation_types: a mapping of tool name to operation type (§10)")):
        assert D.dsl_shape_problems({**multi, "operation_types": ops}) == [want], ops
    # Valid forms stay valid: every enum value, emit_turn with omit_if_none,
    # a block predicate inside on_block. A rule with no when was a warning
    # and is refused since the 2026-10-17 ruling.
    assert D.dsl_shape_problems({**multi, "operation_types": {"default": "OTHER", **{f"t{i}": v for i, v in enumerate(
        ("READ", "MODIFY", "NEW", "EXECUTE", "DELEGATE", "OTHER"))}}}) == []
    assert D.dsl_shape_problems(with_rule(do=[{"emit_turn": {}, "omit_if_none": ["ordering"]}])) == []
    assert D.dsl_shape_problems(block(when={"block_type": "text"})) == []
    no_when = copy.deepcopy(multi)
    no_when["emit"].append({"name": "r", "target": "agent", "do": []})
    assert D.dsl_shape_problems(no_when) == ["emit.r: no when; a rule without one never fires (§8)"]
    with tempfile.TemporaryDirectory() as td:   # wired into run(), before any input is read
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(with_rule(do=[{"set": 5}]), sort_keys=False))
        with pytest.raises(SystemExit, match=r"emit\.r\.do\[0\]\.set: the action body is a mapping"):
            D.run(td / "a.dsl.yaml", td / "missing.json", None, operator_override=_OP)


def test_a_predicate_operand_or_action_body_field_of_the_wrong_type_is_refused_at_load():
    # Ruled 2026-10-10, match the schema: after the shape check these still
    # loaded. A path or key that is not a string crashed when the rule fired
    # (has: [x] TypeError, field: 5 AttributeError), attach_tool_result
    # without target_id raised KeyError, has_any and present were read by
    # truthiness (`present: "false"` meant present, `has_any: "false"`
    # matched every record), and unknown body keys, a non-string note level
    # and a non-string rule name or target were accepted silently.
    import copy  # noqa: PLC0415
    import json  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import pytest  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    multi = yaml.safe_load((pathlib.Path(__file__).resolve().parent / "fixtures" / "synthetic-trace-pair.dsl.yaml").read_text())
    rule = {"name": "r", "target": "agent", "when": {"has_any": True}, "do": []}

    def with_rule(**kw):
        d = copy.deepcopy(multi)
        d["emit"].append({**rule, **kw})
        return d

    def block(when):   # a rule with foreach_block and no do
        d = with_rule(foreach_block="$record.s", on_block=[{"name": "b", "when": when, "do": []}])
        del d["emit"][-1]["do"]
        return d

    cases = [
        (with_rule(when={"has": ["x"]}), "emit.r.when: has takes a field name, a string (§8.1)"),
        (with_rule(when={"has": 5}), "emit.r.when: has takes a field name, a string (§8.1)"),
        (with_rule(when={"field": 5, "value": 1}), "emit.r.when: field takes a path, a string (§8.1)"),
        (with_rule(when={"container_field": ["a"], "present": True}), "emit.r.when: container_field takes a path, a string (§8.1)"),
        (block({"block_field": 5, "value": 1}), "emit.r.on_block.b.when: block_field takes a path, a string (§8.1)"),
        (with_rule(when={"field": "a", "present": "false"}), "emit.r.when: present takes true or false (§8.1)"),
        (with_rule(when={"field": "a", "present": 0}), "emit.r.when: present takes true or false (§8.1)"),
        (with_rule(when={"has_any": "false"}), "emit.r.when: has_any takes only the value true (§8.1)"),
        (with_rule(when={"has_any": 1}), "emit.r.when: has_any takes only the value true (§8.1)"),
        (with_rule(when={"all_of": [{"has": "a"}, {"field": "b", "present": "yes"}]}),
         "emit.r.when.all_of[1]: present takes true or false (§8.1)"),
        (with_rule(do=[{"attach_tool_result": {}}]), "emit.r.do[0].attach_tool_result: target_id is required (§8.2.4)"),
        (with_rule(do=[{"attach_tool_result": {"target_id": "a", "result": {}}}]),
         "emit.r.do[0].attach_tool_result: unknown key 'result' (§8.2)"),
        (with_rule(do=[{"attach_tool_result": {"target_id": "a", "output": 5}}]),
         "emit.r.do[0].attach_tool_result.output: a mapping (§8.2.4)"),
        (with_rule(do=[{"attach_tool_result": {"target_id": "a", "output": None}}]),
         "emit.r.do[0].attach_tool_result.output: a mapping (§8.2.4)"),
        (with_rule(do=[{"note": {"msg": "x"}}]), "emit.r.do[0].note: unknown key 'msg' (§8.2)"),
        (with_rule(do=[{"note": {"level": 5, "message": "x"}}]), "emit.r.do[0].note.level: a string (§8.2.6)"),
        (with_rule(target=["agent"]), "emit.r.target: a string (§8)"),
    ]
    for d, want in cases:
        assert D.dsl_shape_problems(d) == [want], (want, D.dsl_shape_problems(d))
    named5 = copy.deepcopy(multi)
    named5["emit"].append({**rule, "name": 5})
    assert D.dsl_shape_problems(named5) == [f"emit[{len(named5['emit']) - 1}].name: a string (§8)"]
    # In an on_block sub-rule too, and nested inside a combinator there.
    sub = block({"has_any": True})
    sub["emit"][-1]["on_block"][0]["name"] = 5
    assert D.dsl_shape_problems(sub) == ["emit.r.on_block[0].name: a string (§8)"]
    assert D.dsl_shape_problems(block({"any_of": [{"block_field": ["a"], "present": 1}]})) == [
        "emit.r.on_block.b.when.any_of[0]: block_field takes a path, a string (§8.1)",
        "emit.r.on_block.b.when.any_of[0]: present takes true or false (§8.1)"]
    # Both problems of one predicate are reported.
    assert D.dsl_shape_problems(with_rule(when={"field": 5, "present": "x"})) == [
        "emit.r.when: field takes a path, a string (§8.1)", "emit.r.when: present takes true or false (§8.1)"]
    # Valid forms stay valid: any value: (§8.3 equality), present false, a
    # numeric or null target_id (§8.2.4 renders a number), any message.
    for d in (with_rule(when={"field": "a", "value": [1, {"b": None}]}), with_rule(when={"field": "a", "present": False}),
              with_rule(when={"container_field": "a.b", "value": None}), block({"block_type": 5}),
              with_rule(do=[{"attach_tool_result": {"target_id": 5}}]),
              with_rule(do=[{"attach_tool_result": {"target_id": None, "output": {"x": "$record.a"}}}]),
              with_rule(do=[{"note": {}}]), with_rule(do=[{"note": {"level": "warn", "message": ["x"]}}])):
        assert D.dsl_shape_problems(d) == [], D.dsl_shape_problems(d)
    # Through run(), refused before any input is read, where it crashed or
    # silently matched with a matching record.
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "in.json").write_text(json.dumps([{"id": "c0", "events": [{"index": 0, "role": "user", "content": "p"}]}]))
        for d, pat in ((with_rule(when={"has": ["x"]}), "has takes a field name"),
                       (with_rule(do=[{"attach_tool_result": {}}]), "target_id is required"),
                       (with_rule(when={"field": "content", "present": "false"}), "present takes true or false")):
            (td / "a.dsl.yaml").write_text(yaml.safe_dump(d, sort_keys=False))
            with pytest.raises(SystemExit, match=pat):
                D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override=_OP)


def test_a_backslash_or_hash_outside_a_string_constant_is_a_structural_failure():
    # Ruled 2026-10-09, structural failure: §8.3
    # defines neither a line continuation nor a comment, but the runner
    # parsed expressions with Python's tokenizer, which joined
    # `$record.a + \<newline> 1` into one line and dropped `# ...` to the end
    # of the line, so both loaded and evaluated (to 2 and to 1). A backslash
    # or a # outside a string constant is a structural failure, at load and
    # at evaluation; inside a string constant a # is text.
    import re  # noqa: PLC0415

    import pytest  # noqa: PLC0415
    D = _dr()
    ctx = {"record": {"a": 1}}
    for expr, what in (("$record.a + \\\n 1", "a backslash"), ("$record.a \\\n+ 1", "a backslash"),
                       ("coalesce($record.a, 2) \\", "a backslash"), ("$record.a #comment", "a #"),
                       ("coalesce($record.a, 2) # x", "a #"), ("$record.a#", "a #"),
                       ("coalesce($record.zz, 'x') #", "a #"),
                       # Whitespace is space and tab only (ruled 2026-10-09): Python joined
                       # lines inside brackets and took \f as whitespace.
                       ("($record.a\n+ 1)", "a line break"), ("coalesce($record.zz,\r 2)", "a line break"),
                       ("$record.a\n+ 1", "a line break"), ("$record.a\f+ 1", "a form feed")):
        want = f"{what} outside a string constant (§8.3)"
        assert D.expression_problem(expr) == want, (expr, D.expression_problem(expr))
        with pytest.raises(D.ExpressionEvalError, match=re.escape(want)):
            D.evaluate(expr, ctx)
    with pytest.raises(D.ExpressionEvalError, match="a # outside a string constant"):
        D.evaluate("v-${$record.a # x}", ctx)   # a template body too
    for expr, want in (("coalesce($record.zz, '#1')", "#1"), ("coalesce($record.zz, \"a # b\")", "a # b"),
                       ("$record.a + 1", 2), ("$record.a\t+ 1", 2), ("$record.a + 1\n", 2),
                       ("\n$record.a + 1", 2)):   # both ends stripped
        assert D.expression_problem(expr) is None, expr
        assert D.evaluate(expr, ctx) == want, expr


def test_a_null_rhs_into_a_null_or_absent_counter_writes_nothing():
    # DSL rc3 §8.2.5 (spec ed6d769b; ruled 2026-10-10: option i): "a
    # null RHS adds nothing: into a null or absent counter it writes nothing,
    # so the key stays as it was (absent or null) and no value before the
    # last segment is created for it". The runner wrote 0 (and created the
    # parents), so an all-null input invented a measured zero.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    dsl["input"] = {"type": "json"}
    dsl["state"] = {"z": None, "n": 0, "w": None, "s": "text"}
    dsl["session"] = base["sessions"]["agent"]
    dsl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
    dsl["emit"] = [{"name": "r", "when": {"has_any": True}, "do": [
        {"accumulate": {"state.z": "$record.none", "state.q": "$record.none", "state.p.q": "$record.none",
                        "state.n": "$record.none", "state.w": "$record.one", "state.s": "$record.none"}},
        {"set": {"session.operational_context.seen": "$state"}}]}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps([{"none": None, "one": 1}]))
        diags = []
        t = D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"},
                  diagnostics_out=diags)
    seen = (t[0] if isinstance(t, list) else t)["operational_context"]["seen"]
    # z stays null, q and p stay absent (no parent created), n stays 0, and a
    # number into a null counter still counts it as 0 and adds.
    assert seen == {"z": None, "n": 0, "w": 1, "s": "text"}, seen
    # A counter holding a non-number is still an input failure with a null
    # RHS (§8.2.5, spec 6cd5a2e3): only a null or absent counter is skipped.
    assert [d["message"] for d in diags if d.get("kind") == "input-failure"] == [
        "accumulate.state.s: the counter holds a string, not a number; prior value kept (record 0)"], diags


def test_action_values_are_evaluated_through_nested_maps_and_lists():
    # (DSL rc3 §8.3 "Evaluated positions": every value in an
    # action's body, recursively through nested maps and lists). set,
    # accumulate, emit_turn and attach_tool_result output evaluated only the
    # top level, so a "$record.x" inside a map or list stayed text. They now
    # evaluate as emit_tool_call already did. A side effect for literal copying: a
    # literal [] in a set is a fresh list each time, so a reset resets.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    dsl["input"] = {"type": "json"}
    dsl["session"] = base["sessions"]["agent"]
    dsl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
    dsl["emit"] = [
        {"name": "r", "when": {"field": "kind", "value": "r"},
         "do": [{"set": {"state.lst": [], "session.environment.extra": {"k": "$record.x", "l": ["$record.y", 1]}}},
                {"accumulate": {"state.lst": {"x": "$record.x"}}},
                {"set": {"session.environment.seen": "$state.lst"}},
                {"emit_tool_call": {"id": "$record.id", "tool_name": "t"}},
                {"attach_tool_result": {"target_id": "$record.id", "output": {"result": {"v": "$record.x"}}}},
                {"emit_turn": {"index": "$record.index", "role": "user",
                               "content": [{"type": "text", "text": "$record.y"}]}}]}]
    recs = [{"kind": "r", "id": "a", "index": 0, "x": "N", "y": "n"},
            {"kind": "r", "id": "b", "index": 1, "x": "M", "y": "m"}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps(recs))
        t = D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"},
                  diagnostics_out=[])
    env = t["environment"]
    assert env["extra"] == {"k": "M", "l": ["m", 1]}, env
    assert env["seen"] == [{"x": "M"}], env          # the reset to [] is a fresh list
    assert [c["output"]["result"] for c in t["tool_calls"]] == [{"v": "N"}, {"v": "M"}], t["tool_calls"]
    assert [tn["content"] for tn in t["turns"]] == [[{"type": "text", "text": "n"}],
                                                    [{"type": "text", "text": "m"}]], t["turns"]


def test_748_review_fixes_accumulate_messages_and_dollar_boundary():
    # Ruled 2026-10-06:
    # - §8.2.5: an accumulate RHS that is neither null nor a number, or a
    #   counter holding a non-number, is an input failure (it was skipped
    #   silently); the state keeps its prior value; booleans are not numbers.
    # - an emit_tool_call with no id key reports no expression.
    # - a $ after a non-ASCII letter or a digit is not a reference start.
    # - f'x' reads as an undefined construct, not a string-form problem.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    for text in ("é$record.a", "x1$record.a"):
        assert D.expression_problem(text) == "does not parse as an expression", text
    assert D.expression_problem("f'x'").startswith("unsupported AST node: JoinedStr"), D.expression_problem("f'x'")
    # The form message is not replaced by a §3.4 domain
    # message (the structural check also runs the domain check), and load
    # and evaluation check in the same order.
    for text in ("1_0e400", "0x" + "f" * 40):
        assert "is not in the JSON number form" in D.expression_problem(text), text
        try:
            D._evaluate_expression(text, {})
        except D.ExpressionEvalError as e:
            assert "is not in the JSON number form" in str(e), e
        else:
            raise AssertionError(f"{text} evaluated")
    assert "outside the §3.4 domain" in D.expression_problem("1e400")
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    dsl["input"] = {"type": "json"}
    dsl["state"] = {"n": 1, "word": "w"}
    dsl["session"] = base["sessions"]["agent"]
    dsl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
    dsl["emit"] = [
        {"name": "acc", "when": {"field": "kind", "value": "a"},
         "do": [{"accumulate": {"state.n": "$record.v"}}, {"accumulate": {"state.word": 1}}]},
        {"name": "call", "when": {"field": "kind", "value": "c"},
         "do": [{"emit_tool_call": {"tool_name": "t"}}]}]
    dsl["finalize"] = {"computed": {"environment.n": "$state.n", "environment.word": "$state.word"}}
    recs = [{"kind": "a", "v": "x"}, {"kind": "a", "v": True}, {"kind": "a", "v": 2}, {"kind": "c"}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps(recs))
        diags = []
        t = D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"},
                  diagnostics_out=diags)
    assert t["environment"]["n"] == 3 and t["environment"]["word"] == "w", t["environment"]
    acc = [(d["record_index"], d["expression"], d["message"].split(": ", 1)[1]) for d in diags
           if d.get("field") in ("accumulate.state.n", "accumulate.state.word")]
    # Each message ends with its record.
    assert acc == [(0, "$record.v", "the value is a string, not a number; prior value kept (record 0)"),
                   (0, "1", "the counter holds a string, not a number; prior value kept (record 0)"),
                   (1, "$record.v", "the value is a boolean, not a number; prior value kept (record 1)"),
                   (1, "1", "the counter holds a string, not a number; prior value kept (record 1)"),
                   (2, "1", "the counter holds a string, not a number; prior value kept (record 2)")], acc   # JSON names
    [no_id] = [d for d in diags if d.get("field") == "emit_tool_call.id"]
    assert "expression" not in no_id and no_id["record_index"] == 3, no_id


def test_yaml_number_scalars_are_written_as_json_numbers():
    # Ruled 2026-10-06: JSON form everywhere; DSL rc3 §2, spec
    # 37b78f9d: a plain scalar the YAML 1.2 core schema reads as a number is
    # written as a JSON number with an optional leading '-'; 0x10, 0o7, +1,
    # .5, 1. and 007 are structural failures at load. Scalars the core schema
    # reads as strings (0b1, 1_000, quoted "0x10") are untouched, and .inf /
    # .nan fail the §3.4 domain check as before.
    D = _dr()
    for v in ("0x10", "0o7", "+1", ".5", "1.", "007", "-01", "+1.5e3"):
        try:
            D.load_adapter_yaml(f"a: {v}")
        except ValueError as e:
            assert f"number {v!r} is not written as a JSON number" in str(e), e
        else:
            raise AssertionError(f"{v} loaded")
    for v, want in (("0", 0), ("-0", 0), ("-1", -1), ("2.10", 2.1), ("1e5", 1e5), ("-1.5E-3", -0.0015),
                    ("0b1", "0b1"), ("1_000", "1_000"), ('"0x10"', "0x10")):
        assert D.load_adapter_yaml(f"a: {v}") == {"a": want}, v
    assert D.load_adapter_yaml("a: [1, 2.5]") == {"a": [1, 2.5]}
    try:
        D.load_adapter_yaml("a: [1, 0x2]")
    except ValueError as e:
        assert "'0x2'" in str(e), e
    else:
        raise AssertionError("0x2 in a flow sequence loaded")


def test_a_path_is_one_lexical_unit():
    # Ruled 2026-10-06; DSL rc3 §8.3, spec 3edc6b97: no whitespace
    # after the $ of a root, between root and first segment, between segments,
    # or inside [n] / ['name'] outside a string constant. Whitespace elsewhere
    # (around operators, between arguments, around a whole path) is allowed,
    # and a grouped base (($record).a) is grouping, not whitespace in a path.
    D = _dr()
    for text in ("$record . a", "$record .a", "$record. a", "$record ?. a", "$record [0]", "$record[ 0 ]",
                 "$record[ 'k']", "$record.a . b", "($record) .a", "$ record.a"):
        problem = D.expression_problem(text)
        assert problem is not None and ("not one lexical unit" in problem or "does not parse" in problem), \
            (text, problem)
    for text in ("$record.a", "$record?.a", "$record[0].x", "$record['a b']", "($record).a", "( $record ).a",
                 "$record.a + $record.b", "len( $record.a )", "coalesce($record.a, $record.b).x",
                 "$record['é']"):   # a .name segment is a §8.3 identifier, so .é is not; ['é'] reads that key
        assert D.expression_problem(text) is None, text
    def title(t):
        return D.dsl_expression_problems({"adapter": {"dsl_version": "0.1.0"},
                                          "session": {"static": {}, "derived": {"id": "x", "title": t}}, "emit": []})
    assert len(title("x-${record . a}")) == 1 and "not one lexical unit" in title("x-${record . a}")[0]
    assert title("x-${ record.a }") == []          # whitespace around a whole path in a template body
    try:
        D._evaluate_expression("$record . a", {"record": {"a": 1}})
    except D.ExpressionEvalError as e:
        assert "not one lexical unit" in str(e), e
    else:
        raise AssertionError("$record . a evaluated")


def test_path_lines_fstrings_and_dollar_left_context():
    # Ruled 2026-10-06:
    # - a bare \r inside a grouping crashed the path check (IndexError); lines
    #   are split as ast counts them now;
    # - whitespace and `)` of a grouping are skipped across lines too;
    # - the path check does not walk into f-strings, so f'...' reads as an
    #   undefined construct.
    # DSL rc3 §8.3 (spec 281925d0; ruled 2026-10-06): a $ after a Unicode
    # letter or digit, `_` or `.` is no reference marker: `price a$b` and
    # `a.$record` are literals, while in a string that is an expression by
    # another marker (or a ${...} body) such a $ is a structural failure.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    # These loaded before the 2026-10-09 ruling (whitespace is space and tab
    # only); they are refused now, still without a crash.
    for text in ("($record.a +\r$record.b)", "($record.a +\r\n$record.b)", "($record\n).a", "( $record\n ).a"):
        assert D.expression_problem(text) == "a line break outside a string constant (§8.3)", text
    assert D.expression_problem("f'{record .a}'").startswith("unsupported AST node: JoinedStr")
    for text in ("price a$b", "a.$record", "é$x", "x1$y"):
        assert not D.is_expression(text), text
    for text in ("not$record.a", "len(a.$record)", "x $record.a"):
        assert D.is_expression(text), text
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    dsl = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl["sessions"]["agent"]["derived"]["environment.price"] = "price a$b"
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "a.dsl.yaml"
        f.write_text(yaml.safe_dump(dsl, sort_keys=False))
        traces = D.run(f, fixtures / "synthetic-trace-pair.input.json", None, diagnostics_out=[], operator_override=_OP)
        assert {t["environment"].get("price") for t in traces if t["id"].startswith("agent-")} == {"price a$b"}
        for bad in ("not$record.a", "x-${not$record.a}"):
            dsl["sessions"]["agent"]["derived"]["environment.price"] = bad
            f.write_text(yaml.safe_dump(dsl, sort_keys=False))
            try:
                D.run(f, fixtures / "synthetic-trace-pair.input.json", None, diagnostics_out=[], operator_override=_OP)
            except SystemExit as e:
                assert "rejected at load" in str(e), e
            else:
                raise AssertionError(f"{bad} loaded")


def test_section_4_reports_name_the_container_in_container_mode():
    # DSL rc3 §4, spec 5fc0192d; ruled 2026-10-06:
    # in container mode the depth-truncation, string-repair and summary
    # reports name the container. The reader numbers top-level elements, so
    # its index is the container; the record is the one under record_path
    # the path lies in, none for a container-level value.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    dsl = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl["emit"][0]["do"] = [{"emit_turn": {"index": "$record.index", "role": "$record.role",
                                           "content": {"a": {"b": "$record.deep"}}}}]
    deep = "x"
    for _ in range(60):
        deep = [deep]
    good = [{"index": 0, "role": "user", "content": "p"}]
    text = json.dumps([{"id": "c0", "events": good},
                       {"id": "c1", "meta": "\ud800", "events": [{"index": 0, "role": "user", "deep": deep},
                                                                 {"index": 1, "role": "user", "content": "\ud800"}]}])
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(text)
        diags = []
        D.run(td / "a.dsl.yaml", td / "in.json", None, diagnostics_out=diags, operator_override=_OP)
        repairs = sorted(((d.get("container_index"), d.get("record_index"), d.get("path")) for d in diags
                          if d.get("reason") == "lone-surrogate"), key=repr)
        assert repairs == sorted([(1, None, "/meta"), (1, 1, "/events/1/content")], key=repr), repairs
        msgs = [d["message"] for d in diags if d.get("reason") == "lone-surrogate"]
        assert any(m.startswith("input record 1, container 1:") for m in msgs), msgs
        assert any(m.startswith("input container 1:") for m in msgs), msgs
        depth = [d for d in diags if "nested deeper than" in d.get("message", "") and d.get("field", "").startswith("/turns")]
        assert depth and all(d.get("container_index") == 1 and "(container 1)" in d["message"] for d in depth), depth
        # The summary past the reader's bound names the first container.
        many = [{"index": i, "role": "user", "content": "\ud800"} for i in range(1002)]
        (td / "in.json").write_text(json.dumps([{"id": "c0", "events": good}, {"id": "c1", "events": many}]))
        diags = []
        D.run(td / "a.dsl.yaml", td / "in.json", None, diagnostics_out=diags, operator_override=_OP)
        [summary] = [d for d in diags if "suppressed_count" in d]
        # It names the record too.
        assert summary["message"].endswith("the first at record 1000, container 1"), summary
        # Should-fix 1: a container the reader skips (nested past 512) keeps
        # its input position in every report, so the next one is container 1
        # for the reader and for its own derived field alike.
        deep = "[" * 600 + "]" * 600
        dsl["sessions"]["agent"]["derived"]["environment.n"] = "n-${container.obj}"
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text('[{"id": "c0", "x": ' + deep + '}, '
                                    + json.dumps({"id": "c1", "obj": {"a": 1}, "meta": "\ud800", "events": good}) + "]")
        diags = []
        D.run(td / "a.dsl.yaml", td / "in.json", None, diagnostics_out=diags, operator_override=_OP)
        reader = [d for d in diags if d.get("reason") == "lone-surrogate"]
        derived = [d for d in diags if d.get("field") == "session.derived.environment.n"]
        assert [d.get("container_index") for d in reader] == [1] and \
            [d.get("container_index") for d in derived] == [1], (reader, derived)


def test_jsonl_containers_and_container_first_are_named_by_input_position():
    # JSONL input carries the reader's positions
    # too, and $container_first names the first kept container's position.
    # The first line is damaged and skipped, so the kept containers are at
    # input positions 1 and 2 (their $container_index values are 0 and 1).
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    dsl = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl["input"]["type"] = "jsonl"
    dsl["sessions"]["agent"]["derived"].update({"environment.c": "c-${container.obj}",
                                                "environment.f": "f-${container_first.obj}",
                                                "environment.i": "i-${container_index}"})
    good = [{"index": 0, "role": "user", "content": "p"}]
    lines = ["{damaged", json.dumps({"id": "c1", "obj": {"a": 1}, "events": good}),
             json.dumps({"id": "c2", "obj": {"a": 2}, "events": good})]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.jsonl").write_text("\n".join(lines) + "\n")
        diags = []
        traces = D.run(td / "a.dsl.yaml", td / "in.jsonl", None, diagnostics_out=diags, operator_override=_OP)
    skipped = [d for d in diags if d.get("kind", "").startswith("input-") and d.get("loss") == "set"]
    assert [d.get("container_index") for d in skipped] == [0], skipped
    got = sorted((d["field"].rsplit(".", 1)[1], d.get("container_index")) for d in diags
                 if str(d.get("field", "")).startswith("session.derived.environment."))
    assert got == [("c", 1), ("c", 2), ("f", 1), ("f", 1)], got
    # The $container_index value itself counts kept containers only (§7).
    assert sorted(t["environment"]["i"] for t in traces if t["id"].startswith("agent-")) == ["i-0", "i-1"]


def test_a_record_path_that_is_not_a_list_is_an_input_failure_for_its_container():
    # Ruled 2026-10-07: a container whose record_path held an object
    # aborted the conversion (KeyError: 0), a number or boolean too
    # (TypeError), and a string was walked character by character. Such a
    # value is an input failure for that container (value-level per §15, naming it),
    # the container yields no records as a null does (§4.1.1), and the other
    # containers convert (§8.3: input never aborts a conversion).
    import tempfile  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    good = [{"index": 0, "role": "user", "content": "p"}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        for bad, typ in (({"k": 1}, "an object"), ("text", "a string"), (5, "a number"), (True, "a boolean")):
            (td / "in.json").write_text(json.dumps([{"id": "c0", "events": bad}, {"id": "c1", "events": good}]))
            diags = []
            traces = D.run(fixtures / "synthetic-trace-pair.dsl.yaml", td / "in.json", None, diagnostics_out=diags, operator_override=_OP)
            by_id = {t["id"]: t for t in traces}
            assert by_id["agent-c0"]["turns"] == [] and by_id["agent-c1"]["turns"], (bad, traces)
            [rp] = [d for d in diags if d.get("field") == "input.container.record_path"]
            assert (rp["loss"], rp["container_index"], rp["record_index"]) == ("value", 0, None), rp
            assert f"holds {typ}, not a list" in rp["message"], rp   # JSON names
            assert not D.set_level_loss(diags)   # §15: an input failure is value-level
        for fine in (None, []):   # null and an empty list stay an empty record set, unreported
            (td / "in.json").write_text(json.dumps([{"id": "c0", "events": fine}, {"id": "c1", "events": good}]))
            diags = []
            D.run(fixtures / "synthetic-trace-pair.dsl.yaml", td / "in.json", None, diagnostics_out=diags, operator_override=_OP)
            assert not [d for d in diags if d.get("field") == "input.container.record_path"], (fine, diags)


def test_an_integer_record_path_segment_indexes_a_list():
    # DSL rc3 §4.1.1 "integer segments are array-index lookups", but
    # the lookup returned null at any list, so record_path a.0.events read no
    # records from {"a": [{"events": [...]}]} and reported nothing. A segment
    # in the canonical form of a non-negative integer indexes a list; at an
    # object every segment is a key; anything else, or an index out of range,
    # is a missing key: null, an empty record set (§4.1.1).
    import tempfile  # noqa: PLC0415

    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    dsl = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    good = [{"index": 0, "role": "user", "content": "p"}]

    def turns(record_path, container):
        dsl["input"]["container"]["record_path"] = record_path
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
            (td / "in.json").write_text(json.dumps([{"id": "c0", **container}]))
            diags = []
            traces = D.run(td / "a.dsl.yaml", td / "in.json", None, diagnostics_out=diags, operator_override=_OP)
        assert not [d for d in diags if d.get("field") == "input.container.record_path"], diags
        return len({t["id"]: t for t in traces}["agent-c0"]["turns"])

    listed = {"a": [{"events": []}, {"events": good}]}
    assert turns("a.1.events", listed) == 1
    assert turns("a.0.events", listed) == 0
    assert turns("a.2.events", listed) == 0          # out of range: missing
    assert turns("a." + "9" * 5000 + ".events", listed) == 0   # past int()'s digit limit
    assert turns("a.1", {"a": [None, good]}) == 1     # the last segment too
    assert turns("0.events", {"0": {"events": good}}) == 1   # at an object, a key
    for odd in ("01", "-1", "+1", "1_0", " 1", "١"):   # not the canonical form
        assert turns(f"a.{odd}.events", {"a": [{"events": good}] * 11}) == 0, odd


def test_a_reader_finding_under_a_digit_like_key_names_no_record():
    # _input_place read the segment after
    # the record_path with str.isdigit() and int(), so a key such as "²"
    # (isdigit() true, int() raises) under an object at record_path made a
    # repaired lone surrogate abort the conversion (§8.3: input never does).
    # It reads a record index as record_path_get does now: canonical digits
    # only; any other key lies in no record.
    import tempfile  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "in.json"
        for key in ("²", "١", "01", "1" * 5000):
            p.write_text('[{"id": "c0", "events": {%s: "\\ud800"}}]' % json.dumps(key))
            diags = []
            D.run(fixtures / "synthetic-trace-pair.dsl.yaml", p, None, diagnostics_out=diags, operator_override=_OP)
            [rep] = [d for d in diags if "/events/" in str(d.get("path") or "")]
            assert (rep["container_index"], rep["record_index"]) == (0, None), (key, rep)
        p.write_text('[{"id": "c0", "events": [{"index": 0, "role": "user", "content": "p"}, {"x": "\\ud800"}]}]')
        diags = []
        D.run(fixtures / "synthetic-trace-pair.dsl.yaml", p, None, diagnostics_out=diags, operator_override=_OP)
        [rep] = [d for d in diags if "/events/" in str(d.get("path") or "")]
        assert (rep["container_index"], rep["record_index"]) == (0, 1), rep


def test_equality_is_type_strict_everywhere():
    # DSL rc3 §8.3, §8.1, §9.1: equality is type-strict (true != 1,
    # "1" != 1), numbers compare by value (1 == 1.0), objects and lists by the
    # same rule; ==, !=, predicate value: matching on records, containers and
    # blocks, count_where and the block type tests all use it.
    D = _dr()
    ctx = {"record": {"t": True, "f": False, "one": 1, "fl": 1.0, "s": "1", "l": [True], "o": {"a": 1}}}
    for expr, want in (("$record.t == 1", False), ("$record.f == 0", False), ("$record.t != 1", True),
                       ("$record.one == $record.fl", True), ("$record.s == 1", False),
                       ("$record.l == $record.l", True), ("$record.t == true", True), ("$record.missing == 0", False)):
        value, diags = _eval_collecting(expr, ctx)
        assert value is want and diags == [], (expr, value, diags)
    assert not D.json_equal([True], [1]) and D.json_equal({"a": [1, {"b": 2}]}, {"a": [1.0, {"b": 2.0}]})
    assert not D.json_equal({"a": 1}, {"a": 1, "b": None}) and not D.json_equal(None, 0)
    rec, blk, cont = {"x": 1, "y": True}, {"type": True, "k": 0}, {"c": 1}
    assert not D.predicate_matches({"field": "y", "value": 1}, rec, None, None)
    assert D.predicate_matches({"field": "x", "value": 1.0}, rec, None, None)
    assert not D.predicate_matches({"container_field": "c", "value": True}, rec, None, cont)
    assert not D.predicate_matches({"block_field": "k", "value": False}, rec, blk, None)
    assert not D.predicate_matches({"block_type": 1}, rec, blk, None)
    assert D.fn_count_where([{"v": True}, {"v": 1}, {"v": 1.0}], "v", 1) == 2
    assert D.fn_first_block_text_of_type([{"type": True, "text": "no"}], 1) is None


def test_blocks_join_of_type_skips_empty_payloads_and_fails_on_other_non_strings():
    # DSL rc3 §9.1: missing, null or empty payloads are skipped without a
    # separator; any other non-string payload, 0 and false included, is an
    # input failure; content that is not a list gives null.
    D = _dr()
    blocks = [{"type": "text", "text": "a"}, {"type": "text"}, {"type": "text", "text": None},
              {"type": "text", "text": ""}, {"type": "thinking", "thinking": "t"}, {"type": "text", "text": "b"}]
    assert D.fn_blocks_join_of_type(blocks, "text") == "a\nb"
    assert D.fn_blocks_join_of_type(blocks, "thinking") == "t"
    assert D.fn_blocks_join_of_type({"type": "text", "text": "a"}, "text") is None
    assert D.fn_blocks_join_of_type([{"type": "text", "text": ""}], "text") is None
    for bad in (0, False, 1, ["x"], {"k": 1}):
        value, diags = _eval_collecting("blocks_join_of_type($record.c, 'text')",
                                        {"record": {"c": [{"type": "text", "text": "a"},
                                                          {"type": "text", "text": bad}]}})
        assert value is None and len(diags) == 1, (bad, value, diags)


def test_every_runtime_structural_case_is_flagged_statically():
    # Each expression the evaluation tests treat as structural is decided by
    # its text, so the load-time pass must name it too (data-dependent cases
    # such as an instant reaching a value are not in this list).
    D = _dr()
    for expr in ("$foo", "$recrod.type", "$timing.ended_at", "parse_iso($record.a) - parse_iso($record.b)",
                 "coalesce($record.n, 1j)", "coalesce($record.n, b'x')", "len($record.a, $record.b)",
                 "not " * 3000 + "$record.a"):
        assert D.expression_problem(expr), expr
    assert D._template_bodies("agent-${contianer_index}") == ["$contianer_index"]
    assert D.expression_problem("$contianer_index")
    # Through a DSL: a bad body inside a template, and inside a placeholder's
    # trace-id portion, are found by the load-time pass.
    for tmpl in ("tu-${len($record.a, 1)}", "${trace:agent-${record.a.upper()}:content_hash}"):
        d = _lineage_dsl({"target_content_hash": tmpl}) if tmpl.startswith("${trace:") \
            else _lineage_dsl(**{"emit/0/do/0": {"set": {"state.x": tmpl}}})
        assert len(D.dsl_expression_problems(d)) == 1, (tmpl, D.dsl_expression_problems(d))


def test_a_computed_float_beyond_the_domain_is_an_input_failure():
    # minitrace-spec §3.4 by value: 1e16 is outside whatever its spelling, so
    # a float sum past 2^53-1 is an input failure like an integer one.
    for expr in ("$record.a + $record.a", "$record.a - $record.b"):
        value, diags = _eval_collecting(expr, {"record": {"a": 6e15, "b": -6e15}})
        assert value is None and len(diags) == 1, (expr, value, diags)
    value, diags = _eval_collecting("$record.a + 1.5", {"record": {"a": 9007199254740989}})
    assert value == 9007199254740990.5 and diags == [], (value, diags)


def test_dsl_numbers_outside_the_domain_are_rejected_at_load():
    # DSL spec rc3 §2: any number anywhere in the DSL file, and any number
    # constant in an expression, outside the §3.4 domain is structural.
    D = _dr()
    for name, d in (("static int", _lineage_dsl(**{"sessions/agent/static/metrics.n": 2**53})),
                    ("static 1e16", _lineage_dsl(**{"sessions/agent/static/metrics.n": 1e16})),
                    ("passthrough inf", _lineage_dsl(**{"sessions/agent/passthrough/x": float("inf")})),
                    ("nan in a list", _lineage_dsl(**{"sessions/agent/static/l": [1, float("nan")]})),
                    ("1e400", _lineage_dsl(**{"sessions/agent/static/x": float("1e400")}))):
        assert len(D.dsl_number_problems(d)) == 1, (name, D.dsl_number_problems(d))
    for expr in ("coalesce($record.a, 9007199254740992)", "$record.a + 1e16", "coalesce($record.a, 1e400)"):
        assert "outside the §3.4 domain" in (D.expression_problem(expr) or ""), expr
    ok = _lineage_dsl(**{"sessions/agent/static/metrics.n": 2**53 - 1, "sessions/agent/static/f": -1e15,
                          "sessions/agent/static/z": -0.0, "sessions/agent/static/b": True})
    assert D.dsl_number_problems(ok) == []
    assert D.expression_problem("coalesce($record.a, 9007199254740991)") is None


def test_an_input_number_beyond_the_domain_reaches_the_trace_as_its_source_text():
    # DSL spec §4 end to end: 1e16 in a record is read as "1e16", carried as a
    # string, and the trace seals (minitrace-spec §3.4); no record is skipped.
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    D = _dr()
    dsl = _DIAG_DSL.replace('set: {state.x: "$record.n + 1"}', 'set: {session.metrics.n: "$record.n"}')
    assert dsl != _DIAG_DSL
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(dsl)
        (td / "in.jsonl").write_text('{"sid": "s", "n": 1e16}\n')
        collected: list = []
        with contextlib.redirect_stderr(io.StringIO()):
            t = D.run(td / "a.dsl.yaml", td / "in.jsonl", td / "o.json", operator_override=_OP,
                      diagnostics_out=collected)
    import rfc8785  # noqa: PLC0415
    assert t["metrics"]["n"] == "1e16", t["metrics"]
    rfc8785.dumps(t)               # seals: no number outside the domain reached it
    assert not [d for d in collected if d.get("loss") == "set"], collected


# Attack fixtures the load-time passes reject by design, with the reason.
_EXPECTED_LOAD_REJECTIONS = {
    "_a15.dsl.yaml": "outside the §3.4 domain",     # r2 F11: a DSL constant beyond ±(2^53-1)
    "_a16.dsl.yaml": "parse_iso",                   # r2 F12: parse_iso, no longer a function (§9.1)
}


def test_the_load_time_expression_pass_accepts_every_dsl_in_the_tree():
    D = _dr()
    n, problems, rejected = 0, [], {}
    for p in sorted(_ADAPTERS.rglob("*.dsl.yaml")):
        d = D.load_adapter_yaml(p.read_text())
        n += sum(len(D._template_bodies(v)) if "${" in v else int(D.is_expression(v))
                 for _, v in D.evaluated_values(d))
        found = D.dsl_expression_problems(d) + D.dsl_number_problems(d)
        if p.name in _EXPECTED_LOAD_REJECTIONS:
            rejected[p.name] = found
        else:
            problems += [f"{p.name}: {x}" for x in found]
    assert n >= 1000, n            # the walk reached the corpus (the release tree holds fewer DSLs than dev)
    assert problems == [], problems[:5]
    # The fixtures live under fuzz/, which the release tree does not carry; where
    # fuzz/ exists, every one of them must still be there.
    present = {p.name for p in _ADAPTERS.rglob("*.dsl.yaml")}
    if (_ADAPTERS / "fuzz").is_dir():
        assert set(_EXPECTED_LOAD_REJECTIONS) <= present, set(_EXPECTED_LOAD_REJECTIONS) - present
    for name, why in _EXPECTED_LOAD_REJECTIONS.items():   # each still rejected, for its reason
        if name in present:
            assert rejected.get(name) and all(why in x for x in rejected[name]), (name, rejected.get(name))


def test_a_structural_failure_in_a_rule_that_never_fires_is_rejected_before_input():
    import tempfile  # noqa: PLC0415
    D = _dr()
    dsl = _DIAG_DSL.replace('set: {state.x: "$record.n + 1"}', 'set: {state.x: "len($record.n, 1)"}')
    assert dsl != _DIAG_DSL
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(dsl)
        try:
            D.run(td / "a.dsl.yaml", td / "never-read.jsonl", None, operator_override=_OP)
        except SystemExit as e:
            assert "rejected at load" in str(e) and "called with 2 argument(s)" in str(e), e
            return
    raise AssertionError("an adapter with a structural failure loaded")


def test_unknown_dollar_root_is_structural_unbound_context_variable_is_null():
    for expr in ("$foo", "$recrod.type", "agent-${contianer_index}", "Use ${HOME}", "$timing.ended_at"):
        assert _structural(expr, {"record": {"type": "x"}}, why="is not a context variable"), expr
    D = _dr()
    assert D.evaluate("$block.id", {"record": {}}) is None   # §8.4 variable, not bound here


def test_expression_outside_template_parts_and_unclosed_template_are_structural():
    for expr in ("coalesce($record.a, 'x-${record.b}')", "$record.a ? 'y' : 'n-${record.b}'",
                 "sha256('${record.id}')", "x ${record.type"):
        why = "unclosed ${" if expr.startswith("x ") else "expression outside its ${...} parts"
        assert _structural(expr, {"record": {"a": None, "b": "B", "id": "X", "type": "t"}}, why=why), expr
    D = _dr()
    assert D.evaluate("tu-${record.b}", {"record": {"b": "B"}}) == "tu-B"


def test_parse_iso_is_an_undefined_function_whatever_the_data():
    # rc3 §9.1 replaced parse_iso and the instant type with iso_normalise;
    # parse_iso is now an undefined function, structural on any data.
    for rec in ({"a": None, "b": None}, {"a": "garbage", "b": "x"},
                {"a": "2026-01-01T00:00:00Z", "b": "2026-01-01T00:00:01Z"}):
        assert _structural("parse_iso($record.a) - parse_iso($record.b)", {"record": rec},
                           why="does not define"), rec
        assert _structural("parse_iso($record.a)", {"record": rec}, why="does not define"), rec


def test_non_json_values_are_structural():
    assert _structural("coalesce($record.n, 1j)", {"record": {}}, why="complex is not a JSON value")
    assert _structural("coalesce($record.n, b'x')", {"record": {}}, why="bytes is not a JSON value")
    assert _structural(float("nan"), why="a non-finite number is not a JSON value")


def test_rewrites_do_not_touch_quoted_strings():
    D = _dr()
    ctx = {"record": {"a": True, "b": "q?"}}
    assert D.evaluate("$record.a ? 'price $5' : 'n'", ctx) == "price $5"
    assert D.evaluate("$record.b == 'q?' ? 'yes' : 'no'", ctx) == "yes"
    assert D.evaluate("coalesce($record.n, 'is it? yes: no')", ctx) == "is it? yes: no"


def test_arity_and_deep_nesting_are_structural():
    assert _structural("len($record.a, $record.b)", {"record": {"a": [], "b": []}},
                       why="len() called with 2 argument(s)")
    assert _structural("not " * 3000 + "$record.a", {"record": {"a": 1}}, why="nested too deeply")


def test_min_max_require_a_list():
    for expr in ("min($record.s)", "max($record.d)"):
        value, diags = _eval_collecting(expr, {"record": {"s": "xyz", "d": {"k": 1}}})
        assert value is None and len(diags) == 1, (expr, value, diags)


def test_whole_value_copy_is_iterative_and_unaliased():
    # deepcopy recursed and raised on data nested a few hundred levels deep;
    # the copy must also share no sub-object.
    D = _dr()
    deep: list = [1]
    for _ in range(3000):
        deep = [deep]
    src = {"deep": deep, "blocks": [{"x": 1}]}
    got = D.evaluate("$record", {"record": src})
    assert got == src
    assert got["blocks"] is not src["blocks"] and got["blocks"][0] is not src["blocks"][0]


def test_literal_null_hash_edge_is_kept():
    D = _dr()
    trace: dict = {}
    # The null literal an adapter writes (§13.1), not a reference that
    # happens to evaluate to null.
    D._apply_actions([{"emit_lineage": {"target_trace_id": "$record.t", "target_content_hash": None}}],
                     {"record": {"t": "peer"}}, trace, {}, {}, [])
    assert trace["lineage"] == [{"target_trace_id": "peer", "target_content_hash": None}], trace


# --- load-time templates, back-fill ids, offsets, diagnostics, scaling, precision -

def test_mixed_template_rejected_at_load():
    dsl = _MT_DSL.replace('metrics.total_tokens: "$state.total"',
                          '''metrics.total_tokens: "coalesce($state.total, 'n-${state.total}')"''')
    try:
        _run_dsl(dsl, [{"trace_id": "a", "events": []}])
    except SystemExit as e:
        assert "§8.3" in str(e), e
        return
    raise AssertionError("adapter with a mixed expression/template loaded")


def test_back_fill_tolerates_data_typed_ids():
    import cross_trace as X  # noqa: PLC0415
    ref = X.make_cross_trace_reference
    mixed = [{"id": "a", "lineage": [{"target_trace_id": "b", "target_content_hash": ref("b")}]},
             {"id": 5, "lineage": []}, {"id": "b", "lineage": []}]
    kept = X.back_fill_cross_trace_references(mixed, [])
    # R5: the int id is dropped, not converted; the rest resolves.
    assert [t["id"] for t in kept] == ["a", "b"]
    assert all(t["content_hash"].startswith("sha256:") for t in kept)
    edge_int = [{"id": "p", "lineage": [{"target_trace_id": 7, "target_content_hash": ref("7")}]},
                {"id": "7", "lineage": []}]
    diags: list = []
    X.back_fill_cross_trace_references(edge_int, diags)
    assert edge_int[0]["lineage"] == [] and len(diags) == 1


def test_back_fill_drops_non_scalar_and_non_string_ids_with_a_diagnostic():
    # A list or object id once raised (TypeError, later ValueError); under R5
    # every unusable id is dropped and reported, and the rest is kept.
    import cross_trace as X  # noqa: PLC0415
    for bad in ([1], {"a": 1}, None, True, ""):
        diags: list = []
        kept = X.back_fill_cross_trace_references(
            [{"id": "a", "lineage": []}, {"id": bad, "lineage": []}], diags)
        assert [t["id"] for t in kept] == ["a"], (bad, kept)
        assert [d.get("kind") for d in diags] == ["trace-dropped"], (bad, diags)


def test_back_fill_omits_exactly_the_in_cycle_edges_of_several_components():
    # R3 over a graph with two cycles, a self-loop, and edges into cycles from
    # outside: every in-cycle edge is omitted, every other edge resolves to
    # the hash its target ends with.
    import cross_trace as X  # noqa: PLC0415
    ref = X.make_cross_trace_reference
    edges = {"A": ["B"], "B": ["A"], "C": ["A"], "D": ["E"], "E": ["F"], "F": ["D"],
             "G": ["D", "C"], "H": ["H"], "I": []}
    traces = [{"id": tid, "lineage": [{"target_trace_id": to, "target_content_hash": ref(to)}
                                      for to in tos]} for tid, tos in edges.items()]
    diags: list = []
    by = {t["id"]: t for t in X.back_fill_cross_trace_references(traces, diags)}
    in_cycle = [d for d in diags if "inside a reference cycle" in d.get("message", "")]
    assert len(in_cycle) == 6 and len(diags) == 6, diags   # A-B, B-A, D-E, E-F, F-D, H-H
    for tid in ("A", "B", "D", "E", "F", "H", "I"):
        assert by[tid]["lineage"] == [], (tid, by[tid]["lineage"])
    assert [(e["target_trace_id"], e["target_content_hash"]) for e in by["C"]["lineage"]] \
        == [("A", by["A"]["content_hash"])]
    assert [(e["target_trace_id"], e["target_content_hash"]) for e in by["G"]["lineage"]] \
        == [("D", by["D"]["content_hash"]), ("C", by["C"]["content_hash"])]


def test_nested_json_reports_keys_that_collide_after_repair():
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        v = source_reader.nested_json_loads('{"\\ud83d": 1, "\\ufffd": 2}')
    assert v == {"�": 2}, v
    assert "1 key(s) that collided after surrogate repair" in err.getvalue(), err.getvalue()


def test_offset_minutes_above_59_rejected():
    value, diags = _eval_collecting("duration_seconds($record.a, $record.b)",
                                    {"record": {"a": "2026-01-01T00:00:00Z", "b": "2026-01-01T00:00:00+05:75"}})
    assert value is None and diags
    assert timestamps.to_spec_utc("2026-01-01T00:00:00+05:75") is None


def test_omission_diagnostic_is_bounded():
    import cross_trace as X  # noqa: PLC0415
    long_id = "x" * 100_000
    t = {"id": "p", "lineage": [{"target_trace_id": long_id,
                                 "target_content_hash": X.make_cross_trace_reference(long_id)}]}
    diags: list = []
    X.back_fill_cross_trace_references([t], diags)
    assert diags and all(len(d["message"]) < 400 for d in diags), [len(d["message"]) for d in diags]


def test_back_fill_scans_each_trace_a_bounded_number_of_times():
    # Dependencies are computed once and refreshed only for a trace whose
    # edges change; rescanning every trace on every pass made a 2,000-trace
    # chain take about 7 s. Counted, not timed, so load cannot flake it.
    import cross_trace as X  # noqa: PLC0415
    orig, calls = X._placeholders_in_trace, [0]

    def counting(t):
        calls[0] += 1
        return orig(t)

    for n in (500, 2000):
        chain = [{"id": f"t{i:05d}", "lineage": [{"target_trace_id": f"t{i+1:05d}",
                  "target_content_hash": X.make_cross_trace_reference(f"t{i+1:05d}")}]}
                 for i in range(n - 1)]
        chain.append({"id": f"t{n-1:05d}", "lineage": []})
        calls[0] = 0
        X._placeholders_in_trace = counting
        try:
            X.back_fill_cross_trace_references(chain, [])
        finally:
            X._placeholders_in_trace = orig
        # Once for dependencies, once to resolve; the lower bound keeps the
        # count from passing because the scans stopped going through the hook.
        assert n <= calls[0] <= 2 * n, (n, calls[0])


def test_iso_from_ms_is_exact_for_integers():
    D = _dr()
    assert D.fn_iso_from_ms(253402300799999) == "9999-12-31T23:59:59.999000Z"
    assert D.fn_iso_from_ms(1700000000123) == "2023-11-14T22:13:20.123000Z"


def test_nested_json_reports_lost_keys():
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        v = source_reader.nested_json_loads('{"k": "first", "k": "second"}')
    assert v == {"k": "second"} and "duplicate key" in err.getvalue(), err.getvalue()


def test_claude_ai_nulls_and_counts_invalid_but_well_shaped_timestamps():
    # Ruled 2026-10-29: written as null and counted, never passed through.
    import preprocess_claude_ai as P  # noqa: PLC0415
    counts: dict = {}
    out = P.preprocess_conversation({"uuid": "c", "created_at": "2026-13-01T00:00:00.000000Z",
                                     "updated_at": 7, "chat_messages": []}, counts)
    assert counts.get("nulled") == 2, counts
    assert out["created_at"] is None and out["updated_at"] is None, out


# --- deep nesting aborted the run in the reader or at sealing ---------

def _nested_text(depth):
    return "[" * depth + "0" + "]" * depth


def test_nesting_bound_is_exact():
    import source_reader as SR  # noqa: PLC0415
    limit = SR.DEFAULT_LIMITS.max_depth
    assert limit == 64
    assert not SR.nesting_exceeds(json.loads(_nested_text(limit)), limit)
    assert SR.nesting_exceeds(json.loads(_nested_text(limit + 1)), limit)
    assert not SR.nesting_exceeds("x", limit) and not SR.nesting_exceeds({}, 1)


_DEEP_DSL = """
adapter: {name: t, harness_version: "1", dsl_version: "0.1.0", source_format: x, converter_version: x, produces_schema: "0.3.0"}
input: {type: %s, encoding: utf-8, container: {record_path: events}}
sessions:
  agent:
    static: {schema_version: "0.3.0", trace_type: agent-session}
    derived: {id: "$container.trace_id"}
emit:
  - target: agent
    name: note
    when: {has: v}
    do:
      - emit_annotation: {type: x, value: {wrapped: {in: {more: "$record.v"}}}}
"""


def _run_raw(dsl_text, input_text, suffix):
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    D = _dr()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(dsl_text)
        (td / f"in.{suffix}").write_text(input_text)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = D.run(td / "a.dsl.yaml", td / f"in.{suffix}", None, operator_override=_OP)
    return out, err.getvalue()


def test_a_json_container_beyond_the_read_bound_is_skipped_not_fatal():
    # Before any bound, about 1000 levels made the recursive surrogate repair
    # raise out of load_input, and just under that, sealing overflowed. Now a
    # container deeper than 512 is unreadable and skipped; one within 512 is
    # kept with its too-deep value truncated.
    for depth, kept in ((65, ["good", "deep"]), (500, ["good", "deep"]), (985, ["good"]), (1000, ["good"])):
        text = ('[{"trace_id": "good", "events": [{"v": 1}]}, '
                '{"trace_id": "deep", "events": [{"v": ' + _nested_text(depth) + '}]}]')
        out, err = _run_raw(_DEEP_DSL % "json", text, "json")
        assert [t["id"] for t in out] == kept, (depth, out)
        assert "nesting-too-deep" in err, (depth, err)


def test_a_jsonl_line_beyond_the_read_bound_is_skipped_not_fatal():
    # Before any bound, a JSONL line just under the interpreter's own limit
    # passed the reader and then overflowed the recursive serialiser at
    # sealing; a line deeper than 512 is now skipped as one record.
    dsl = """
adapter: {name: t, harness_version: "1", dsl_version: "0.1.0", source_format: x, converter_version: x, produces_schema: "0.3.0"}
input: {type: jsonl, encoding: utf-8}
session:
  static: {schema_version: "0.3.0", trace_type: agent-session, id: t}
emit:
  - name: note
    when: {has: v}
    do:
      - emit_annotation: {type: x, value: {wrapped: {in: {more: "$record.v"}}}}
"""
    text = '{"v": 1}\n{"v": ' + _nested_text(985) + '}\n'
    out, err = _run_raw(dsl, text, "jsonl")
    assert out is not None and len(out["annotations"]) == 1, out
    assert "nesting-too-deep" in err, err
    import cross_trace  # noqa: PLC0415
    assert cross_trace.compute_content_hash(out).startswith("sha256:")  # the kept trace seals


def test_records_are_truncated_at_level_64_and_read_up_to_depth_512():
    # Both bounds are exact: a record within 64 is unchanged, one within 512
    # is kept with each array or object at level 64 replaced by null and
    # reported, one deeper than 512 is skipped.
    import tempfile  # noqa: PLC0415
    import source_reader as SR  # noqa: PLC0415
    assert (SR.MAX_DEPTH, SR.READ_DEPTH) == (64, 512)
    with tempfile.TemporaryDirectory() as td:
        for name, wrap in (("in.jsonl", lambda recs: "".join(r + "\n" for r in recs)),
                           ("in.json", lambda recs: "[" + ", ".join(recs) + "]")):
            p = pathlib.Path(td) / name
            # {"v": X} is one level deeper than X: depths 64, 65, 512, 513.
            p.write_text(wrap(['{"v": ' + _nested_text(d - 1) + '}' for d in (64, 65, 512, 513)]))
            read = (SR.read_jsonl if name.endswith("jsonl") else SR.read_json)(p)
            assert read.record_indices == [0, 1, 2] and read.record_count_skipped == 1, (name, read.record_indices)
            assert read.records[0] == {"v": json.loads(_nested_text(63))}, name   # unchanged
            assert all(not SR.nesting_exceeds(r, 64) for r in read.records), name
            truncated = [f for f in read.findings if f["kind"] == "truncated"]
            assert [f["record_index"] for f in truncated] == [1, 2], (name, truncated)
            assert truncated[0]["path"] == "/v" + "/0" * 63, (name, truncated[0]["path"])


def test_a_trace_nested_past_64_by_the_dsl_is_truncated_not_dropped():
    # The record is within the limit; the DSL nests its value further. The
    # trace is kept with the too-deep value replaced, so its edge resolves.
    dsl = _DEEP_DSL % "json" + """
  - target: agent
    name: lin
    when: {has: to}
    do:
      - emit_lineage:
          relation: derived_from
          target_trace_id: "$record.to"
          target_content_hash: "${trace:${record.to}:content_hash}"
"""
    text = json.dumps([{"trace_id": "deep", "events": [{"v": json.loads(_nested_text(60))}]},
                       {"trace_id": "good", "events": [{"v": 1}, {"to": "deep"}]}])
    out, err = _run_raw(dsl, text, "json")
    import source_reader as SR  # noqa: PLC0415
    by = {t["id"]: t for t in out}
    assert sorted(by) == ["deep", "good"], out
    assert not SR.nesting_exceeds(by["deep"], 64) and SR.nesting_exceeds(by["deep"], 63)
    assert by["good"]["lineage"][0]["target_content_hash"] == by["deep"]["content_hash"]
    assert "replaced with null" in err and "R4" not in err, err


def test_json_parse_truncates_within_the_read_bound_and_refuses_beyond_it():
    import source_reader as SR  # noqa: PLC0415
    value, diags = _eval_collecting("json_parse($record.s)", {"record": {"s": _nested_text(65)}})
    assert value is not None and not SR.nesting_exceeds(value, 64), value
    assert len(diags) == 1 and "replaced with null" in diags[0]["message"], diags
    value, diags = _eval_collecting("json_parse($record.s)", {"record": {"s": _nested_text(513)}})
    SR.take_nested_refused()   # reset the pre-processors' counter this call increments
    assert value is None and any("json_parse" in str(d) for d in diags), (value, diags)
    value, diags = _eval_collecting("json_parse($record.s)", {"record": {"s": "{not json"}})
    assert value is None and not diags, diags   # malformed JSON stays a plain null


def _eval_at_site(expr, ctx):
    """_eval_collecting at a record and field, as the record loop sets them,
    with stderr captured."""
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    D = _dr()
    err = io.StringIO()
    tok = D._EVAL_SITE.set({"record_index": 3, "field": "emit.x"})
    try:
        with contextlib.redirect_stderr(err):
            value, diags = _eval_collecting(expr, ctx)
    finally:
        D._EVAL_SITE.reset(tok)
    return value, diags, err.getvalue()


def test_json_parse_reports_name_the_expression_and_the_input():
    # Ruled 2026-10-18: a §4 finding inside a json_parse value
    # names the field, the expression and the input (§8.3), besides the path.
    expr = "json_parse($record.s)"
    for s, what in ((_nested_text(65), "replaced with null"),
                    ('{"k": "\\ud800"}', "U+FFFD")):
        value, diags, _ = _eval_at_site(expr, {"record": {"s": s}})
        assert len(diags) == 1 and what in diags[0]["message"], diags
        d = diags[0]
        assert d["expression"] == expr and d["field"] == "emit.x" and d["record_index"] == 3, d
    # In a template the expression named is the whole template, as for any
    # input failure inside one.
    tmpl = "n=${len(json_parse($record.s))}"
    value, diags, _ = _eval_at_site(tmpl, {"record": {"s": '["\\ud800"]'}})
    assert value == "n=1" and len(diags) == 1 and diags[0]["expression"] == tmpl, (value, diags)


def test_json_parse_duplicate_and_colliding_keys_are_reported_in_the_diagnostics():
    # Ruled 2026-10-18, fold in: DSL rc3 §4 "Objects in the input"
    # and "Strings in the input": a duplicate key, and a member name that
    # collides with another after repair, in a json_parse value are reported,
    # named as a depth replacement is; before, only an unplaced stderr line.
    expr = "json_parse($record.s)"
    value, diags, err = _eval_at_site(expr, {"record": {"s": '{"k": 1, "k": 2, "j": 0, "j": 1}'}})
    assert value == {"k": 2, "j": 1}, value
    assert len(diags) == 1, diags
    d = diags[0]
    assert d["kind"] == "input-failure" and d["expression"] == expr and d["record_index"] == 3, d
    assert "2 duplicate key(s)" in d["message"] and "emit.x" in d["message"], d
    assert "duplicate" not in err, err   # reported once, in the diagnostics
    value, diags, err = _eval_at_site(expr, {"record": {"s": '{"a": {"\\ud800": 1, "\\udbff": 2}}'}})
    assert value == {"a": {"�": 2}}, value
    collided = [d for d in diags if "collided" in d["message"]]
    assert len(collided) == 1 and "/a/�" in collided[0]["message"], diags
    assert collided[0]["expression"] == expr, collided
    assert "collided" not in err, err
    # A report made outside an evaluation carries no stale expression.
    _, diags, _ = _eval_at_site("json_parse($record.s)", {"record": {"s": "{}"}})
    D = _dr()
    sink: list = []
    tok = D._EVAL_DIAGNOSTICS.set(sink)
    try:
        D._report_truncation("probe")
    finally:
        D._EVAL_DIAGNOSTICS.reset(tok)
    assert "expression" not in sink[0], sink


def test_nested_json_value_beyond_the_read_bound_is_refused_with_a_count():
    import source_reader as SR  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    SR.take_nested_refused()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        v = SR.nested_json_loads(_nested_text(SR.READ_DEPTH + 1), default="D")
        w = SR.nested_json_loads(_nested_text(SR.MAX_DEPTH + 1), default="D")
    assert v == "D" and SR.take_nested_refused() == 1 and "deeper than 512" in err.getvalue()
    assert w != "D" and not SR.nesting_exceeds(w, 64) and "truncated" in err.getvalue()


# --- input failures past the 20th were reported nowhere ------------------

_DIAG_DSL = """
adapter: {name: t, harness_version: "1", dsl_version: "0.1.0", source_format: x, converter_version: x, produces_schema: "0.3.0"}
input: {type: jsonl, encoding: utf-8}
session:
  static: {schema_version: "0.3.0", trace_type: agent-session}
  derived: {id: "$record_first.sid"}
emit:
  - name: bad
    when: {has: n}
    do:
      - set: {state.x: "$record.n + 1"}
"""


def _diag_file(path):
    """The diagnostics list of a sibling file, after checking its §15 shape."""
    doc = json.loads(pathlib.Path(path).read_text())
    assert isinstance(doc, dict) and isinstance(doc.get("set_level_loss"), bool), doc
    return doc["diagnostics"]


def _diag_run(lines, with_output, sid="s"):
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    D = _dr()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(_DIAG_DSL)
        (td / "in.jsonl").write_text("".join(json.dumps({"sid": sid, "n": v}) + "\n" for v in lines))
        out = td / "out.json" if with_output else None
        err, collected = io.StringIO(), []
        with contextlib.redirect_stderr(err):
            D.run(td / "a.dsl.yaml", td / "in.jsonl", out, operator_override=_OP, diagnostics_out=collected)
        sibling = td / "out.json.diagnostics.json"
        written = _diag_file(sibling) if sibling.exists() else None
    return err.getvalue(), collected, written


def test_every_input_failure_reaches_the_diagnostics_file():
    # 25 records whose `n` is a string: 25 input failures, more than the console shows.
    err, collected, written = _diag_run(["x"] * 25, with_output=True)
    failures = [d for d in written if d.get("kind") == "input-failure"]
    assert len(failures) == 25 and len(collected) == len(written), (len(failures), len(collected))
    assert err.count("input failure") == 20 and "5 more in" in err and "diagnostics.json" in err, err


def test_without_an_output_file_the_console_shows_every_diagnostic():
    err, collected, written = _diag_run(["x"] * 25, with_output=False)
    assert written is None and err.count("input failure") == 25 and "more in" not in err, err


def test_diagnostics_file_is_written_even_when_the_trace_is_dropped():
    err, collected, written = _diag_run([1], with_output=True, sid=None)
    assert any("(R5)" in d["message"] for d in written), written


def test_directory_output_gets_a_conversion_diagnostics_file():
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    D = _dr()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(_ID_DSL % (_MULTI % "$container.trace_id"))
        (td / "in.json").write_text(json.dumps([{"trace_id": "a", "events": []}, {"trace_id": None, "events": []}]))
        (td / "out").mkdir()   # directory output: an existing directory
        with contextlib.redirect_stderr(io.StringIO()):
            D.run(td / "a.dsl.yaml", td / "in.json", td / "out", operator_override=_OP)
        names = sorted(p.name for p in (td / "out").iterdir())
        written = _diag_file(td / "out" / "conversion.diagnostics.json")
    assert names == ["a.minitrace.json", "conversion.diagnostics.json"], names
    assert len(written) == 1 and "(R5)" in written[0]["message"], written


def test_directory_output_without_a_container_is_refused_at_entry():
    # Ruled 2026-10-19: directory output is a container-mode form. Without
    # input.container it crashed (IsADirectoryError, a traceback) for any
    # trace id; it is now a refused output location (§15), exit 4, refused
    # before the input is read, nothing written or removed.
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    runner = pathlib.Path(__file__).resolve().parent.parent / "dsl_runner.py"
    dsl = {"adapter": {"name": "probe", "harness_version": "0", "dsl_version": "0.1.0", "source_format": "test",
                       "converter_version": "test", "produces_schema": "0.3.0"},
           "input": {"type": "json"}, "session": {"static": {}, "derived": {"id": "good"}},
           "emit": [{"name": "t", "when": {"has": "content"},
                     "do": [{"emit_turn": {"index": 0, "content": "$record.content"}}]}]}
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i, out = td / "a.dsl.yaml", td / "in.json", td / "out"
        a.write_text(yaml.safe_dump(dsl, sort_keys=False))
        i.write_text(json.dumps([{"index": 0, "role": "user", "content": "p"}]))
        out.mkdir()
        (out / "notes.txt").write_text("x")
        r = subprocess.run([sys.executable, str(runner), str(a), str(i), "--output", str(out),
                            "--operator-type", "scripted"], capture_output=True, text=True, timeout=120)
        assert r.returncode == 4 and "Traceback" not in r.stderr, (r.returncode, r.stderr[-400:])
        assert "needs input.container" in r.stderr and "refused before writing" in r.stderr, r.stderr[-400:]
        assert sorted(p.name for p in out.iterdir()) == ["notes.txt"]
        # With --coverage, an earlier coverage file
        # outside the directory is cleared first, as on any refusal, so the
        # message does not claim that nothing was removed.
        cov = td / "cov.json"
        cov.write_text("EARLIER")
        r = subprocess.run([sys.executable, str(runner), str(a), str(i), "--output", str(out),
                            "--coverage", str(cov), "--operator-type", "scripted"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 4 and "needs input.container" in r.stderr, (r.returncode, r.stderr[-400:])
        assert "nothing removed" not in r.stderr and not cov.exists(), (r.stderr[-400:], cov.exists())
        assert sorted(p.name for p in out.iterdir()) == ["notes.txt"]
        try:   # before the input is read: a missing input is not reached
            D.run(a, td / "absent.json", out, operator_override=_OP)
            raise AssertionError("ran")
        except D.OutputRefused as e:
            assert "needs input.container" in str(e), e
        # The same adapter writes one file as before.
        r = subprocess.run([sys.executable, str(runner), str(a), str(i), "--output", str(td / "t.json"),
                            "--operator-type", "scripted"], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and json.loads((td / "t.json").read_text())["id"] == "good", r.stderr[-400:]


def test_directory_output_drops_a_trace_it_cannot_name_instead_of_aborting():
    # DSL rc3 §15 Writing output: an id that is not usable as
    # a file name, or whose file name collides with another's (letter case
    # ignored), is not written, a set-level loss, reported; the run goes on.
    # None of the colliding traces is written. The check runs after R5 and
    # before resolution (ruled 2026-10-19), so a judge's edge to a dropped
    # agent degrades under R4 and a duplicated id stays R5's report.
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "synthetic-trace-pair.dsl.yaml"
    dsl = yaml.safe_load(fixture.read_text())
    dsl["sessions"]["agent"]["derived"]["id"] = "${container.aid}"
    lineage = dsl["emit"][1]["do"][0]["emit_lineage"]
    lineage["target_trace_id"] = "${container.aid}"
    lineage["target_content_hash"] = "${trace:${container.aid}:content_hash}"
    aids = {"j1": "../x", "j2": "Dup", "j3": "dup", "j4": "ok", "j5": "../y", "j6": "../y", "j7": "CON"}
    data = [{"id": j, "aid": a, "events": [{"index": 0, "role": "user", "content": "p"}]} for j, a in aids.items()]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps(data))
        (td / "out").mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            traces = D.run(td / "a.dsl.yaml", td / "in.json", td / "out", operator_override=_OP)
        names = sorted(p.name for p in (td / "out").iterdir())
        files = {p.name: json.loads(p.read_text()) for p in (td / "out").iterdir()
                 if p.name.endswith(".minitrace.json")}
        doc = json.loads((td / "out" / "conversion.diagnostics.json").read_text())
    judges = [f"judge-{j}.minitrace.json" for j in aids]
    assert names == sorted(["conversion.diagnostics.json", "ok.minitrace.json", *judges]), names
    assert sorted(t["id"] + ".minitrace.json" for t in traces) == sorted(files), "run() returns what it wrote"
    assert doc["set_level_loss"] is True
    named = [d for d in doc["diagnostics"] if d.get("reason") in ("file-name-unusable", "file-name-collision")]
    assert all(d["kind"] == "trace-dropped" and d["loss"] == "set" for d in named), named
    unusable = sorted(d["message"] for d in named if d["reason"] == "file-name-unusable")
    assert len(unusable) == 2 and "'../x'" in unusable[0] and "'CON'" in unusable[1], unusable
    collided = [d["message"] for d in named if d["reason"] == "file-name-collision"]
    assert len(collided) == 1 and "'Dup'" in collided[0] and "'dup'" in collided[0], collided
    assert not any("../y" in d["message"] for d in named), "a duplicated id is R5's, not the name check's"
    assert any("../y" in d["message"] and "(R5)" in d["message"] for d in doc["diagnostics"]), doc
    ok_hash = files["ok.minitrace.json"]["content_hash"]
    for j in aids:
        edges = files[f"judge-{j}.minitrace.json"].get("lineage") or []
        if j == "j4":
            assert [e["target_content_hash"] for e in edges] == [ok_hash], edges
        else:
            assert edges == [], (j, edges)


def test_reader_skips_reach_the_diagnostics_file():
    # A record deeper than the read bound is skipped (§4): a set-level loss in
    # the conversion's diagnostics, not only a stderr line. Depth 70 would be
    # truncated, not skipped, so the fixture must exceed the read bound.
    import tempfile  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    D = _dr()
    deep = source_reader.DEFAULT_LIMITS.read_depth + 1
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(_DIAG_DSL)
        (td / "in.jsonl").write_text('{"sid": "s", "n": 1}\n{"v": ' + _nested_text(deep) + '}\n')
        with contextlib.redirect_stderr(io.StringIO()):
            D.run(td / "a.dsl.yaml", td / "in.jsonl", td / "out.json", operator_override=_OP)
        doc = json.loads((td / "out.json.diagnostics.json").read_text())
    skips = [d for d in doc["diagnostics"] if d.get("reason") == "nesting-too-deep"]
    assert [(d["kind"], d["loss"], d["record_index"]) for d in skips] \
        == [("input-parse-error", "set", 1)], doc
    assert doc["set_level_loss"] is True


def test_a_dropped_single_trace_removes_an_earlier_output_file():
    import tempfile  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    D = _dr()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(_DIAG_DSL)
        (td / "out.json").write_text('{"id": "from-an-earlier-run"}')
        (td / "in.jsonl").write_text('{"n": 1}\n')   # no sid: the trace id is null
        with contextlib.redirect_stderr(io.StringIO()):
            out = D.run(td / "a.dsl.yaml", td / "in.jsonl", td / "out.json", operator_override=_OP)
        assert out is None and not (td / "out.json").exists()
        assert "(R5)" in (td / "out.json.diagnostics.json").read_text()


def test_directory_output_keeps_its_report_when_a_trace_is_not_written():
    # The diagnostics file is written first and, like the trace files, never
    # over an existing one. An id that is no safe file name no longer fails
    # the run (§15 Writing output): the trace is not written and the report says so.
    import tempfile  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    D = _dr()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(_ID_DSL % (_MULTI % "$container.trace_id"))
        (td / "in.json").write_text(json.dumps([{"trace_id": "../x", "events": []},
                                                {"trace_id": None, "events": []}]))
        (td / "out").mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            D.run(td / "a.dsl.yaml", td / "in.json", td / "out", operator_override=_OP)
        assert sorted(p.name for p in (td / "out").iterdir()) == ["conversion.diagnostics.json"]
        written = _diag_file(td / "out" / "conversion.diagnostics.json")
        assert any("(R5)" in d["message"] for d in written), written
        assert [d.get("reason") for d in written if "'../x'" in d["message"]] == ["file-name-unusable"], written
        (td / "in.json").write_text(json.dumps([{"trace_id": "b", "events": []}]))
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                D.run(td / "a.dsl.yaml", td / "in.json", td / "out", operator_override=_OP)
            raise AssertionError("an existing diagnostics file was overwritten")
        except D.OutputRefused:   # refused before writing (it was a FileExistsError)
            pass


def _read_with(tmp_lines, **limits):
    import dataclasses  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "in.jsonl"
        p.write_bytes(b"".join(tmp_lines))
        return source_reader.read_jsonl(
            p, limits=dataclasses.replace(source_reader.DEFAULT_LIMITS, **limits))


def test_reader_loss_class_follows_the_skip_not_the_finding_kind():
    # §15: a skipped record is set-level whatever the finding kind (a line over
    # the length bound, kind bounds-exceeded, was reported value-level); repairs, duplicates and truncations are value-level.
    lines = [
        b'{"ok": 1, "pad": "' + b"x" * 200 + b'"}\n',   # skipped: line-too-long
        b'{"broken": \n',                                  # skipped: parse error
        b'{"n": NaN}\n',                                   # skipped: number-out-of-range (not JSON)
        b'[1, 2]\n',                                       # skipped: not-an-object
        b'{"v": ' + _nested_text(9).encode() + b'}\n',     # skipped: deeper than read bound 8
        b'{"v": ' + _nested_text(5).encode() + b'}\n',     # kept, truncated at 4
        b'{"a": 1, "a": 2}\n',                             # kept, duplicate key
        b'{"s": "\xff"}\n',                                # kept, decode repair
    ]
    r = _read_with(lines, max_line_bytes=128, max_depth=4, read_depth=8)
    classes = {(f.get("reason"), loss) for f, loss in r.findings_with_loss()}
    assert {("line-too-long", "set"), ("number-out-of-range", "set"), ("not-an-object", "set"),
            ("nesting-too-deep", "set"), ("nesting-too-deep", "value"),
            ("duplicate-key", "value")} <= classes, classes
    assert not any(loss == "set" and f["kind"] in ("truncated", "decode-error")
                   for f, loss in r.findings_with_loss()), classes
    assert sum(loss == "set" for _, loss in r.findings_with_loss()) == r.record_count_skipped == 5
    assert len(r.records) == 3


def test_reader_cap_marker_is_set_level_only_when_it_hides_a_skip():
    # R2: truncations alone past the findings cap are no set-level loss; a
    # skip among the suppressed findings is.
    trunc = b'{"v": [' + b",".join([_nested_text(3).encode()] * 6) + b']}\n'
    only_truncations = _read_with([trunc], max_findings=3, max_depth=3)
    marker = only_truncations.findings_with_loss()[-1]
    assert marker[0]["kind"] == "findings-truncated" and marker[0]["suppressed_count"] == 3
    assert marker[1] == "value" and only_truncations.record_count_skipped == 0
    hidden_skip = _read_with([trunc, b'{"broken": \n'], max_findings=3, max_depth=3)
    marker = hidden_skip.findings_with_loss()[-1]
    assert marker[0]["suppressed_count"] == 4 and hidden_skip.record_count_skipped == 1
    assert marker[1] == "set"


def _run_signal(text):
    """Run the diagnostics DSL over JSONL text; the diagnostics document."""
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    D = _dr()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(_DIAG_DSL)
        (td / "in.jsonl").write_text(text)
        with contextlib.redirect_stderr(io.StringIO()):
            D.run(td / "a.dsl.yaml", td / "in.jsonl", td / "out.json", operator_override=_OP)
        return json.loads((td / "out.json.diagnostics.json").read_text())


def test_a_line_over_the_length_bound_raises_the_set_level_signal():
    # Through the runner: the default 32 MB line bound.
    over = source_reader.DEFAULT_LIMITS.max_line_bytes + 1
    doc = _run_signal('{"sid": "s", "n": 1}\n{"pad": "' + "x" * over + '"}\n')
    skips = [d for d in doc["diagnostics"] if d.get("reason") == "line-too-long"]
    assert [d["loss"] for d in skips] == ["set"] and doc["set_level_loss"] is True, doc


def test_truncations_past_the_findings_cap_raise_no_set_level_signal():
    # Through the runner: nothing is skipped.
    n = source_reader.DEFAULT_LIMITS.max_findings + 1
    level = source_reader.DEFAULT_LIMITS.max_depth
    inner = "[" + ",".join([_nested_text(1)] * n) + "]"
    doc = _run_signal(json.dumps({"sid": "s", "n": 1})[:-1] + ', "v": '
                      + "[" * (level - 2) + inner + "]" * (level - 2) + "}\n")
    kinds = [d["kind"] for d in doc["diagnostics"]]
    assert kinds.count("input-findings-truncated") == 1, kinds   # the fixture reaches the cap
    assert doc["set_level_loss"] is False, [d for d in doc["diagnostics"] if d["loss"] == "set"]
    # §4: the summary names the count and the first affected record.
    (summary,) = [d for d in doc["diagnostics"] if d["kind"] == "input-findings-truncated"]
    assert (summary["suppressed_count"], summary["record_index"], summary["loss"]) == (1, 0, "value"), summary
    assert "1 further input finding(s)" in summary["message"], summary


def test_a_skip_hidden_by_the_findings_cap_is_counted_and_set_level():
    n = source_reader.DEFAULT_LIMITS.max_findings
    level = source_reader.DEFAULT_LIMITS.max_depth
    inner = "[" + ",".join([_nested_text(1)] * n) + "]"
    full = json.dumps({"sid": "s", "n": 1})[:-1] + ', "v": ' + "[" * (level - 2) + inner + "]" * (level - 2) + "}\n"
    doc = _run_signal(full + '{"broken": \n')
    (summary,) = [d for d in doc["diagnostics"] if d["kind"] == "input-findings-truncated"]
    assert (summary["suppressed_count"], summary["record_index"], summary["loss"]) == (1, 1, "set"), summary
    assert doc["set_level_loss"] is True


def test_diagnostics_file_bounds_messages_and_console_escapes_control_characters():
    D = _dr()
    long = {"level": "warning", "kind": "x", "message": "m" * 5000}
    bounded = D._bounded_message(long)
    assert len(bounded["message"]) < 2100 and bounded["message"].endswith("(5000 chars)")
    assert D._bounded_message({"message": "short"}) == {"message": "short"}
    shown = D._console_safe("type=\x1b]0;x\x07\x9b31m‮ok")
    assert "\x1b" not in shown and "\x07" not in shown and "\x9b" not in shown and "‮" not in shown
    assert shown.endswith("ok") and "\\x{1b}" in shown


# --- a lossy run looked like a clean one ---------------------------------

def test_losses_are_classified_set_or_value_without_reading_messages():
    D = _dr()
    diags = [{"level": "warning", "kind": "trace-dropped", "message": "x"},
             {"level": "warning", "kind": "input-failure", "message": "x"},
             {"level": "warning", "kind": "lineage-edge-omitted", "message": "x"},
             {"level": "info", "kind": "note", "message": "x"},
             {"level": "warning", "kind": "input-parse-error", "loss": "set", "message": "x"},
             {"level": "warning", "kind": "input-truncated", "loss": "value", "message": "x"}]
    assert D.set_level_loss(diags)
    assert [(d["loss"], d["level"]) for d in diags] == [
        ("set", "error"), ("value", "warning"), ("value", "warning"),
        (None, "info"), ("set", "error"), ("value", "warning")]
    assert not D.set_level_loss([{"kind": "input-failure", "message": "x"}])


def test_diagnostics_file_flags_set_level_loss():
    err, collected, written = _diag_run(["x"], with_output=True)            # value-level only
    assert not any(d["loss"] == "set" for d in written)
    err, collected, written = _diag_run([1], with_output=True, sid=None)    # trace not emitted
    assert any(d["loss"] == "set" for d in written)
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        D = _dr()
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(_DIAG_DSL)
        for name, lines, flag in (("v", '{"sid": "s", "n": "x"}\n', False),
                                  ("s", '{"sid": "s", "n": 1}\n{"v": ' + _nested_text(600) + '}\n', True),
                                  ("t", '{"sid": "s", "n": 1}\n{"v": ' + _nested_text(70) + '}\n', False)):
            (td / f"{name}.jsonl").write_text(lines)
            import contextlib  # noqa: PLC0415
            import io  # noqa: PLC0415
            with contextlib.redirect_stderr(io.StringIO()):
                D.run(td / "a.dsl.yaml", td / f"{name}.jsonl", td / f"{name}.json", operator_override=_OP)
            doc = json.loads((td / f"{name}.json.diagnostics.json").read_text())
            assert doc["set_level_loss"] is flag, (name, doc)


def test_cli_exit_status_marks_set_level_loss():
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(_DIAG_DSL)
        codes = {}
        for name, sid in (("clean", "s"), ("value", "s"), ("set", None)):
            (td / f"{name}.jsonl").write_text(json.dumps({"sid": sid, "n": "x" if name == "value" else 1}) + "\n")
            r = subprocess.run([sys.executable, str(_ADAPTERS / "dsl_runner.py"), str(td / "a.dsl.yaml"),
                                str(td / f"{name}.jsonl"), "--operator-type", "scripted",
                                "--output", str(td / f"{name}.json")], capture_output=True, timeout=60)
            codes[name] = r.returncode
    assert codes == {"clean": 0, "value": 0, "set": 3}, codes


def test_all_records_skipped_reports_inside_an_output_directory():
    # The report of an all-skipped container run goes into the directory
    # (conversion.diagnostics.json), not beside it, and run() returns no
    # traces; a single-trace run returns None.
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    D = _dr()
    too_deep = _nested_text(source_reader.DEFAULT_LIMITS.read_depth + 1)
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(_ID_DSL % (_MULTI % "$container.trace_id"))
        (td / "in.json").write_text("[" + too_deep + "]")
        (td / "out").mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            got = D.run(td / "a.dsl.yaml", td / "in.json", td / "out", operator_override=_OP)
        assert got == []
        assert sorted(p.name for p in (td / "out").iterdir()) == ["conversion.diagnostics.json"]
        assert not (td / "out.diagnostics.json").exists()
        doc = json.loads((td / "out" / "conversion.diagnostics.json").read_text())
        assert doc["set_level_loss"] is True
        (td / "s.dsl.yaml").write_text(_DIAG_DSL)
        (td / "in.jsonl").write_text('{"broken": \n')
        with contextlib.redirect_stderr(io.StringIO()):
            assert D.run(td / "s.dsl.yaml", td / "in.jsonl", td / "o.json", operator_override=_OP) is None


def test_all_records_skipped_is_a_reported_set_level_loss_not_an_abort():
    # Every record skipped: exit 3, an earlier run's output removed, and the
    # report rewritten, instead of exit 1 with the old files left in place.
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(_DIAG_DSL)
        cmd = [sys.executable, str(_ADAPTERS / "dsl_runner.py"), str(td / "a.dsl.yaml"),
               str(td / "in.jsonl"), "--operator-type", "scripted", "--output", str(td / "o.json")]
        (td / "in.jsonl").write_text(json.dumps({"sid": "s", "n": 1}) + "\n")
        assert subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
        assert (td / "o.json").is_file()
        (td / "in.jsonl").write_text('{"broken": \n[1, 2]\n')
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        doc = json.loads((td / "o.json.diagnostics.json").read_text())
        assert r.returncode == 3, r.stderr
        assert not (td / "o.json").exists()
        assert doc["set_level_loss"] is True
        assert [(d["kind"], d["loss"], d["record_index"]) for d in doc["diagnostics"]] \
            == [("input-parse-error", "set", 0), ("input-parse-error", "set", 1)], doc


# --- claude-code: one record's value types aborted the whole export ----

def _cc_records():
    b = {"sessionId": "s1", "version": "2.1.131", "cwd": "/w"}
    ts = "2026-01-01T00:00:%02d.000Z"
    parent = [
        {**b, "type": "attachment", "uuid": "p0", "parentUuid": None, "timestamp": ts % 0,
         "attachment": {"type": "prompt_snapshot", "systemPrompt": "s", "tools": [{"name": "Bash"}]}},
        {**b, "type": "attachment", "uuid": "p0b", "parentUuid": "p0", "timestamp": ts % 0,
         "attachment": {"type": "instructions", "files": [{"path": "CLAUDE.md", "content": "x"}]}},
        {**b, "type": "user", "uuid": "p1", "parentUuid": "p0b", "timestamp": ts % 1,
         "message": {"role": "user", "content": "hello"}},
        {**b, "type": "assistant", "uuid": "p2", "parentUuid": "p1", "timestamp": ts % 2,
         "message": {"model": "m1", "content": [
             {"type": "text", "text": "ok"},
             {"type": "tool_use", "id": "tu1", "name": "Agent", "input": {"prompt": "go"}}]}},
    ]
    sub = [{**b, "type": "user", "uuid": "a1", "parentUuid": None, "timestamp": ts % 10,
            "isSidechain": True, "agentId": "a1", "message": {"role": "user", "content": "go"}}]
    return parent, sub


def _cc_convert(parent_mut=None, sub_mut=None):
    import tempfile  # noqa: PLC0415
    import preprocess_claude_code as C  # noqa: PLC0415
    parent, sub = _cc_records()
    for recs, mut in ((parent, parent_mut), (sub, sub_mut)):
        if mut:
            index, path, value = mut
            node = recs[index]
            for p in path[:-1]:
                node = node[p]
            node[path[-1]] = value
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "s1.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in parent))
        (pathlib.Path(td) / "s1" / "subagents").mkdir(parents=True)
        (pathlib.Path(td) / "s1" / "subagents" / "agent-a1.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in sub))
        containers, _ = C.build_containers_with_fidelity(p)
    for c in containers:
        c.pop("session_path", None)   # the temporary directory, different per run
    json.dumps(containers, allow_nan=False)
    return containers


def test_claude_code_hostile_value_types_do_not_abort_the_export():
    honest = _cc_convert()
    cases = [
        ((2, ("parentUuid",), ["p0b"]), None),            # unhashable key
        ((0, ("timestamp",), True), None),                 # mixed-type sort
        ((2, ("message",), True), None),                   # .get on a bool
        ((3, ("message", "content"), 5), None),            # iterating an int
        ((3, ("message", "model"), [1]), None),            # unhashable model
        ((0, ("attachment", "type"), []), None),           # unhashable subtype
        ((1, ("attachment", "files", 0, "path"), []), None),  # unhashable set member
        ((3, ("message", "content", 1, "input", "prompt"), [1]), None),  # unhashable spawn key
        (None, (0, ("uuid",), [1])),                       # hostile subagent record
    ]
    for parent_mut, sub_mut in cases:
        out = _cc_convert(parent_mut, sub_mut)
        assert [c["kind"] for c in out] == ["parent", "subagent"], (parent_mut, sub_mut)
        # Read as absent, not dropped: no record is lost (an attachment whose
        # type is not a string is no longer hoisted, so it stays visible as
        # one more event), and the container it is not in is unchanged.
        if parent_mut:
            assert len(out[0]["events"]) >= len(honest[0]["events"]), parent_mut
            assert out[1] == honest[1], parent_mut
        else:
            assert len(out[1]["events"]) >= len(honest[1]["events"]), sub_mut
    # A hostile subagent record costs nothing in the parent's own events.
    out = _cc_convert(None, (0, ("uuid",), [1]))
    assert out[0]["events"] == honest[0]["events"]


# --- sibling pre-processors: the same value-type class (ruled 2026-10-27) ----

def _gemini_session() -> dict:
    return {"sessionId": "g1", "projectHash": "h", "startTime": "2026-01-01T00:00:00.000Z",
            "messages": [
                {"type": "user", "timestamp": "2026-01-01T00:00:01.000Z", "content": [{"text": "hi"}]},
                {"type": "gemini", "timestamp": "2026-01-01T00:00:02.000Z", "content": "ok",
                 "model": "g-1", "toolCalls": [{"id": "c1", "name": "ls"}]},
            ]}


def test_gemini_cli_hostile_value_types_do_not_abort_the_export():
    import preprocess_gemini_cli as G  # noqa: PLC0415
    honest = G.build_container(_gemini_session())
    for value in (-1, True, "x", {"a": 1}):               # messages not a list
        s = _gemini_session()
        s["messages"] = value
        out = G.build_container(s)
        assert out["messages"] == [] and out["model_observed"] is None, value
    for value in ([], True, 5, "x"):                      # a message not an object
        s = _gemini_session()
        s["messages"].insert(0, value)
        out = G.build_container(s)
        json.dumps(out, allow_nan=False)
        # It stays visible as an unknown event; the honest messages are unchanged.
        assert out["messages"][0]["event_kind"] == "unknown", value
        assert out["messages"][1:] == honest["messages"], value
        assert out["model_observed"] == honest["model_observed"] == "g-1", value


def test_gemini_cli_session_id_counts_only_as_a_string():
    """Ruled 2026-10-27: a sessionId that is not a string reads as absent
    ("unknown"), never formatted into the trace id by str()."""
    import preprocess_gemini_cli as G  # noqa: PLC0415
    s = _gemini_session()
    del s["sessionId"]
    absent = G.build_container(s)
    assert absent["sessionId"] == "unknown" and absent["trace_id"] == "gemini-cli-unknown"
    for value in (None, 0, False, 5, True, [1], {"a": 1}, 1.5):
        s = _gemini_session()
        s["sessionId"] = value
        assert G.build_container(s) == absent, value
    assert G.build_container(_gemini_session())["trace_id"] == "gemini-cli-g1"
    # A string is kept as written, an empty one included (as before).
    s = _gemini_session()
    s["sessionId"] = ""
    out = G.build_container(s)
    assert out["sessionId"] == "" and out["trace_id"] == "gemini-cli-", out["trace_id"]


def _vibe_session(td: pathlib.Path, name: str, meta_mut=None, call_id=None) -> pathlib.Path:
    ref = _ADAPTERS / "vibe" / "format-reference"
    meta = json.loads((ref / "meta-container-run.json").read_text())
    recs = [json.loads(l) for l in (ref / "session-container-run.jsonl").read_text().splitlines() if l.strip()]
    if meta_mut:
        meta.update(meta_mut)
    if call_id is not None:
        (first,) = [r for r in recs if r.get("tool_calls")][:1]
        first["tool_calls"][0]["id"] = call_id
    d = td / name
    d.mkdir()
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "messages.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    return d


def test_vibe_hostile_value_types_do_not_abort_the_export():
    import tempfile  # noqa: PLC0415
    import preprocess_vibe as V  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as t:
        td = pathlib.Path(t)
        honest = V.process_one_session(_vibe_session(td, "honest"))
        for i, value in enumerate(([1], ["x"], {"a": 1}, 7, True, 0, "")):
            # A session id that is not a string reads as absent: the directory name.
            c = V.process_one_session(_vibe_session(td, f"sid{i}", meta_mut={"session_id": value}))
            assert c["session_id"] == f"sid{i}" and c["trace_id"] == f"vibe-sid{i}", value
            V.link_parent_child([honest, c])
            json.dumps(c, allow_nan=False)
            # A tool call id that is not a string matches no result, and costs nothing else.
            c = V.process_one_session(_vibe_session(td, f"cid{i}", call_id=value))
            calls = [tc for m in c["messages"] for tc in m.get("tool_calls") or []]
            hcalls = [tc for m in honest["messages"] for tc in m.get("tool_calls") or []]
            assert len(calls) == len(hcalls), value
            assert calls[0]["id"] == "" and calls[0]["has_result"] is False, value
            assert calls[1:] == hcalls[1:], value


def test_cursor_hostile_value_types_do_not_abort_the_export():
    import copy  # noqa: PLC0415
    import preprocess_cursor as C  # noqa: PLC0415
    text = (_ADAPTERS / "cursor" / "regression" / "2026.06.04-5fd875e" / "inputs"
            / "s1-config.stream.jsonl").read_text()
    recs = [json.loads(l) for l in text.splitlines() if l.strip()]

    def convert(rs):
        out = C.build_container(*C.parse_stream("".join(json.dumps(r) + "\n" for r in rs)))
        json.dumps(out, allow_nan=False)
        return out

    honest = convert(recs)
    assert recs[5]["type"] == "assistant" and recs[12]["type"] == "result"
    no_usage = copy.deepcopy(recs)
    del no_usage[12]["usage"]
    absent = convert(no_usage)
    for value in (True, 5, [1], {"a": 1}):
        # A text block whose text is not a string reads as empty text.
        rs = copy.deepcopy(recs)
        rs[5]["message"]["content"][0]["text"] = value
        out = convert(rs)
        changed = [i for i, (a, b) in enumerate(zip(out["messages"], honest["messages"])) if a != b]
        assert len(out["messages"]) == len(honest["messages"]) and len(changed) == 1, value
        assert out["messages"][changed[0]]["content_text"] == "", value
        # A usage that is not an object reads as absent: the container equals
        # the one converted with no usage at all.
        rs = copy.deepcopy(recs)
        rs[12]["usage"] = value
        assert convert(rs) == absent, value
        # A parsed command name that is not a string reads as absent: never NEW or MODIFY.
        rs = copy.deepcopy(recs)
        rs[9]["tool_call"]["shellToolCall"]["args"]["parsingResult"]["executableCommands"][0]["name"] = value
        out = convert(rs)
        ops = [tc["operation_type"] for m in out["messages"] for tc in m.get("tool_calls") or []]
        hops = [tc["operation_type"] for m in honest["messages"] for tc in m.get("tool_calls") or []]
        assert len(ops) == len(hops) and all(o not in ("NEW", "MODIFY") or o == h
                                              for o, h in zip(ops, hops)), (value, ops, hops)


def test_codex_hostile_value_types_read_as_absent():
    """Each value of the wrong type converts exactly as if its key were absent,
    and never aborts the export (one before wrote the output, then exited 1)."""
    import copy  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import preprocess_codex as X  # noqa: PLC0415
    recs = [json.loads(l) for l in (_ADAPTERS / "codex" / "regression" / "0.114.0" / "inputs"
                                    / "s1-files.jsonl").read_text().splitlines() if l.strip()]
    sites = [  # (record, path under payload, what it is)
        (0, ("id",), "session_meta"), (4, ("approval_policy",), "turn_context"),
        (6, ("message",), "user_message"), (8, ("text",), "agent_reasoning"),
        (10, ("name",), "function_call"), (10, ("call_id",), "function_call"),
        (12, ("call_id",), "function_call_output"), (18, ("output",), "function_call_output"),
        (15, ("summary",), "reasoning"), (21, ("summary", 0, "text"), "reasoning"),
        (22, ("message",), "agent_message"),
    ]

    def convert(rs):
        with tempfile.TemporaryDirectory() as td:
            i, o = pathlib.Path(td) / "in.jsonl", pathlib.Path(td) / "out.json"
            i.write_text("".join(json.dumps(r) + "\n" for r in rs))
            with contextlib.redirect_stderr(io.StringIO()):
                rc = X.main(["preprocess_codex.py", str(i), str(o)])
            assert rc == 0, rc
            return json.loads(o.read_text())

    for rec, path, kind in sites:
        payload = recs[rec]["payload"]
        assert kind in (recs[rec]["type"], payload.get("type")), (rec, kind)
        absent = copy.deepcopy(recs)
        node = absent[rec]["payload"]
        for p in path[:-1]:
            node = node[p]
        del node[path[-1]]
        expected = convert(absent)
        for value in (-1, True, [1], {"a": 1}) + (("x",) if path == ("summary",) else ()):
            rs = copy.deepcopy(recs)
            node = rs[rec]["payload"]
            for p in path[:-1]:
                node = node[p]
            node[path[-1]] = value
            assert convert(rs) == expected, (rec, path, value)


def _codex_convert(recs):
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import preprocess_codex as X  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        i, o = pathlib.Path(td) / "in.jsonl", pathlib.Path(td) / "out.json"
        i.write_text("".join(json.dumps(r) + "\n" for r in recs))
        with contextlib.redirect_stderr(io.StringIO()):
            rc = X.main(["preprocess_codex.py", str(i), str(o)])
        assert rc == 0, rc
        return json.loads(o.read_text())


def test_cursor_session_id_of_the_wrong_type_reads_as_absent():
    """Ruled 2026-10-27: a session id counts only as a non-empty string, so a
    value of another type never names the trace, a fallback tool id or the
    title (which aborted when there was no user text)."""
    import copy  # noqa: PLC0415
    import preprocess_cursor as C  # noqa: PLC0415
    text = (_ADAPTERS / "cursor" / "regression" / "2026.06.04-5fd875e" / "inputs"
            / "s1-config.stream.jsonl").read_text()
    recs = [json.loads(l) for l in text.splitlines() if l.strip()]
    init = next(i for i, r in enumerate(recs) if r.get("type") == "system" and r.get("subtype") == "init")
    res = next(i for i, r in enumerate(recs) if r.get("type") == "result")
    no_user = [r for r in recs if r.get("type") != "user"]

    def convert(rs):
        out = C.build_container(*C.parse_stream("".join(json.dumps(r) + "\n" for r in rs)))
        json.dumps(out, allow_nan=False)
        return out

    for base in (recs, no_user):
        i_init = next(i for i, r in enumerate(base) if r.get("type") == "system")
        i_res = next(i for i, r in enumerate(base) if r.get("type") == "result")
        for where in ((i_init,), (i_res,), (i_init, i_res)):
            absent = copy.deepcopy(base)
            for w in where:
                del absent[w]["session_id"]
            expected = convert(absent)
            for value in (5, True, [1], {"a": 1}):
                rs = copy.deepcopy(base)
                for w in where:
                    rs[w]["session_id"] = value
                assert convert(rs) == expected, (where, value)
    assert init != res
    # With the init id unusable, the result record's string id is the session id.
    rs = copy.deepcopy(recs)
    rs[init]["session_id"] = 5
    rs[res]["session_id"] = "from-result"
    out = convert(rs)
    assert out["session_id"] == out["trace_id"] == "from-result", out["session_id"]


def test_cursor_rejection_and_shell_args_of_the_wrong_type_read_as_absent():
    import preprocess_cursor as C  # noqa: PLC0415

    def rejected(r):
        call = {"shellToolCall": {"args": _cursor_shell("ls", "ls"), "result": {"rejected": r}}}
        return C.build_tool_call("shellToolCall", None, call, 0, "s1")

    expected = rejected({})
    assert expected["error"] == "rejected: no reason given"
    for value in (True, 5, [1], "x"):
        assert rejected(value) == expected, value                  # rejected not an object
    for value in (True, 5, [1], {"a": 1}):
        assert rejected({"reason": value}) == expected, value      # reason not a string
    assert rejected({"reason": "denied"})["error"] == "rejected: denied"

    for name in ("sed", "perl", "git"):
        absent = _cursor_shell("x f", name)
        del absent["parsingResult"]["executableCommands"][0]["args"]
        # A dict is a wrong type too: before, its keys were iterated as argv
        # ({"-i": 1} read as an in-place sed); now it reads as no args.
        for value in (5, True, 1.5, {"-i": 1}, {"status": 1}, {"apply": 1}):
            args = _cursor_shell("x f", name)
            args["parsingResult"]["executableCommands"][0]["args"] = value
            assert C.classify_shell(args) == C.classify_shell(absent), (name, value)


def test_codex_falsy_function_call_name_keeps_the_command_dispatch():
    """A falsy name (None, 0, False, [], {}, 0.0) keeps the old behaviour, where
    a call with a command is read as exec_command, and passes through unchanged
    (ruled 2026-10-27); only a truthy non-string reads as the absent key's
    "unknown"."""
    text = (_ADAPTERS / "codex" / "regression" / "0.114.0" / "inputs" / "s1-files.jsonl").read_text()
    for value in (None, 0, False, [], {}, 0.0):
        recs = [json.loads(l) for l in text.splitlines() if l.strip()]
        for r in recs:
            p = r.get("payload")
            if isinstance(p, dict) and p.get("type") == "function_call":
                p["name"] = value
        (c,) = _codex_convert(recs)
        calls = [tc for m in c["messages"] for tc in m.get("tool_calls") or []]
        assert [(tc["tool_name"], tc["operation_type"]) for tc in calls] == \
            [("exec_command", "NEW"), ("exec_command", "READ")], (value, calls)
        assert c["tools_enabled"] == ["exec_command"], value
        # The value itself passes through, as before: it is not replaced.
        assert all(tc["codex_function"] == value and type(tc["codex_function"]) is type(value)
                   for tc in calls), (value, calls)


def test_codex_exec_reasoning_text_of_the_wrong_type_reads_as_absent():
    import copy  # noqa: PLC0415
    inputs = _ADAPTERS / "codex" / "regression" / "0.114.0" / "inputs"
    for name in ("s2-todo-grep.jsonl", "s3-readme-edit.jsonl"):
        recs = [json.loads(l) for l in (inputs / name).read_text().splitlines() if l.strip()]
        idx = [i for i, r in enumerate(recs)
               if isinstance(r.get("item"), dict) and r["item"].get("type") == "reasoning"
               and "text" in r["item"]]
        assert idx, name  # the exec input carries reasoning
        absent = copy.deepcopy(recs)
        del absent[idx[0]]["item"]["text"]
        expected = _codex_convert(absent)
        for value in (-1, True, [1], {"a": 1}, 1.5):
            rs = copy.deepcopy(recs)
            rs[idx[0]]["item"]["text"] = value
            assert _codex_convert(rs) == expected, (name, value)


def test_a_library_run_without_an_operator_is_refused_before_anything_is_read():
    # DSL rc3 §7.1, ruled 2026-10-17: run() requires operator.type
    # at invocation, as the command line requires --operator-type; refused at
    # entry, after the output clearing and before the adapter or input is read.
    import tempfile  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    dsl = fixtures / "synthetic-trace-pair.dsl.yaml"
    inp = fixtures / "synthetic-trace-pair.input.json"
    assert D.run(dsl, inp, None, operator_override=_OP)  # the same call with an operator emits
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        for kwargs in ({}, {"operator_override": None}, {"diagnostics_out": []}):
            for adapter, source in ((dsl, inp), (td / "absent.dsl.yaml", td / "absent.json")):
                out = td / "out.json"
                out.write_text("earlier run")
                try:
                    D.run(adapter, source, out, **kwargs)
                except SystemExit as e:
                    assert "operator.type" in str(e.code), (kwargs, adapter.name, e.code)
                else:
                    raise AssertionError(f"no refusal: {kwargs} {adapter.name}")
                assert not out.exists(), (kwargs, adapter.name)  # cleared first
        # No output path (the traces returned): refused all the same.
        try:
            D.run(dsl, inp, None)
        except SystemExit as e:
            assert "operator.type" in str(e.code), e.code
        else:
            raise AssertionError("no refusal without an output path")
        # A directory output too (an existing directory; pathlib drops a
        # trailing slash): refused, and no trace written into it.
        outdir = td / "dir"
        outdir.mkdir()
        try:
            D.run(dsl, inp, outdir)
        except SystemExit as e:
            assert "operator.type" in str(e.code), e.code
        else:
            raise AssertionError("no refusal for a directory output")
        assert outdir.is_dir() and not list(outdir.iterdir())


def test_a_derived_or_computed_key_through_a_non_object_is_an_input_failure():
    # Ruled 2026-10-17, as for set: a session.derived or
    # finalize.computed key whose path runs through a string, number,
    # boolean or array replaced it with an object and reported nothing. It
    # is an input failure naming the path in the way and its JSON type;
    # nothing is written, the path first (the RHS is not evaluated).
    # A null or absent parent is still created.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    dsl["input"] = {"type": "json"}
    dsl["session"] = base["sessions"]["agent"]
    dsl["session"]["derived"] = {
        "id": "agent-x", "title": "$record_first.s",
        "environment": "$record_first.s", "environment.model": "m",           # a string
        "timing": "$record_first.n", "timing.x": 1,                           # a number
        "flags": "$record_first.b", "flags.x": 1,                             # a boolean
        "metrics": "$record_first.lst", "metrics.x": 1,                       # an array
        "ctx.k": "$record_first.s", "ctx.k.z": 1,                             # depth 2
        "environment.v": "$record_first.s + 1",                               # path first
        "fresh.a": 1, "nul": None, "nul.b": 2,                                # created
    }
    dsl["emit"] = [{"name": "w", "when": {"has_any": True}, "do": []}]
    dsl["finalize"] = {"computed": {"title.sub": 1, "ctx.k.y": "$trace.title + 1", "fresh.c": 3}}
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps([{"s": "s", "n": 5, "b": True, "lst": [1]}]))
        diags = []
        trace = D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override=_OP, diagnostics_out=diags)
    if isinstance(trace, list):
        [trace] = trace
    got = {d["field"]: d["message"].split(": ", 1)[1] for d in diags if d.get("kind") == "input-failure"}
    # The place is the record the RHS reads, or "no input" for a
    # literal or a $trace RHS, which reads no input root (§8.3).
    assert got == {
        "session.derived.environment.model": "environment holds a string, not an object; prior value kept (no input)",
        "session.derived.timing.x": "timing holds a number, not an object; prior value kept (no input)",
        "session.derived.flags.x": "flags holds a boolean, not an object; prior value kept (no input)",
        "session.derived.metrics.x": "metrics holds an array, not an object; prior value kept (no input)",
        "session.derived.ctx.k.z": "ctx.k holds a string, not an object; prior value kept (no input)",
        "session.derived.environment.v": "environment holds a string, not an object; prior value kept (record 0)",
        "finalize.computed.title.sub": "title holds a string, not an object; prior value kept (no input)",
        "finalize.computed.ctx.k.y": "ctx.k holds a string, not an object; prior value kept (no input)",
    }, got
    fields = [d.get("field") for d in diags if d.get("kind") == "input-failure"]
    assert len(fields) == len(set(fields)) == len(got), fields   # one report per key
    assert (trace["environment"], trace["timing"], trace["flags"], trace["metrics"], trace["ctx"], trace["title"]) \
        == ("s", 5, True, [1], {"k": "s"}, "s"), trace
    assert trace["fresh"] == {"a": 1, "c": 3} and trace["nul"] == {"b": 2}, (trace["fresh"], trace["nul"])


def test_a_literal_that_is_not_an_object_on_another_session_key_path_is_refused_at_load():
    # Ruled 2026-10-17, refuse at load: static and passthrough
    # values are literal, so a key beneath one that is not an object (in
    # static, derived, passthrough or finalize.computed) is known at load,
    # whichever is written first. Nested literal objects count by their own
    # keys. Null, as an absent value, is a parent that is created.
    import tempfile  # noqa: PLC0415
    import pytest  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    single = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    single["input"] = {"type": "json"}
    single["session"] = {**base["sessions"]["agent"], "derived": {"id": "agent-x", "title": "agent-x"}}
    single["emit"] = [{"name": "w", "when": {"has_any": True}, "do": []}]

    def with_maps(**maps):
        d = json.loads(json.dumps(single))
        for where, kv in maps.items():
            if where == "computed":
                d.setdefault("finalize", {}).setdefault("computed", {}).update(kv)
            else:
                d["session"].setdefault(where, {}).update(kv)
        return d
    refused = {
        "static string, derived beneath": (with_maps(static={"u_env": "x"}, derived={"u_env.model": "'m'"}),
                                           "session.derived.u_env.model", "session.static.u_env"),
        "static string, passthrough beneath": (with_maps(static={"u_env": "x"}, passthrough={"u_env.a": 1}),
                                               "session.passthrough.u_env.a", "session.static.u_env"),
        "passthrough number, static beneath": (with_maps(passthrough={"timing": 5}, static={"timing.x": 1}),
                                               "session.static.timing.x", "session.passthrough.timing"),
        "static array, computed beneath": (with_maps(static={"metrics": [1]}, computed={"metrics.x": 1}),
                                           "finalize.computed.metrics.x", "session.static.metrics"),
        "static boolean, static beneath": (with_maps(static={"flags": True, "flags.x": 1}),
                                           "session.static.flags.x", "session.static.flags"),
        "nested literal string": (with_maps(passthrough={"ctx": {"k": "s"}}, derived={"ctx.k.z": 1}),
                                  "session.derived.ctx.k.z", "session.passthrough.ctx.k"),
        "dotted map key": (with_maps(static={"u_dot.b": "x"}, derived={"u_dot.b.c": "1"}),
                           "session.derived.u_dot.b.c", "session.static.u_dot.b"),
    }
    multi = json.loads(json.dumps(base))
    multi["sessions"]["agent"].setdefault("static", {})["u_env"] = "x"
    multi["sessions"]["agent"].setdefault("derived", {})["u_env.model"] = "'m'"
    refused["a sessions shape"] = (multi, "sessions.agent.derived.u_env.model", "sessions.agent.static.u_env")
    loads = {
        "object parent": with_maps(passthrough={"u_env": {"a": 1}}, derived={"u_env.model": "'m'"}),
        "null parent": with_maps(static={"u_env": None}, derived={"u_env.model": "'m'"}),
        "nested object": with_maps(passthrough={"ctx": {"k": {"j": 1}}}, derived={"ctx.k.z": 1}),
        "siblings only": with_maps(static={"u_env": "x", "u_env2.a": 1}),
        # A key inside a literal object is one segment as
        # written; at run time these paths are created, not blocked.
        "dotted key inside a literal": with_maps(static={"u_obj": {"b.c": 1}}, derived={"u_obj.b.c.d": "1"}),
    }
    # An int key inside a literal is not the segment "2" either; the YAML
    # loader refuses it before this check (§2), so it is asked directly.
    assert D._literal_path_problems({"session": {"static": {"u_int": {2: "y"}}, "derived": {"u_int.2.z": "1"}}}) == []
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        p = td / "a.dsl.yaml"
        for name, (d, key, parent) in refused.items():
            p.write_text(yaml.safe_dump(d, sort_keys=False))
            with pytest.raises(SystemExit) as e:
                D.run(p, td / "missing.json", None, operator_override=_OP)
            msg = str(e.value.code)
            assert f"{key}: runs through {parent}" in msg and "(§7)" in msg, (name, msg)
        for name, d in loads.items():
            p.write_text(yaml.safe_dump(d, sort_keys=False))
            with pytest.raises(ValueError):   # reached the (missing) input: loaded
                D.run(p, td / "missing.json", None, operator_override=_OP)


def test_a_string_constant_outside_the_one_run_form_is_named_before_the_text_is_rewritten():
    # Message only; every case was already refused: the
    # quote tracking of _quoted_spans and of the ternary split reads
    # backslash escapes, which §8.3 does not have, and knows no triple
    # quotes, prefixes or adjacent constants, so the text was misread before
    # the constant-form check: 'p\\' moved the ternary split into a later
    # string ("does not parse"), and the word scan named a word inside a
    # string or none. The string constants are now read first by §8.3's own
    # rule (a quote runs to the next same quote), at load and at evaluation.
    import pytest  # noqa: PLC0415
    D = _dr()
    want = "is not one quoted run without a backslash"
    bad = [
        r"$record.a ? 'p\\' + 'q:' : 1",
        r"Ｎｏｎｅ == $record.a ? Ｆａｌｓｅ + 'p\\' + 'q:' : 1",   # the word scan
        "coalesce('''a' Ｔｒｕｅ ''', ℕone)",
        # A bare f'...' or r'...' is raw text by §8.3's classification, not
        # an expression, so these are written inside a call.
        "coalesce('''it's''', Ｎｏｎｅ)",
        "coalesce('a' 'b', 1)", "coalesce(r'x', 1)", "coalesce(U'x', 1)",
        'coalesce("a"  "b", 1)', "coalesce('a'  'b', Ｎｏｎｅ)",   # spaced, where parsing fails first
    ]
    for expr in bad:
        got = D.expression_problem(expr)
        assert got and want in got and "§8.3" in got, (expr, got)
        with pytest.raises(D.ExpressionEvalError, match=want):
            D.evaluate(expr, {"record": {"a": 1}})
    # An f-string keeps the name the parsed tree gives it, also
    # where it does not parse (f'{Ｔｒｕｅ}' once read "does not parse").
    for expr in ("coalesce(f'{Ｔｒｕｅ}', 1)", "coalesce(f'x', 1)", "coalesce(Rf'x', 1)"):
        assert D.expression_problem(expr) == "unsupported AST node: JoinedStr", (expr, D.expression_problem(expr))
        with pytest.raises(D.ExpressionEvalError, match="unsupported AST node: JoinedStr"):
            D.evaluate(expr, {"record": {"a": 1}})
    good = [("coalesce($record.zz, 'a', \"b\")", "a"), ("coalesce($record.zz, \"it's\")", "it's"),
            ("$record.a ? 'x:y' : 'z'", "x:y"),
            ("$record.a == 1 ? \"'\" : '\"'", "'")]
    # A name after $ or . is no prefix; a triple-quoted
    # run is named whole; a line break between constants keeps its message.
    assert D.expression_problem("$fr'x'") == "$fr is not a context variable (§8.4)"
    assert D.expression_problem("$u'x'") == "$u is not a context variable (§8.4)"
    for expr in ("$record.u'x'", "$record.f'x'"):
        assert D.expression_problem(expr) == "does not parse as an expression", (expr, D.expression_problem(expr))
    triple = "coalesce('''x''', 1)"
    assert repr("'''x'''") in D.expression_problem(triple), D.expression_problem(triple)
    assert D.expression_problem("coalesce('a'\n'b', 1)") == "a line break outside a string constant (§8.3)"
    # A prefix is a whole word before the quote: xr'a' has none (no valid
    # expression puts a word there, so this is asked of the check itself).
    assert D._string_text_problem("xr'a'") is None and D._string_text_problem("(r'a')")
    for expr, value in good:
        assert D.expression_problem(expr) is None, (expr, D.expression_problem(expr))
        assert D.evaluate(expr, {"record": {"a": 1}}) == value, expr


def test_a_finding_in_a_container_whose_record_path_is_no_list_names_no_record():
    # _input_place saw only the path, so a finding under key "0"
    # of an object at record_path was named record 0 of the container,
    # though that container yields no records. It names the container
    # alone; a list at record_path still names its record. The repair guard
    # names its findings the same way.
    import tempfile  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    dsl = fixtures / "synthetic-trace-pair.dsl.yaml"   # container mode, record_path: events
    bad = "\ud800"
    data = [{"id": "c0", "events": {"0": {"index": 0, "role": "user", "content": bad}}},
            {"id": "c1", "events": [{"index": 0, "role": "user", "content": bad}]},
            {"id": "c2", "events": {"1": {"content": bad}, "x": [bad]}}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "in.json").write_text(json.dumps(data))
        diags = []
        D.run(dsl, td / "in.json", None, operator_override=_OP, diagnostics_out=diags)
        repairs = [d for d in diags if d.get("reason") == "lone-surrogate"]
        got = sorted(((d["container_index"], d["record_index"], d["message"].split(":", 1)[0]) for d in repairs),
                     key=lambda t: (t[0], -1 if t[1] is None else t[1]))
        assert got == [(0, None, "input container 0"), (1, 0, "input record 0, container 1"),
                       (2, None, "input container 2"), (2, None, "input container 2")], got
        # load_input called without positions (the CLI) names them the same.
        diags2 = []
        D.load_input({"type": "json", "container": {"record_path": "events"}}, td / "in.json", diags2)
        got2 = sorted(((d["container_index"], d["record_index"]) for d in diags2 if d.get("reason") == "lone-surrogate"),
                      key=lambda t: (t[0], -1 if t[1] is None else t[1]))
        assert got2 == [(0, None), (1, 0), (2, None), (2, None)], got2
        # $self: the container is its one record, so a finding there is record 0.
        (td / "self.json").write_text(json.dumps([{"id": "c0", "index": 0, "role": "user", "content": bad}]))
        diags3 = []
        D.load_input({"type": "json", "container": {"record_path": "$self"}}, td / "self.json", diags3)
        assert [(d["container_index"], d["record_index"]) for d in diags3 if d.get("reason") == "lone-surrogate"] \
            == [(0, 0)], diags3
        # The repair guard after loading (a reader that did not repair is
        # simulated, as test_input_repair_reports_carry_count_and_member_name
        # does in single-trace mode) names its findings the same way.
        import source_reader  # noqa: PLC0415

        class Unrepaired:
            root_kind = "array"
            record_indices = [0, 1]
            records = [{"id": "c0", "events": {"0": {"c": "﷐"}}}, {"id": "c1", "events": [{"c": "﷐"}]}]
            def findings_with_loss(self):
                return []
        real = source_reader.read_json
        source_reader.read_json = lambda *a, **k: Unrepaired()
        try:
            diags4 = []
            D.load_input({"type": "json", "container": {"record_path": "events"}}, td / "in.json", diags4)
        finally:
            source_reader.read_json = real
        got4 = sorted(((d["container_index"], d["record_index"], d["message"].split(":", 1)[0]) for d in diags4),
                      key=lambda t: (t[0], -1 if t[1] is None else t[1]))
        assert got4 == [(0, None, "input container 0"), (1, 0, "input record 0, container 1")], diags4


_POSITION_DSL = """
adapter: {name: t, harness_version: "1", dsl_version: "0.1.0", source_format: x, converter_version: x, produces_schema: "0.3.0"}
input: {type: jsonl, encoding: utf-8}
%s
emit:
  - %sname: note
    when: {has: v}
    do:
      - emit_annotation: {type: x, value: "$record.n + 1"}
"""
_POSITION_SINGLE = ('session:\n  static: {schema_version: "0.3.0", trace_type: agent-session, id: t}\n'
                    '  derived: {metadata.first: "$record_first.n + 1"}', "")
_POSITION_MULTI = ('sessions:\n  agent:\n    static: {schema_version: "0.3.0", trace_type: agent-session, id: t}\n'
                   '    derived: {metadata.first: "$record_first.n + 1"}', "target: agent\n    ")


def _position_reports(shape, text):
    import tempfile  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    D = _dr()
    diags: list = []
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(_POSITION_DSL % shape)
        (td / "in.jsonl").write_text(text)
        with contextlib.redirect_stderr(io.StringIO()):
            D.run(td / "a.dsl.yaml", td / "in.jsonl", None, operator_override=_OP, diagnostics_out=diags)
    return diags


def test_a_record_without_containers_carries_its_input_position_in_every_report():
    # Ruled 2026-10-18, input position: after a skipped line, an
    # evaluation report named the next record by its kept index (record 0,
    # the skipped line's number) while the reader named it by its input
    # position (record 1), as container mode always did.
    for label, shape in (("single", _POSITION_SINGLE), ("multi", _POSITION_MULTI)):
        text = '{bad json\n{"v": "a\\ud800b", "n": "x"}\n{"v": 1, "n": "y"}\n'
        diags = _position_reports(shape, text)
        by_kind = {}
        for d in diags:
            by_kind.setdefault((d.get("kind"), d.get("field")), []).append(d.get("record_index"))
        assert by_kind[("input-parse-error", None)] == [0], (label, diags)
        assert by_kind[("input-decode-error", None)] == [1], (label, diags)
        assert by_kind[("input-failure", "emit_annotation.value")] == [1, 2], (label, diags)
        # $record_first is the first kept record, named by its position.
        derived = [d for d in diags if str(d.get("field", "")).endswith("derived.metadata.first")]
        assert [d.get("record_index") for d in derived] == [1], (label, diags)
        # With no skipped line the numbers are unchanged.
        diags = _position_reports(shape, '{"v": 1, "n": "x"}\n{"v": 1, "n": "y"}\n')
        assert [d["record_index"] for d in diags if d.get("field") == "emit_annotation.value"] == [0, 1], diags


_ORDER_DSL = """
adapter: {name: t, harness_version: "1", dsl_version: "0.1.0", source_format: x, converter_version: x, produces_schema: "0.3.0"}
input: {type: jsonl, encoding: utf-8}
%s
emit:
  - %sname: n
    when: {has_any: true}
    do: []
"""


def test_a_static_key_beneath_a_passthrough_object_is_kept():
    # Ruled 2026-10-18, reorder: static was written before
    # passthrough, so a passthrough object replaced a static key beneath it,
    # its value lost or the key dropped, unreported. Now passthrough is
    # written first, then static, then derived.
    static = "{schema_version: \"0.3.0\", trace_type: agent-session, id: t, coordination.human_attention: active}"
    for label, head, target in (("single", "session:", ""), ("multi", "sessions:\n  agent:", "target: agent\n    ")):
        ind = "  " if label == "single" else "    "
        for passthrough, want in (
                ("{coordination: {project_id: null, human_attention: passive}}",
                 {"project_id": None, "human_attention": "active"}),
                ("{coordination: {project_id: null}}", {"project_id": None, "human_attention": "active"})):
            block = f"{head}\n{ind}static: {static}\n{ind}passthrough: {passthrough}\n"
            out, _ = _run_raw(_ORDER_DSL % (block, target), '{"x": "p1"}\n', "jsonl")
            trace = out[0] if isinstance(out, list) else out
            assert trace["coordination"] == want, (label, passthrough, trace["coordination"])
        # derived is written last, into the passthrough object.
        block = (f"{head}\n{ind}static: {static}\n{ind}passthrough: {{coordination: {{project_id: null}}}}\n"
                 f"{ind}derived: {{coordination.project_id: \"$record_first.x\"}}\n")
        out, _ = _run_raw(_ORDER_DSL % (block, target), '{"x": "p1"}\n', "jsonl")
        trace = out[0] if isinstance(out, list) else out
        assert trace["coordination"] == {"project_id": "p1", "human_attention": "active"}, (label, trace)
        # A static key naming the passthrough key itself replaces its object.
        block = (f"{head}\n{ind}static: {{schema_version: \"0.3.0\", trace_type: agent-session, id: t, "
                 f"coordination: {{human_attention: passive}}}}\n{ind}passthrough: {{coordination: {{project_id: null}}}}\n")
        out, _ = _run_raw(_ORDER_DSL % (block, target), '{"x": "p1"}\n', "jsonl")
        trace = out[0] if isinstance(out, list) else out
        assert trace["coordination"] == {"human_attention": "passive"}, (label, trace)
        # The key order is unchanged: static keys first, then passthrough.
        block = f"{head}\n{ind}static: {static}\n{ind}passthrough: {{handover: {{received: null}}, coordination: {{project_id: null}}}}\n"
        out, _ = _run_raw(_ORDER_DSL % (block, target), '{"x": "p1"}\n', "jsonl")
        keys = list(out[0] if isinstance(out, list) else out)
        assert keys.index("schema_version") < keys.index("handover"), (label, keys)


_PLACE_HEAD = """
adapter: {name: t, harness_version: "1", dsl_version: "0.1.0", source_format: x, converter_version: x, produces_schema: "0.3.0"}
input: {type: jsonl, encoding: utf-8}
constants: {k: v}
session:
  static: {schema_version: "0.3.0", trace_type: agent-session, id: t}
"""


def _failures(dsl_tail, text='{"x": "s", "c": 5}\n'):
    import tempfile  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    D = _dr()
    diags: list = []
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(_PLACE_HEAD + dsl_tail)
        (td / "in.jsonl").write_text(text)
        with contextlib.redirect_stderr(io.StringIO()):
            D.run(td / "a.dsl.yaml", td / "in.jsonl", None, operator_override=_OP, diagnostics_out=diags)
    return [d for d in diags if d.get("kind") == "input-failure"]


def test_an_input_failure_entry_names_its_input_in_the_message():
    # Ruled 2026-10-18, the place in every message: entries built
    # by _input_failure named the input only in record_index, and one that
    # read no input named nothing (DSL rc3 §8.3: name the input, or that it
    # read none). The message now ends with the place, as
    # _report_input_failure's always did.
    rule = "emit:\n  - name: n\n    when: {has_any: true}\n    do:\n%s"
    for tail, field, place in (
            ('  derived: {metadata.a: "$adapter.name", metadata.a.b: "$constants.k"}\n' + rule % "      []\n",
             "session.derived.metadata.a.b", "(no input)"),
            ('  derived: {metadata.a: "$adapter.name", metadata.a.b: "$record_first.x"}\n' + rule % "      []\n",
             "session.derived.metadata.a.b", "(record 0)"),
            (rule % '      - set: {session.metadata.a: "$record.x"}\n' + 'finalize:\n  computed: {metadata.a.b: "$constants.k"}\n',
             "finalize.computed.metadata.a.b", "(no input)"),
            ("state: {n: 0}\n" + rule % '      - accumulate: {state.n: "$record.x"}\n', "accumulate.state.n", "(record 1)")):
        text = '{bad\n{"x": "s", "c": 5}\n' if field.startswith("accumulate") else '{"x": "s", "c": 5}\n'
        got = [d for d in _failures(tail, text) if d.get("field") == field]
        assert len(got) == 1 and got[0]["message"].endswith(place), (field, got)
    # foreach_block names its record once, in the place.
    tail = ("emit:\n  - name: blocks\n    when: {has_any: true}\n    foreach_block: \"$record.c\"\n"
            "    on_block:\n      - when: {has_any: true}\n        do: []\n")
    got = [d for d in _failures(tail) if d.get("field") == "blocks.foreach_block"]
    assert len(got) == 1 and got[0]["message"].endswith("(record 0)"), got
    assert got[0]["message"].count("record 0") == 1, got
    # In container mode the place names the record and its container.
    import tempfile  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    D = _dr()
    dsl = _PLACE_HEAD.replace("input: {type: jsonl, encoding: utf-8}",
                              "input: {type: json, container: {record_path: events}}")
    dsl += "state: {n: 0}\n" + rule % '      - accumulate: {state.n: "$record.x"}\n'
    diags: list = []
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(dsl)
        (td / "in.json").write_text(json.dumps([{"events": [{"x": 1}]}, {"events": [{"x": 2}, {"x": "s"}]}]))
        with contextlib.redirect_stderr(io.StringIO()):
            D.run(td / "a.dsl.yaml", td / "in.json", None, operator_override=_OP, diagnostics_out=diags)
    got = [d for d in diags if d.get("field") == "accumulate.state.n"]
    assert len(got) == 1 and got[0]["message"].endswith("(record 1, container 1)"), got


_BOM_DSL = """
adapter: {name: t, harness_version: "1", dsl_version: "0.1.0", source_format: x, converter_version: x, produces_schema: "0.3.0"}
input: {type: %s%s}
session:
  static: {schema_version: "0.3.0", trace_type: agent-session, id: t}
%s
emit:
  - name: n
    when: {has_any: true}
    do:
      - emit_annotation: {type: x, value: "$record.a"}
"""


def _bom_run(typ, data, container=False):
    import tempfile  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    D = _dr()
    diags: list = []
    cont = ", container: {record_path: events}" if container else ""
    dsl = _BOM_DSL % (typ, cont, "")
    if container:
        dsl = dsl.replace("  static: {schema_version: \"0.3.0\", trace_type: agent-session, id: t}",
                          "  static: {schema_version: \"0.3.0\", trace_type: agent-session}\n"
                          "  derived: {id: \"$container.id\"}")
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "a.dsl.yaml").write_text(dsl)
        (td / f"in.{typ}").write_bytes(data)
        with contextlib.redirect_stderr(io.StringIO()):
            out = D.run(td / "a.dsl.yaml", td / f"in.{typ}", None, operator_override=_OP, diagnostics_out=diags)
    return out, [d for d in diags if d.get("kind") != "input-failure"]


def test_a_byte_order_mark_at_the_start_of_the_input_is_ignored_unreported():
    # DSL rc3 §4 "Encoding" (ruled 2026-10-18): a UTF-8 BOM at the very
    # start of the input is not part of it; the runner ignores it, does not
    # report it, and reads a json input, or the first jsonl line, as if it
    # were absent. A json input with one was unreadable, a first jsonl line
    # skipped as malformed, a BOM-only jsonl a skipped record.
    bom = b"\xef\xbb\xbf"
    out, diags = _bom_run("json", bom + b'[{"a": 1}, {"a": 2}]')
    assert [a["value"] for a in out["annotations"]] == [1, 2] and diags == [], (out, diags)
    out, diags = _bom_run("json", bom + b'[{"id": "c", "events": [{"a": 1}]}]', container=True)
    assert [a["value"] for a in out[0]["annotations"]] == [1] and diags == [], (out, diags)
    out, diags = _bom_run("jsonl", bom + b'{"a": 1}\n{"a": 2}\n')
    assert [a["value"] for a in out["annotations"]] == [1, 2] and diags == [], (out, diags)
    # A first line holding only the mark and JSON whitespace is blank.
    out, diags = _bom_run("jsonl", bom + b' \n{"a": 1}\n')
    assert [a["value"] for a in out["annotations"]] == [1] and diags == [], (out, diags)
    # A BOM-only jsonl has nothing to convert (a warning, no skipped line).
    out, diags = _bom_run("jsonl", bom)
    assert out is None and [d["kind"] for d in diags] == ["input-empty"], (out, diags)


def test_a_byte_order_mark_elsewhere_stays_an_ordinary_character():
    # §4: anywhere else U+FEFF is an ordinary character: kept in a string,
    # and outside one not JSON whitespace, so its line is malformed.
    bom = b"\xef\xbb\xbf"
    out, diags = _bom_run("jsonl", b'{"a": 1}\n' + bom + b'{"a": 2}\n')
    assert [a["value"] for a in out["annotations"]] == [1], out
    assert [(d["kind"], d["record_index"]) for d in diags] == [("input-parse-error", 1)], diags
    out, diags = _bom_run("jsonl", bom + bom + b'{"a": 1}\n')
    assert out is None or out.get("annotations") == [], out   # only the first mark is dropped
    assert [d["kind"] for d in diags] == ["input-parse-error"], diags
    out, diags = _bom_run("json", b'[{"a": "' + bom + b'x"}]')
    assert [a["value"] for a in out["annotations"]] == ["﻿x"] and diags == [], (out, diags)


def test_a_dropped_byte_order_mark_still_counts_in_byte_offsets():
    # Offsets name bytes of the input as written, the mark included.
    import tempfile  # noqa: PLC0415
    import source_reader as SR  # noqa: PLC0415
    bom = b"\xef\xbb\xbf"
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "in.jsonl"
        p.write_bytes(bom + b'{"a": "x\xff"}\n')
        findings: list = []
        SR.read_jsonl_records(p, report_out=findings)
        offs = [f.get("byte_offset") for f, *_ in findings if f.get("kind") == "decode-error"]
        assert offs == [bom.__len__() + 8], (offs, findings)
        q = pathlib.Path(td) / "in.json"
        q.write_bytes(bom + b'[{"a": "x\xff"}]')
        src = SR.read_json(q, require_object=True)
        offs = [f.get("byte_offset") for f, _ in src.findings_with_loss() if f.get("kind") == "decode-error"]
        assert offs == [bom.__len__() + 9], (offs, src.findings_with_loss())


def test_tool_calls_sharing_an_id_are_all_removed_and_reported_once():
    # DSL rc3 §8.2.3 (ruled 2026-10-20): once a trace is built, before
    # finalize, each id held by two or more emitted tool calls, compared after
    # rendering (a source 1 and "1" share "1"), is reported once, and none of
    # those calls is kept, nor any result attached to them: a value-level
    # loss (§15). $state is not changed by the removal.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    dsl["input"] = {"type": "json"}
    dsl["session"] = base["sessions"]["agent"]
    dsl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
    dsl["emit"] = [
        {"when": {"field": "kind", "value": "call"},
         "do": [{"emit_tool_call": {"id": "$record.id", "tool_name": "t"}}]},
        {"when": {"field": "kind", "value": "result"},
         "do": [{"attach_tool_result": {"target_id": "$record.target", "output": {"result": "$record.out"}}}]}]
    # finalize sees the trace after the removal; $state still names the last
    # emitted call, although it was removed.
    dsl["finalize"] = {"computed": {"metrics.tool_call_count": "len($trace.tool_calls)",
                                    "title": "$state.last_tool_call_id",
                                    "summary": "$state.tool_calls_by_id.c.tool_name"}}
    recs = [{"kind": "call", "id": "a"}, {"kind": "call", "id": 1}, {"kind": "result", "target": 1, "out": "r1"},
            {"kind": "call", "id": "c"}, {"kind": "call", "id": "1"}, {"kind": "result", "target": "1", "out": "r1b"},
            {"kind": "result", "target": "a", "out": "ra"}, {"kind": "call", "id": "b"},
            {"kind": "call", "id": "c"}, {"kind": "call", "id": "c"}, {"kind": "call", "id": "b"}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "k.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps(recs))
        diags = []
        t = D.run(td / "k.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"},
                  diagnostics_out=diags)
    got = [(c["id"], (c.get("output") or {}).get("result")) for c in t["tool_calls"]]
    assert got == [("a", "ra")], got
    assert t["metrics"]["tool_call_count"] == 1, t["metrics"]
    assert t["title"] == "b", t["title"]
    assert t["summary"] == "t", t.get("summary")   # tool_calls_by_id still holds the removed "c"
    shared = [d for d in diags if "emitted tool calls; none of them is kept" in str(d.get("message"))]
    # One report per id, in the order the ids were first emitted.
    assert [(d["message"].split("'")[3], d["kind"], d["loss"]) for d in shared] == [
        ("1", "input-failure", "value"), ("c", "input-failure", "value"), ("b", "input-failure", "value")], shared
    assert "held by 3 emitted tool calls" in shared[1]["message"], shared
    assert all("in trace 'agent-x'" in d["message"] for d in shared), shared
    assert all(d.get("record_index") is None for d in shared), shared
    assert not D.set_level_loss(diags)


def test_a_trace_without_shared_tool_call_ids_keeps_every_call():
    # The removal touches only shared ids (ruled 2026-10-20): distinct ids,
    # 1 and "2" among them, are all kept with their results, unreported.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    D = _dr()
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    base = yaml.safe_load((fixtures / "synthetic-trace-pair.dsl.yaml").read_text())
    dsl = {k: v for k, v in base.items() if k not in ("sessions", "emit")}
    dsl["input"] = {"type": "json"}
    dsl["session"] = base["sessions"]["agent"]
    dsl["session"]["derived"] = {"id": "agent-x", "title": "agent-x"}
    dsl["emit"] = [
        {"when": {"field": "kind", "value": "call"},
         "do": [{"emit_tool_call": {"id": "$record.id", "tool_name": "t"}}]},
        {"when": {"field": "kind", "value": "result"},
         "do": [{"attach_tool_result": {"target_id": "$record.target", "output": {"result": "$record.out"}}}]}]
    recs = [{"kind": "call", "id": 1}, {"kind": "call", "id": "2"}, {"kind": "call", "id": "x"},
            {"kind": "result", "target": 1, "out": "r1"}, {"kind": "result", "target": "x", "out": "rx"}]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "k.dsl.yaml").write_text(yaml.safe_dump(dsl, sort_keys=False))
        (td / "in.json").write_text(json.dumps(recs))
        diags = []
        t = D.run(td / "k.dsl.yaml", td / "in.json", None, operator_override={"type": "scripted"},
                  diagnostics_out=diags)
    got = [(c["id"], (c.get("output") or {}).get("result")) for c in t["tool_calls"]]
    assert got == [("1", "r1"), ("2", None), ("x", "rx")], got
    assert not [d for d in diags if "none of them is kept" in str(d.get("message"))], diags


def test_the_cli_keeps_every_operator_field_a_producer_asserted():
    # Ruled 2026-10-20: the runner keeps every field the base schema allows
    # where the producer asserted it, for every operator type, held to the
    # rules run() applies (minitrace-spec §5.2-§5.4): a human operator's
    # capture_purpose in any capture mode (well-formed, §5.3), a delegated
    # operator's operator_aware, any asserted field of a scripted or
    # ai_overseer one. Nothing asserted adds nothing.
    import types  # noqa: PLC0415
    import jsonschema  # noqa: PLC0415
    D = _dr()
    schemas = pathlib.Path(__file__).resolve().parents[2] / "schemas"
    base = json.loads((schemas / "minitrace-v0.3.0.base.schema.json").read_text())
    op_schema = {**base["$defs"]["Operator"], "$defs": base["$defs"]}

    def args(**kw):
        a = dict(operator_type=None, operator_aware=None, operator_capture_mode=None,
                 operator_capture_purpose_legal_basis=None, operator_capture_purpose_reference=None,
                 operator_capture_purpose_authorised_at=None)
        a.update(kw)
        return types.SimpleNamespace(**a)

    def refused(**kw):
        try:
            D._build_operator_from_args(args(**kw))
        except SystemExit as e:
            return str(e)
        raise AssertionError(f"accepted {kw}")
    purpose = {"legal_basis": "gdpr_art6_consent", "reference": "DPIA-7",
               "authorised_at": "2026-10-20T00:00:00.000000Z"}
    p = {f"operator_capture_purpose_{k}": v for k, v in purpose.items()}
    # Only full is accepted (ruled 2026-10-27), so the fields an operator
    # keeps are pinned in full mode.
    cases = [
        (dict(operator_type="human", operator_aware=False, operator_capture_mode="full", **p),
         {"type": "human", "operator_aware": False, "capture_mode": "full", "capture_purpose": purpose}),
        (dict(operator_type="delegated", operator_aware=True, operator_capture_mode="full"),
         {"type": "delegated", "operator_aware": True, "capture_mode": "full"}),
        (dict(operator_type="delegated", operator_capture_mode="full",
              operator_capture_purpose_legal_basis="anything"),
         {"type": "delegated", "capture_mode": "full", "capture_purpose": {"legal_basis": "anything"}}),
        (dict(operator_type="scripted", operator_aware=False, operator_capture_mode="full", **p),
         {"type": "scripted", "operator_aware": False, "capture_mode": "full", "capture_purpose": purpose}),
        (dict(operator_type="ai_overseer", operator_aware=True),
         {"type": "ai_overseer", "operator_aware": True}),
        (dict(operator_type="ai_overseer"), {"type": "ai_overseer"}),
        (dict(operator_type="scripted"), {"type": "scripted"}),
    ]
    for kw, want in cases:
        got = D._build_operator_from_args(args(**kw))
        assert got == want, (kw, got)
        assert list(got) == list(want), (kw, got)   # key order: byte-identical output where unchanged
        jsonschema.validate(got, op_schema)
        assert D._operator_problems(got) == [], got
    # §5.3: a human operator's non-null capture_purpose is well-formed in
    # every mode, all three fields; the schema's typing binds every type.
    assert "legal basis 'gdpr'" in refused(operator_type="human", operator_aware=True,
                                           operator_capture_mode="full",
                                           **{**p, "operator_capture_purpose_legal_basis": "gdpr"})
    msg = refused(operator_type="human", operator_aware=True, operator_capture_mode="full",
                  operator_capture_purpose_legal_basis="gdpr_art6_consent")
    # The refusal names the missing fields, and only those.
    assert "Missing: ['reference', 'authorised_at']" in msg, msg
    assert "authorised_at" in refused(operator_type="scripted",
                                      **{**p, "operator_capture_purpose_authorised_at": "junk"})
    assert "noncharacter U+FDD0" in refused(operator_type="ai_overseer",
                                            operator_capture_purpose_reference="r﷐")


def test_verify_run_keeps_a_delegated_operators_awareness():
    # Ruled 2026-10-20: found through verify_run on the pi fixture, where a
    # delegated operator's operator_aware was dropped and the run still
    # read complete.
    import tempfile  # noqa: PLC0415
    verify_run = _verify_run_built()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, i = _verify_run_case(td, None, "")
        result, out = _quiet(verify_run.run, a, i, operator_type="delegated", operator_aware=False,
                             capture_mode="full", workdir=td / "work")
        traces = json.loads((td / "work" / "traces.json").read_text())
    traces = traces if isinstance(traces, list) else [traces]
    assert result.outcome == "complete", out
    assert [t["operator"] for t in traces] == [{"type": "delegated", "operator_aware": False,
                                                "capture_mode": "full"}], traces


def _drift_core():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import drift_core  # noqa: PLC0415
    return drift_core


def _drift_cfg(dc, tmp: pathlib.Path, **kw):
    return dc.DriftConfig(harness_name="probe", preprocess=None, dsl=tmp / "a.dsl.yaml",
                          dsl_runner=tmp / "dsl_runner.py", run_cwd=tmp, cell_signals={}, **kw)


def test_drift_core_requires_an_operator_and_passes_it_through(tmp_path, capsys, monkeypatch):
    # Ruled 2026-10-20, as for verify_run: no default operator. A config
    # without one is a usage error (exit 2) before the manifest is read; the
    # operator a watcher was given reaches dsl_runner unchanged.
    import argparse  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import types  # noqa: PLC0415
    dc = _drift_core()
    (tmp_path / "manifest.json").write_text("not json")   # read would raise
    for kw in ({}, {"operator_args": []}, {"operator_args": ["--operator-aware", "true"]},
               {"operator_args": ["--operator-type"]},
               {"operator_args": ["--operator-capture-purpose-reference", "--operator-type"]}):
        assert dc.run_drift(tmp_path, None, _drift_cfg(dc, tmp_path, **kw)) == 2, kw
        assert "no operator" in capsys.readouterr().err, kw
    # An operator dsl_runner would refuse is one usage error before any
    # scenario runs, not an adapter failure per scenario read as drift.
    for args, why in ((["--operator-type", "human"], "--operator-capture-mode is REQUIRED"),
                      (["--operator-type", ""], "invalid choice"),
                      (["--operator-type", "human", "--operator-aware", "true", "--operator-capture-mode", "full",
                        "--operator-capture-purpose-reference", "r"], "Missing:"),
                      # A mode the runner does not perform: drift_core offers only full
                      # (ruled 2026-10-27).
                      (["--operator-type", "delegated", "--operator-capture-mode", "scrubbed"],
                       "invalid choice: 'scrubbed'")):
        assert dc.run_drift(tmp_path, None, _drift_cfg(dc, tmp_path, operator_args=args)) == 2, args
        assert why in capsys.readouterr().err, args
    ap = argparse.ArgumentParser()
    dc.add_operator_arguments(ap)
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            ap.parse_args([])
            raise AssertionError("parsed without --operator-type")
        except SystemExit as e:
            assert e.code == 2
    args = ap.parse_args(["--operator-type", "human", "--operator-aware", "false",
                          "--operator-capture-mode", "full", "--operator-capture-purpose-legal-basis", "x-a",
                          "--operator-capture-purpose-reference", "r",
                          "--operator-capture-purpose-authorised-at", "2026-10-20T00:00:00.000000Z"])
    want = ["--operator-type", "human", "--operator-aware", "false", "--operator-capture-mode", "full",
            "--operator-capture-purpose-legal-basis", "x-a", "--operator-capture-purpose-reference", "r",
            "--operator-capture-purpose-authorised-at", "2026-10-20T00:00:00.000000Z"]
    assert dc.operator_args_from(args) == want
    assert dc.operator_args_from(ap.parse_args(["--operator-type", "scripted"])) == ["--operator-type", "scripted"]
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        (tmp_path / "coverage.json").write_text("{}")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(dc.subprocess, "run", fake_run)
    dc.run_adapter(tmp_path / "in.jsonl", tmp_path, _drift_cfg(dc, tmp_path, operator_args=want))
    assert seen and seen[0][-len(want):] == want, seen


def test_every_drift_watch_requires_an_operator_type(tmp_path):
    # Ruled 2026-10-20: each shipped watcher has a required --operator-type
    # (argparse usage error, exit 2, without it); with it, the run reaches
    # run_drift (here: no manifest at the baseline, also exit 2).
    import subprocess  # noqa: PLC0415
    adapters = pathlib.Path(__file__).resolve().parent.parent
    watchers = sorted(adapters.glob("*/regression/drift_watch.py"))
    assert [w.parent.parent.name for w in watchers] == [
        "aider", "claude-code", "codex", "cursor", "droid", "goose", "opencode", "pi", "vibe"], watchers
    for w in watchers:
        base = ["--baseline", str(tmp_path / "no-such-baseline")]
        r = subprocess.run([sys.executable, str(w), *base], cwd=w.parent, capture_output=True, text=True,
                           timeout=120)
        assert r.returncode == 2 and "--operator-type" in r.stderr, (w, r.returncode, r.stderr[-400:])
        r = subprocess.run([sys.executable, str(w), *base, "--operator-type", "scripted"], cwd=w.parent,
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 2 and "no manifest" in r.stderr, (w, r.returncode, r.stderr[-400:])


def test_now_microseconds_can_be_pinned_for_one_run():
    # DSL rc3 §12 (ruled 2026-10-22): --now (a §3.5 timestamp) or
    # SOURCE_DATE_EPOCH (whole seconds) fixes now_microseconds() for one run,
    # --now first; two pinned runs are byte-identical; an unusable pin is
    # refused before anything runs, an earlier output left as it was; the
    # pin ends with the run, a failed one included.
    import os  # noqa: PLC0415
    import re  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415
    D = _dr()
    adapters = pathlib.Path(__file__).resolve().parent.parent
    a, i = adapters / "pi" / "0.58.1.dsl.yaml", adapters / "pi" / "format-reference" / "session-v0.58.1.jsonl"
    pin = "2026-01-02T03:04:05.123456Z"

    def converted(now=None, epoch=None, out=None):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("SOURCE_DATE_EPOCH", None)
            if epoch is not None:
                os.environ["SOURCE_DATE_EPOCH"] = epoch
            t = D.run(a, i, out, operator_override=_OP, now=now)
        t = t[0] if isinstance(t, list) else t
        return t["provenance"]["converted_at"], json.dumps(t, sort_keys=True)

    first, again = converted(now=pin), converted(now=pin)
    assert first[0] == pin and first[1] == again[1], (first[0], again[0])
    # §12: content_hash values included. pi leaves it to back-fill (as
    # verify_run does); the pinned value is inside the hash.
    from cross_trace import back_fill_cross_trace_references  # noqa: PLC0415

    def hashed(now):
        t = D.run(a, i, None, operator_override=_OP, now=now)
        return [x["content_hash"] for x in back_fill_cross_trace_references(t if isinstance(t, list) else [t])]
    one, two = hashed(pin), hashed(pin)
    assert one == two and all(h and h.startswith("sha256:") for h in one), (one, two)
    assert hashed("2026-01-02T03:04:05.123457Z") != one
    assert converted(epoch="1700000000")[0] == "2023-11-14T22:13:20.000000Z"
    assert converted(now=pin, epoch="1700000000")[0] == pin
    for clock in (converted(), converted(epoch="")):   # empty: unset, as in source_reader
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", clock[0]) and clock[0] != pin, clock[0]
    assert D._NOW_PINNED is None and D.fn_now_microseconds() != pin
    # One reading of SOURCE_DATE_EPOCH (timestamps.source_date_epoch): where
    # the runner refuses a value, source_reader omits asserted_at; where it
    # pins, both write the same timestamp.
    for epoch in (" 1700000000", "1700000000\n", "1_700_000_000", "-1", "+1", "253402300800"):
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": epoch}):
            assert source_reader.asserted_at_now() is None, epoch
            try:
                D.pinned_now()
            except ValueError:
                continue
            raise AssertionError(f"SOURCE_DATE_EPOCH {epoch!r} was not refused")
    for epoch, want in (("1700000000", "2023-11-14T22:13:20.000000Z"), ("0", "1970-01-01T00:00:00.000000Z"),
                        ("253402300799", "9999-12-31T23:59:59.000000Z")):
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": epoch}):
            assert source_reader.asserted_at_now() == D.pinned_now() == want, epoch
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td) / "o.json"
        for now, epoch in (("2026-01-02T03:04:05Z", None), ("x", None), ("", None), (None, "-1"), (None, "1.5"),
                           (None, "1e3"), (None, "\uff11\uff12"), (None, "99999999999999999")):
            out.write_text("keep")
            try:
                converted(now=now, epoch=epoch, out=out)
            except ValueError:
                assert out.read_text() == "keep", (now, epoch)
                continue
            raise AssertionError(f"pin {now!r} / {epoch!r} was not refused")
        try:
            D.run(pathlib.Path(td) / "no-such.dsl.yaml", i, None, operator_override=_OP, now=pin)
        except SystemExit:
            pass
        assert D._NOW_PINNED is None
        env = {**os.environ, "COLUMNS": "80", "NO_COLOR": "1", "PYTHON_COLORS": "0", "FORCE_COLOR": ""}
        env.pop("SOURCE_DATE_EPOCH", None)
        cli = [sys.executable, str(adapters / "dsl_runner.py"), str(a), str(i), "--operator-type", "scripted",
               "--output", str(out)]
        out.write_text("keep")
        r = subprocess.run([*cli, "--now", "2026-01-02"], capture_output=True, text=True, timeout=300, env=env)
        assert r.returncode == 2 and "--now" in r.stderr and out.read_text() == "keep", r.stderr[-300:]
        r = subprocess.run(cli, capture_output=True, text=True, timeout=300,
                           env={**env, "SOURCE_DATE_EPOCH": "soon"})
        assert r.returncode == 2 and "SOURCE_DATE_EPOCH" in r.stderr and out.read_text() == "keep", r.stderr[-300:]
        r = subprocess.run([*cli, "--now", pin], capture_output=True, text=True, timeout=300, env=env)
        t = json.loads(out.read_text())
        assert r.returncode == 0 and (t[0] if isinstance(t, list) else t)["provenance"]["converted_at"] == pin


def test_direct_run_reports_skips_apart_from_failures():
    # Ruled 2026-10-22: run without pytest, a test that takes fixtures or
    # skips itself is reported as skipped, not failed; only a failure makes
    # the exit status 1.
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import pytest  # noqa: PLC0415

    def needs_fixture(tmp_path):
        raise AssertionError("called without its fixture")

    def skips():
        pytest.skip("not here")

    def passes():
        pass

    def fails():
        raise AssertionError("boom")

    def direct(tests):
        # A skip escaping main() would skip this test rather than fail it.
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                got = main(tests)
        except pytest.skip.Exception as e:
            raise AssertionError(f"a skip escaped main(): {e}") from None
        return got, buf.getvalue()

    for tests, code, summary in (([("a", needs_fixture), ("b", skips), ("c", passes)], 0,
                                  "1/3 passed, 2 skipped, 0 failed"),
                                 ([("c", passes), ("d", fails)], 1, "1/2 passed, 0 skipped, 1 failed")):
        got, out = direct(tests)
        assert got == code and summary in out, out
    _, out = direct([("a", needs_fixture), ("b", skips)])
    assert "skip  a: needs pytest fixtures" in out and "skip  b: not here" in out, out


def test_the_runner_refuses_capture_modes_it_does_not_perform():
    # Ruled 2026-10-27: pseudonymised, scrubbed and reference_only promise a
    # transformation the runner does not perform, so they are refused for
    # every operator type, on the command line and the library path alike;
    # full is accepted, and a mode stays optional where it was.
    import types  # noqa: PLC0415
    D = _dr()
    purpose = {"legal_basis": "gdpr_art6_consent", "reference": "DPIA-7",
               "authorised_at": "2026-10-27T00:00:00.000000Z"}

    def args(op_type, mode):
        a = dict(operator_type=op_type, operator_aware=True, operator_capture_mode=mode,
                 **{f"operator_capture_purpose_{k}": v for k, v in purpose.items()})
        return types.SimpleNamespace(**a)

    remedies = {"human": ("Use full with --operator-capture-purpose-legal-basis, -reference and -authorised-at.",
                          "Use full with a capture_purpose."),
                "delegated": ("Use full.", "Use full."),
                "scripted": ("Use full, or omit the mode.", "Use full, or leave capture_mode out."),
                "ai_overseer": ("Use full, or omit the mode.", "Use full, or leave capture_mode out.")}
    for op_type, (cli_remedy, lib_remedy) in remedies.items():
        for mode in ("pseudonymised", "scrubbed", "reference_only"):
            try:
                D._build_operator_from_args(args(op_type, mode))
            except SystemExit as e:
                msg = str(e)
            else:
                raise AssertionError(f"accepted {op_type} {mode}")
            assert msg == (f"ERROR: --operator-capture-mode {mode} is refused: this runner does not perform "
                           f"the {mode} transformation, so the trace would declare {mode} while carrying the "
                           f"content unchanged (minitrace-spec §5.2, §8.4). {cli_remedy}"), msg
            op = {"type": op_type, "operator_aware": True, "capture_mode": mode, "capture_purpose": purpose}
            problems = D._operator_problems(op)
            assert (f"capture_mode {mode} is refused: this runner does not perform the {mode} transformation, "
                    f"so the trace would declare {mode} while carrying the content unchanged "
                    f"(minitrace-spec §5.2, §8.4). {lib_remedy}") in problems, (op_type, mode, problems)
        # full is accepted on both paths, and the object it builds passes run()'s rules.
        built = D._build_operator_from_args(args(op_type, "full"))
        assert built["capture_mode"] == "full" and D._operator_problems(built) == [], built
    for op_type in ("scripted", "ai_overseer"):
        built = D._build_operator_from_args(args(op_type, None))
        assert "capture_mode" not in built and D._operator_problems(built) == [], built
    # A mode is still required for a delegated operator; the message names
    # full as the one accepted value.
    try:
        D._build_operator_from_args(args("delegated", None))
    except SystemExit as e:
        assert "REQUIRED when operator-type is 'delegated'" in str(e) and "Accepted value: full" in str(e), e
    else:
        raise AssertionError("accepted a delegated operator without a mode")


def test_run_with_a_refused_capture_mode_writes_no_output(tmp_path):
    # The library path: run() refuses the operator before the adapter or the
    # input is read, so nothing is written; the same call in full mode writes
    # the trace, so the refusal is what kept the file away.
    import subprocess  # noqa: PLC0415
    import pytest  # noqa: PLC0415
    D = _dr()
    adapters = pathlib.Path(__file__).resolve().parent.parent
    pre = tmp_path / "pre.json"
    subprocess.run([sys.executable, str(adapters / "claude-ai" / "preprocess_claude_ai.py"),
                    str(adapters / "tests" / "fixtures" / "claude-ai-2026-09-26-probe.json"), str(pre)],
                   check=True, capture_output=True, timeout=120)
    dsl = adapters / "claude-ai" / "2026-09-26.dsl.yaml"
    for op in ({"type": "scripted", "capture_mode": "scrubbed"},
               {"type": "delegated", "capture_mode": "reference_only"},
               {"type": "human", "operator_aware": True, "capture_mode": "pseudonymised"}):
        out = tmp_path / f"out-{op['type']}.json"
        with pytest.raises(SystemExit) as e:
            D.run(dsl, pre, out, operator_override=op)
        assert f"capture_mode {op['capture_mode']} is refused" in str(e.value), e.value
        assert not out.exists(), op
    out = tmp_path / "out-full.json"
    D.run(dsl, pre, out, operator_override={"type": "delegated", "capture_mode": "full"})
    assert json.loads(out.read_text())[0]["operator"] == {"type": "delegated", "capture_mode": "full"}


def test_the_capture_mode_options_offer_only_what_is_performed():
    # Ruled 2026-10-27: dsl_runner's help says only full is accepted (its
    # choices keep the four so the refusal can say why); verify_run offers
    # only full.
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    adapters = pathlib.Path(__file__).resolve().parent.parent
    env = {**os.environ, "COLUMNS": "200", "NO_COLOR": "1", "PYTHON_COLORS": "0", "FORCE_COLOR": ""}

    def run(script, *argv):
        r = subprocess.run([sys.executable, str(adapters / script), *argv], capture_output=True, text=True,
                           timeout=120, env=env)
        return r.returncode, r.stdout + r.stderr

    code, out = run("dsl_runner.py", "--help")
    assert code == 0 and "Only 'full' is accepted" in out, out
    code, out = run("verify_run.py", "--help")
    assert code == 0 and "{full}" in out and "only full" in out and "scrubbed}" not in out, out
    code, out = run("verify_run.py", "a", "i", "-", "--operator-type", "human", "--operator-aware", "true",
                    "--operator-capture-mode", "scrubbed")
    assert code == 2 and "invalid choice: 'scrubbed'" in out, out
    # drift_core's options, shared by every drift_watch.py, are the same.
    import argparse  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    ap = argparse.ArgumentParser()
    _drift_core().add_operator_arguments(ap)
    assert ap.parse_args(["--operator-type", "delegated", "--operator-capture-mode", "full"]).operator_capture_mode \
        == "full"
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            ap.parse_args(["--operator-type", "delegated", "--operator-capture-mode", "scrubbed"])
            raise AssertionError("drift_core accepted scrubbed")
        except SystemExit as e:
            assert e.code == 2
    assert "invalid choice: 'scrubbed'" in err.getvalue(), err.getvalue()


# --- a JSON input nested past the parser's own limit ----------------------
# Ruled 2026-10-28: DSL rc3 §4 holds at every depth past 512, not only up to
# where json.loads gives up. Past that (RecursionError), the document is
# validated by an iterative scan, an over-deep top-level element is skipped as
# one 513 deep is, and the others are kept; a malformed document stays
# unreadable with the reason a readable depth gives; an object root that deep
# is skipped as record 0. Each case is read at a depth the parser handles and
# at one it does not, and the two verdicts must be equal.

_PAST_PARSER = 200_000   # json.loads raises RecursionError (3.14: from about 150,000)


def _read_json_text(text, **kw):
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "in.json"
        p.write_text(text, encoding="utf-8")
        return source_reader.read_json(p, **kw)


def _json_verdict(text, **kw):
    """read_json's verdict on `text`: the unreadable reason, or the kept
    indices and records, the skipped indices, the root kind, the record count
    and whether a duplicate key was reported."""
    try:
        r = _read_json_text(text, **kw)
    except source_reader.SourceUnreadable as e:
        return ("unreadable", e.reason, e.detail)
    skipped = sorted(f["record_index"] for f in r.findings
                     if f.get("kind") == "parse-error" and f.get("reason") == "nesting-too-deep")
    dup = any(f.get("reason") == "duplicate-key" for f in r.findings)
    return ("read", r.record_indices, r.records, skipped, r.root_kind, r.record_count_read, dup)


def _past_parser(text):
    try:
        json.loads(text)
    except RecursionError:
        return True
    return False


_766_WELL_FORMED = (
    lambda d: '[{"a": 1}, ' + _nested_text(d) + ', {"b": 2}]',
    lambda d: ' \n[\t' + _nested_text(d) + ' ]\r\n',                  # the only element, whitespace around
    lambda d: ' \r\n[{"a": 1}, ' + _nested_text(d) + ']',              # whitespace before an array root with a sibling
    lambda d: '{"v": ' + _nested_text(d) + '}',                       # an object root: skipped as record 0
    lambda d: _nested_text(d),                                        # an array root whose element is deep
    lambda d: '[null, 0, false, "", [], {}, ' + _nested_text(d) + ', 0.0]',   # present null and falsy kept
    lambda d: '[{"s": "' + "[" * 600 + '\\"[{"}, ' + _nested_text(d) + ']',   # brackets in a string count nothing
    lambda d: '[' + _nested_text(512) + ', ' + _nested_text(513) + ', ' + _nested_text(d) + ']',   # the exact bound
    lambda d: '[{"k": 1, "k": 2}, ' + _nested_text(d) + ']',          # a duplicate in a kept element
    lambda d: '[' + "[" * d + '{"k": 1, "k": 2}' + "]" * d + ', {"a": 1}]',   # a duplicate only in the skipped one
    lambda d: '[{"n": 1e999, "m": 9007199254740993}, ' + _nested_text(d) + ']',   # numbers kept as strings
    lambda d: '[{"v": [' + _nested_text(d) + ']}, {"a": 1}]',         # deep inside an object inside the root
    lambda d: '[{"a": 1}, ' + "[" * d + "1" * 5000 + "]" * d + ']',   # an integer past int()'s digit limit, read
    lambda d: '[' + "[" * d + '{"a": 1, "b": 2, "a": 3}' + "]" * d + ', 1]',   # a duplicate at the third name
    lambda d: '[' + "[" * d + '{"a": 1, "b": 2, "c": 3}' + "]" * d + ', 1]',   # three names, no duplicate
    lambda d: '[' + "[" * d + '{"a": 1, "b": 2, "c": 3, "c": 4}' + "]" * d + ', 1]',   # a duplicate past the second name
    lambda d: '[{"a": 1}, ' + '{"k": ' * d + '1' + '}' * d + ']',     # a deep chain of one-member objects
)


def test_766_a_json_container_past_the_parsers_limit_is_skipped_not_fatal():
    # Reproducer b11 (adversarial-2026-10-03-r3): the good sibling was lost
    # with the whole input from about 150,000 levels.
    for depth in (150_000, 1_000_000):
        text = ('[{"trace_id": "good", "events": [{"v": 1}]}, '
                '{"trace_id": "deep", "events": [{"v": ' + _nested_text(depth) + '}]}]')
        out, err = _run_raw(_DEEP_DSL % "json", text, "json")
        assert out is not None and [t["id"] for t in out] == ["good"], (depth, out)
        assert "nesting-too-deep" in err, (depth, err)


def test_766_past_the_parsers_limit_read_json_gives_the_readable_depths_verdict():
    for n, doc in enumerate(_766_WELL_FORMED):
        deep = doc(_PAST_PARSER)
        assert _past_parser(deep), n   # else this case never reaches the new path
        for require_object in (False, True):
            want = _json_verdict(doc(1000), require_object=require_object)
            assert want[0] == "read" and want[3], (n, want)   # something is skipped, not unreadable
            got = _json_verdict(deep, require_object=require_object)
            assert got == want, (n, require_object, got[:2], want[:2])


def test_766_an_object_root_past_the_parsers_limit_is_record_0_skipped():
    got = _json_verdict('{"v": ' + _nested_text(_PAST_PARSER) + '}')
    assert got == ("read", [], [], [0], "object", 1, False), got


def test_766_kept_siblings_of_a_deep_element_are_unchanged_present_null_and_falsy_included():
    got = _json_verdict('[null, 0, false, "", [], {}, ' + _nested_text(_PAST_PARSER) + ', 0.0]')
    assert got[1] == [0, 1, 2, 3, 4, 5, 7] and got[3] == [6], got[1:4]
    assert got[2] == [None, 0, False, "", [], {}, 0.0], got[2]
    assert [type(v) for v in got[2]] == [type(None), int, bool, str, list, dict, float]


_766_MALFORMED_INNER = ("1 2", "1,", '"a\\q"', '"\\u12"', '"a', "NaN", "-Infinity", "01", "1.",
                        "tru", '{"a" 1}', '{"a":1,}', '{1:2}', '{"a":}', '"\x01"', "]", "-",
                        '{"a":1 "b":2}', '[1,]',
                        "1١", "1.١", "1e١", "2９")   # JSON digits are ASCII only


def test_766_a_malformed_document_past_the_parsers_limit_stays_unreadable_with_the_same_reason():
    docs = [lambda d, i=i: '[{"ok": 1}, ' + "[" * d + i + "]" * d + ']' for i in _766_MALFORMED_INNER]
    docs += [
        lambda d: '[' + "[" * d,                                       # never closed
        lambda d: '[' + _nested_text(d),                               # the root never closed
        lambda d: '[{"ok": 1}, ' + _nested_text(d) + '] x',            # extra data
        lambda d: '{"v": ' + _nested_text(d) + '} x',                  # extra data after an object root
        lambda d: '{"v": ' + _nested_text(d) + '} {}',
        lambda d: '[' + _nested_text(d) + ',]',                        # a trailing comma at the top
        lambda d: '[1 2, ' + _nested_text(d) + ']',                    # the error before the deep element
        lambda d: '[' + _nested_text(d) + ', 1 2]',                    # the error after it
        lambda d: '{"v": ' + _nested_text(d) + ', }',
        lambda d: '{"v" ' + _nested_text(d) + '}',
    ]
    for n, doc in enumerate(docs):
        want = _json_verdict(doc(1000))
        assert want[0] == "unreadable" and want[1:] != ("malformed-json", "nesting-too-deep"), (n, want)
        got = _json_verdict(doc(_PAST_PARSER))
        assert got == want, (n, got, want)


def test_766_gemini_cli_refuses_a_session_file_with_no_readable_record():
    """Ruled 2026-10-28: a session file whose one record is skipped (nested
    deeper than 512, at any depth) has nothing to convert; gemini-cli refuses
    it with a message and exit 2, as an unparsable file, where it raised
    IndexError at src.records[0]."""
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    script = _ADAPTERS / "gemini-cli" / "preprocess_gemini_cli.py"
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        cases = {"honest": json.dumps(_gemini_session()), "empty array": " [ ] "}
        for depth in (1000, _PAST_PARSER):
            cases[f"deep {depth}"] = '{"sessionId": "g1", "v": ' + _nested_text(depth) + '}'
        for name, text in cases.items():
            src, out = td / "s.json", td / "o.json"
            out.unlink(missing_ok=True)
            src.write_text(text)
            p = subprocess.run([sys.executable, str(script), str(src), str(out)],
                               capture_output=True, text=True, timeout=120)
            if name == "honest":
                assert p.returncode == 0 and out.exists(), (name, p.returncode, p.stderr[-300:])
                continue
            assert p.returncode == 2, (name, p.returncode, p.stderr[-300:])
            assert "Traceback" not in p.stderr, (name, p.stderr[-300:])
            # As an unparsable file reports it: the path as written, unquoted.
            assert f"error: {src}: no readable record" in p.stderr, (name, p.stderr[-300:])
            assert not out.exists(), name


def main(tests: list | None = None) -> int:
    """A run without pytest, of every test_ function here unless `tests`
    (name, function) pairs are given. A test that takes pytest fixtures, or
    skips itself with pytest.skip, is reported as skipped, not as failed
    (ruled 2026-10-22); the exit status is 1 only for a failure."""
    import inspect  # noqa: PLC0415
    try:
        import pytest  # noqa: PLC0415
        skip_exc: tuple = (pytest.skip.Exception,)
    except ImportError:
        skip_exc = ()
    if tests is None:
        tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = skipped = 0
    for name, fn in tests:
        if inspect.signature(fn).parameters:
            skipped += 1
            print(f"  skip  {name}: needs pytest fixtures")
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except skip_exc as e:
            skipped += 1
            print(f"  skip  {name}: {e}")
        except Exception as e:  # noqa: BLE001 - an unexpected raise is a failure too
            failed += 1
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed - skipped}/{len(tests)} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
