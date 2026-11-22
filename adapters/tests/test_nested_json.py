"""JSON nested in an accepted record (tool arguments, content or metadata
columns), capture-side --framework-meta files and agentdojo run files are
read under the DSL spec §4 input rule: a number the trace
cannot hold as a number (beyond +-(2^53 - 1) by value, or too long for int())
is kept as its source text, so it neither raises out of the pre-processor nor
costs the value. Only NaN and Infinity, which are not JSON, are refused: the
nested value falls back to its default and is counted."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

_ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADAPTERS))
sys.path.insert(0, str(_ADAPTERS / "tests"))

from source_reader import nested_json_loads, take_nested_refused  # noqa: E402
from test_content_loss import _goose_msg, _vibe_session, convert  # noqa: E402

BIG = str(2**60)                 # beyond +-(2^53 - 1)
LONG = "1" * 5000                # beyond Python's 4300-digit conversion limit


def _load(adapter: str, module: str):
    sys.path.insert(0, str(_ADAPTERS / adapter))
    return __import__(module)


def test_helper_defaults_counts_and_resets():
    take_nested_refused()
    assert nested_json_loads("{not json", {}) == {}
    assert nested_json_loads(None, []) == []
    assert take_nested_refused() == 0                     # malformed: not counted
    assert nested_json_loads('{"a": %s}' % BIG, {}) == {"a": BIG}     # kept as source text
    assert nested_json_loads('[%s]' % LONG, []) == [LONG]
    assert nested_json_loads('{"a": NaN}', {}) == {}                # not JSON: refused
    assert nested_json_loads('{"a": Infinity}', {}, count=False) == {}
    assert take_nested_refused() == 1
    assert take_nested_refused() == 0
    assert nested_json_loads('{"a": 1}', {}) == {"a": 1}


def test_vibe_tool_arguments_keep_out_of_domain_numbers_as_text(tmp_path):
    def call(cid, args):
        return {"id": cid, "type": "function", "function": {"name": "read_file", "arguments": args}}
    d = _vibe_session(tmp_path, [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            call("a1", '{"offset": %s}' % BIG), call("a2", '{"offset": %s}' % LONG),
            call("a3", '{"offset": 3}')]},
        {"role": "tool", "name": "read_file", "tool_call_id": "a1", "content": "x"},
        {"role": "tool", "name": "read_file", "tool_call_id": "a2", "content": "y"},
        {"role": "tool", "name": "read_file", "tool_call_id": "a3", "content": "z"},
        {"role": "assistant", "content": "done"}])
    [trace] = convert(tmp_path, "vibe", "vibe.dsl.yaml", "preprocess_vibe.py", d)
    args = {c["id"]: c["input"]["arguments"] for c in trace["tool_calls"]}
    assert args == {"a1": {"offset": BIG}, "a2": {"offset": LONG}, "a3": {"offset": 3}}
    assert not [a for a in trace["annotations"] if "nested_json_refused" in a]   # nothing lost


def test_vibe_clean_session_has_no_refusal_annotation(tmp_path):
    d = _vibe_session(tmp_path, [{"role": "user", "content": "hi"},
                                 {"role": "assistant", "content": "hello"}])
    [trace] = convert(tmp_path, "vibe", "vibe.dsl.yaml", "preprocess_vibe.py", d)
    assert not [a for a in trace["annotations"] if "nested_json_refused" in a]


def test_goose_content_and_metadata_keep_out_of_domain_numbers_as_text():
    pg = _load("goose", "preprocess_goose")
    take_nested_refused()
    big = {"role": "user", "content_json": '[{"type": "text", "text": "a", "n": %s}]' % BIG,
           "metadata_json": '{"n": %s}' % LONG, "created_timestamp": 1767261601,
           "message_id": "g1"}
    pg.annotate_messages([_goose_msg(0, "user", [{"type": "text", "text": "ok"}]), big])
    assert pg.parse_model_config('{"n": %s}' % BIG) == {"n": BIG}
    assert pg.parse_extension_data('{"e": %s}' % BIG) == (["e"], {"e": BIG})
    assert take_nested_refused() == 0
    assert pg.parse_model_config('{"n": NaN}') == {} and take_nested_refused() == 1


def test_codex_arguments_keep_out_of_domain_numbers_and_refuse_nan_once():
    pc = _load("codex", "preprocess_codex")
    take_nested_refused()
    raw = '{"cmd": "ls", "n": %s}' % BIG
    pc.classify_function("exec_command", raw)
    tc = pc.build_tool_call_record("c1", "exec_command", raw)
    assert tc["arguments"] == {"cmd": "ls", "n": BIG} and tc["raw_arguments"] == raw
    assert take_nested_refused() == 0
    nan = '{"cmd": "ls", "n": NaN}'
    tc = pc.build_tool_call_record("c2", "exec_command", nan)
    assert tc["arguments"] == {"raw": nan} and take_nested_refused() == 1


def test_framework_meta_keeps_its_fields_beside_a_long_number_and_reports_nan(tmp_path, capsys):
    pa = _load("aider", "preprocess_aider")
    fm = tmp_path / "framework-meta.json"
    fm.write_text('{"model": "m", "n": %s}' % LONG)
    container: dict = {"model": None}
    pa.apply_framework_meta(container, fm)
    assert container["model"] == "m" and "unusable" not in capsys.readouterr().err
    fm.write_text('{"model": "m2", "n": NaN}')
    container = {"model": None}
    pa.apply_framework_meta(container, fm)
    assert container["model"] is None and "unusable" in capsys.readouterr().err


def test_agentdojo_run_with_nan_is_skipped_and_counted(tmp_path):
    # A run file is input: a big number is now read as text (DSL spec §4),
    # so the unreadable case is a token that is not JSON.
    root = tmp_path / "runs"
    root.mkdir()
    (root / "r.json").write_text('{"messages": [], "n": NaN}')
    out = tmp_path / "out.json"
    p = subprocess.run([sys.executable, str(_ADAPTERS / "agentdojo" / "preprocess_agentdojo.py"),
                        "--runs-root", str(root), "--output", str(out)],
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr
    assert json.loads(out.read_text()) == []
    assert "'unreadable': 1" in p.stderr


def test_malformed_or_non_string_nested_json_is_reported_not_silent(capsys):
    """Ruled 2026-10-28: a nested value that is not JSON, or not a string,
    still gives the default, and now says so on stderr with "warning:" (so
    verify_run reports the conversion incomplete). It is not counted as
    refused: that count feeds the strict-parsing annotation. None is an
    absent column and stays silent, as does an honest value."""
    take_nested_refused()
    for raw, default, what in (("{not json", {}, "not JSON"), ('[{"a": 1}, ', [], "not JSON"),
                               (5, [], "not a string (int)"), ([1], {}, "not a string (list)")):
        assert nested_json_loads(raw, default) == default
        err = capsys.readouterr().err
        assert err.count("warning:") == 1 and "nested JSON value unreadable" in err and what in err, (raw, err)
    assert take_nested_refused() == 0
    for raw, default in ((None, []), ('{"a": 1}', {}), ("[]", [])):
        nested_json_loads(raw, default)
        assert capsys.readouterr().err == "", raw
    # A second parse of a value already reported, and a caller that opts out.
    nested_json_loads("{not json", {}, count=False)
    nested_json_loads("{not json", {}, report_malformed=False)
    assert capsys.readouterr().err == ""


def test_dsl_json_parse_of_malformed_text_stays_silent(capsys):
    """DSL rc3 §9: json_parse returns None on parse failure; the runner adds
    no warning of its own."""
    import dsl_runner  # noqa: PLC0415
    assert dsl_runner.fn_json_parse("{not json") is None
    assert capsys.readouterr().err == ""


def test_goose_truncated_content_json_is_reported(tmp_path):
    """The reproducer: the last message's content_json cut in half used to
    drop the final assistant answer with exit 0 and no warning."""
    import shutil  # noqa: PLC0415
    import sqlite3  # noqa: PLC0415
    db = tmp_path / "s1.db"
    shutil.copyfile(_ADAPTERS / "goose/regression/1.28.0/inputs/s1-fetch.db", db)
    c = sqlite3.connect(db)
    rowid, cj = c.execute("SELECT rowid, content_json FROM messages ORDER BY rowid DESC LIMIT 1").fetchone()
    c.execute("UPDATE messages SET content_json = ? WHERE rowid = ?", (cj[: len(cj) // 2], rowid))
    c.commit(); c.close()
    p = subprocess.run([sys.executable, str(_ADAPTERS / "goose/preprocess_goose.py"), str(db), str(tmp_path / "o.json")],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-300:]
    assert "warning: nested JSON value unreadable, not in the trace: not JSON" in p.stderr, p.stderr[-300:]


def _store_run(tmp_path, harness: str, rel: str, edits) -> str:
    """Run a shipped pre-processor on a copy of a shipped store after `edits`
    (sqlite statements); return its stderr."""
    import shutil  # noqa: PLC0415
    import sqlite3  # noqa: PLC0415
    db = tmp_path / f"{harness}.db"
    shutil.copyfile(_ADAPTERS / rel, db)
    c = sqlite3.connect(db)
    for sql in edits:
        c.execute(sql)
    c.commit(); c.close()
    p = subprocess.run([sys.executable, str(_ADAPTERS / harness / f"preprocess_{harness}.py"), str(db),
                        str(tmp_path / f"{harness}.json")], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-300:]
    return p.stderr


def test_goose_reports_shape_losses_once_per_session(tmp_path):
    """Ruled 2026-10-28: content that is not a list, a block that is not an
    object or of a type the adapter does not know, and an assistant message
    dropped although it held blocks, each counted and reported in one
    "warning:" line per session; an honest store gives none."""
    for honest in sorted((_ADAPTERS / "goose/regression").glob("*/inputs/*.db")):   # thinking, text, tools
        assert "warning:" not in _store_run(tmp_path, "goose", str(honest.relative_to(_ADAPTERS)), []), honest
    rel = "goose/regression/1.33.1/inputs/s1-fetch.db"
    first = "(SELECT min(id) FROM messages WHERE role = '%s')"
    err = _store_run(tmp_path, "goose", rel, [
        f"UPDATE messages SET content_json = '{{\"type\": \"text\"}}' WHERE id = {first % 'user'}",
        f"UPDATE messages SET content_json = '[5, {{\"type\": \"futureBlock\"}}, {{\"type\": \"text\", \"text\": \"\"}}]'"
        f" WHERE id = {first % 'assistant'}",
    ])
    lines = [ln for ln in err.splitlines() if ln.startswith("warning: goose session ")]
    assert len(lines) == 1, err[-500:]
    line = lines[0]
    for part in ("1 message content not a list", "1 block not an object", "1 block of unknown type (futureBlock)",
                 "1 assistant message dropped with no text, thinking or tool call"):
        assert part in line, (part, line)
    # An assistant message whose only block is an empty text block lost
    # nothing: no warning.
    err = _store_run(tmp_path, "goose", rel, [
        f"UPDATE messages SET content_json = '[{{\"type\": \"text\", \"text\": \"\"}}]' WHERE id = {first % 'assistant'}"])
    assert "warning:" not in err, err[-300:]
    # A text block whose text is not a string did hold something: dropped;
    # so did a thinking block with only a signature.
    for block in ('{"type": "text", "text": 5}', '{"type": "text", "text": 0}',
                  '{"type": "thinking", "thinking": "", "signature": "SIGabc"}'):
        err = _store_run(tmp_path, "goose", rel, [
            f"UPDATE messages SET content_json = '[{block}]' WHERE id = {first % 'assistant'}"])
        assert "1 assistant message dropped with no text, thinking or tool call" in err, (block, err[-300:])


def test_opencode_reports_shape_losses_once_per_session(tmp_path):
    for honest in sorted((_ADAPTERS / "opencode/regression").glob("*/inputs/*.db")):
        assert "warning:" not in _store_run(tmp_path, "opencode", str(honest.relative_to(_ADAPTERS)), []), honest
    rel = "opencode/regression/1.17.15/inputs/s1-config.db"
    asst = "(SELECT id FROM message WHERE json_extract(data, '$.role') = 'assistant' ORDER BY time_created, id LIMIT 1)"
    # An assistant message with only step-start/step-finish parts is an honest
    # shape (seen in the 1.2.20 corpus): no content, nothing lost.
    step_only = _store_run(tmp_path, "opencode", rel, [
        f"DELETE FROM part WHERE message_id = {asst} AND json_extract(data, '$.type') NOT IN ('step-start', 'step-finish')"])
    assert "warning:" not in step_only, step_only[-300:]
    err = _store_run(tmp_path, "opencode", rel, [
        f"UPDATE part SET data = '5' WHERE id = (SELECT min(id) FROM part WHERE message_id != {asst})",
        f"UPDATE part SET data = '{{\"type\": \"futurePart\"}}' WHERE message_id = {asst}",
    ])
    lines = [ln for ln in err.splitlines() if ln.startswith("warning: opencode session ")]
    assert len(lines) == 1, err[-500:]
    line = lines[0]
    assert "1 part not an object" in line, line
    assert "part(s) of unknown type (futurePart)" in line, line
    assert "1 assistant message with no text, reasoning or tool call" in line, line
    # Ruled 2026-10-28: a patch part never reaches the
    # trace (the DSL does not map it), so it is reported, in any version;
    # an empty text part beside step parts lost nothing.
    err = _store_run(tmp_path, "opencode", rel, [
        f"INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) SELECT 'prt_patch', {asst},"
        f" session_id, time_created, time_updated, '{{\"type\": \"patch\", \"hash\": \"h1\", \"files\": [\"a.py\"]}}'"
        f" FROM message WHERE id = {asst}"])
    assert "1 patch part(s) not carried into the trace" in err, err[-300:]
    err = _store_run(tmp_path, "opencode", rel, [
        f"DELETE FROM part WHERE message_id = {asst} AND json_extract(data, '$.type') NOT IN ('step-start', 'step-finish')",
        f"INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) SELECT 'prt_empty', {asst},"
        f" session_id, time_created, time_updated, '{{\"type\": \"text\", \"text\": \"\"}}' FROM message WHERE id = {asst}"])
    assert "warning:" not in err, err[-300:]


def test_codex_plain_text_arguments_are_kept_and_not_reported_lost(capsys):
    """codex keeps arguments that are not JSON as {"raw": text} (and in
    raw_arguments), so nothing is lost and no "warning:" is printed."""
    pc = _load("codex", "preprocess_codex")
    for raw in ("ls -la", "", "{half"):
        rec = pc.build_tool_call_record("c", "exec_command", raw)
        assert rec["arguments"] == {"raw": raw} and rec["raw_arguments"] == raw, rec
        assert "warning:" not in capsys.readouterr().err, raw
