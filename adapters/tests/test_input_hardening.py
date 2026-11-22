"""Hostile session input: files reached through symlinks are not read, and
the parsers stay linear on crafted lines."""
from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "codex"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "aider"))

from input_tree import keep_within_tree, within_tree  # noqa: E402
import source_reader  # noqa: E402
from preprocess_codex import sed_inplace_target  # noqa: E402
from preprocess_aider import match_model_line  # noqa: E402


def test_symlinked_and_escaping_files_are_skipped(tmp_path, capsys):
    root = tmp_path / "sessions"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "secret.jsonl"
    outside.write_text("{}\n")
    real = root / "a.jsonl"
    real.write_text("{}\n")
    link = root / "b.jsonl"
    link.symlink_to(outside)
    (root / "linkdir").symlink_to(tmp_path)
    via_dir = root / "linkdir" / "secret.jsonl"
    assert within_tree(real, root)
    assert not within_tree(link, root)
    assert not within_tree(via_dir, root)
    kept = keep_within_tree([real, link, via_dir], root)
    assert kept == [real]
    assert capsys.readouterr().err.count("warning: skipped") == 2


def test_warning_escapes_control_characters(tmp_path, capsys):
    root = tmp_path / "r"
    root.mkdir()
    bad = root / "x\x1b]0;PWNED\x07.jsonl"
    bad.symlink_to(tmp_path)
    keep_within_tree([bad], root)
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\\x1b" in err


def _fast(fn, *args, limit=1.0):
    t = time.time()
    fn(*args)
    return time.time() - t < limit


def test_decode_is_linear_on_invalid_bytes():
    assert _fast(source_reader._decode_line, b"\xff" * 400_000, "utf-8")


def test_sed_target_is_linear_and_correct():
    assert _fast(sed_inplace_target, "sed -i " + " " * 20_000)
    assert sed_inplace_target("sed -i 's/a b/c/' file.txt") == "file.txt"
    assert sed_inplace_target("sed -i.bak  f") == "f"
    assert sed_inplace_target("sed -i f") is None


def test_aider_model_line_is_linear_and_correct():
    assert _fast(match_model_line, "> Model: m with " + " " * 200_000)
    assert match_model_line("> Model: gpt-4o with diff edit format") == ("gpt-4o", "diff")
    assert match_model_line("> Model: m with  \t edit format") == ("m", "\t")
    assert match_model_line("> Model: m with  edit format") is None


def test_huge_integer_is_read_as_its_source_text(tmp_path):
    # DSL spec §4: a number the trace cannot hold is read as its source text,
    # so a 5000-digit integer is kept, not skipped (and int() never sees it).
    f = tmp_path / "x.jsonl"
    f.write_bytes(b'{"a":1}\n{"n":' + b"9" * 5000 + b"}\n")
    r = source_reader.read_jsonl(f)
    assert r.record_count_skipped == 0 and r.records == [{"a": 1}, {"n": "9" * 5000}]


def test_lone_surrogate_is_replaced_and_recorded(tmp_path):
    f = tmp_path / "x.jsonl"
    f.write_text('{"t": "ok \\ud83d end"}\n')
    r = source_reader.read_jsonl(f)
    assert r.records[0]["t"] == "ok � end"
    assert any(x.get("reason") == "lone-surrogate" for x in r.findings)


def test_invalid_but_well_shaped_timestamps_are_null_and_a_leap_second_maps():
    # Ruled 2026-10-29: never passed through; a leap second at 23:59:60 UTC
    # is 23:59:59.999999 of that day (DSL rc3 §9.1, minitrace-spec §3.5).
    from timestamps import naive_local_to_utc, to_spec_utc
    assert to_spec_utc("2026-06-30T23:59:60Z") == "2026-06-30T23:59:59.999999Z"
    for ts in ("2026-02-30T10:00:00Z", "2026-01-01T10:00:00+24:00"):
        assert to_spec_utc(ts) is None, ts
    assert naive_local_to_utc("2026-02-30 10:00:00") == "2026-02-30 10:00:00"
    assert to_spec_utc("2026-01-01T10:00:00+01:00") == "2026-01-01T09:00:00.000000Z"


def test_lint_discovery_ignores_the_checkout_path():
    import lint_dispatch
    assert len(lint_dispatch.discover_adapters()) > 10


def test_printable_escapes_terminal_and_bidi_controls():
    from input_tree import printable
    out = printable("a\x1b]0;T\x07b\rc\u202ed\u200be\u0085f\n\tg")
    assert out == "a\\u001b]0;T\\u0007b\\u000dc\\u202ed\\u200be\\u0085f\n\tg"


def test_installed_sanitiser_covers_stdout_and_stderr():
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); from input_tree import install_output_sanitiser;"
            "install_output_sanitiser(); print('x\\x1b[2Jy'); print('z\\u202e', file=sys.stderr)"
            % str(pathlib.Path(__file__).resolve().parent.parent))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0
    assert "\x1b" not in r.stdout and "\\u001b" in r.stdout
    assert "\u202e" not in r.stderr and "\\u202e" in r.stderr


def test_symlinked_subagents_directory_is_refused(tmp_path, capsys):
    # The walk root must sit above <session>/subagents/, else a symlinked
    # subagents/ directory resolves "inside" itself and is followed.
    proj = tmp_path / "proj"
    (proj / "sid").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "agent-1.jsonl").write_text("{}\n")
    (proj / "sid" / "subagents").symlink_to(elsewhere)
    found = sorted((proj / "sid" / "subagents").glob("*.jsonl"))
    assert found  # the glob does follow the link
    assert keep_within_tree(found, proj) == []
    assert "warning: skipped" in capsys.readouterr().err


def test_preprocessor_reader_reports_skipped_lines(tmp_path, capsys):
    f = tmp_path / "s.jsonl"
    f.write_bytes(b'{"a": 1}\n{"b": \n{"c": "\xff"}\n')
    recs = source_reader.read_jsonl_records(f)
    assert [list(r) for r in recs] == [["a"], ["c"]]
    err = capsys.readouterr().err
    assert "1 of 3 line(s) skipped" in err and "parse-error" in err and "decode-error" in err
    empty = tmp_path / "e.jsonl"
    empty.write_text("")
    assert source_reader.read_jsonl_records(empty) == []


def test_decode_bookkeeping_is_capped():
    text, runs = source_reader._decode_line(b"a\xff" * 5000, "utf-8")
    assert len(runs) == source_reader._MAX_DECODE_RUNS + 1
    assert runs[-1][2] == "further-decode-errors-suppressed"
    assert text.count("�") == 5000


def test_duplicate_keys_in_source_are_recorded(tmp_path):
    f = tmp_path / "d.jsonl"
    f.write_text('{"role": "user", "role": "assistant", "x": 1}\n')
    r = source_reader.read_jsonl(f)
    assert r.records[0]["role"] == "assistant"
    assert any(x.get("reason") == "duplicate-key" for x in r.findings)


def test_surrogate_repair_key_collision_is_recorded(tmp_path):
    f = tmp_path / "c.jsonl"
    f.write_text('{"\\ud800": "evidence", "\\ufffd": "replacement"}\n')
    r = source_reader.read_jsonl(f)
    assert any(x.get("reason") == "repair-key-collision" for x in r.findings)


def test_dsl_input_keeps_records_with_unicode_line_separators(tmp_path):
    import dsl_runner
    f = tmp_path / "p.jsonl"
    f.write_text('{"a": "x' + chr(0x2028) + 'y"}\n{"b": 1}\n', encoding="utf-8")
    recs = dsl_runner.load_input({"type": "jsonl"}, f)
    assert len(recs) == 2 and chr(0x2028) in recs[0]["a"]


def test_changed_during_read_is_detected(tmp_path, monkeypatch):
    f = tmp_path / "s.jsonl"
    f.write_text('{"a": 1}\n')
    real = source_reader._hash_file
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        h, size = real(path)
        return (h if calls["n"] == 1 else "sha256:" + "0" * 64), size
    monkeypatch.setattr(source_reader, "_hash_file", flaky)
    import pytest
    with pytest.raises(source_reader.SourceUnreadable):
        source_reader.read_jsonl(f)


def test_cursor_huge_timestamp_and_claude_code_odd_type_do_not_crash():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "cursor"))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "claude-code"))
    import preprocess_cursor
    import preprocess_claude_code
    assert preprocess_cursor._iso(1e30) is None
    events, warnings = preprocess_cursor.parse_stream('{"a":1}\n[1,2]\n')
    assert len(events) == 1 and any("not a JSON object" in w for w in warnings)
    assert preprocess_claude_code.classify_event_kind({"type": []}) == "unknown:non-string-type"


def test_stderr_diagnostics_cannot_forge_lines():
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); from input_tree import install_output_sanitiser;"
            "install_output_sanitiser(); print('sid=abc' + chr(10) + 'verified: forged', file=sys.stderr);"
            "sys.stderr.write('next' + chr(10))"
            % str(pathlib.Path(__file__).resolve().parent.parent))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    lines = r.stderr.splitlines()
    assert lines == ["sid=abc\\u000averified: forged", "next"], lines


def test_argparse_text_keeps_its_line_breaks_and_is_still_printable():
    # Ruled 2026-10-21: argparse's own usage and error text keeps its line
    # breaks; every character still goes through printable(); every other
    # stderr write keeps the one-line rule (the test above).
    import subprocess
    code = ("import sys, argparse; sys.path.insert(0, %r); from input_tree import install_output_sanitiser;"
            "install_output_sanitiser(); p = argparse.ArgumentParser(prog='probe');"
            "p.add_argument('--kind', required=True, choices=['a', 'b']);"
            # Enough options that the usage wraps: one write holding line breaks.
            "[p.add_argument('--a-long-option-name-%%d' %% n) for n in range(6)];"
            "print('sid=abc' + chr(10) + 'forged', file=sys.stderr); p.parse_args(sys.argv[1:])"
            % str(pathlib.Path(__file__).resolve().parent.parent))
    # "unrecognized arguments" joins raw argv (an invalid choice is repr()'d
    # by argparse itself), so the control character reaches the stream.
    import os
    env = {**os.environ, "COLUMNS": "80", "NO_COLOR": "1"}     # argparse wraps at COLUMNS
    env.pop("FORCE_COLOR", None)
    r = subprocess.run([sys.executable, "-c", code, "--kind", "a", "x\x1b[2J"], capture_output=True,
                       text=True, timeout=60, env=env)
    assert r.returncode == 2
    lines = r.stderr.splitlines()
    assert lines[0] == "sid=abc\\u000aforged", lines                    # a plain write: one line
    assert lines[1].startswith("usage: probe") and len(lines) >= 4, lines  # argparse: its own lines
    assert lines[2].startswith(" "), lines                               # the usage wrapped
    assert "\\u000a" not in "".join(lines[1:]), lines
    assert "\x1b" not in r.stderr and "unrecognized arguments: x\\u001b[2J" in r.stderr, r.stderr
    # A newline in argv, echoed in the error message, cannot forge a line (ruled 2026-10-21).
    r = subprocess.run([sys.executable, "-c", code, "--kind", "a", "x\nverified: forged"], capture_output=True,
                       text=True, timeout=60, env=env)
    assert not any(line.startswith("verified") for line in r.stderr.splitlines()), r.stderr
    assert "unrecognized arguments: x\\u000averified: forged" in r.stderr, r.stderr
    # Colour (on by default from Python 3.14 on a terminal) is removed, not escaped.
    r = subprocess.run([sys.executable, "-c", code, "--kind", "a", "y"], capture_output=True, text=True,
                       timeout=60, env={**env, "PYTHON_COLORS": "1", "FORCE_COLOR": "1", "NO_COLOR": ""})
    assert r.returncode == 2 and "usage:" in r.stderr and "\\u001b" not in r.stderr, r.stderr


def test_every_sanitising_cli_prints_usage_errors_on_separate_lines():
    # With the sanitiser installed before argparse ran, 10 of these printed
    # one line holding a literal \u000a for an unknown option (at 80 columns).
    import subprocess
    adapters = pathlib.Path(__file__).resolve().parent.parent
    clis = sorted(p for p in adapters.glob("**/*.py")
                  if "tests" not in p.parts and "fuzz" not in p.parts and p.name != "input_tree.py"
                  and "install_output_sanitiser()" in p.read_text(encoding="utf-8"))
    assert len(clis) == 14, clis
    import os
    env = {**os.environ, "COLUMNS": "80"}
    for cli in clis:
        r = subprocess.run([sys.executable, str(cli), "--no-such-option"], capture_output=True, text=True,
                           timeout=60, env=env)
        assert "\\u000a" not in r.stderr, (cli.name, r.stderr)
        assert r.returncode != 0, cli.name


def test_field_escapes_newlines_and_bounds_length():
    from input_tree import field
    assert field("a" + chr(10) + "b" + chr(9) + "c") == "a\\u000ab\\u0009c"
    assert field("z" * 1000).endswith("... [1000 chars]")


def test_droid_settings_sidecar_symlink_is_ignored(tmp_path, capsys):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "droid"))
    import preprocess_droid
    private_file = tmp_path / "private.json"
    private_file.write_text('{"k": "v"}')
    d = tmp_path / "s"
    d.mkdir()
    (d / "abc.jsonl").write_text("{}\n")
    (d / "abc.settings.json").symlink_to(private_file)
    assert preprocess_droid.read_settings(d / "abc.jsonl") == {}
    assert "warning: skipped" in capsys.readouterr().err


def test_vibe_direct_session_dir_with_symlinked_member_is_refused(tmp_path, capsys):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "vibe"))
    import preprocess_vibe
    d = tmp_path / "session_20260101_000000_0000abcd"
    d.mkdir()
    (d / "meta.json").write_text("{}")
    outside = tmp_path / "other.jsonl"
    outside.write_text("{}\n")
    (d / "messages.jsonl").symlink_to(outside)
    assert preprocess_vibe.discover_sessions(d) == []
    assert "warning: skipped" in capsys.readouterr().err


def test_foreach_over_a_list_it_extends_terminates(tmp_path):
    import subprocess
    dsl = tmp_path / "loop.dsl.yaml"
    dsl.write_text(
        "adapter: {name: loop, harness_version: '0', dsl_version: '0.1.0',"
        " source_format: test, converter_version: test, produces_schema: '0.3.0'}\n"
        "input: {type: jsonl}\n"
        "session: {static: {}, finalize: {}}\n"
        "state: {xs: [1]}\n"
        "emit:\n"
        "  - name: r\n"
        "    when: {has_any: true}   # was all_of: [] (matched every record: all([])); refused at load\n"
        "    foreach_block: \"$state.xs\"\n"
        "    on_block:\n"
        "      - when: {has_any: true}\n"
        "        do: [{accumulate: {state.xs: 1}}]\n")
    inp = tmp_path / "in.jsonl"
    inp.write_text('{"a": 1}\n')
    runner = pathlib.Path(__file__).resolve().parent.parent / "dsl_runner.py"
    try:
        r = subprocess.run([sys.executable, str(runner), str(dsl), str(inp), "--operator-type", "scripted",
                            "--output", str(tmp_path / "o.json")], capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise AssertionError("foreach over a self-extending list did not terminate")
    # The loop was reached and completed. 3 is a converted run with a set-level
    # loss: this DSL gives its trace no id, so R5 does not emit it.
    assert r.returncode in (0, 3), r.stderr


def test_lint_fails_on_an_unreadable_adapter(tmp_path):
    import subprocess
    bad = tmp_path / "bad.dsl.yaml"
    bad.write_text("a: &x [1]\nb: *x\n")
    lint = pathlib.Path(__file__).resolve().parent.parent / "lint_dispatch.py"
    r = subprocess.run([sys.executable, str(lint), str(bad)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 1 and "could not be checked" in r.stdout


def test_gemini_timestamp_offset_and_tool_status():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "gemini-cli"))
    import preprocess_gemini_cli
    import dsl_runner
    assert preprocess_gemini_cli.normalise_timestamp("2026-01-01T10:00:00.123456+02:00") == "2026-01-01T08:00:00.123456Z"
    assert preprocess_gemini_cli.normalise_timestamp("2026-04-12T01:24:24.366Z") == "2026-04-12T01:24:24.366000Z"
    expr = "true if $block.status == 'success' else (false if $block.status == 'error' else null)"
    assert dsl_runner.evaluate(expr, {"block": {"status": "success"}}) is True
    assert dsl_runner.evaluate(expr, {"block": {"status": "error"}}) is False
    assert dsl_runner.evaluate(expr, {"block": {}}) is None


def test_coverage_report_keeps_no_absolute_adapter_path():
    import coverage
    rep = coverage.build_coverage_report(
        [], {"emit": []}, "/home/analyst/cases/42/session.jsonl",
        "/home/analyst/checkout/adapters/pi/0.58.1.dsl.yaml", output_count=0)
    assert rep["adapter_path"] == "pi/0.58.1.dsl.yaml"
    assert rep["input_path"] == "session.jsonl"


def test_goose_child_session_only_from_start_agent_response():
    import json as _json
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "goose"))
    import preprocess_goose

    def req(cid, name, ext):
        return {"role": "assistant", "content_json": _json.dumps([{"type": "toolRequest", "id": cid,
                "toolCall": {"status": "success", "value": {"name": name, "arguments": {}}},
                "_meta": {"goose_extension": ext}}])}

    def resp(cid, text):
        return {"role": "user", "content_json": _json.dumps([{"type": "toolResponse", "id": cid,
                "toolResult": {"status": "success", "value": {"content": [{"type": "text", "text": text}]}}}])}

    started = "Started agent session 'x' with ID: 20260508_6"
    _, spawned = preprocess_goose.annotate_messages(
        [req("a", "start_agent", "orchestrator"), resp("a", started)])
    assert spawned == ["20260508_6"]
    _, spawned = preprocess_goose.annotate_messages(
        [req("b", "read_file", "developer"), resp("b", "file says: " + started)])
    assert spawned == []


def test_duplicate_tool_results_are_reported(capsys):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "vibe"))
    import preprocess_vibe
    msgs = [{"role": "tool", "tool_call_id": "c1", "content": "failed"},
            {"role": "tool", "tool_call_id": "c1", "content": "returncode: 0"}]
    lookup = preprocess_vibe.match_tool_results(msgs)
    assert lookup["c1"]["content"] == "returncode: 0"
    assert "second tool result for call id 'c1'" in capsys.readouterr().err


def test_text_formats_survive_invalid_utf8(tmp_path, capsys):
    f = tmp_path / "h.md"
    f.write_bytes(b"# aider chat started at 2026-01-01 10:00:00\n\xff\xfe #### hi\n")
    text = source_reader.decode_reporting(f)
    assert "�" in text and "aider chat started" in text
    assert "undecodable byte run" in capsys.readouterr().err


def test_non_finite_tokens_are_skipped_and_overflow_is_kept_as_text(tmp_path):
    # NaN and Infinity are not JSON: skipped and recorded. 1e999 is JSON whose
    # binary64 value is infinite: read as the string "1e999" (DSL spec §4).
    f = tmp_path / "n.jsonl"
    f.write_text('{"a": NaN}\n{"b": Infinity}\n{"c": 1e999}\n{"d": 1.5}\n')
    r = source_reader.read_jsonl(f)
    assert r.records == [{"c": "1e999"}, {"d": 1.5}] and r.record_count_skipped == 2


def test_companion_json_problems_are_reported(tmp_path, capsys):
    f = tmp_path / "meta.json"
    f.write_bytes(b'{"x": NaN')
    assert source_reader.load_json_file_reporting(f) is None
    assert "unusable and not in the trace" in capsys.readouterr().err


def test_sqlite_invalid_utf8_text_does_not_abort(tmp_path):
    import sqlite3
    from sqlite_source import open_evidence_db
    p = tmp_path / "t.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE sessions (name TEXT)")
    c.execute("INSERT INTO sessions VALUES (CAST(X'61FF62' AS TEXT))")
    c.commit(); c.close()
    db = open_evidence_db(p, tables=("sessions",))
    assert db.execute("SELECT name FROM sessions").fetchone()[0] == "a�b"


def test_second_tool_result_is_diagnosed():
    import dsl_runner
    state = {"tool_calls_by_id": {"c1": {"id": "c1", "output": {"result": "real"}}}}
    diags = []
    dsl_runner._apply_actions([{"attach_tool_result": {"target_id": "c1", "output": {"result": "'forged'"}}}],
                              {"state": state}, {}, state, {}, diags)
    assert any("replaces the first" in d["message"] for d in diags)


def test_numbers_beyond_the_domain_are_read_as_source_text_by_value(tmp_path):
    # minitrace-spec §3.4 decides by value, in any notation; DSL spec §4 reads
    # such a number as its source text exactly as written. 2^53 .. the
    # 4300-digit limit used to abort sealing, and later skipped the record;
    # now nothing is skipped and every kept value seals (rfc8785).
    edge = 2**53 - 1
    f = tmp_path / "i.jsonl"
    f.write_text(f'{{"a": {edge}}}\n{{"b": {-edge}}}\n{{"c": {edge + 1}}}\n'
                 f'{{"d": [{-(edge + 1)}]}}\n{{"e": 1{"0" * 30}}}\n'
                 '{"f": 1e16}\n{"g": 9007199254740992.0}\n{"h": -1E300}\n{"i": 1e15}\n{"j": -0}\n')
    r = source_reader.read_jsonl(f)
    assert r.records == [{"a": edge}, {"b": -edge}, {"c": str(edge + 1)}, {"d": [str(-(edge + 1))]},
                         {"e": "1" + "0" * 30}, {"f": "1e16"}, {"g": "9007199254740992.0"},
                         {"h": "-1E300"}, {"i": 1e15}, {"j": 0}], r.records
    assert r.record_count_skipped == 0 and r.findings == []
    import rfc8785
    rfc8785.dumps(r.records)    # everything kept is sealable
    w = tmp_path / "w.json"
    w.write_text(f'{{"x": {edge + 1}}}')
    assert source_reader.read_json(w).records == [{"x": str(edge + 1)}]


def test_lint_exclusion_is_by_segment_and_reported(tmp_path, monkeypatch, capsys):
    import lint_dispatch
    for d in ("code-review-x", "review-copy", "pi/fixtures", "pi"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
        (tmp_path / d / "a.dsl.yaml").write_text("{}\n")
    monkeypatch.setattr(lint_dispatch, "__file__", str(tmp_path / "lint_dispatch.py"))
    found = [pathlib.Path(p).relative_to(tmp_path).as_posix()
             for p in lint_dispatch.discover_adapters()]
    assert found == ["code-review-x/a.dsl.yaml", "pi/a.dsl.yaml"]
    assert "not linted (review copy): review-copy/a.dsl.yaml" in capsys.readouterr().err


# --- T2: pre-processors read counts from numbers the reader may keep as text

def test_input_count_reads_only_in_domain_numbers():
    assert source_reader.input_count(None, "x") == 0
    assert source_reader.input_count(0, "x") == 0
    assert source_reader.input_count(12, "x") == 12 and source_reader.input_count(12.0, "x") == 12
    for bad in ("1e16", "9007199254740993", "12", True, [], {}):
        assert source_reader.input_count(bad, "x") is None, bad
    assert source_reader.input_amount(None, "x") == 0.0 and source_reader.input_amount(0.5, "x") == 0.5
    assert source_reader.input_amount("1e300", "x") is None


def test_preprocessors_survive_counts_the_reader_kept_as_text(tmp_path, capsys):
    # DSL spec §4: a number beyond ±(2^53-1) reaches a pre-processor as its
    # source text; int("1e16") used to raise and lose the whole conversion.
    import importlib  # noqa: PLC0415
    import json  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    adapters = pathlib.Path(__file__).resolve().parent.parent
    preprocess_codex = importlib.import_module("preprocess_codex")
    for sub in ("opencode", "vibe"):
        sys.path.insert(0, str(adapters / sub))
    po = importlib.import_module("preprocess_opencode")
    pv = importlib.import_module("preprocess_vibe")
    # An unusable part makes the sum null, never 0 (review #4); a sum of
    # in-domain parts outside ±(2^53-1) is null too (review #5).
    m = po.extract_step_finish_metrics([
        {"type": "step-finish", "tokens": {"input": "1e16", "output": 5}},
        {"type": "step-finish", "tokens": {"input": 7, "cache": {"read": "9007199254740993"}}}])
    assert (m["input_tokens"], m["output_tokens"], m["cache_read_tokens"]) == (None, 5, None), m
    m = po.extract_step_finish_metrics([{"type": "step-finish", "tokens": {"input": 2**52}}] * 2)
    assert m["input_tokens"] is None, m
    assert pv.derive_tool_approval_outcomes({"stats": {"tool_calls_agreed": "1e16", "tool_calls_failed": 2}}) \
        == {"agreed": None, "rejected": 0, "failed": 2, "succeeded": 0}
    tokens = {"input": 0, "output": 0}
    for v in (5, "1e16", 7):
        preprocess_codex.add_token(tokens, "input", v)
    preprocess_codex.add_token(tokens, "output", 5)
    assert tokens == {"input": None, "output": 5}, tokens
    err = capsys.readouterr().err
    assert err.count("is not a usable count;") == 4, err                     # reported, not silent
    assert err.count("sum outside ±(2^53-1)") == 1, err
    # Aider's suffixes ('M' too), droid's token reads
    # and a non-integral count follow the same rule (null and reported).
    for sub in ("aider", "droid"):
        sys.path.insert(0, str(adapters / sub))
    pa = importlib.import_module("preprocess_aider")
    pd = importlib.import_module("preprocess_droid")
    assert [pa._num(x) for x in ("1.2M", "1.2k", "3,800", "abc", "99999999999999999999", "nan")] \
        == [1_200_000, 1200, 3800, None, None, None]
    assert pd.derive_token_totals({"tokenUsage": {"inputTokens": "1e16", "outputTokens": 3}}) == (None, 3)
    assert pd.derive_token_totals({"tokenUsage": {}}) == (None, None)            # absent stays silent
    from source_reader import input_count  # noqa: PLC0415
    assert (input_count(3.7, "x"), input_count(3.0, "x")) == (None, 3)
    assert (input_count(-5, "x"), input_count(0, "x")) == (None, 0)   # a count has minimum 0
    err = capsys.readouterr().err
    assert err.count("is not a usable count;") == 6, err   # abc, 9999..., nan, 1e16, 3.7, -5
    # Aider's own counts are not truncated either; an
    # abbreviation is the nearest whole number in decimal (a float gave 4099999).
    assert [pa._num(x) for x in ("3.7", "0.5", "4.1M", "8.2M", "1.001k", "12")] \
        == [None, None, 4_100_000, 8_200_000, 1001, 12]
    assert all(pa._num(f"{i / 10}M") == i * 100_000 for i in range(1, 1000))
    assert capsys.readouterr().err.count("is not a usable count;") == 2
    # Reported as written in the transcript (a float gave '3.7').
    assert pa._num("3.70") is None and "'3.70'" in capsys.readouterr().err
    assert pa._num("1." + "0" * 28 + "1") is None   # #2: no rounding at 28 digits
    assert capsys.readouterr().err.count("is not a usable count;") == 1
    src = adapters / "cursor/regression/2026.06.04-5fd875e/inputs/s1-config.stream.jsonl"
    text = src.read_text()
    assert text.count('"inputTokens":14') == 1
    (tmp_path / "s.jsonl").write_text(text.replace('"inputTokens":14', '"inputTokens":1e16'))
    p = subprocess.run([sys.executable, str(adapters / "cursor/preprocess_cursor.py"),
                        str(tmp_path / "s.jsonl"), str(tmp_path / "c.json")], capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr
    c = json.loads((tmp_path / "c.json").read_text())
    c = c[0] if isinstance(c, list) else c
    assert c["total_input_tokens"] is None and c["total_tokens"] is None, c
    assert isinstance(c["total_output_tokens"], int)
    # Review #6: the reason is kept in the container, not only on stderr.
    assert [w for w in c["preprocessor_warnings"] if "inputTokens" in w and "not a usable count" in w], c


def test_validate_py_reads_a_trace_file_under_section_3_4(tmp_path):
    # schemas/validate.py reads the file through source_reader's verdict
    # (T19 I-JSON): duplicate names, unpaired surrogates, noncharacters
    # and bad UTF-8 are refused by reason before any schema check, and so are
    # the numeric domain and depth rules it did not check before.
    import importlib.util  # noqa: PLC0415
    path = pathlib.Path(__file__).resolve().parents[2] / "schemas" / "validate.py"
    spec = importlib.util.spec_from_file_location("validate_778", path)
    V = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(V)
    cases = {"duplicate-key": b'{"id":"a","id":"b"}', "unpaired-surrogate": b'{"k":"\\udd1e"}',
             "noncharacter": '{"k":"￿"}'.encode(), "not-utf8": b'{"k":"\xff"}',
             "number-outside-safe-range": b'{"x":1e16}', "non-finite-number": b'{"x":1e999}',
             "depth-exceeds-64": b"[" * 65 + b"]" * 65, "not-json": b"\xef\xbb\xbf{}"}
    for reason, raw in cases.items():
        f = tmp_path / f"{reason}.json"
        f.write_bytes(raw)
        errors, _ = V.validate(f)
        assert errors == [f"{f.name}: not conformant (§3.4): {reason}"], (reason, errors)
    # Nesting far past the limit is the same
    # verdict, not a RecursionError out of the parser.
    f = tmp_path / "very-deep.json"
    f.write_bytes(b"[" * 200000 + b"]" * 200000)
    assert V.validate(f)[0] == [f"{f.name}: not conformant (§3.4): depth-exceeds-64"]
    f = tmp_path / "ok.json"
    f.write_text('{"k":"\\ufdcf"}')
    errors, _ = V.validate(f)
    assert errors and not any("§3.4" in e for e in errors), errors   # reaches the schema


def test_validate_py_names_a_top_level_that_is_not_an_object_and_a_non_regular_file(tmp_path):
    # Ruled 2026-10-21: an error by name, not an AttributeError on .get, and
    # a FIFO is refused before a read that would block.
    import os  # noqa: PLC0415
    import importlib.util  # noqa: PLC0415
    path = pathlib.Path(__file__).resolve().parents[2] / "schemas" / "validate.py"
    spec = importlib.util.spec_from_file_location("validate_1014", path)
    V = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(V)
    for raw, kind in (("[1, 2]", "array"), ('"s"', "string"), ("3", "number"), ("null", "null")):
        f = tmp_path / f"{kind}.json"
        f.write_text(raw)
        assert V.validate(f) == ([f"top level is not an object (a JSON {kind})"], []), kind
    assert V.validate_doc([]) == (["top level is not an object (a JSON array)"], [])
    os.mkfifo(tmp_path / "fifo.json")
    assert V.validate(tmp_path / "fifo.json") == (["fifo.json: not a regular file"], [])
    # A path that cannot be examined says why, not "not a regular file".
    os.symlink(tmp_path / "absent.json", tmp_path / "dangling.json")
    assert V.validate(tmp_path / "dangling.json") == (["dangling.json: unreadable (No such file or directory)"], [])


def test_a_lone_surrogate_is_escaped_for_output():
    # Ruled 2026-10-21: a lone surrogate, which a JSON \\uXXXX escape can
    # carry and no UTF-8 output can encode, is rendered escaped; a paired
    # character outside the BMP is kept.
    from input_tree import field, printable  # noqa: PLC0415
    assert field("a\ud800b\udfff") == "a\\ud800b\\udfff"
    assert printable("x\udbff") == "x\\udbff"
    assert field("é\U0001F600") == "é\U0001F600"
    (field("a\ud800") + "\n").encode("utf-8")   # writable


def test_strings_in_the_input_are_repaired_and_reported_with_paths(tmp_path, capsys):
    # DSL rc3 §4 "Strings in the input" (T19b): each unpaired surrogate
    # and each noncharacter in an input string or member name is replaced with
    # U+FFFD and reported with record and path; a repair that makes two names
    # equal keeps the later value and is reported; json_parse values and
    # pre-processor reads follow the same rule.
    text = ('{"ok": "x"}\n'
            '{"a": "p\\ufdd0q", "b": ["\\uffff", "\\ud83d\\ude00"], "c/d": {"\\udbff\\udfff": 1}}\n'
            '{"\\ufdd0": "evidence", "\\ufffd": "later"}\n')
    for name, reader in (("x.jsonl", source_reader.read_jsonl), ("x.json", source_reader.read_json)):
        f = tmp_path / name
        f.write_text(text if name.endswith("jsonl") else "[" + ",".join(text.strip().split("\n")) + "]")
        r = reader(f)
        assert r.records[1] == {"a": "p�q", "b": ["�", "\U0001F600"], "c/d": {"�": 1}}, (name, r.records)
        assert r.records[2] == {"�": "later"}, (name, r.records)
        got = sorted((x["record_index"], x["reason"], x["path"]) for x in r.findings
                     if x.get("reason") in ("noncharacter", "lone-surrogate", "repair-key-collision"))
        assert got == [(1, "noncharacter", "/a"), (1, "noncharacter", "/b/0"),
                       (1, "noncharacter", "/c~1d/�"),
                       (2, "noncharacter", "/�"), (2, "repair-key-collision", "/�")], (name, got)
        assert all(loss == "value" for f_, loss in r.findings_with_loss() if f_.get("path")), name
    # json_parse: value and member name, reported as input failures with the path.
    import dsl_runner as D  # noqa: PLC0415
    diags = []
    tok_d, tok_s = D._EVAL_DIAGNOSTICS.set(diags), D._EVAL_SITE.set({"record_index": 4, "field": "f"})
    try:
        assert D.fn_json_parse('{"k": ["\\ufffe"], "\\ud800": 1}') == {"k": ["�"], "�": 1}
    finally:
        D._EVAL_DIAGNOSTICS.reset(tok_d)
        D._EVAL_SITE.reset(tok_s)
    assert [d["record_index"] for d in diags] == [4, 4], diags
    assert "/k/0" in diags[0]["message"] and "noncharacter" in diags[0]["message"], diags
    assert "member name" in diags[1]["message"] and "lone-surrogate" in diags[1]["message"], diags
    # A pre-processor's nested read repairs too, and says where.
    assert source_reader.nested_json_loads('{"t": "\\ufdef"}') == {"t": "�"}
    assert "replaced with U+FFFD at /t" in capsys.readouterr().err


def test_a_noncharacter_in_dsl_input_reaches_the_trace_repaired(tmp_path):
    # End to end: the emitted trace is I-JSON (minitrace-spec §3.4) and the
    # diagnostics name record and path.
    import dsl_runner as D  # noqa: PLC0415
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    data = [{"id": "rec-001", "events": [
        {"index": 0, "role": "user", "content": "p﷐"},
        {"index": 1, "role": "assistant", "content": "r￿"}]}]
    import json  # noqa: PLC0415
    (tmp_path / "in.json").write_text(json.dumps(data))
    diags = []
    traces = D.run(fixtures / "synthetic-trace-pair.dsl.yaml", tmp_path / "in.json", None,
                   diagnostics_out=diags, operator_override={"type": "scripted"})
    agent = next(t for t in traces if t["id"] == "agent-rec-001")
    assert [t["content"] for t in agent["turns"]] == ["p�", "r�"], agent["turns"]
    for t in traces:
        assert source_reader.trace_json_verdict(json.dumps(t, ensure_ascii=False).encode())[1] is None
    rep = sorted(d["path"] for d in diags if d.get("reason") == "noncharacter")
    assert rep == ["/events/0/content", "/events/1/content"], diags
    assert not D.set_level_loss(diags)


def test_input_repair_reports_carry_count_and_member_name(tmp_path, capsys, monkeypatch):
    # DSL rc3 §4 "Strings in the input" (spec b0ebcb2d): each report names the
    # record, the path, whether the member name or its value was repaired,
    # and how many characters; the spec lane's verifier case for d87fa89c.
    import dsl_runner as D  # noqa: PLC0415
    f = tmp_path / "in.json"
    f.write_text('[{"a":"x\\ufdd0y\\ufdd1","k\\ufffe":"\\ud800"}]')
    diags = []
    D.load_input({"type": "json"}, f, diags)
    got = sorted((d["path"], d["reason"], d["member_name"], d["count"]) for d in diags)
    assert got == [("/a", "noncharacter", False, 2), ("/k�", "lone-surrogate", False, 1),
                   ("/k�", "noncharacter", True, 1)], diags
    assert all(d["record_index"] == 0 and "character(s) replaced" in d["message"] for d in diags), diags
    # The guard after loading reports the same fields, record index from the
    # records list (a reader that did not repair is simulated).
    import source_reader  # noqa: PLC0415

    class Unrepaired:
        records = [{"ok": 1}, {"v": "﷐", "￿": 1}]
        def findings_with_loss(self):
            return []
    monkeypatch.setattr(source_reader, "read_json", lambda *a, **k: Unrepaired())
    diags = []
    assert D.load_input({"type": "json"}, f, diags) == [{"ok": 1}, {"v": "�", "�": 1}]
    got = sorted((d["record_index"], d["path"], d["member_name"], d["count"]) for d in diags)
    assert got == [(1, "/v", False, 1), (1, "/�", True, 1)], diags
    # json_parse reports through the evaluation diagnostics only, not twice.
    capsys.readouterr()
    sink = []
    tok = D._EVAL_DIAGNOSTICS.set(sink)
    try:
        D.fn_json_parse('{"k":"\\ufdd0"}')
    finally:
        D._EVAL_DIAGNOSTICS.reset(tok)
    assert len(sink) == 1 and "replaced with U+FFFD" not in capsys.readouterr().err


def test_validate_doc_a_trace_type_that_is_not_a_string_is_an_error_not_a_crash():
    # Ruled 2026-10-29: a list or object trace_type raised TypeError
    # (unhashable) and stopped a batch; it is reported as a number or null
    # is: the base schema's type error and an unknown trace_type without
    # schema_uri. A string keeps its type schema.
    import importlib.util  # noqa: PLC0415
    path = pathlib.Path(__file__).resolve().parents[2] / "schemas" / "validate.py"
    spec = importlib.util.spec_from_file_location("validate_trace_type", path)
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)
    for tt in (["agent-session"], {"type": "agent-session"}, 5, None, True):
        errors, _ = v.validate_doc({"trace_type": tt})
        assert any(e.startswith("BASE: ") and e.endswith("at ['trace_type']") for e in errors), (tt, errors)
        assert f"unknown trace_type {tt!r} requires schema_uri (spec §3.2)" in errors, (tt, errors)
    errors, _ = v.validate_doc({"trace_type": "agent-session"})
    assert any(e.startswith("AGENT-SESSION: ") for e in errors), errors


def test_decode_reporting_is_bounded_and_refuses_what_is_not_a_regular_file(tmp_path, monkeypatch):
    # Ruled 2026-10-29: decode_reporting (aider, cursor, claude-ai, companion
    # files) takes the shared reader's bound: a path that is not a regular
    # file, or one over ReadLimits.max_file_bytes, raises SourceUnreadable
    # before it is read; the limit itself is read.
    import os  # noqa: PLC0415
    import types  # noqa: PLC0415
    import source_reader  # noqa: PLC0415
    monkeypatch.setattr(source_reader, "ReadLimits", lambda: types.SimpleNamespace(max_file_bytes=4))
    (tmp_path / "four").write_bytes(b"abcd")
    (tmp_path / "five").write_bytes(b"abcde")
    # The read asks for one byte past the limit, never the whole file.
    reads = []
    real_open = pathlib.Path.open

    def recording_open(self, *args, **kwargs):
        fh = real_open(self, *args, **kwargs)

        class Recording:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                fh.close()

            def read(self, n=-1):
                reads.append(n)
                return fh.read(n)
        return Recording()
    with monkeypatch.context() as m:
        m.setattr(pathlib.Path, "open", recording_open)
        assert source_reader.decode_reporting(tmp_path / "four") == "abcd"
    assert reads == [5], reads
    raised = None
    try:
        source_reader.decode_reporting(tmp_path / "five")
    except source_reader.SourceUnreadable as e:
        raised = e
    assert raised is not None and raised.detail == "5 > 4", raised   # by its size, before reading
    # A file that grows past the bound after its size was read is refused
    # by the read itself.
    (tmp_path / "grows").write_bytes(b"abcdef")
    real_stat = pathlib.Path.stat

    def stat_before_growing(self, *args, **kwargs):
        st = real_stat(self, *args, **kwargs)
        if self.name == "grows":
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid, 4,
                                   st.st_atime, st.st_mtime, st.st_ctime))
        return st
    raised = None
    with monkeypatch.context() as m:
        m.setattr(pathlib.Path, "stat", stat_before_growing)
        try:
            source_reader.decode_reporting(tmp_path / "grows")
        except source_reader.SourceUnreadable as e:
            raised = e
    assert raised is not None and raised.reason == "file-too-large" and raised.detail == "more than 4", raised
    # Stat errors as the shared reader maps them: through a regular file, a
    # symlink loop.
    os.symlink(tmp_path / "loop", tmp_path / "loop")
    for name, reason in (("five", "file-too-large"), ("absent", "not-a-file"), (".", "not-a-file"),
                         ("four/x", "not-a-file"), ("loop", "not-a-file"), ("fifo", "not-a-file")):
        if name == "fifo":
            os.mkfifo(tmp_path / "fifo")   # would block a read
        raised = None
        try:
            source_reader.decode_reporting(tmp_path / name)
        except source_reader.SourceUnreadable as e:
            raised = e
        assert raised is not None and raised.reason == reason, (name, raised)
    # A name over the file system's limit is not-a-file, and a file the
    # open refuses (mode 000) is read-error. Root reads such a file, so as
    # root the read-error case is not checked.
    limit = os.pathconf(tmp_path, "PC_NAME_MAX") if hasattr(os, "pathconf") else 255
    cases = [("n" * (limit + 1), "not-a-file")]
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        (tmp_path / "closed").write_bytes(b"ab")
        os.chmod(tmp_path / "closed", 0)
        cases.append(("closed", "read-error"))
    for name, reason in cases:
        raised = None
        try:
            source_reader.decode_reporting(tmp_path / name)
        except source_reader.SourceUnreadable as e:
            raised = e
        assert raised is not None and raised.reason == reason, (name[:20], raised)


def test_preprocessors_reading_whole_files_refuse_one_over_the_shared_bound(tmp_path):
    # Ruled 2026-10-29: claude-ai, cursor and aider exit with an error line
    # (no traceback) for an input over the bound, and agentdojo skips such a
    # run file with its "warning: skip (unreadable)" line, before reading.
    import subprocess  # noqa: PLC0415
    import source_reader  # noqa: PLC0415
    adapters = pathlib.Path(__file__).resolve().parent.parent
    limit = source_reader.ReadLimits().max_file_bytes
    big = tmp_path / "big.json"
    with big.open("wb") as fh:
        fh.truncate(limit + 1)   # sparse: nothing of it is stored or read
    out = tmp_path / "out.json"
    for pre, args, code in (("claude-ai/preprocess_claude_ai.py", [big, out], 2),
                            ("cursor/preprocess_cursor.py", [big, out], 66),
                            ("aider/preprocess_aider.py", [big, out], 2)):
        r = subprocess.run([sys.executable, str(adapters / pre), *map(str, args)], capture_output=True, text=True,
                           timeout=120)
        assert r.returncode == code and "Traceback" not in r.stderr and "file-too-large" in r.stderr, \
            (pre, r.returncode, r.stderr[-300:])
        assert not out.exists(), pre
    runs = tmp_path / "runs"
    runs.mkdir()
    big.rename(runs / "big.json")
    r = subprocess.run([sys.executable, str(adapters / "agentdojo" / "preprocess_agentdojo.py"),
                        "--runs-root", str(runs), "--output", str(out)], capture_output=True, text=True, timeout=120)
    assert "Traceback" not in r.stderr and f"warning: skip (unreadable): {runs / 'big.json'}: {limit + 1} bytes, " \
        f"more than {limit}" in r.stderr, (r.returncode, r.stderr[-300:])
