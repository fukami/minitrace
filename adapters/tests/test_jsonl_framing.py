"""Tests for jsonl line framing outside the shared reader.

DSL rc3 §15: a jsonl line is framed at CR, LF or CRLF, and is blank only if
empty or JSON whitespace (space, tab, CR, LF). The shared reader was made to
do so. Rulings (2026-10-18): the cursor, inspect-swe, halo and minimcp
pre-processors frame through source_reader.jsonl_text_lines, so str.strip()'s
VT, FF and Unicode spaces no longer make a line blank, and str.splitlines()
no longer cuts a record at U+2028, U+2029 or U+0085 inside a string; each
keeps its own handling of a line that does not parse. The drift_watch tools
only stop using splitlines().

Run:
    python adapters/tests/test_jsonl_framing.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ADAPTERS))

import source_reader  # noqa: E402

# A record holding the three characters splitlines() splits at, raw.
SPLIT_RECORD = '{"type": "session", "version": 3, "t": "a b c\x85d"}'
NOT_BLANK = ("\x0b", "\x0c", " ", " \x0b ")


def _load(rel: str):
    spec = importlib.util.spec_from_file_location(rel.replace("/", "_")[:-3], ADAPTERS / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_jsonl_text_lines_frames_and_blanks_as_the_shared_reader():
    text = 'a\r\nb\rc\n\n \t \r\n' + SPLIT_RECORD + "\n" + "\n".join(NOT_BLANK) + "\n  d  "
    got = list(source_reader.jsonl_text_lines(text))
    # Each line comes without its JSON whitespace; VT and NBSP are kept.
    assert [line for _, line in got] == ["a", "b", "c", SPLIT_RECORD, *(s.strip(" ") for s in NOT_BLANK), "d"], got
    # Physical line numbers, blank lines counted.
    assert [n for n, _ in got] == [1, 2, 3, 6, 7, 8, 9, 10, 11], got


def test_cursor_reports_a_not_blank_line_and_keeps_a_record_with_line_separators():
    cursor = _load("cursor/preprocess_cursor.py")
    events, warnings = cursor.parse_stream(SPLIT_RECORD + "\n\x0b\n\n")
    assert [e.get("t") for e in events] == ["a b c\x85d"], events
    assert len(warnings) == 1 and warnings[0].startswith("line 2:"), warnings


def test_inspect_swe_frames_at_cr_and_reports_a_not_blank_line():
    # The module exits at import without inspect_ai, which only reads .eval
    # files; parse_inner_stream is plain text handling, so a stub suffices.
    import types  # noqa: PLC0415
    stub = types.ModuleType("inspect_ai.log")
    stub.read_eval_log = lambda *a, **k: None
    saved = {k: sys.modules.get(k) for k in ("inspect_ai", "inspect_ai.log")}
    sys.modules.update({"inspect_ai": types.ModuleType("inspect_ai"), "inspect_ai.log": stub})
    try:
        inspect_swe = _load("inspect-swe/preprocess_inspect_swe.py")
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    events = inspect_swe.parse_inner_stream('{"type": "a"}\r{"type": "b"}\r\n\x0c\n\n')
    kinds = [e["event_kind"] for e in events]
    assert len(events) == 3 and kinds[-1] == "parse_error", events
    assert [e.get("type") for e in events[:2]] == ["a", "b"], events


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ADAPTERS / script), *args],
                          capture_output=True, text=True, timeout=60)


def test_minimcp_keeps_a_record_with_line_separators_and_reports_a_not_blank_line():
    with tempfile.TemporaryDirectory() as td:
        inp = pathlib.Path(td) / "in.ndjson"
        inp.write_text(SPLIT_RECORD + "\n\x0b\n\n", encoding="utf-8")
        r = _run("minimcp/preprocess_minimcp.py", str(inp), "--output", str(pathlib.Path(td) / "out.jsonl"))
    warned = [ln for ln in r.stderr.splitlines() if "skipping malformed line" in ln]
    assert warned == [w for w in warned if "line 2:" in w] and len(warned) == 1, r.stderr
    # The record itself reaches processing, whole.
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    minimcp = _load("minimcp/preprocess_minimcp.py")
    seen: list = []
    minimcp.process = lambda events: seen.extend(events) or []
    with tempfile.TemporaryDirectory() as td:
        inp = pathlib.Path(td) / "in.ndjson"
        inp.write_text(SPLIT_RECORD + "\n\x0b\n\n", encoding="utf-8")
        argv, sys.argv = sys.argv, ["preprocess_minimcp.py", str(inp), "--output", str(pathlib.Path(td) / "o.jsonl")]
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                minimcp.main()
        finally:
            sys.argv = argv
    assert [e.get("t") for e in seen] == ["a b c\x85d"], seen


def test_halo_skips_blank_lines_and_does_not_treat_a_not_blank_line_as_blank():
    span = json.dumps({"trace_id": "t", "span_id": "s", "name": "x"})
    with tempfile.TemporaryDirectory() as td:
        inp, out = pathlib.Path(td) / "in.jsonl", pathlib.Path(td) / "out.json"
        inp.write_text(span + "\n\n \t \n" + span + "\n", encoding="utf-8")
        ok = _run("otel-openinference/preprocess_halo.py", str(inp), str(out))
        ok_out = out.read_text(encoding="utf-8") if out.exists() else ""
        inp.write_text(span + "\n\x0b\n", encoding="utf-8")
        bad = _run("otel-openinference/preprocess_halo.py", str(inp), str(out))
    # halo stops at any line that does not parse; a VT line is now one.
    assert ok.returncode == 0 and "JSONDecodeError" not in ok.stderr, (ok.returncode, ok.stderr)
    assert ok_out.strip().startswith("[") and "wrote 1 containers" in ok.stderr, (ok_out[:200], ok.stderr)
    assert bad.returncode != 0 and "JSONDecodeError" in bad.stderr, (bad.returncode, bad.stderr[-300:])


def test_drift_watch_readers_do_not_split_a_record_at_line_separators():
    # Before, splitlines() cut each record in four and no piece parsed.
    sep = "a b c\x85d"
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "s.jsonl"
        for rel, fn, record, want in (
                ("pi/regression/drift_watch.py", "_format_version", {"type": "session", "version": 3, "t": sep}, 3),
                ("codex/regression/drift_watch.py", "_cli_version",
                 {"type": "session_meta", "payload": {"cli_version": "0.9", "t": sep}}, "0.9"),
                ("droid/regression/drift_watch.py", "_format_version", {"type": "session_start", "version": 2, "t": sep}, 2)):
            p.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            got = getattr(_load(rel), fn)(p)
            assert got == want, (rel, got)
        p.write_text(SPLIT_RECORD + "\n", encoding="utf-8")
        recs = _load("cursor/regression/drift_watch.py").read_stream(p)
        assert len(recs) == 1 and recs[0].get("type") != "unparseable", recs
        sdir = pathlib.Path(td) / "session_1"
        sdir.mkdir()
        (sdir / "meta.json").write_text("{}", encoding="utf-8")
        (sdir / "messages.jsonl").write_text(json.dumps({"role": "user", "t": sep}, ensure_ascii=False) + "\n",
                                             encoding="utf-8")
        recs = _load("vibe/regression/drift_watch.py").vibe_raw_reader(sdir)
        assert [r["type"] for r in recs] == ["meta", "message.user"], recs


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
