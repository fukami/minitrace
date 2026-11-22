"""verify_envelope.py: a manifest verifies only against the files it lists."""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cross_trace import compute_content_hash  # noqa: E402
from verify_envelope import check  # noqa: E402


def _schema_valid(t: dict) -> dict:
    """The fields the trace schemas require that a probe trace leaves out
    (trace_type, operator, a turn's role), filled in where absent, so only
    what a test sets out to break fails (the trace schema check, ruled
    2026-10-21). Malformed turns are left as they are."""
    t.setdefault("trace_type", "agent-session")
    t.setdefault("operator", {"type": "scripted"})
    ts = "2026-10-20T00:00:00.000000Z"
    for i, turn in enumerate(t.get("turns") if isinstance(t.get("turns"), list) else []):
        if isinstance(turn, dict):
            turn.setdefault("index", i)
            turn.setdefault("role", "user")
    for s in t.get("segment_markers") if isinstance(t.get("segment_markers"), list) else []:
        if isinstance(s, dict):
            s.setdefault("timestamp", ts)
            s.setdefault("segment_type", "x-probe")
    for p in t.get("policy_triggers") if isinstance(t.get("policy_triggers"), list) else []:
        if isinstance(p, dict):
            p.setdefault("timestamp", ts)
            p.setdefault("trigger_name", "probe")
            p.setdefault("fired", True)
    return t


def _envelope(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "env"
    (root / "traces").mkdir(parents=True)
    (root / "bindings").mkdir()
    ids = []
    entries = []
    for n in ("a", "b"):
        t = {"id": f"t-{n}", "schema_version": "0.3.0",
             "turns": [{"index": 0, "content": n}, {"index": 1, "content": n + n}]}
        _schema_valid(t)
        t["content_hash"] = compute_content_hash(t)
        f = root / "traces" / f"t-{n}.minitrace.json"
        f.write_text(json.dumps(t))
        entries.append({"trace_id": t["id"], "path": f"traces/{f.name}", "schema_version": "0.3.0",
                        "trace_content_hash": t["content_hash"],
                        "file_content_hash": "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()})
        ids.append(t["id"])
    lab = root / "bindings" / "labels.csv"
    lab.write_text("id,label\nt-a,x\n")
    m = {"envelope_version": "0.1.0", "envelope_id": "env-probe",
         "created_at": "2026-10-20T00:00:00.000000Z",
         "producer": {"id": "probe", "kind": "automated_pipeline"}, "traces": entries,
         "bindings": [{"kind": "x-labels", "path": "bindings/labels.csv",
                       "content_hash": "sha256:" + hashlib.sha256(lab.read_bytes()).hexdigest(),
                       "produces_trace_id": "t-a"}]}
    (root / "manifest.json").write_text(json.dumps(m))
    return root / "manifest.json"


def _mutate(mp: pathlib.Path, fn) -> pathlib.Path:
    m = json.loads(mp.read_text())
    fn(m)
    mp.write_text(json.dumps(m))
    return mp


def test_consistent_unsigned_envelope_has_integrity_only(tmp_path):
    # §6.2 step 5 applies only when a signature is present: an unsigned
    # envelope whose steps 1 to 4 pass is not informational by §6.2; it
    # lacks producer authenticity (§5.2) (ruled 2026-10-20).
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_envelope(tmp_path))
    assert r.problems == []
    assert any(n.startswith("unsigned") for n in r.notes), r.notes
    assert r.verdict().startswith("integrity only"), r.verdict()


def test_swapped_trace_hashes_fail(tmp_path):
    def swap(m):
        a, b = m["traces"]
        a["trace_content_hash"], b["trace_content_hash"] = b["trace_content_hash"], a["trace_content_hash"]
    problems, _ = check(_mutate(_envelope(tmp_path), swap))
    assert len(problems) == 2


def test_edited_binding_and_trace_fail(tmp_path):
    mp = _envelope(tmp_path)
    (mp.parent / "bindings" / "labels.csv").write_text("id,label\nt-a,y\n")
    tf = mp.parent / "traces" / "t-a.minitrace.json"
    t = json.loads(tf.read_text())
    t["turns"][0]["content"] = "tampered"
    tf.write_text(json.dumps(t))
    problems, _ = check(mp)
    assert any("bindings[0]: content_hash" in p for p in problems)
    assert any("file_content_hash" in p for p in problems)
    assert any("stored content_hash does not match" in p for p in problems)


def test_paths_duplicates_and_dangling_references_fail(tmp_path):
    for fn, needle in (
        (lambda m: m["traces"].append(dict(m["traces"][0])), "listed twice"),
        (lambda m: m["traces"][0].update(path="traces\\..\\..\\etc\\passwd"), "not relative"),
        # Refused by the schema's RelPath pattern now, before resolve_member.
        (lambda m: m["traces"][0].update(path="../outside.json"), "schema: traces/0/path"),
        # §6.1's at-least-one is the schema's minItems now.
        (lambda m: m.update(traces=[]), "schema: traces: [] should be non-empty"),
    ):
        problems, _ = check(_mutate(_envelope(tmp_path / needle.replace(" ", "_").replace("§", "s")), fn))
        assert any(needle in p for p in problems), (needle, problems)


def test_symlinked_member_fails(tmp_path):
    mp = _envelope(tmp_path)
    f = mp.parent / "traces" / "t-b.minitrace.json"
    moved = tmp_path / "moved.json"
    f.rename(moved)
    f.symlink_to(moved)
    problems, _ = check(mp)
    assert any("symlink" in p for p in problems)


def test_unlisted_files_are_noted(tmp_path):
    mp = _envelope(tmp_path)
    (mp.parent / "traces" / "extra.minitrace.json").write_text("{}")
    problems, notes = check(mp)
    assert problems == [] and any("not listed" in n for n in notes)


def test_member_trace_that_is_not_i_json_fails(tmp_path):
    # §3.4 "JSON form" (T19): a listed trace is read under the same
    # verdict as validate.py and the vectors, so a duplicate member name, an
    # unpaired surrogate or a noncharacter is refused, not hashed last-wins.
    texts = {"duplicate-key": '{"id":"t-a","id":"t-x","schema_version":"0.3.0","turns":[]}',
             "unpaired-surrogate": '{"id":"t-a","x":"\\ud800","schema_version":"0.3.0","turns":[]}',
             "noncharacter": '{"id":"t-a","x":"\\ufdd0","schema_version":"0.3.0","turns":[]}'}
    for reason, text in texts.items():
        mp = _envelope(tmp_path / reason)
        tf = mp.parent / "traces" / "t-a.minitrace.json"
        tf.write_text(text)
        _mutate(mp, lambda m: m["traces"][0].update(
            file_content_hash="sha256:" + hashlib.sha256(tf.read_bytes()).hexdigest()))
        problems, _ = check(mp)
        # The binding names t-a, which one listing holds: it resolves although
        # that trace was refused (§4.7 counts listings, ruled 2026-10-21).
        assert problems == [f"traces[0]: trace not conformant: {reason}"], (reason, problems)


# Manifest shape before use (ruled 2026-10-20): the manifest is checked
# against the envelope schema first, and a manifest that fails it is
# reported without any set/dict use or file access; limits are a bounded
# refusal (exit 2); signatures live in signature.json at the root (§9).

import pytest  # noqa: E402

from verify_envelope import EnvelopeRefused, main  # noqa: E402


@pytest.mark.parametrize("name,fn", [
    ("path a list", lambda m: m["traces"][0].__setitem__("path", [])),
    ("path an object", lambda m: m["bindings"][0].__setitem__("path", {"a": 1})),
    ("trace_id an object", lambda m: m["traces"][0].__setitem__("trace_id", {"a": 1})),
    ("traces an object", lambda m: m.__setitem__("traces", {"a": 1})),
    ("bindings a string", lambda m: m.__setitem__("bindings", "x")),
    ("produces_trace_id a list", lambda m: m["bindings"][0].__setitem__("produces_trace_id", ["t-a"])),
    ("no envelope_id", lambda m: m.pop("envelope_id")),
    ("no producer", lambda m: m.pop("producer")),
])
def test_a_malformed_manifest_is_a_schema_problem_not_a_crash(tmp_path, name, fn):
    problems, _ = check(_mutate(_envelope(tmp_path), fn))
    assert problems and all(p.startswith("schema: ") for p in problems), (name, problems)


def test_a_schema_failure_reads_no_member_file(tmp_path, monkeypatch):
    mp = _mutate(_envelope(tmp_path), lambda m: m.pop("producer"))
    import verify_envelope  # noqa: PLC0415
    monkeypatch.setattr(verify_envelope, "resolve_member",
                        lambda *a: pytest.fail("a member was resolved after a schema failure"))
    problems, _ = check(mp)
    assert problems


@pytest.mark.parametrize("limit", [{"max_manifest_bytes": 100}, {"max_items": 1}, {"max_trace_bytes": 10}])
def test_limits_are_a_bounded_refusal(tmp_path, limit):
    mp = _envelope(tmp_path)
    with pytest.raises(EnvelopeRefused):
        check(mp, **limit)


def test_limits_on_the_command_line_exit_2(tmp_path, capsys):
    mp = _envelope(tmp_path)
    assert main(["verify_envelope.py", str(mp)]) == 0
    assert main(["verify_envelope.py", "--max-items", "1", str(mp)]) == 2
    assert "limit" in capsys.readouterr().err


_SIG = {"id": "s1", "signer": "probe", "algorithm": "x-probe", "key_id": "k1",
        "manifest_hash": "sha256:" + "0" * 64, "signed_at": "2026-10-20T00:00:00.000000Z",
        "signature": "AA==", "covers": ["manifest"]}


def _manifest_sha(mp: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(mp.read_bytes()).hexdigest()


def _sigs(mp: pathlib.Path, *entries) -> None:
    (mp.parent / "signature.json").write_text(json.dumps({"signatures": list(entries)}))


def _sign(mp: pathlib.Path) -> None:
    """One signature whose manifest_hash matches the manifest as it stands."""
    _sigs(mp, {**_SIG, "manifest_hash": _manifest_sha(mp)})


def test_signature_is_read_from_signature_json(tmp_path):
    mp = _envelope(tmp_path)
    _, notes = check(mp)
    assert any(n.startswith("unsigned") for n in notes), notes
    (mp.parent / "signature.json").write_text(json.dumps({"signatures": [_SIG]}))
    _, notes = check(mp)
    assert any("signature.json lists 1 signature(s), NOT verified" in n for n in notes), notes
    assert not any("not listed in the manifest" in n for n in notes), notes


def test_a_missing_schema_fails_the_check(tmp_path, monkeypatch):
    # Fail closed: no validator is a problem, never a skipped check.
    import verify_envelope  # noqa: PLC0415
    monkeypatch.setattr(verify_envelope, "SCHEMA", tmp_path / "no-such.schema.json")
    monkeypatch.setattr(verify_envelope, "SCHEMA_V010", tmp_path / "no-such.schema.json")
    problems, _ = check(_envelope(tmp_path))
    assert problems and problems[0].startswith("schema: validator unavailable"), problems


@pytest.mark.parametrize("section", ["raw_artefacts", "witnesses", "annotations", "bindings", "clocks"])
def test_the_item_limit_holds_for_every_listing(tmp_path, section):
    # Three entries against a limit of two; the fixture's two traces fit.
    mp = _mutate(_envelope(tmp_path), lambda m: m.__setitem__(section, [{}, {}, {}]))
    with pytest.raises(EnvelopeRefused, match=section):
        check(mp, max_items=2)


# Bounded on hostile input; the schema of the
# declared revision (§12); limit flags positive; signature.json not a file.

_CLOCK_010_ONLY = {"id": "c", "kind": "manipulated", "manipulated": False}


def test_a_0_1_0_manifest_is_held_to_the_0_1_0_schema(tmp_path):
    # Valid under v0.1.0; v0.1.1 added "kind manipulated means manipulated".
    mp = _mutate(_envelope(tmp_path), lambda m: m.__setitem__("clocks", [_CLOCK_010_ONLY]))
    problems, _ = check(mp)
    assert problems == [], problems
    (tmp_path / "v011").mkdir()
    mp = _mutate(_envelope(tmp_path / "v011"), lambda m: m.update(envelope_version="0.1.1",
                                                                  clocks=[_CLOCK_010_ONLY]))
    problems, _ = check(mp)
    assert problems and problems[0].startswith("schema: clocks/0/manipulated"), problems


def test_schema_errors_are_capped_without_reading_them_all(tmp_path, monkeypatch):
    import jsonschema  # noqa: PLC0415
    import verify_envelope  # noqa: PLC0415
    mp = _mutate(_envelope(tmp_path), lambda m: m.__setitem__("bindings", [{}] * 5000))
    # Count the errors the validator yields (its own nested calls included):
    # the cap stops it, so 5000 bad entries are never all produced.
    drawn = []
    real = jsonschema.Draft202012Validator.iter_errors

    def counting(self, *a, **k):
        for e in real(self, *a, **k):
            drawn.append(1)
            yield e
    monkeypatch.setattr(jsonschema.Draft202012Validator, "iter_errors", counting)
    problems, _ = check(mp)
    assert len(drawn) < 4 * verify_envelope.MAX_SCHEMA_ERRORS, len(drawn)
    assert len(problems) == verify_envelope.MAX_SCHEMA_ERRORS + 1, len(problems)
    assert problems[-1].startswith("schema: and more (stopped after"), problems[-1]


@pytest.mark.parametrize("text", [
    '{"envelope_version": "0.1.0", "x-deep": ' + "[" * 1_000_000 + "]" * 1_000_000 + "}",
    '{"envelope_version": "0.1.0", "x-n": NaN}',
    '{"envelope_version": "0.1.0", "x-n": Infinity}',
    '{"envelope_version": "0.1.0", "x-n": 1e999}',
    '{"envelope_version": "0.1.0", "x-n": -1e400}',
])
def test_an_unreadable_manifest_exits_2(tmp_path, text):
    mp = tmp_path / "manifest.json"
    mp.write_text(text)
    assert main(["verify_envelope.py", str(mp)]) == 2


def test_every_limit_flag_on_the_command_line(tmp_path):
    mp = _envelope(tmp_path)
    for flag, value in (("--max-manifest-bytes", "100"), ("--max-trace-bytes", "10"), ("--max-items", "1")):
        assert main(["verify_envelope.py", flag, value, str(mp)]) == 2, flag
    for value in ("0", "-1"):
        assert main(["verify_envelope.py", "--max-items", value, str(mp)]) == 2, value
    assert main(["verify_envelope.py", str(mp)]) == 0


def test_an_empty_path_segment_is_refused(tmp_path):
    # a//b passes the schema's RelPath and is refused by resolve_member.
    mp = _mutate(_envelope(tmp_path), lambda m: m["bindings"][0].update(path="bindings//labels.csv"))
    problems, _ = check(mp)
    assert any("empty, '.' or '..' segment" in p for p in problems), problems


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_signature_json_that_is_not_a_file_is_named(tmp_path, kind):
    mp = _envelope(tmp_path)
    sig = mp.parent / "signature.json"
    if kind == "symlink":
        (tmp_path / "real.sig").write_text("{}")
        sig.symlink_to(tmp_path / "real.sig")
    else:
        sig.mkdir()
    _, notes = check(mp)
    assert any(n.startswith("signature.json is not a regular file") for n in notes), notes


# Permitted states are not problems (envelope §6.3, §7.4, §10.4).

_REMOVE = {"operation": "remove", "reason_code": "pii_detected", "authorised_by": "probe",
           "authorised_at": "2026-10-20T00:00:00.000000Z"}


def _raw(tmp_path: pathlib.Path, version: str, entry: dict) -> tuple[list[str], list[str]]:
    return check(_mutate(_envelope(tmp_path), lambda m: m.update(envelope_version=version,
                                                                 raw_artefacts=[entry])))


def test_a_raw_artefact_removed_as_a_whole_is_not_a_problem(tmp_path):
    # 0.1.1: the entry carries no file fields (§10.4).
    problems, notes = _raw(tmp_path, "0.1.1", {"id": "r1", "kind": "harness_log", "redaction": _REMOVE})
    assert problems == [], problems
    assert any("removed as a whole" in n for n in notes), notes


def test_a_0_1_0_raw_artefact_removed_with_its_file_absent_is_not_a_problem(tmp_path):
    # 0.1.0 still lists path and content_hash; the file itself is absent.
    entry = {"id": "r1", "kind": "harness_log", "path": "raw/gone.log",
             "content_hash": "sha256:" + "0" * 64, "redaction": _REMOVE}
    problems, notes = _raw(tmp_path, "0.1.0", entry)
    assert problems == [], problems
    assert any("removed as a whole" in n for n in notes), notes


def test_a_removed_artefact_whose_file_is_present_is_still_checked(tmp_path):
    # §7.4: a file that is present is checked as it stands.
    (tmp_path / "env" / "raw").mkdir(parents=True)
    (tmp_path / "env" / "raw" / "kept.log").write_text("bytes")
    entry = {"id": "r1", "kind": "harness_log", "path": "raw/kept.log",
             "content_hash": "sha256:" + "0" * 64, "redaction": _REMOVE}
    problems, _ = _raw(tmp_path, "0.1.0", entry)
    assert any("content_hash does not match" in p for p in problems), problems


@pytest.mark.parametrize("redaction", [None, dict(_REMOVE, operation="mask"),
                                       dict(_REMOVE, target_kind="payload_keys", targets=["k"])])
def test_a_missing_file_that_was_not_removed_as_a_whole_is_a_problem(tmp_path, redaction):
    entry = {"id": "r1", "kind": "harness_log", "path": "raw/gone.log",
             "content_hash": "sha256:" + "0" * 64}
    if redaction is not None:
        entry["redaction"] = redaction
    problems, _ = _raw(tmp_path, "0.1.1", entry)
    assert any("no such regular file" in p for p in problems), problems


def _lineage_envelope(tmp_path: pathlib.Path, target_hash) -> pathlib.Path:
    mp = _envelope(tmp_path)
    m = json.loads(mp.read_text())
    a = mp.parent / m["traces"][0]["path"]
    t = json.loads(a.read_text())
    t["lineage"] = [{"type": "derived-from", "target_trace_id": "t-b",
                     "target_content_hash": target_hash(m)}]
    t["content_hash"] = compute_content_hash(t)
    a.write_text(json.dumps(t))
    m["traces"][0].update(trace_content_hash=t["content_hash"],
                          file_content_hash="sha256:" + hashlib.sha256(a.read_bytes()).hexdigest())
    mp.write_text(json.dumps(m))
    return mp


def test_a_lineage_edge_to_another_state_of_its_target_is_a_note(tmp_path):
    # §6.3: not a failed check; reported as referring to another state and
    # not verified against the bundled target.
    problems, notes = check(_lineage_envelope(tmp_path, lambda m: "sha256:" + "1" * 64))
    assert problems == [], problems
    assert any("other than the one bundled" in n and "not verified" in n for n in notes), notes


def test_a_lineage_edge_matching_the_manifest_is_not_noted(tmp_path):
    problems, notes = check(_lineage_envelope(tmp_path, lambda m: m["traces"][1]["trace_content_hash"]))
    assert problems == [], problems
    assert not any("other than the one bundled" in n for n in notes), notes


def test_a_corrupted_trace_file_is_still_a_problem(tmp_path):
    mp = _envelope(tmp_path)
    m = json.loads(mp.read_text())
    (mp.parent / m["traces"][1]["path"]).write_text('{"id": "t-b", "tampered": true}')
    problems, _ = check(mp)
    assert any("file_content_hash does not match" in p for p in problems), problems


# A removed entry's path still obeys §3.2/§14.1, only
# nothing at all at a well-formed path is absence, and duplicates count.

@pytest.mark.parametrize("path", ["raw\\gone.log", "C:gone.log", "raw/‮gone.log"])
def test_a_removed_entry_with_a_malformed_path_is_a_problem(tmp_path, path):
    entry = {"id": "r1", "kind": "harness_log", "path": path,
             "content_hash": "sha256:" + "0" * 64, "redaction": _REMOVE}
    problems, _ = _raw(tmp_path, "0.1.0", entry)
    assert problems and not any(p.startswith("schema: ") for p in problems), problems


def test_a_removed_entry_through_an_outside_symlink_is_a_problem(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "raw").symlink_to(outside, target_is_directory=True)
    entry = {"id": "r1", "kind": "harness_log", "path": "raw/gone.log",
             "content_hash": "sha256:" + "0" * 64, "redaction": _REMOVE}
    problems, _ = _raw(tmp_path, "0.1.0", entry)
    assert any("symlink" in p for p in problems), problems


def test_a_removed_entry_at_a_dangling_symlink_is_a_problem(tmp_path):
    (tmp_path / "env" / "raw").mkdir(parents=True)
    (tmp_path / "env" / "raw" / "gone.log").symlink_to(tmp_path / "nowhere")
    entry = {"id": "r1", "kind": "harness_log", "path": "raw/gone.log",
             "content_hash": "sha256:" + "0" * 64, "redaction": _REMOVE}
    problems, _ = _raw(tmp_path, "0.1.0", entry)
    assert any("symlink" in p for p in problems), problems


def test_a_removed_entry_at_a_directory_is_a_problem(tmp_path):
    (tmp_path / "env" / "raw" / "gone.log").mkdir(parents=True)
    entry = {"id": "r1", "kind": "harness_log", "path": "raw/gone.log",
             "content_hash": "sha256:" + "0" * 64, "redaction": _REMOVE}
    problems, _ = _raw(tmp_path, "0.1.0", entry)
    assert any("no such regular file" in p for p in problems), problems


def test_a_removed_entry_that_cannot_be_examined_is_not_absent(tmp_path, monkeypatch):
    # No permission to look is not absence (a removed file is only noted
    # when nothing is there).
    import verify_envelope  # noqa: PLC0415
    real = verify_envelope.os.lstat

    def denied(p, *a, **k):
        if str(p).endswith("gone.log"):
            raise PermissionError(13, "Permission denied", str(p))
        return real(p, *a, **k)
    monkeypatch.setattr(verify_envelope.os, "lstat", denied)
    entry = {"id": "r1", "kind": "harness_log", "path": "raw/gone.log",
             "content_hash": "sha256:" + "0" * 64, "redaction": _REMOVE}
    problems, notes = _raw(tmp_path, "0.1.0", entry)
    assert any("no such regular file" in p for p in problems), problems
    assert not any("removed as a whole" in n for n in notes), notes


def test_two_removed_entries_with_one_path_are_a_duplicate(tmp_path):
    e = {"id": "r1", "kind": "harness_log", "path": "raw/gone.log",
         "content_hash": "sha256:" + "0" * 64, "redaction": _REMOVE}
    mp = _mutate(_envelope(tmp_path), lambda m: m.update(raw_artefacts=[e, dict(e, id="r2")]))
    problems, _ = check(mp)
    assert any("already listed" in p for p in problems), problems


def test_a_lineage_edge_to_another_state_is_a_note_under_0_1_1(tmp_path):
    mp = _lineage_envelope(tmp_path, lambda m: "sha256:" + "1" * 64)
    mp = _mutate(mp, lambda m: m.update(envelope_version="0.1.1"))
    problems, notes = check(mp)
    assert problems == [], problems
    assert any("other than the one bundled" in n for n in notes), notes


# Witnesses: §6.2 steps 3 and 4, §7.1, §7.3, §7.4; per-check status and the
# verdict (ruled 2026-10-20).

def _witness_envelope(tmp_path: pathlib.Path, version: str = "0.1.1", fn=None,
                      payload: bytes = b'{"pid": 1}\n', scope=None) -> pathlib.Path:
    """An envelope with one witness whose payload, path_hashes and record
    hash are right; fn(witness, root) edits the witness before its record
    hash is taken, so only what fn sets out to break is broken."""
    mp = _envelope(tmp_path)
    root = mp.parent
    (root / "witnesses" / "w1").mkdir(parents=True)
    pf = root / "witnesses" / "w1" / "procs.json"
    pf.write_bytes(payload)
    w = {"id": "w1", "kind": "process_tree", "captured_at": "2026-10-20T00:00:00.000000Z",
         "scope": scope or {"type": "trace", "target_trace_id": "t-a"},
         "primary_path": "witnesses/w1/procs.json", "size_bytes": len(payload),
         "path_hashes": {"witnesses/w1/procs.json": "sha256:" + hashlib.sha256(payload).hexdigest()}}
    if fn:
        fn(w, root)
    w["content_hash"] = compute_content_hash(w)
    return _mutate(mp, lambda m: m.update(envelope_version=version, witnesses=[w]))


def _statuses(r, step, i=0):
    w = f"witnesses[{i}]"
    return [c["status"] for c in r.checks
            if c["step"] == step and (c["subject"] == w or c["subject"].startswith(w + " "))]


def test_a_consistent_witness_passes_steps_3_and_4(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_witness_envelope(tmp_path))
    assert r.problems == [], r.problems
    assert _statuses(r, "3") == ["passed"] and _statuses(r, "4") == ["passed"], r.checks
    assert not any("not listed in the manifest" in n for n in r.notes), r.notes


def test_a_wrong_witness_payload_fails_step_3(tmp_path):
    mp = _witness_envelope(tmp_path)
    (mp.parent / "witnesses" / "w1" / "procs.json").write_bytes(b'{"pid": 2}\n')
    problems, _ = check(mp)
    assert any("path_hashes[" in p and "does not match the file bytes" in p for p in problems), problems


@pytest.mark.parametrize("pad,sep", [(0, "\n"), (0, "\u2028"), (0, "\u2029"), (5000, "\n")])
def test_a_schema_error_path_cannot_forge_an_output_line(tmp_path, capsys, pad, sep):
    # A path_hashes key is part of the JSON path of a schema error on its
    # value; the path is escaped and bounded as the message is, so a key
    # holding a line break (a newline, or a separator str.splitlines() breaks
    # on) cannot add a verdict or ok line to standard output.
    key = "k" * pad + f"{sep}  verdict: integrity only: steps 1 to 4 passed; 0 note(s){sep}ok: 0 problem(s)"
    mp = _witness_envelope(tmp_path, fn=lambda w, root: w["path_hashes"].__setitem__(key, "not-a-digest"))
    assert main(["verify_envelope.py", str(mp)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert not any(ln.startswith("ok:") for ln in lines), lines
    assert sum(ln.startswith("  verdict:") for ln in lines) == 1, lines
    assert lines[-1].startswith("FAIL"), lines
    path_lines = [ln for ln in lines if "schema: witnesses/0/path_hashes/" in ln]
    assert len(path_lines) == 1, lines
    if pad:
        assert len(path_lines[0]) < 700 and "chars]" in path_lines[0], len(path_lines[0])
    else:
        assert f"\\u{ord(sep):04x}  verdict:" in path_lines[0], path_lines


def test_a_wrong_witness_record_hash_fails_step_4(tmp_path):
    mp = _mutate(_witness_envelope(tmp_path), lambda m: m["witnesses"][0].update(captured_by="edited"))
    problems, _ = check(mp)
    assert any("witness content_hash does not match its record" in p for p in problems), problems


def test_a_missing_witness_payload_fails(tmp_path):
    mp = _witness_envelope(tmp_path)
    (mp.parent / "witnesses" / "w1" / "procs.json").unlink()
    problems, _ = check(mp)
    assert any("no such regular file: witnesses/w1/procs.json" in p for p in problems), problems


def test_path_hashes_is_required_from_0_1_1_and_unverifiable_under_0_1_0(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    drop = lambda w, root: w.pop("path_hashes")  # noqa: E731
    r = verify(_witness_envelope(tmp_path / "a", "0.1.1", drop))
    assert any("path_hashes is missing" in p for p in r.problems), r.problems
    r = verify(_witness_envelope(tmp_path / "b", "0.1.0", drop))
    assert r.problems == [], r.problems
    assert _statuses(r, "3") == ["skipped"], r.checks
    assert any("unverifiable" in n for n in r.notes), r.notes


@pytest.mark.parametrize("edit,needle", [
    (lambda w, root: w["path_hashes"].clear(), "has no key for listed path"),
    (lambda w, root: w["path_hashes"].update({"witnesses/w1/other.json": "sha256:" + "0" * 64}),
     "which the record does not list"),
])
def test_path_hashes_must_match_the_listed_paths_exactly_from_0_1_1(tmp_path, edit, needle):
    problems, _ = check(_witness_envelope(tmp_path, "0.1.1", edit))
    assert any(needle in p for p in problems), problems


def test_a_witness_path_through_a_symlink_fails(tmp_path):
    def via_symlink(w, root):
        (root / "elsewhere").mkdir()
        (root / "elsewhere" / "procs.json").write_bytes(b'{"pid": 1}\n')
        (root / "witnesses" / "w1" / "procs.json").unlink()
        (root / "witnesses" / "w1" / "procs.json").symlink_to(root / "elsewhere" / "procs.json")
    problems, _ = check(_witness_envelope(tmp_path, fn=via_symlink))
    assert any("symlink" in p for p in problems), problems


def test_witness_scope(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    # A trace scope naming no listing is noted, not failed (§7.3, ruled 2026-10-21) ...
    problems, notes = check(_witness_envelope(tmp_path / "a", scope={"type": "trace", "target_trace_id": "t-zz"}))
    assert problems == [] and any("t-zz names no trace listing; the witness applies to no" in n for n in notes), \
        (problems, notes)
    # ... while a sub-trace scope naming it fails.
    problems, _ = check(_witness_envelope(tmp_path / "c", scope={"type": "turn", "target_trace_id": "t-zz",
                                                               "target_id": "turn-0"}))
    assert any("is not a bundled trace" in p for p in problems), problems
    r = verify(_witness_envelope(tmp_path / "b", scope={"type": "turn", "target_trace_id": "t-a",
                                                        "target_id": "turn-1"}))
    assert r.problems == [], r.problems
    assert _statuses(r, "scope") == ["passed", "passed"], r.checks   # trace bundled; turn-1 resolves


def test_a_witness_removed_as_a_whole(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    rem = {"operation": "remove", "reason_code": "pii_detected", "authorised_by": "probe",
           "authorised_at": "2026-10-20T00:00:00.000000Z"}

    def remove(w, root):
        for k in ("primary_path", "size_bytes", "path_hashes"):
            w.pop(k)
        w["redaction"] = rem
        (root / "witnesses" / "w1" / "procs.json").unlink()
    r = verify(_witness_envelope(tmp_path / "a", fn=remove))
    assert r.problems == [], r.problems
    assert _statuses(r, "3") == ["removed"] and _statuses(r, "4") == ["passed"], r.checks
    # Removal is not a skipped step (§6.2; ruled 2026-10-20): a correctly
    # redacted, unsigned envelope has integrity only.
    assert r.verdict().startswith("integrity only"), r.verdict()
    assert _statuses(r, "witness") == ["removed"], r.checks
    # The record itself stays integrity-relevant (§7.4).
    mp = _mutate(_witness_envelope(tmp_path / "b", fn=remove), lambda m: m["witnesses"][0].update(captured_by="x"))
    problems, _ = check(mp)
    assert any("witness content_hash does not match" in p for p in problems), problems


def test_no_check_is_reported_passed_when_it_failed_and_a_signed_envelope_is_informational(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _witness_envelope(tmp_path)
    (mp.parent / "witnesses" / "w1" / "procs.json").write_bytes(b"tampered")
    r = verify(mp)
    assert "failed" in _statuses(r, "3") and "passed" not in _statuses(r, "3"), r.checks
    assert r.verdict().startswith("informational only"), r.verdict()
    (tmp_path / "s").mkdir()
    mp = _witness_envelope(tmp_path / "s")
    _sign(mp)
    r = verify(mp)
    assert r.problems == [] and r.verdict().startswith("informational only"), r.verdict()
    assert "signature present but not verified" in r.verdict()


def test_json_report(tmp_path, capsys):
    mp = _witness_envelope(tmp_path)
    assert main(["verify_envelope.py", "--json", str(mp)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"].startswith("integrity only") and out["counts"]["failed"] == 0, out
    assert {c["step"] for c in out["checks"]} >= {"schema", "1", "2", "3", "4", "scope"}, out["checks"]



# 0.1.0 robustness, unverifiable versus failed, the
# per-witness summary, removal rules under 0.1.0, and the verdict.

@pytest.mark.parametrize("value", ["sha256:x", 5, ["a"]])
def test_a_0_1_0_path_hashes_that_is_not_an_object_is_a_problem_not_a_crash(tmp_path, value):
    problems, _ = check(_witness_envelope(tmp_path, "0.1.0", lambda w, root: w.update(path_hashes=value)))
    assert any("path_hashes is not an object" in p for p in problems), problems


def _deep(n):
    v = "leaf"
    for _ in range(n):
        v = {"a": v}
    return v


@pytest.mark.parametrize("version,metadata,status", [
    ("0.1.0", _deep(1500), "skipped"),           # no reproducible canonical form: unverifiable
    ("0.1.0", {"n": 2 ** 60}, "skipped"),
    ("0.1.1", {"n": 2 ** 60}, "failed"),         # from 0.1.1 the record MUST meet §3.4
])
def test_a_record_without_a_canonical_form(tmp_path, version, metadata, status):
    from verify_envelope import verify  # noqa: PLC0415
    # Set after the record hash is taken: the canonical form is what fails.
    mp = _mutate(_witness_envelope(tmp_path, version),
                 lambda m: m["witnesses"][0].update(metadata=metadata))
    r = verify(mp)
    assert _statuses(r, "4") == [status], r.checks
    assert (status == "failed") == bool(r.problems), r.problems
    assert r.verdict().startswith("informational only"), r.verdict()


@pytest.mark.parametrize("metadata,why", [
    ({"n": 1e300}, "number-outside-safe-range"),
    ({"s": "￿"}, "noncharacter"),
    (_deep(70), "depth-exceeds-64"),
])
def test_a_0_1_0_record_outside_the_3_4_rules_whose_hash_matches_is_unverifiable(tmp_path, capsys,
                                                                                metadata, why):
    # The hash is taken over the record as it stands, so it reproduces here; the
    # record still has no canonical form every verifier reproduces, so step 4 is
    # skipped under 0.1.0 (envelope §7.1), never passed, and 0.1.1 fails it.
    from verify_envelope import main, verify  # noqa: PLC0415
    mp = _witness_envelope(tmp_path, "0.1.0", lambda w, root: w.update(metadata=metadata))
    r = verify(mp)
    assert _statuses(r, "4") == ["skipped"] and r.problems == [], (r.checks, r.problems)
    assert any(f"(0.1.0): {why}; its content_hash is unverifiable" in n for n in r.notes), r.notes
    assert main(["verify_envelope.py", str(mp)]) == 3
    assert "verdict: informational only" in capsys.readouterr().out
    r = verify(_mutate(mp, lambda m: m.update(envelope_version="0.1.1")))
    assert _statuses(r, "4") == ["failed"], r.checks


@pytest.mark.parametrize("version,status", [("0.1.0", "skipped"), ("0.1.1", "failed")])
def test_a_witness_nested_past_the_serialiser_is_a_depth_departure_not_a_traceback(tmp_path, version,
                                                                                  status):
    # Written as text: json.dumps cannot build it. Where the manifest loads
    # (CPython 3.14 parses this deep), the record's own serialisation runs out
    # of stack, and that is a §3.4 depth departure, not a RecursionError out
    # of verify(). Where the parser refuses first (3.13 stops near 10,000
    # levels), the manifest is refused cleanly: exit 2, no traceback.
    from verify_envelope import verify  # noqa: PLC0415
    n = 100_000
    mp = _witness_envelope(tmp_path, version)
    m = json.loads(mp.read_text())
    m["witnesses"][0]["metadata"] = "@deep@"
    mp.write_text(json.dumps(m).replace('"@deep@"', '{"a":' * n + "1" + "}" * n))
    try:
        r = verify(mp)
    except ValueError as e:
        assert str(e) == "manifest is nested too deeply to read", e
        code, out = _cli(mp)
        assert code == 2 and "Traceback" not in out and "nested too deeply to read" in out, (code, out[-2000:])
        return
    assert _statuses(r, "4") == [status], r.checks
    assert any("depth-exceeds-64" in t for t in r.problems + r.notes), (r.problems, r.notes)


def test_a_0_1_0_extra_path_hashes_entry_is_skipped_not_silent(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_witness_envelope(tmp_path, "0.1.0", lambda w, root: w["path_hashes"].update(
        {"witnesses/w1/other.json": "sha256:" + "0" * 64})))
    assert r.problems == [] and "skipped" in _statuses(r, "3"), r.checks
    assert r.verdict().startswith("informational only"), r.verdict()


def test_a_resolved_event_scope_leaves_the_verdict_integrity_only(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_witness_envelope(tmp_path, scope={"type": "turn", "target_trace_id": "t-a",
                                                  "target_id": "turn-1"}))
    assert r.problems == [] and "unsupported" not in _statuses(r, "scope"), r.checks
    assert r.verdict().startswith("integrity only"), r.verdict()


def test_the_witness_summary(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_witness_envelope(tmp_path / "ok"))
    assert _statuses(r, "witness") == ["passed"], r.checks

    def with_aux(w, root):
        (root / "witnesses" / "w1" / "aux.txt").write_text("aux")
        w["auxiliary_paths"] = ["witnesses/w1/aux.txt"]
        w["path_hashes"]["witnesses/w1/aux.txt"] = "sha256:" + "0" * 64   # wrong
    r = verify(_witness_envelope(tmp_path / "aux", fn=with_aux))
    # One listed file passed, one failed: the witness is not verified.
    assert "passed" in _statuses(r, "3") and "failed" in _statuses(r, "3"), r.checks
    assert _statuses(r, "witness") == ["failed"], r.checks


def test_a_wrong_witness_size_bytes_fails(tmp_path):
    problems, _ = check(_witness_envelope(tmp_path, fn=lambda w, root: w.update(size_bytes=99)))
    assert any("size_bytes does not match the primary file" in p for p in problems), problems


def test_a_duplicate_witness_path_fails(tmp_path):
    problems, _ = check(_witness_envelope(tmp_path, fn=lambda w, root: w.update(
        auxiliary_paths=["witnesses/w1/procs.json"])))
    assert any("already listed" in p for p in problems), problems


_REMOVE_W = {"operation": "remove", "reason_code": "pii_detected", "authorised_by": "probe",
             "authorised_at": "2026-10-20T00:00:00.000000Z"}


@pytest.mark.parametrize("name,edit,needle", [
    ("primary present", lambda w, root: w.update(redaction=_REMOVE_W, path_hashes={}), "is present"),
    ("aux present", lambda w, root: (root / "witnesses" / "w1" / "procs.json").unlink() or w.update(
        redaction=_REMOVE_W, path_hashes={}, size_bytes=None) or w.pop("size_bytes") or w.update(
        auxiliary_paths=["witnesses/w1/aux.txt"]) or (root / "witnesses" / "w1" / "aux.txt").write_text("x"),
     "is present"),
])
def test_a_0_1_0_removed_witness_lists_nothing_present(tmp_path, name, edit, needle):
    problems, _ = check(_witness_envelope(tmp_path, "0.1.0", edit))
    assert any(needle in p for p in problems), (name, problems)


def test_a_0_1_0_removed_witness_may_keep_size_bytes_but_not_from_0_1_1(tmp_path):
    # The no-file-fields rule is 0.1.1's (§7.4); 0.1.0 left it unstated (§12).
    # 0.1.0 still requires primary_path; its file is absent.
    keep = lambda w, root: (root / "witnesses" / "w1" / "procs.json").unlink() or w.update(  # noqa: E731
        redaction=_REMOVE_W, path_hashes={})
    problems, _ = check(_witness_envelope(tmp_path / "a", "0.1.0", keep))
    assert problems == [], problems
    from verify_envelope import _check_witness, Report  # noqa: PLC0415
    rep = Report()
    _check_witness(rep, tmp_path, "witnesses[0]", {"id": "w", "content_hash": "x", "size_bytes": 3,
                                                   "redaction": _REMOVE_W}, True, {}, set())
    assert any("carries size_bytes" in p for p in rep.problems), rep.problems


def test_a_removed_witness_reports_every_problem(tmp_path):
    def two(w, root):
        w.update(redaction=_REMOVE_W, path_hashes={"witnesses/w1/procs.json": "sha256:" + "0" * 64})
    problems, _ = check(_witness_envelope(tmp_path, "0.1.0", two))
    assert any("path_hashes is not empty" in p for p in problems), problems
    assert any("is present" in p for p in problems), problems


@pytest.mark.parametrize("name,mp_fn", [
    ("schema failure", lambda tp: _mutate(_envelope(tp), lambda m: m.pop("producer"))),
    ("scope failure", lambda tp: _witness_envelope(tp, scope={"type": "turn", "target_trace_id": "t-a",
                                                              "target_id": "turn-9"})),
])
def test_a_failed_check_is_never_integrity_only(tmp_path, name, mp_fn):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(mp_fn(tmp_path))
    assert r.problems, name
    assert r.verdict().startswith("informational only"), (name, r.verdict())
    assert main(["verify_envelope.py", str(mp_fn(tmp_path / "cli"))]) == 1


def test_an_event_scoped_witness_is_verified_when_its_steps_pass(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_witness_envelope(tmp_path, scope={"type": "turn", "target_trace_id": "t-a", "target_id": "turn-1"}))
    assert _statuses(r, "witness") == ["passed"], r.checks


# Trace versions: §6.2 step 2 and §12 (ruled 2026-10-20). Every fixture
# names the envelope version and the trace version under test.

_NO_HASH = object()


def _trace_statuses(r, i=0):
    return [c["status"] for c in r.checks if c["step"] == "2" and c["subject"] == f"traces[{i}]"]


def _versioned(tmp_path: pathlib.Path, envelope_version: str, schema_version, listing_version=None,
               content_hash=None, trace_content_hash=None) -> pathlib.Path:
    """One trace declaring schema_version (omitted when None) in an envelope
    declaring envelope_version. content_hash: None for the right hash,
    _NO_HASH for none, or the value to store."""
    root = tmp_path / "env"
    (root / "traces").mkdir(parents=True)
    t = {"id": "t-a", "turns": [{"content": "a"}]}
    if schema_version is not None:
        t["schema_version"] = schema_version
    _schema_valid(t)
    right = compute_content_hash(t)
    if content_hash is None:
        t["content_hash"] = right
    elif content_hash is not _NO_HASH:
        t["content_hash"] = content_hash
    f = root / "traces" / "t-a.minitrace.json"
    f.write_text(json.dumps(t))
    m = {"envelope_version": envelope_version, "envelope_id": "env-probe",
         "created_at": "2026-10-20T00:00:00.000000Z",
         "producer": {"id": "probe", "kind": "automated_pipeline"},
         "traces": [{"trace_id": "t-a", "path": "traces/t-a.minitrace.json",
                     "schema_version": listing_version or (schema_version if isinstance(schema_version, str)
                                                           else "0.3.0"),
                     "trace_content_hash": trace_content_hash or right,
                     "file_content_hash": "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()}]}
    (root / "manifest.json").write_text(json.dumps(m))
    return root / "manifest.json"


def test_a_v0_2_trace_under_0_1_0_has_only_its_listing_hash_checked(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_versioned(tmp_path, "0.1.0", "minitrace-v0.2.0", content_hash=_NO_HASH))
    assert r.problems == [], r.problems
    assert _trace_statuses(r) == ["passed"], r.checks
    assert r.verdict().startswith("integrity only"), r.verdict()


def test_a_v0_2_trace_under_0_1_0_still_has_its_listing_hash_checked(tmp_path):
    problems, _ = check(_versioned(tmp_path, "0.1.0", "minitrace-v0.2.0", content_hash=_NO_HASH,
                                   trace_content_hash="sha256:" + "0" * 64))
    assert any("trace_content_hash does not match" in p for p in problems), problems


def test_a_content_hash_on_a_v0_2_trace_under_0_1_0_is_noted_not_checked(tmp_path):
    problems, notes = check(_versioned(tmp_path, "0.1.0", "minitrace-v0.2.0",
                                       content_hash="sha256:" + "0" * 64))
    assert problems == [], problems
    assert any("content_hash member is not checked" in n for n in notes), notes


@pytest.mark.parametrize("envelope_version", ["0.1.1", "0.1.2"])
@pytest.mark.parametrize("schema_version", ["minitrace-v0.2.0", "0.4.0", "0.3.01", "minitrace-v0.3", 0.3, None])
def test_from_0_1_1_a_trace_that_is_not_v0_3_fails_step_2(tmp_path, envelope_version, schema_version):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_versioned(tmp_path, envelope_version, schema_version))
    assert any("is not v0.3.x" in p for p in r.problems), r.problems
    assert _trace_statuses(r) == ["failed"], r.checks


@pytest.mark.parametrize("schema_version", ["minitrace-v0.4", "minitrace-v0.2.1", "minitrace-v0.1.0", "0.2.0", "0.3.01", 0.3, None])
def test_under_0_1_0_a_trace_neither_v0_3_nor_v0_2_fails_step_2(tmp_path, schema_version):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_versioned(tmp_path, "0.1.0", schema_version))
    assert any("neither v0.3.x nor v0.2.x" in p for p in r.problems), r.problems
    assert _trace_statuses(r) == ["failed"], r.checks


@pytest.mark.parametrize("envelope_version", ["0.1.0", "0.1.1"])
@pytest.mark.parametrize("schema_version", ["0.3.0", "0.3.12"])
def test_a_v0_3_trace_passes_under_every_revision(tmp_path, envelope_version, schema_version):
    problems, _ = check(_versioned(tmp_path, envelope_version, schema_version))
    assert problems == [], problems


@pytest.mark.parametrize("envelope_version", ["0.1.0", "0.1.1"])
def test_a_v0_3_trace_without_content_hash_fails_under_every_revision(tmp_path, envelope_version):
    problems, _ = check(_versioned(tmp_path, envelope_version, "0.3.0", content_hash=_NO_HASH))
    assert any("stored content_hash does not match" in p for p in problems), problems


@pytest.mark.parametrize("envelope_version,schema_version,listing_version,content_hash", [
    # A v0.2.x trace listed as 0.3.0 under 0.1.0 is still a v0.2.x trace.
    ("0.1.0", "minitrace-v0.2.0", "0.3.0", _NO_HASH),
    # A v0.3.x trace listed as v0.2.x under 0.1.1 is still a v0.3.x trace.
    ("0.1.1", "0.3.0", "minitrace-v0.2.0", None),
])
def test_the_trace_declares_its_version_and_the_listing_only_records_it(
        tmp_path, envelope_version, schema_version, listing_version, content_hash):
    problems, notes = check(_versioned(tmp_path, envelope_version, schema_version,
                                       listing_version=listing_version, content_hash=content_hash))
    assert problems == [], problems
    assert any("listing schema_version" in n and "the trace's decides" in n for n in notes), notes


# A trace_id held by two or more listings (ruled 2026-10-20): every holder
# fails step 2 and none is verified, as R5 treats shared trace ids; counted
# before any file is read; a reference to the id names no one trace.

def _add_holder(mp: pathlib.Path, tid: str, k: int = 0, first: bool = False, missing: bool = False) -> pathlib.Path:
    """Another 0.3.0 trace listed under trace_id tid, with its own content;
    first puts the listing before the others; missing lists a file that is
    not there."""
    m = json.loads(mp.read_text())
    t = _schema_valid({"id": tid, "schema_version": "0.3.0", "turns": [{"content": f"dup{k}"}]})
    t["content_hash"] = compute_content_hash(t)
    f = mp.parent / "traces" / f"dup-{tid}-{k}.minitrace.json"
    if not missing:
        f.write_text(json.dumps(t))
    entry = {"trace_id": tid, "path": f"traces/{f.name}", "schema_version": "0.3.0",
             "trace_content_hash": t["content_hash"],
             "file_content_hash": "sha256:" + (hashlib.sha256(f.read_bytes()).hexdigest() if not missing
                                               else "0" * 64)}
    m["traces"].insert(0, entry) if first else m["traces"].append(entry)
    mp.write_text(json.dumps(m))
    return mp


def _step2(r, subject):
    return [c["status"] for c in r.checks if c["step"] == "2" and c["subject"] == subject]


def test_every_listing_holding_a_shared_trace_id_fails_and_none_is_verified(tmp_path):
    from verify_envelope import main, verify  # noqa: PLC0415
    r = verify(_add_holder(_envelope(tmp_path), "t-a"))   # traces[0] t-a, traces[1] t-b, traces[2] t-a
    assert _step2(r, "traces[0]") == ["failed"], r.checks
    assert _step2(r, "traces[2]") == ["failed"], r.checks
    assert _step2(r, "traces[1]") == ["passed"], r.checks          # t-b is untouched
    assert sum("is held by 2 trace listings; none is verified" in p for p in r.problems) == 2, r.problems
    assert r.verdict().startswith("informational only"), r.verdict()
    assert main(["verify_envelope.py", str(_add_holder(_envelope(tmp_path / "cli"), "t-a"))]) == 1


def test_a_shared_trace_id_is_found_before_any_file_is_read(tmp_path):
    # The first holder's file is missing, so it is never parsed; the later
    # holder still fails as a holder of a shared id.
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_add_holder(_envelope(tmp_path), "t-a", first=True, missing=True))
    assert _step2(r, "traces[1]") == ["failed"], r.checks          # the original t-a, now second
    assert any(p.startswith("traces[1]: trace_id t-a is held by 2") for p in r.problems), r.problems
    # The unread holder fails as a holder too, and is not also "skipped".
    assert _step2(r, "traces[0]") == ["failed"], r.checks
    assert any(p.startswith("traces[0]: trace_id t-a is held by 2") for p in r.problems), r.problems


def test_a_holder_whose_file_does_not_parse_still_fails_as_a_holder(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _add_holder(_envelope(tmp_path), "t-a")
    m = json.loads(mp.read_text())
    f = mp.parent / m["traces"][2]["path"]
    f.write_text('{"id": "t-a", "id": "x"}')                       # a duplicate key: not conformant
    m["traces"][2]["file_content_hash"] = "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()
    mp.write_text(json.dumps(m))
    r = verify(mp)
    assert any(p.startswith("traces[2]: trace_id t-a is held by 2") for p in r.problems), r.problems
    assert any(p.startswith("traces[2]: trace not conformant") for p in r.problems), r.problems
    assert "passed" not in _step2(r, "traces[2]"), r.checks


def test_a_listing_repeated_verbatim_gets_no_passed_check_beside_its_failures(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_mutate(_envelope(tmp_path), lambda m: m["traces"].append(dict(m["traces"][0]))))
    step1 = [c["status"] for c in r.checks if c["step"] == "1" and c["subject"] == "traces[2]"]
    assert step1 == ["failed"], r.checks                               # path listed twice, file hash right
    assert _step2(r, "traces[2]") == ["failed"] and _step2(r, "traces[0]") == ["failed"], r.checks
    assert [c["status"] for c in r.checks if c["step"] == "1" and c["subject"] == "traces[0]"] == ["passed"]


def test_three_holders_are_counted(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _add_holder(_add_holder(_envelope(tmp_path), "t-b", 0), "t-b", 1)
    r = verify(mp)
    assert sum("is held by 3 trace listings" in p for p in r.problems) == 3, r.problems


def test_a_reference_to_a_shared_trace_id_names_no_one_trace(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    # The binding (produces_trace_id t-a) and the witness scope (t-a).
    r = verify(_add_holder(_witness_envelope(tmp_path / "w"), "t-a"))
    # Both are notes (§4.7, §7.3, ruled 2026-10-21): the entries apply to no trace.
    assert any("produces_trace_id t-a is held by 2 trace listings" in n for n in r.notes), r.notes
    assert any("scope target_trace_id t-a is held by 2 trace listings" in n for n in r.notes), r.notes
    assert not any("produces_trace_id" in p or "scope" in p for p in r.problems), r.problems
    assert _statuses(r, "scope") == [] and _statuses(r, "witness") == ["passed"], r.checks
    # A sub-trace scope naming the shared id still fails.
    r = verify(_add_holder(_witness_envelope(tmp_path / "e", scope={"type": "turn", "target_trace_id": "t-a",
                                                                    "target_id": "turn-0"}), "t-a"))
    assert any("scope target_trace_id t-a is held by 2 trace listings" in p for p in r.problems), r.problems
    assert _statuses(r, "witness") == ["failed"], r.checks
    # A lineage edge (t-a -> t-b) to a shared id is noted, not compared.
    problems, notes = check(_add_holder(_lineage_envelope(tmp_path / "l", lambda m: "sha256:" + "1" * 64), "t-b"))
    assert any("lineage[0] -> t-b: the target id is held by 2 trace listings" in n for n in notes), notes
    assert not any("other than the one bundled" in n for n in notes), notes


def test_a_shared_trace_ids_own_lineage_is_not_compared(tmp_path):
    # None of the holders is verified, so the lineage edges of a holder are
    # not compared with the bundled target either (t-a -> t-b would be noted
    # as another state of t-b if t-a were a verified trace).
    _, notes = check(_add_holder(_lineage_envelope(tmp_path, lambda m: "sha256:" + "1" * 64), "t-a", first=True))
    assert not any("other than the one bundled" in n for n in notes), notes
    # Control: the same envelope without the second holder is compared.
    _, notes = check(_lineage_envelope(tmp_path / "control", lambda m: "sha256:" + "1" * 64))
    assert any("other than the one bundled" in n for n in notes), notes


# Event scopes (envelope §7.3, minitrace-spec §4.2; ruled 2026-10-20): an
# event-scoped witness names exactly one event of its type in the bundled
# target, a turn as "turn-<index>" only; no event, an event of another type,
# or an id held by more than one event is a failed scope check; the target is
# read even when it failed step 2.

def _with_events(mp: pathlib.Path, events: dict, break_hash: bool = False) -> pathlib.Path:
    """Add the given event arrays to trace t-a (traces[0]) and re-hash it,
    or with break_hash leave its listing hash stale (a failed step 2)."""
    m = json.loads(mp.read_text())
    f = mp.parent / m["traces"][0]["path"]
    t = json.loads(f.read_text())
    t.update(events)
    _schema_valid(t)
    t["content_hash"] = compute_content_hash(t)
    f.write_text(json.dumps(t))
    if not break_hash:
        m["traces"][0].update(trace_content_hash=t["content_hash"])
    m["traces"][0].update(file_content_hash="sha256:" + hashlib.sha256(f.read_bytes()).hexdigest())
    mp.write_text(json.dumps(m))
    return mp


_EVENTS = {"tool_calls": [{"id": "tc-1", "tool_name": "x"}], "segment_markers": [{"id": "seg-1"}],
           "policy_triggers": [{"id": "trig-1"}]}


def _scoped(tmp_path, stype, target_id, events=_EVENTS, **kw):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _witness_envelope(tmp_path, scope={"type": stype, "target_trace_id": "t-a", "target_id": target_id})
    return verify(_with_events(mp, events, **kw))


@pytest.mark.parametrize("stype,target_id", [("turn", "turn-0"), ("turn", "turn-1"), ("tool_call", "tc-1"),
                                             ("segment", "seg-1"), ("policy_trigger", "trig-1")])
def test_an_event_scope_naming_one_event_of_its_type_passes(tmp_path, stype, target_id):
    r = _scoped(tmp_path, stype, target_id)
    assert r.problems == [], r.problems
    assert _statuses(r, "scope") == ["passed", "passed"] and _statuses(r, "witness") == ["passed"], r.checks


@pytest.mark.parametrize("stype,target_id,why", [
    ("turn", "1", "names no turn"),                    # the bare index is not the witness form
    ("turn", "turn-2", "names no turn"),
    ("turn", "turn-01", "names no turn"),
    ("tool_call", "tc-9", "names no tool_call"),
    ("segment", "tc-1", "names a tool_call, not a segment"),
    ("policy_trigger", "seg-1", "names a segment, not a policy_trigger"),
    ("tool_call", "turn-0", "names a turn, not a tool_call"),
])
def test_an_event_scope_that_names_no_event_of_its_type_fails(tmp_path, stype, target_id, why):
    from verify_envelope import main  # noqa: PLC0415
    r = _scoped(tmp_path, stype, target_id)
    assert any(f"scope target_id {target_id} in trace t-a: {why}" in p for p in r.problems), r.problems
    assert "failed" in _statuses(r, "scope") and _statuses(r, "witness") == ["failed"], r.checks
    assert r.verdict().startswith("informational only"), r.verdict()
    mp = _witness_envelope(tmp_path / "cli", scope={"type": stype, "target_trace_id": "t-a", "target_id": target_id})
    assert main(["verify_envelope.py", str(_with_events(mp, _EVENTS))]) == 1


def test_an_event_id_held_twice_fails_the_scope(tmp_path):
    # §4.2: ids are unique within a trace, across the event types too.
    for events, why in (({"tool_calls": [{"id": "x"}, {"id": "x"}]}, "is held by 2 events (tool_call, tool_call)"),
                        ({"tool_calls": [{"id": "x"}], "segment_markers": [{"id": "x"}]},
                         "is held by 2 events (tool_call, segment)"),
                        ({"tool_calls": [{"id": "turn-0"}]}, "is held by 2 events (turn, tool_call)"),
                        ({"turns": [{"index": 0}, {"index": 0}]}, "is held by 2 events (turn, turn)")):
        stype = "turn" if "(turn, turn)" in why else "tool_call"
        target = "turn-0" if "turn" in why.split("(")[1] else "x"
        r = _scoped(tmp_path / f"case{len(list(tmp_path.iterdir()))}", stype, target, events)
        assert any(why in p for p in r.problems), (events, r.problems)


def test_a_turn_index_is_written_in_plain_decimal(tmp_path):
    # An index written 1.0 is turn-1 (§4.2); a boolean, negative, fractional
    # or string index names no turn, each tried under the name it would get.
    r = _scoped(tmp_path / "a", "turn", "turn-1", {"turns": [{"index": 0}, {"index": 1.0}]})
    assert r.problems == [], r.problems
    for i, (bad, name) in enumerate(((True, "turn-1"), (True, "turn-True"), (-1, "turn--1"), (-1, "turn-1"),
                                     (1.5, "turn-1.5"), (1.5, "turn-1"), ("1", "turn-1"))):
        r = _scoped(tmp_path / f"b{i}", "turn", name, {"turns": [{"index": bad}]})
        assert any("names no turn" in p for p in r.problems), (bad, name, r.problems)
    # The witness names the plain decimal only, never another spelling.
    for i, name in enumerate(("turn-1.0", "turn-1e0", "turn-01", "turn-+1")):
        r = _scoped(tmp_path / f"c{i}", "turn", name, {"turns": [{"index": 1}]})
        assert any("names no turn" in p for p in r.problems), (name, r.problems)


def test_an_index_spelled_3e0_in_the_file_is_turn_3(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _with_events(_witness_envelope(tmp_path, scope={"type": "turn", "target_trace_id": "t-a",
                                                         "target_id": "turn-3"}), {"turns": [{"index": 3}]})
    m = json.loads(mp.read_text())
    f = mp.parent / m["traces"][0]["path"]
    text = f.read_text()
    assert text.count('"index": 3') == 1
    f.write_text(text.replace('"index": 3', '"index": 3e0'))   # same canonical form, same content_hash
    m["traces"][0]["file_content_hash"] = "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()
    mp.write_text(json.dumps(m))
    r = verify(mp)
    assert r.problems == [], r.problems
    assert _statuses(r, "scope") == ["passed", "passed"], r.checks


def test_an_event_scope_is_resolved_in_a_target_that_failed_step_2(tmp_path):
    r = _scoped(tmp_path, "tool_call", "tc-1", break_hash=True)
    assert any("trace_content_hash does not match" in p for p in r.problems), r.problems
    assert not any("scope target_id" in p for p in r.problems), r.problems
    assert _statuses(r, "scope") == ["passed", "passed"], r.checks


# Envelope §4.7, §4.11, §7.3 (spec 3cca1c67, b626b158; ruled 2026-10-21): a
# produces_trace_id or a trace scope that names no listing's trace_id, or the
# trace_id of two or more, is noted, applies to no trace, and fails no check;
# listings are counted whether or not their trace was read; removed and
# missing raw artefacts are checked too; sub-trace scopes must resolve.

def _raw_entry(tmp_path: pathlib.Path, ptid, removed=False, missing=False) -> dict:
    root = tmp_path / "env"
    (root / "raw").mkdir(exist_ok=True)
    f = root / "raw" / "log.jsonl"
    entry = {"id": "r1", "kind": "harness_log", "path": "raw/log.jsonl", "produces_trace_id": ptid}
    if removed:
        entry = {"id": "r1", "kind": "harness_log", "produces_trace_id": ptid, "redaction": _REMOVE}
    elif missing:
        entry["content_hash"] = "sha256:" + "0" * 64
    else:
        f.write_text("{}\n")
        entry["content_hash"] = "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()
    return entry


@pytest.mark.parametrize("section", ["bindings", "raw_artefacts"])
@pytest.mark.parametrize("version", ["0.1.0", "0.1.1"])
def test_a_produces_trace_id_naming_no_listing_is_a_note(tmp_path, section, version):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _envelope(tmp_path)
    if section == "bindings":
        mp = _mutate(mp, lambda m: m["bindings"][0].update(produces_trace_id="nope"))
    else:
        mp = _mutate(mp, lambda m: m.update(raw_artefacts=[_raw_entry(tmp_path, "nope")]))
    r = verify(_mutate(mp, lambda m: m.update(envelope_version=version)))
    assert r.problems == [], r.problems
    assert any(f"{section}[0]: produces_trace_id nope names no trace listing; the entry applies to no trace" in n
               for n in r.notes), r.notes
    assert [c["status"] for c in r.checks if c["subject"] == f"{section}[0]"] == ["passed"], r.checks
    assert r.verdict().startswith("integrity only"), r.verdict()


def test_a_raw_artefact_naming_a_shared_trace_id_is_a_note(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _mutate(_envelope(tmp_path), lambda m: m.update(raw_artefacts=[_raw_entry(tmp_path, "t-a")]))
    r = verify(_add_holder(mp, "t-a"))
    assert any("raw_artefacts[0]: produces_trace_id t-a is held by 2 trace listings" in n for n in r.notes), r.notes
    assert not any(p.startswith("raw_artefacts[0]") for p in r.problems), r.problems


def test_removed_and_missing_raw_artefacts_have_their_produces_trace_id_checked(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_mutate(_envelope(tmp_path / "a"), lambda m: m.update(
        envelope_version="0.1.1", raw_artefacts=[_raw_entry(tmp_path / "a", "nope", removed=True)])))
    assert any("raw_artefacts[0]: produces_trace_id nope names no trace listing" in n for n in r.notes), r.notes
    assert r.problems == [], r.problems
    r = verify(_mutate(_envelope(tmp_path / "b"), lambda m: m.update(
        raw_artefacts=[_raw_entry(tmp_path / "b", "nope", missing=True)])))
    assert any("raw_artefacts[0]: produces_trace_id nope names no trace listing" in n for n in r.notes), r.notes
    assert any(p.startswith("raw_artefacts[0]: no such regular file") for p in r.problems), r.problems


def test_a_reference_to_one_listing_resolves_when_its_trace_was_not_read(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    # traces[0] (t-a) points at a missing file: not read, but listed once.
    mp = _witness_envelope(tmp_path / "t")
    mp = _mutate(mp, lambda m: m["traces"][0].update(path="traces/gone.minitrace.json"))
    r = verify(mp)
    assert not any("produces_trace_id" in n or "scope target_trace_id" in n for n in r.notes), r.notes
    assert _statuses(r, "scope") == ["passed"] and _statuses(r, "witness") == ["passed"], r.checks
    # An event scope there resolves no event, a failed check (§7.3).
    mp = _witness_envelope(tmp_path / "e", scope={"type": "turn", "target_trace_id": "t-a", "target_id": "turn-0"})
    r = verify(_mutate(mp, lambda m: m["traces"][0].update(path="traces/gone.minitrace.json")))
    assert any("the trace's file is absent or cannot be parsed, so it resolves no event" in p
               for p in r.problems), r.problems
    assert _statuses(r, "witness") == ["failed"], r.checks


def test_a_0_1_0_removed_raw_artefact_with_a_path_has_its_produces_trace_id_noted(tmp_path):
    problems, notes = _raw(tmp_path, "0.1.0", {"id": "r1", "kind": "harness_log", "path": "raw/gone.jsonl",
                                               "content_hash": "sha256:" + "0" * 64, "produces_trace_id": "nope",
                                               "redaction": _REMOVE})
    assert problems == [], problems
    assert any("produces_trace_id nope names no trace listing; the entry applies to no trace, "
               "and not to the bundle as a whole" in n for n in notes), notes


def test_a_noted_trace_scope_leaves_the_witness_verified(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_witness_envelope(tmp_path, scope={"type": "trace", "target_trace_id": "t-zz"}))
    assert r.problems == [] and _statuses(r, "scope") == [] and _statuses(r, "witness") == ["passed"], r.checks
    assert r.verdict().startswith("integrity only"), r.verdict()


def test_a_binding_path_listed_twice_gets_no_passed_record(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    def twice(m):
        m["bindings"].append(dict(m["bindings"][0]))
    r = verify(_mutate(_envelope(tmp_path), twice))
    assert [c["status"] for c in r.checks if c["subject"] == "bindings[1]"] == ["failed"], r.checks
    assert [c["status"] for c in r.checks if c["subject"] == "bindings[0]"] == ["passed"], r.checks


# Robustness (ruled 2026-10-21): a malformed trace lineage is a named step-2
# failure, a value outside the §3.4 rules in a 0.1.1 manifest is a step-1
# failure (witness records stay with step 4), a manifest that is not a
# regular file is unreadable (exit 2), and untrusted text never crashes the
# report.

def _with_lineage(tmp_path: pathlib.Path, lineage, version: str = "0.1.1") -> pathlib.Path:
    """The two-trace envelope at `version`, trace a carrying `lineage` and
    its hashes restamped, so only the lineage itself can fail."""
    mp = _mutate(_envelope(tmp_path), lambda m: m.update(envelope_version=version))
    m = json.loads(mp.read_text())
    a = mp.parent / m["traces"][0]["path"]
    t = json.loads(a.read_text())
    t["lineage"] = lineage
    t["content_hash"] = compute_content_hash(t)
    a.write_text(json.dumps(t))
    m["traces"][0].update(trace_content_hash=t["content_hash"],
                          file_content_hash="sha256:" + hashlib.sha256(a.read_bytes()).hexdigest())
    mp.write_text(json.dumps(m))
    return mp


@pytest.mark.parametrize("lineage, why", [
    (42, "lineage is not a list (a JSON number)"),
    ({"target_trace_id": "t-b"}, "lineage is not a list (a JSON object)"),
    ([7], "lineage[0] is not an object (a JSON number)"),
    ([{"type": "derived-from", "target_trace_id": [], "target_content_hash": "sha256:" + "0" * 64}],
     "lineage[0].target_trace_id is not a string (a JSON array); a non-empty string is required"),
    ([{"type": "derived-from", "target_trace_id": "t-b"},
      {"type": "derived-from", "target_trace_id": {}, "target_content_hash": "sha256:" + "0" * 64}],
     "lineage[1].target_trace_id is not a string (a JSON object); a non-empty string is required"),
    ([{"type": "derived-from", "target_trace_id": "t-b", "target_content_hash": 5}],
     "lineage[0].target_content_hash is not a string (a JSON number)"),
])
def test_a_malformed_lineage_is_a_named_step_2_failure(tmp_path, lineage, why):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_with_lineage(tmp_path, lineage))
    # The trace schema check reports the same lineage too (ruled 2026-10-21);
    # step 2 names it exactly once.
    assert [p for p in r.problems if not p.startswith("traces[0]: trace schema:")] == [f"traces[0]: {why}"], r.problems
    assert [c["status"] for c in r.checks if c["step"] == "2" and c["subject"] == "traces[0]"] == ["failed"], r.checks
    assert [c["status"] for c in r.checks if c["step"] == "2" and c["subject"] == "traces[1]"] == ["passed"], r.checks


def test_a_lineage_edge_without_a_type_or_target_is_a_named_step_2_failure(tmp_path):
    # Every edge carries type and target_trace_id as non-empty strings (the
    # base schema requires both); step 2 names each that is absent, null or
    # empty, besides what the trace schema check reports, as minitrace-verify
    # fails them.
    problems, _ = check(_with_lineage(tmp_path, [{"type": "derived-from", "target_trace_id": None}, {},
                                                 {"type": "", "target_trace_id": ""}]))
    named = [p for p in problems if not p.startswith("traces[0]: trace schema:")]
    req = "; a non-empty string is required"
    assert named == [f"traces[0]: lineage[0].target_trace_id is not a string (a JSON null){req}",
                     f"traces[0]: lineage[1].type is absent{req}",
                     f"traces[0]: lineage[1].target_trace_id is absent{req}",
                     f"traces[0]: lineage[2].type is the empty string{req}",
                     f"traces[0]: lineage[2].target_trace_id is the empty string{req}"], problems


def _lineage_on_first_trace(mp: pathlib.Path, lineage) -> pathlib.Path:
    m = json.loads(mp.read_text())
    a = mp.parent / m["traces"][0]["path"]
    t = json.loads(a.read_text())
    t["lineage"] = lineage
    t["content_hash"] = compute_content_hash(t)
    a.write_text(json.dumps(t))
    m["traces"][0].update(trace_content_hash=t["content_hash"],
                          file_content_hash="sha256:" + hashlib.sha256(a.read_bytes()).hexdigest())
    mp.write_text(json.dumps(m))
    return mp


@pytest.mark.parametrize("lineage, fails", [
    ([{"target_trace_id": "x"}], False),                       # no type: v0.2.0 defines no lineage
    ([{"type": "derived-from", "target_trace_id": None}], False),
    ([{"type": "derived-from"}], False),
    ([{"type": "derived-from", "target_trace_id": 42}], True),  # a present non-string id, as before
])
def test_a_v0_2_trace_under_0_1_0_keeps_the_earlier_lineage_check(tmp_path, lineage, fails):
    # The non-empty type and target_trace_id rule rests on the v0.3.0 base
    # schema; a v0.2.x trace, whose specification defines no lineage, keeps
    # the earlier check (ruled 2026-11-12).
    mp = _lineage_on_first_trace(_versioned(tmp_path, "0.1.0", "minitrace-v0.2.0"), lineage)
    problems, _ = check(mp)
    assert bool([p for p in problems if "lineage[" in p]) is fails, problems


def test_a_v0_2_edge_without_a_type_still_gets_its_section_6_3_note(tmp_path):
    # Under the earlier check a v0.2.x edge needs no type, so §6.3 still
    # reads it: an edge to another state of a bundled trace is noted.
    mp = _versioned(tmp_path, "0.1.0", "minitrace-v0.2.0")
    m = json.loads(mp.read_text())
    # The one bundled trace is its own target here; the edge's hash differs.
    mp = _lineage_on_first_trace(mp, [{"target_trace_id": m["traces"][0]["trace_id"],
                                       "target_content_hash": "sha256:" + "1" * 64}])
    problems, notes = check(mp)
    assert not [p for p in problems if "lineage[" in p], problems
    assert any("§6.3" in n and "other than the one bundled" in n for n in notes), notes


def test_an_edge_step_2_fails_gets_no_section_6_3_note(tmp_path):
    # An edge without a type, to a bundled trace whose hash differs, is a
    # step 2 failure only: §6.3 does not also read it as an edge to another
    # state of that trace.
    mp = _lineage_envelope(tmp_path, lambda m: "sha256:" + "1" * 64)
    m = json.loads(mp.read_text())
    a = mp.parent / m["traces"][0]["path"]
    t = json.loads(a.read_text())
    t["lineage"][0].pop("type")
    mp = _lineage_on_first_trace(mp, t["lineage"])
    problems, notes = check(mp)
    assert any("lineage[0].type is absent" in p for p in problems), problems
    assert not any("§6.3" in n for n in notes), notes


def _with_text(mp: pathlib.Path, old: str, new: str) -> pathlib.Path:
    text = mp.read_text()
    assert text.count(old) == 1, old
    mp.write_text(text.replace(old, new))
    return mp


@pytest.mark.parametrize("raw, why", [
    ('"x_probe": 9007199254740992', "/x_probe: number 9007199254740992 lies outside ±(2^53-1)"),
    ('"x_probe": -9007199254740992', "/x_probe: number -9007199254740992 lies outside ±(2^53-1)"),
    ('"x_probe": 1e300', "/x_probe: number 1e300 lies outside ±(2^53-1)"),
    ('"x_probe": [1, {"k": 2.5e16}]', "/x_probe/1/k: number 2.5e16 lies outside ±(2^53-1)"),
    ('"x_probe": "a\\ud800"', "/x_probe: unpaired surrogate U+D800"),
    ('"x_probe": {"\\uffff": 1}', "/x_probe/\uffff: member name: noncharacter U+FFFF"),   # printed as is
    ('"x_\\ufdd0": 1', "/x_\ufdd0: member name: noncharacter U+FDD0"),
])
def test_a_manifest_value_outside_the_3_4_rules(tmp_path, raw, why):
    from verify_envelope import verify  # noqa: PLC0415
    base = _mutate(_envelope(tmp_path), lambda m: m.update(envelope_version="0.1.1"))
    mp = _with_text(base, '"envelope_version": "0.1.1"', '"envelope_version": "0.1.1", ' + raw)
    r = verify(mp)
    assert r.problems == [f"manifest: outside the §3.4 rules: {why}"], r.problems
    assert [c["status"] for c in r.checks if c["step"] == "manifest"] == ["failed"]   # not a §6.2 step (§3.3)
    assert r.verdict().startswith("informational only"), r.verdict()
    # Under 0.1.0 the same value is a note, not a failed check.
    _with_text(mp, '"envelope_version": "0.1.1"', '"envelope_version": "0.1.0"')
    r = verify(mp)
    assert r.problems == [] and f"manifest (0.1.0): outside the §3.4 rules: {why}" in r.notes, (r.problems, r.notes)


def test_manifest_value_paths_are_json_pointers(tmp_path):
    # RFC 6901: "/" and "~" in a member name are escaped, so "a/b" then "c"
    # and "a" then "b/c" are two locations, never one name.
    base = _mutate(_envelope(tmp_path), lambda m: m.update(envelope_version="0.1.1"))
    mp = _with_text(base, '"envelope_version": "0.1.1"', '"envelope_version": "0.1.1", '
                    '"x": {"a/b": {"c": 1e300}, "a": {"b/c": 2e300}, "~": 3e300}')
    problems, _ = check(mp)
    assert sorted(problems) == sorted(f"manifest: outside the §3.4 rules: {p} lies outside ±(2^53-1)" for p in (
        "/x/a~1b/c: number 1e300", "/x/a/b~1c: number 2e300", "/x/~0: number 3e300")), problems


@pytest.mark.parametrize("where", ["top", "witness"])
def test_a_number_with_no_finite_binary64_value_refuses_the_manifest(tmp_path, where):
    # Envelope §3.3: an integer spelling whose nearest binary64 value is
    # infinite has no single reading; the manifest is refused (exit 2), in
    # any revision and inside a witness record too.
    mp = _witness_envelope(tmp_path) if where == "witness" else _envelope(tmp_path)
    big = "1" + "0" * 400
    old = '"envelope_version": "0.1.0"'
    text = mp.read_text()
    if where == "witness":
        text = text.replace('"kind": "process_tree"', '"kind": "process_tree", "metadata": {"n": ' + big + "}", 1)
    else:
        text = text.replace(old, old + ', "x_probe": ' + big, 1)
    assert big in text
    mp.write_text(text)
    code, out = _cli(mp)
    assert code == 2 and "is not finite as binary64" in out, out


def test_manifest_numbers_at_the_bounds_are_in_the_domain(tmp_path):
    mp = _mutate(_envelope(tmp_path), lambda m: m.update(
        envelope_version="0.1.1", x_probe=[9007199254740991, -9007199254740991, 1.5, 9007199254740991.0]))
    problems, notes = check(mp)
    assert problems == [] and not any("§3.4" in n for n in notes), (problems, notes)


def test_a_witness_record_outside_the_3_4_rules_stays_with_step_4(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    # Set after the record hash: RFC 8785 cannot hash it, which is the point.
    mp = _mutate(_witness_envelope(tmp_path), lambda m: m["witnesses"][0].update(metadata={"n": 9007199254740993}))
    r = verify(mp)
    assert any(p.startswith("witnesses[0]") and "outside the §3.4 rules" in p for p in r.problems), r.problems
    assert not any(p.startswith("manifest: outside") for p in r.problems), r.problems


def _cli(*args):
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    script = pathlib.Path(__file__).resolve().parent.parent / "verify_envelope.py"
    env = {**os.environ, "COLUMNS": "80", "NO_COLOR": "1", "PYTHON_COLORS": "0", "FORCE_COLOR": ""}
    r = subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True,
                       timeout=60, env=env)
    return r.returncode, r.stdout + r.stderr


def test_a_manifest_that_is_not_a_regular_file_exits_2(tmp_path):
    import os  # noqa: PLC0415
    os.mkfifo(tmp_path / "manifest.json")   # a read would block
    code, out = _cli(tmp_path / "manifest.json")
    assert code == 2 and "manifest is not a regular file" in out, out
    (tmp_path / "d").mkdir()   # a directory: refused by name, its descriptor closed
    code, out = _cli(tmp_path / "d")
    assert code == 2 and "manifest is not a regular file" in out, out


def test_reading_a_refused_manifest_closes_its_descriptor(tmp_path):
    import os  # noqa: PLC0415
    from verify_envelope import EnvelopeRefused, _read_manifest  # noqa: PLC0415
    (tmp_path / "d").mkdir()
    os.mkfifo(tmp_path / "f")
    (tmp_path / "big.json").write_text("{}" + " " * 100)

    def open_fds():
        return sorted(os.listdir("/dev/fd"))
    before = open_fds()
    for p, exc in ((tmp_path / "d", ValueError), (tmp_path / "f", ValueError),
                   (tmp_path / "big.json", EnvelopeRefused)):
        for _ in range(3):
            try:
                _read_manifest(p, 10)
                raise AssertionError(f"{p.name} was read")
            except exc:
                pass
    assert open_fds() == before


@pytest.mark.parametrize("old, new", [
    ('"trace_id": "t-a"', '"trace_id": "t-a\\ud800"'),
    ('"path": "traces/t-a.minitrace.json"', '"path": "traces/t-a\\udfff.minitrace.json"'),
])
def test_a_lone_surrogate_in_the_manifest_is_reported_escaped(tmp_path, old, new):
    mp = _envelope(tmp_path)
    _with_text(mp, old, new)
    code, out = _cli(mp)
    assert code == 1 and "Traceback" not in out and ("\\ud800" in out or "\\udfff" in out), out
    assert "FAIL:" in out, out


# Consumer duties and the verdict (ruled 2026-10-21): manipulated clocks on
# the verdict line, dangling clock references noted, x-tls-keylog-trace
# records amber or red, capture_status noted, signature.json read, a trace
# schema check, and exit 3 for informational only.

def test_a_manipulated_clock_is_on_the_verdict_line_and_keeps_its_class(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _mutate(_envelope(tmp_path), lambda m: m.update(clocks=[
        {"id": "c-fast", "kind": "manipulated", "manipulated": True,
         "manipulation_notes": "Y2038 fast-forward via libfaketime"}]))
    r = verify(mp)
    assert r.problems == [] and r.verdict().startswith("integrity only"), r.verdict()
    assert "manipulated clock(s)" in r.verdict() and "c-fast: Y2038 fast-forward via libfaketime" in r.verdict()
    assert any(n.startswith("clocks: c-fast: Y2038") for n in r.notes), r.notes
    # Not manipulated: nothing on the verdict line.
    mp = _mutate(mp, lambda m: m["clocks"][0].update(kind="host_system", manipulated=False))
    assert "manipulated" not in verify(mp).verdict()


def test_clock_references_that_name_no_declared_clock_are_noted(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415

    def setup(m):
        m["clocks"] = [{"id": "c1", "kind": "host_system", "manipulated": False},
                       {"id": "c2", "kind": "simulated", "manipulated": False}]
        m["producer"]["host_clock_id"] = "c9"
        m["traces"][0]["clock_id"] = "c7"
    r = verify(_mutate(_envelope(tmp_path), setup))
    assert r.problems == [], r.problems
    assert "traces[0]: clock_id c7 names no declared clock (§8.5)" in r.notes, r.notes
    assert any(n.startswith("traces[1]: no clock_id while 2 clocks are declared") for n in r.notes), r.notes
    assert any(n.startswith("producer.host_clock_id c9 names no declared clock") for n in r.notes), r.notes
    assert r.verdict().startswith("integrity only"), r.verdict()


def _keylog(tmp_path, lines: bytes, **meta):
    return _witness_envelope(tmp_path, payload=lines, fn=lambda w, root: w.update(
        kind="x-tls-keylog-trace", metadata={"tool_call_id_attribution": "never", **meta}))


def _rec(**kw) -> bytes:
    r = {"timestamp": "2026-10-20T00:00:00.000000Z", "pid": 1, "comm": "c", "direction": "send",
         "payload_hash": "sha256:" + "0" * 64, "tool_call_id": None}
    r.update(kw)
    return (json.dumps({k: v for k, v in r.items() if v is not _ABSENT}) + "\n").encode()


_ABSENT = object()


def test_keylog_records_must_be_amber_or_red(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    ok = verify(_keylog(tmp_path / "ok", _rec(redaction_class="amber") + b"\n" + _rec(redaction_class="red")))
    assert ok.problems == [] and _statuses(ok, "x-tls-keylog-trace") == ["passed"], ok.checks
    assert _statuses(ok, "witness") == ["passed"]
    bad = verify(_keylog(tmp_path / "bad", _rec(redaction_class="amber") + _rec(redaction_class="green")
                         + _rec(redaction_class=_ABSENT) + b"[1]\n" + b"{not json\n" + _rec(redaction_class=5)))
    assert bad.problems == [
        "witnesses[0]: x-tls-keylog-trace record rejected (§7.6.7): witnesses/w1/procs.json line 2: "
        "redaction_class green, not amber or red",
        "witnesses[0]: x-tls-keylog-trace record rejected (§7.6.7): witnesses/w1/procs.json line 3: "
        "redaction_class missing",
        "witnesses[0]: x-tls-keylog-trace record rejected (§7.6.7): witnesses/w1/procs.json line 4: "
        "not a JSON object (a JSON array)",
        bad.problems[3],
        "witnesses[0]: x-tls-keylog-trace record rejected (§7.6.7): witnesses/w1/procs.json line 6: "
        "redaction_class 5, not amber or red"], bad.problems
    assert bad.problems[3].startswith("witnesses[0]: x-tls-keylog-trace record rejected (§7.6.7): "
                                      "witnesses/w1/procs.json line 5: not a JSON object (")
    assert _statuses(bad, "witness") == ["failed"] and bad.exit_status() == 1


def test_keylog_rejections_are_bounded_and_a_removed_payload_is_not_read(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_keylog(tmp_path / "many", _rec(redaction_class="green") * 25))
    assert len(r.problems) == 11 and r.problems[-1].endswith("and 15 more rejected record(s)"), r.problems

    def removed(w, root):
        for k in ("primary_path", "size_bytes", "auxiliary_paths"):
            w.pop(k, None)
        w["path_hashes"] = {}
        w.update(kind="x-tls-keylog-trace", metadata={"tool_call_id_attribution": "never"},
                 redaction=dict(_REMOVE))
    r = verify(_witness_envelope(tmp_path / "rm", fn=removed))
    assert r.problems == [] and _statuses(r, "x-tls-keylog-trace") == ["removed"], (r.problems, r.checks)


@pytest.mark.parametrize("status, noted", [("attach_failed", ""), ("unsupported_platform", ""),
                                           (5, " (outside the v0.1.x set)"), ("later", " (outside the v0.1.x set)"),
                                           ("captured", None)])
def test_an_x_capture_status_other_than_captured_is_noted(tmp_path, status, noted):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_keylog(tmp_path, _rec(redaction_class="red"), capture_status=status))
    hits = [n for n in r.notes if "capture_status" in n]
    if noted is None:
        assert hits == [], hits
    else:
        assert hits and hits[0].startswith(f"witnesses[0]: x-tls-keylog-trace capture_status {status}{noted}"), hits
    assert r.problems == [], r.problems


def test_signature_json_is_read(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _envelope(tmp_path)
    sig = mp.parent / "signature.json"
    sig.write_text(json.dumps({"signatures": []}))
    r = verify(mp)
    assert r.problems == [] and not r.signed and r.verdict().startswith("integrity only"), r.verdict()
    assert any(n.startswith("unsigned: signature.json lists no signature") for n in r.notes), r.notes
    for content, why in (("[]", "not a JSON object"), ('{"signatures": {}}', "signatures is not a list"),
                         ("{", "unreadable"), ('{"signatures": [], "signatures": []}', "unreadable")):
        sig.write_text(content)
        r = verify(mp)
        assert [c["status"] for c in r.checks if c["step"] == "5"] == ["failed"], (content, r.checks)
        assert r.problems and why in r.problems[-1] and r.exit_status() == 1, (content, r.problems)


def test_exit_status_names_the_verdict_class(tmp_path):
    mp = _envelope(tmp_path)
    assert _cli(mp)[0] == 0                                   # integrity only
    _sign(mp)
    code, out = _cli(mp)
    assert code == 3 and "informational only" in out, out    # no failed check, step 5 unsupported
    (mp.parent / "signature.json").write_text("[]")
    assert _cli(mp)[0] == 1                                   # a failed check


def test_bundled_traces_are_held_to_the_trace_schema(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415

    def garbage(m):
        a = tmp_path / "env" / m["traces"][0]["path"]
        t = {"id": "t-a", "schema_version": "0.3.0", "junk": [1, 2, 3]}
        t["content_hash"] = compute_content_hash(t)
        a.write_text(json.dumps(t))
        m["traces"][0].update(trace_content_hash=t["content_hash"],
                              file_content_hash="sha256:" + hashlib.sha256(a.read_bytes()).hexdigest())
    r = verify(_mutate(_envelope(tmp_path), garbage))
    assert r.problems and all(p.startswith("traces[0]: trace schema: ") for p in r.problems), r.problems
    assert "traces[0]: trace schema: BASE: 'operator' is a required property at []" in r.problems, r.problems
    assert [c["status"] for c in r.checks if c["step"] == "2"] == ["passed", "passed"]   # integrity holds
    assert [c["status"] for c in r.checks if c["step"] == "trace schema"] == ["failed"] * len(r.problems) + ["passed"]
    assert r.verdict().startswith("informational only") and r.exit_status() == 1


def test_a_v0_2_trace_under_0_1_0_skips_the_trace_schema(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_versioned(tmp_path, "0.1.0", "minitrace-v0.2.0", content_hash=_NO_HASH))
    assert [c["status"] for c in r.checks if c["step"] == "trace schema"] == ["skipped"], r.checks


def test_a_missing_trace_validator_fails_the_check(tmp_path, monkeypatch):
    import verify_envelope  # noqa: PLC0415
    monkeypatch.setattr(verify_envelope, "_TRACE_VALIDATOR", [ImportError("no jsonschema")])
    r = verify_envelope.verify(_envelope(tmp_path))
    assert r.problems == [f"traces[{i}]: trace schema: validator unavailable (ImportError: no jsonschema)"
                          for i in (0, 1)], r.problems


# Keylog, trace schema and clock checks (ruled 2026-10-21 where behaviour was open).

def test_a_keylog_line_over_the_cap_is_rejected_unread_and_the_next_line_still_read(tmp_path):
    import verify_envelope  # noqa: PLC0415
    long_line = b'{"redaction_class": "amber", "pad": "' + b"x" * (verify_envelope._KEYLOG_LINE_BYTES + 10) + b'"}\n'
    r = verify_envelope.verify(_keylog(tmp_path, long_line + _rec(redaction_class="green")))
    assert r.problems == [
        f"witnesses[0]: x-tls-keylog-trace record rejected (§7.6.7): witnesses/w1/procs.json line 1: "
        f"longer than {verify_envelope._KEYLOG_LINE_BYTES} bytes, not read",
        "witnesses[0]: x-tls-keylog-trace record rejected (§7.6.7): witnesses/w1/procs.json line 2: "
        "redaction_class green, not amber or red"], r.problems


def test_keylog_lines_are_read_from_a_regular_file_only(tmp_path):
    import os  # noqa: PLC0415
    import verify_envelope  # noqa: PLC0415
    (tmp_path / "real.jsonl").write_bytes(b"{}\n")
    os.symlink(tmp_path / "real.jsonl", tmp_path / "link.jsonl")
    os.mkfifo(tmp_path / "fifo.jsonl")
    for name, exc in (("link.jsonl", OSError), ("fifo.jsonl", ValueError)):
        raised = False
        try:
            list(verify_envelope._bounded_lines(tmp_path / name, 100))
        except exc:
            raised = True
        assert raised, name
    assert list(verify_envelope._bounded_lines(tmp_path / "real.jsonl", 100)) == [(1, b"{}\n")]


def test_an_empty_keylog_payload_passes_with_no_records(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_keylog(tmp_path, b""))
    assert r.problems == [] and [c["detail"] for c in r.checks if c["step"] == "x-tls-keylog-trace"] == ["no records"]


def test_a_keylog_path_listed_twice_is_read_once(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _witness_envelope(tmp_path, payload=_rec(redaction_class="green"), fn=lambda w, root: w.update(
        kind="x-tls-keylog-trace", metadata={"tool_call_id_attribution": "never"},
        auxiliary_paths=["witnesses/w1/procs.json"]))
    r = verify(mp)
    rejected = [p for p in r.problems if "x-tls-keylog-trace record rejected" in p]
    assert len(rejected) == 1, r.problems
    assert any("already listed" in p for p in r.problems), r.problems   # step 3 reports the duplicate


def test_the_trace_schema_skip_says_why(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_versioned(tmp_path, "0.1.1", "9.9"))
    assert [c["detail"] for c in r.checks if c["step"] == "trace schema"] == \
        ["schema_version 9.9 is not v0.3.x: no schema applied"], r.checks


def test_an_unknown_trace_type_with_a_schema_uri_passes_on_the_base_schema_with_a_note(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415

    def custom(m):
        a = tmp_path / "env" / m["traces"][0]["path"]
        t = json.loads(a.read_text())
        t.update(trace_type="x-probe-session", schema_uri="https://example.invalid/s.json")
        t["content_hash"] = compute_content_hash(t)
        a.write_text(json.dumps(t))
        m["traces"][0].update(trace_content_hash=t["content_hash"],
                              file_content_hash="sha256:" + hashlib.sha256(a.read_bytes()).hexdigest())
    r = verify(_mutate(_envelope(tmp_path), custom))
    assert r.problems == [], r.problems
    assert [c["detail"] for c in r.checks if c["step"] == "trace schema"] == ["base schema only", ""], r.checks
    assert any(n.startswith("traces[0]: trace schema: unknown trace_type 'x-probe-session'") for n in r.notes), r.notes


def test_trace_schema_errors_stop_at_the_cap(tmp_path):
    import importlib.util  # noqa: PLC0415
    from verify_envelope import MAX_SCHEMA_ERRORS, verify  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location(
        "validate_1015", pathlib.Path(__file__).resolve().parents[2] / "schemas" / "validate.py")
    V = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(V)
    many = {"id": "t", "schema_version": "0.3.0", "trace_type": "agent-session",
            "operator": {"type": "scripted"}, "turns": [{"index": i} for i in range(80)]}
    assert len(V.validate_doc(many)[0]) == 80
    assert len(V.validate_doc(many, max_errors=3)[0]) == 3

    def bad(m):
        a = tmp_path / "env" / m["traces"][0]["path"]
        t = dict(many, id="t-a")
        t["content_hash"] = compute_content_hash(t)
        a.write_text(json.dumps(t))
        m["traces"][0].update(trace_content_hash=t["content_hash"],
                              file_content_hash="sha256:" + hashlib.sha256(a.read_bytes()).hexdigest())
    r = verify(_mutate(_envelope(tmp_path), bad))
    schema = [p for p in r.problems if p.startswith("traces[0]: trace schema:")]
    assert len(schema) == MAX_SCHEMA_ERRORS + 1, len(schema)
    assert schema[-1] == f"traces[0]: trace schema: and more (stopped after {MAX_SCHEMA_ERRORS})"


def test_an_integer_beyond_the_reader_is_refused_with_its_size(tmp_path):
    mp = _envelope(tmp_path)
    text = mp.read_text()
    mp.write_text(text.replace('"envelope_version": "0.1.0"', '"envelope_version": "0.1.0", "x_probe": ' + "9" * 5000, 1))
    code, out = _cli(mp)
    assert code == 2 and "an integer of 5000 digits is beyond this reader" in out, out


def test_a_clock_id_declared_twice_is_noted(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    r = verify(_mutate(_envelope(tmp_path), lambda m: m.update(clocks=[
        {"id": "c1", "kind": "host_system", "manipulated": False},
        {"id": "c1", "kind": "simulated", "manipulated": False}])))
    assert "clocks: id c1 declared 2 times; a binding to it names no one clock (§8.5)" in r.notes, r.notes
    assert r.problems == [], r.problems


def test_the_clock_checks_are_linear_in_the_number_of_clocks():
    # Counting each id with a scan was quadratic (about
    # 3 minutes for 100,000 clocks, within MAX_ITEMS); one Counter pass is
    # milliseconds. 20,000 clocks: linear well under a second, quadratic
    # several seconds.
    import time  # noqa: PLC0415
    import verify_envelope  # noqa: PLC0415
    m = {"clocks": [{"id": f"c{i}", "kind": "host_system", "manipulated": False} for i in range(20000)]
         + [{"id": "c7", "kind": "simulated", "manipulated": False}], "traces": [], "producer": {}}
    rep = verify_envelope.Report()
    t0 = time.monotonic()
    verify_envelope._clock_checks(rep, m)
    assert time.monotonic() - t0 < 2.0
    assert rep.notes == ["clocks: id c7 declared 2 times; a binding to it names no one clock (§8.5)"], rep.notes


def test_validate_doc_refuses_a_max_errors_below_one():
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location(
        "validate_1015b", pathlib.Path(__file__).resolve().parents[2] / "schemas" / "validate.py")
    V = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(V)
    for bad in (0, -1):
        raised = False
        try:
            V.validate_doc({}, max_errors=bad)
        except ValueError:
            raised = True
        assert raised, bad
    assert len(V.validate_doc({}, max_errors=1)[0]) == 1


# Signature entries and the report (ruled 2026-10-26): signature.json is held
# to the schema's SignatureFile shape; an entry whose covers includes manifest
# has its manifest_hash compared with the SHA-256 of the manifest bytes as
# read; a malformed entry or a differing hash fails step 5. The report names
# the manifest's SHA-256, the verdict line ends with the note count, and the
# per-witness summaries stand apart from the check counts, so the failed
# checks, the verdict's failed count and the problems agree. A member path
# that cannot be examined is a failed check, not an unreadable envelope.

def _step5(r) -> list[str]:
    return [c["status"] for c in r.checks if c["step"] == "5"]


def test_a_matching_manifest_hash_leaves_step_5_unsupported(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _envelope(tmp_path)
    _sigs(mp, {**_SIG, "manifest_hash": _manifest_sha(mp), "x-extra": 1})
    r = verify(mp)
    assert r.problems == [] and _step5(r) == ["unsupported"] and r.exit_status() == 3, (r.problems, r.checks)
    assert any("signatures[0] (s1): manifest_hash matches the manifest" in n for n in r.notes), r.notes
    assert "signature present but not verified" in r.verdict(), r.verdict()


def test_a_differing_manifest_hash_fails_step_5(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _envelope(tmp_path)
    before = _manifest_sha(mp)
    _sigs(mp, {**_SIG, "manifest_hash": "sha256:" + "0" * 64})
    r = verify(mp)
    assert _step5(r) == ["failed"] and r.exit_status() == 1, r.checks
    assert r.problems == ["signature.json: signatures[0] (s1): manifest_hash does not match the manifest's "
                          "SHA-256 (§9.1, §9.3)"], r.problems
    # The literal bytes, not the parsed manifest: the same JSON re-spaced differs.
    mp.write_text(json.dumps(json.loads(mp.read_text()), indent=1))
    _sigs(mp, {**_SIG, "manifest_hash": before})
    assert _step5(verify(mp)) == ["failed"]
    # Every entry is compared, not only the first.
    _sigs(mp, {**_SIG, "manifest_hash": _manifest_sha(mp)}, {**_SIG, "id": "s2", "manifest_hash": before})
    r = verify(mp)
    assert _step5(r) == ["unsupported", "failed"] and len(r.problems) == 1 and "(s2)" in r.problems[0], \
        (r.checks, r.problems)


def test_an_entry_whose_covers_lacks_manifest_is_noted_not_compared(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _envelope(tmp_path)
    _sigs(mp, {**_SIG, "covers": []})
    r = verify(mp)
    assert r.problems == [] and _step5(r) == ["unsupported"] and r.exit_status() == 3, (r.problems, r.checks)
    assert any("signatures[0] (s1): covers does not include manifest; manifest_hash not compared" in n
               for n in r.notes), r.notes


@pytest.mark.parametrize("edit, where", [
    (lambda s: s.pop("key_id"), "signatures/0: 'key_id' is a required property"),
    (lambda s: s.update(manifest_hash="sha256:XYZ"), "signatures/0/manifest_hash: "),
    (lambda s: s.update(covers=["clock_attestation"]), "signatures/0/covers/0: "),
    (lambda s: s.update(signed_at="yesterday"), "signatures/0/signed_at: "),
    (lambda s: s.update(id=""), "signatures/0/id: "),
    (lambda s: s.update(signature=1), "signatures/0/signature: "),
])
def test_a_malformed_signature_entry_fails_step_5(tmp_path, edit, where):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _envelope(tmp_path)
    s = {**_SIG, "manifest_hash": _manifest_sha(mp)}
    edit(s)
    _sigs(mp, s)
    r = verify(mp)
    assert _step5(r) == ["failed"] and r.exit_status() == 1, r.checks
    assert any(p.startswith("signature.json: " + where) for p in r.problems), r.problems


def test_a_signature_entry_that_is_not_an_object_fails_step_5(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _envelope(tmp_path)
    _sigs(mp, "s1", {**_SIG, "id": "s2", "manifest_hash": _manifest_sha(mp)})
    r = verify(mp)
    assert _step5(r) == ["failed", "unsupported"] and len(r.problems) == 1, (r.checks, r.problems)
    assert r.problems[0].startswith("signature.json: signatures/0: 's1' is not of type 'object'"), r.problems


def test_no_signature_validator_fails_the_file_and_reads_no_entry(tmp_path, monkeypatch):
    import verify_envelope  # noqa: PLC0415
    mp = _envelope(tmp_path)
    monkeypatch.setattr(verify_envelope, "_signature_schema", lambda version: tmp_path / "missing.schema.json")
    _sigs(mp, {"id": "s1"})   # malformed: would be read unchecked if the file did not stop here
    r = verify_envelope.verify(mp)
    assert _step5(r) == ["failed"] and r.exit_status() == 1, r.checks
    assert r.problems == [r.problems[0]] and r.problems[0].startswith(
        "signature.json: (file): validator unavailable (FileNotFoundError: "), r.problems


def test_at_the_error_cap_the_located_entry_errors_are_shown_and_no_entry_is_read(tmp_path):
    import verify_envelope  # noqa: PLC0415
    mp = _envelope(tmp_path)
    _sigs(mp, {**_SIG, "manifest_hash": _manifest_sha(mp)}, *({"id": str(i)} for i in range(59)))
    r = verify_envelope.verify(mp)
    cap = verify_envelope.MAX_SCHEMA_ERRORS
    assert len(r.problems) == cap + 1 and _step5(r) == ["failed"] * (cap + 1), (len(r.problems), _step5(r))
    assert r.problems[-1] == f"signature.json: (file): and more (stopped after {cap})", r.problems[-1]
    assert all(p.startswith("signature.json: signatures/") for p in r.problems[:-1]), r.problems[:3]
    assert not any("manifest_hash matches" in n for n in r.notes), r.notes


def test_every_report_names_the_manifest_sha256(tmp_path, capsys):
    mp = _envelope(tmp_path)
    sha = _manifest_sha(mp)
    code, out = _cli(mp)
    assert code == 0 and f"  manifest: {sha}" in out.splitlines(), out
    assert main(["verify_envelope.py", "--json", str(mp)]) == 0
    assert json.loads(capsys.readouterr().out)["manifest_sha256"] == sha
    _sigs(mp, "s1")
    code, out = _cli(mp)
    assert code == 1 and f"  manifest: {sha}" in out.splitlines(), out


def test_failed_checks_verdict_and_problems_agree_and_witness_summaries_stand_apart(tmp_path, capsys):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _witness_envelope(tmp_path)
    (mp.parent / "witnesses" / "w1" / "procs.json").write_bytes(b'{"pid": 2}\n')
    r = verify(mp)
    assert r.counts()["failed"] == len(r.problems) == 1, (r.counts(), r.problems)
    assert r.witness_counts() == {"passed": 0, "failed": 1, "skipped": 0, "unsupported": 0, "removed": 0}
    assert sum(r.counts().values()) + 1 == len(r.checks), r.checks   # the summary is still a check record
    code, out = _cli(mp)
    lines = out.splitlines()
    assert code == 1 and lines[-1] == "FAIL: 1 problem(s)", lines
    assert any(ln.startswith("  checks: ") and "failed 1," in ln for ln in lines), lines
    assert "  witness summaries: passed 0, failed 1, skipped 0, unsupported 0, removed 0" in lines, lines
    assert any(ln.startswith("  verdict: informational only, not evidence (§6.2): 1 failed;") for ln in lines), lines
    assert main(["verify_envelope.py", "--json", str(mp)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["failed"] == 1 and out["witness_summaries"]["failed"] == 1, out
    # No witness, no witness-summary line.
    code, out = _cli(_envelope(tmp_path / "plain"))
    assert code == 0 and "witness summaries" not in out, out


def test_the_verdict_line_ends_with_the_note_count(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _envelope(tmp_path)
    r = verify(mp)
    assert len(r.notes) == 1 and r.verdict().endswith("; 1 note(s)"), (r.notes, r.verdict())
    (mp.parent / "stray.txt").write_text("x")   # unlisted, a second note
    r = verify(mp)
    assert len(r.notes) == 2 and r.verdict().endswith("; 2 note(s)"), (r.notes, r.verdict())
    assert r.verdict().startswith("integrity only"), r.verdict()


def test_a_member_path_that_cannot_be_examined_is_a_failed_check(tmp_path, monkeypatch, capsys):
    # Python before 3.13 raises ENAMETOOLONG from Path.is_file and
    # Path.is_symlink for a name over the file system's limit; later
    # versions return False. Simulated here so the test holds on either.
    import errno  # noqa: PLC0415
    from verify_envelope import verify  # noqa: PLC0415
    for method in ("is_file", "is_symlink"):
        real = getattr(pathlib.Path, method)

        def too_long(self, *a, _real=real, **k):
            if any(len(part) > 255 for part in self.parts):
                raise OSError(errno.ENAMETOOLONG, "File name too long", str(self))
            return _real(self, *a, **k)
        monkeypatch.setattr(pathlib.Path, method, too_long)
    mp = _mutate(_envelope(tmp_path), lambda m: m["traces"][0].update(path="traces/" + "a" * 300))
    r = verify(mp)
    assert any(p.startswith("traces[0]: path cannot be examined: ") for p in r.problems), r.problems
    assert r.exit_status() == 1
    assert main(["verify_envelope.py", str(mp)]) == 1
    capsys.readouterr()


def _deny_open(monkeypatch, name: str) -> None:
    """Path.open refused for one file name, as a permission would (root-safe)."""
    import errno  # noqa: PLC0415
    real = pathlib.Path.open

    def denied(self, *a, **k):
        if self.name == name:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "open", denied)


@pytest.mark.parametrize("which, name, where, step", [
    ("trace", "t-a.minitrace.json", "traces[0]", "1"),
    ("binding", "labels.csv", "bindings[0]", "3"),
    ("witness", "procs.json", "witnesses[0]", "3"),
])
def test_a_listed_file_that_cannot_be_read_is_a_failed_check(tmp_path, monkeypatch, capsys, which, name, where, step):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _witness_envelope(tmp_path)
    _deny_open(monkeypatch, name)
    r = verify(mp)
    mine = [p for p in r.problems if p.startswith(f"{where}:")]
    assert len(mine) == 1 and mine[0].startswith(f"{where}: cannot be read: Permission denied: "), r.problems
    assert any(c["step"] == step and c["subject"] == where and c["status"] == "failed" for c in r.checks), r.checks
    assert not any(c["subject"] == where and c["status"] == "passed" and c["step"] == step for c in r.checks)
    if which == "trace":   # no file read, so step 2 is not performed
        assert [c["status"] for c in r.checks if c["step"] == "2" and c["subject"] == where] == ["skipped"]
    assert main(["verify_envelope.py", str(mp)]) == 1
    capsys.readouterr()


def test_a_trace_that_cannot_be_read_after_hashing_fails_step_2(tmp_path, monkeypatch):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _envelope(tmp_path)
    real = pathlib.Path.read_bytes

    def denied(self):
        if self.name == "t-a.minitrace.json":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)
    monkeypatch.setattr(pathlib.Path, "read_bytes", denied)
    r = verify(mp)
    assert r.problems == ["traces[0]: cannot be read: Permission denied"], r.problems
    assert r.exit_status() == 1


def test_a_path_the_unlisted_sweep_cannot_examine_is_noted(tmp_path, monkeypatch, capsys):
    import os  # noqa: PLC0415
    from verify_envelope import verify  # noqa: PLC0415
    mp = _envelope(tmp_path)
    root = mp.parent
    (root / "locked").mkdir()
    (root / "locked" / "f").write_text("x")
    (root / "odd").write_text("x")
    real_scandir, real_lstat = os.scandir, os.lstat

    def scandir(p=".", *a, **k):
        if os.fspath(p).endswith("locked"):
            raise PermissionError(13, "Permission denied", os.fspath(p))
        return real_scandir(p, *a, **k)

    def lstat(p, *a, **k):
        if os.fspath(p).endswith("odd"):
            raise PermissionError(13, "Permission denied", os.fspath(p))
        return real_lstat(p, *a, **k)
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "lstat", lstat)
    r = verify(mp)
    assert r.problems == [] and r.verdict().startswith("integrity only"), (r.problems, r.verdict())
    assert "2 path(s) in the envelope directory could not be examined: locked, odd; unlisted files there " \
           "are not accounted for" in r.notes, r.notes
    assert main(["verify_envelope.py", str(mp)]) == 0
    capsys.readouterr()


def test_an_unlisted_symlink_is_noted(tmp_path):
    from verify_envelope import verify  # noqa: PLC0415
    mp = _envelope(tmp_path)
    (mp.parent / "dangling").symlink_to(tmp_path / "nowhere")
    (mp.parent / "dirlink").symlink_to(mp.parent / "traces", target_is_directory=True)
    r = verify(mp)
    assert any(n.startswith("2 file(s) in the envelope directory are not listed") and "dangling" in n
               and "dirlink" in n for n in r.notes), r.notes


def test_a_path_listed_again_as_a_case_or_normalisation_alias_is_listed_twice(tmp_path):
    # Ruled 2026-10-29: on a case-insensitive or normalising file system
    # (APFS, NTFS) a spelling that differs only in letter case or Unicode
    # normalisation names the same file, so it is the same path listed
    # twice; two hard links with different names are two paths. On a
    # case-sensitive or non-normalising file system (ext4, most Linux) the
    # other spelling names no file: it is reported missing, never a duplicate.
    import os  # noqa: PLC0415
    import unicodedata  # noqa: PLC0415
    from verify_envelope import verify  # noqa: PLC0415
    probe = _envelope(tmp_path / "probe").parent
    case_alias = (probe / "traces" / "T-A.MINITRACE.JSON").exists()
    r = verify(_mutate(_envelope(tmp_path / "case"),
                       lambda m: m["traces"].append({**m["traces"][0], "path": "traces/T-A.minitrace.json"})))
    if case_alias:
        assert any(p.startswith("traces[2]: path") and "listed twice" in p for p in r.problems), r.problems
    else:
        assert "traces[2]: no such regular file: traces/T-A.minitrace.json" in r.problems, r.problems
        assert not any("listed twice" in p for p in r.problems), r.problems
    # Unicode normalisation: a binding stored under its NFC name, listed
    # again under the NFD spelling.
    mp = _envelope(tmp_path / "nfd")
    nfc, nfd = "bindings/café.csv", unicodedata.normalize("NFD", "bindings/café.csv")
    (mp.parent / nfc).write_text("id,label\nt-a,y\n")
    nfd_alias = os.path.exists(mp.parent / nfd)
    h = "sha256:" + hashlib.sha256((mp.parent / nfc).read_bytes()).hexdigest()
    r = verify(_mutate(mp, lambda m: m["bindings"].extend(
        [{"kind": "x-labels", "path": p, "content_hash": h, "produces_trace_id": "t-a"} for p in (nfc, nfd)])))
    if nfd_alias:
        assert any(p.startswith("bindings[2]: path") and "already listed" in p for p in r.problems), r.problems
    else:
        assert f"bindings[2]: no such regular file: {nfd}" in r.problems, r.problems
        assert not any("already listed" in p for p in r.problems), r.problems
    assert not any(p.startswith("bindings[1]: path") for p in r.problems), r.problems
    # A hard link under another name is another path, not a duplicate.
    mp = _envelope(tmp_path / "hardlink")
    os.link(mp.parent / "bindings" / "labels.csv", mp.parent / "bindings" / "labels-link.csv")
    r = verify(_mutate(mp, lambda m: m["bindings"].append({**m["bindings"][0], "path": "bindings/labels-link.csv"})))
    assert not any("already listed" in p for p in r.problems), r.problems


def test_a_listing_trace_type_mismatch_is_noted_and_base_only_is_not_passed(tmp_path):
    # Ruled 2026-10-29: a listing whose trace_type differs from the trace's
    # is noted (the trace decides, no failed check); a trace of an unknown
    # type validated against the base schema only records the trace schema
    # check as unsupported, not passed, the verdict unchanged.
    from verify_envelope import verify  # noqa: PLC0415
    base = verify(_envelope(tmp_path / "base"))
    r = verify(_mutate(_envelope(tmp_path / "mismatch"),
                       lambda m: m["traces"][0].update(trace_type="judge-session")))
    assert r.problems == [], r.problems
    assert "traces[0]: the listing's trace_type judge-session differs from the trace's agent-session" \
        in r.notes, r.notes
    assert not any("listing's trace_type" in n for n in base.notes), base.notes
    same = verify(_mutate(_envelope(tmp_path / "same"), lambda m: m["traces"][0].update(trace_type="agent-session")))
    assert not any("listing's trace_type" in n for n in same.notes), same.notes

    def custom(m):
        a = tmp_path / "custom" / "env" / m["traces"][0]["path"]
        t = json.loads(a.read_text())
        t.update(trace_type="x-probe-session", schema_uri="https://example.invalid/s.json")
        t["content_hash"] = compute_content_hash(t)
        a.write_text(json.dumps(t))
        m["traces"][0].update(trace_content_hash=t["content_hash"],
                              file_content_hash="sha256:" + hashlib.sha256(a.read_bytes()).hexdigest())
    r = verify(_mutate(_envelope(tmp_path / "custom"), custom))
    statuses = [(c["subject"], c["status"]) for c in r.checks if c["step"] == "trace schema"]
    assert statuses == [("traces[0]", "unsupported"), ("traces[1]", "passed")], statuses
    assert r.problems == [] and r.verdict().split(";")[0] == base.verdict().split(";")[0], (r.verdict(), base.verdict())


def test_a_listed_path_is_keyed_without_following_a_symlink(tmp_path, monkeypatch):
    # The alias key comes from lstat, so a listed
    # symlink is not followed (outside the envelope, say) before
    # resolve_member refuses it; a letter-case alias of the link's own name
    # is still the same directory entry.
    import os  # noqa: PLC0415
    import verify_envelope  # noqa: PLC0415
    (tmp_path / "target").write_text("x\n")
    os.symlink(tmp_path / "target", tmp_path / "L")

    def not_followed(*args, **kwargs):
        raise AssertionError("a listed path was followed through its symlink")
    seen: set = set()
    with monkeypatch.context() as m:
        m.setattr(verify_envelope.os, "stat", not_followed)
        assert verify_envelope._listed_before(seen, tmp_path, "L") is False
        again = verify_envelope._listed_before(seen, tmp_path, "l")
    assert again == os.path.lexists(tmp_path / "l"), again   # an alias only where the file system folds case


def test_a_trace_that_states_no_trace_type_is_not_compared_with_its_listing(tmp_path):
    # A v0.2.x trace has no trace_type, and a listing
    # stating one (the migration default) is not a mismatch to note.
    from verify_envelope import verify  # noqa: PLC0415

    def no_type(m):
        a = tmp_path / "env" / m["traces"][0]["path"]
        t = json.loads(a.read_text())
        del t["trace_type"]
        t["content_hash"] = compute_content_hash(t)
        a.write_text(json.dumps(t))
        m["traces"][0].update(trace_type="agent-session", trace_content_hash=t["content_hash"],
                              file_content_hash="sha256:" + hashlib.sha256(a.read_bytes()).hexdigest())
    r = verify(_mutate(_envelope(tmp_path), no_type))
    assert not any("listing's trace_type" in n for n in r.notes), r.notes


def test_field_names_a_value_too_deep_to_print():
    # str() of a value nested past the interpreter's reach raises
    # RecursionError; field() names its type instead.
    from input_tree import field  # noqa: PLC0415
    deep: object = 1
    for _ in range(200_000):
        deep = {"a": deep}
    assert field(deep) == "<dict nested too deep to print>"
    assert field({"a": 1}) == "{'a': 1}"   # a shallow value prints as before


@pytest.mark.parametrize("version", ["0.1.0", "0.1.1"])
def test_a_capture_status_nested_too_deep_is_noted_not_a_traceback(tmp_path, version):
    # The nesting is written as text: json.dumps cannot build it, and a
    # record hash cannot be taken over it.
    mp = _witness_envelope(tmp_path, version=version, fn=lambda w, root: w.update(
        kind="x-foo", metadata={"capture_status": "PLACEHOLDER"}))
    n = 100_000
    mp.write_text(mp.read_text().replace('"PLACEHOLDER"', '{"a":' * n + "1" + "}" * n))
    code, out = _cli(mp)
    assert "Traceback" not in out, out[-2000:]
    if code == 2:
        # The parser refused the manifest before any witness was read (3.13
        # stops near 10,000 levels): a clean refusal is the outcome there.
        assert "manifest is nested too deeply to read" in out, out[-2000:]
        return
    assert code in (1, 3), (code, out[-2000:])
    assert "capture_status <dict nested too deep to print>" in out, out[-2000:]


def test_a_recursion_error_in_the_manifest_value_check_is_a_refusal(tmp_path, monkeypatch):
    # The re-parse in _manifest_value_problems calls Python hooks the loader
    # does not, so a manifest nested just short of the loader's refusal
    # overflows there; the depth of that window depends on the stack, so the
    # hook is made to raise as the overflow does.
    import verify_envelope  # noqa: PLC0415
    from verify_envelope import verify  # noqa: PLC0415
    mp = _envelope(tmp_path)
    m = json.loads(mp.read_text())
    m["metadata"] = {"n": 1}   # an integer outside the witnesses, so the hook runs
    mp.write_text(json.dumps(m))
    assert verify(mp).problems == []

    def overflow(s):
        raise RecursionError("Stack overflow")
    monkeypatch.setattr(verify_envelope, "_domain_int", overflow)
    with pytest.raises(ValueError, match="manifest is nested too deeply to read"):
        verify(mp)
    assert main(["verify_envelope.py", str(mp)]) == 2   # refused, as the loader refuses
