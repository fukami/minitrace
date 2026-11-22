"""Cross-language parity tests for the `minitrace-verify` CLI.

Scope B extension of the Path B JCS oracle work. Where
`test_jcs_parity.py` proves the JCS+SHA-256 floor matches between
Python `rfc8785` and Rust `serde_json_canonicalizer` on raw JSON
inputs, this driver exercises the *trace-aware* verifier built on
that floor:

  - take a real-ish minitrace document,
  - compute its `content_hash` via `adapters/cross_trace.py` (the
    Python reference), stamp it onto the trace,
  - feed the stamped trace to the Rust `minitrace-verify` CLI,
  - assert exit 0 and the expected "verified:" line.

Also covers the mismatch path (mutate a field after stamping; expect
exit 1), error paths (missing `content_hash`, unresolved cross-trace
placeholders; expect exit 2), and the §14.3 hint path (synthesise a
bigint-bearing trace with a deliberately wrong stored hash; expect
exit 1 plus the §14.3 note in output).

Prerequisites: a Rust toolchain with the crate's dependencies in the
local cargo cache. The driver builds the verifier itself before first use
(`cargo build --release --offline --locked --manifest-path
adapters/minitrace-verify/Cargo.toml`), so the binary always matches
the source in this tree.

Run:
    python -m pytest adapters/tests/test_verify.py -v
"""
from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

# Make `adapters/` importable when invoked as `pytest adapters/tests/...`.
_ADAPTERS_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_ADAPTERS_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTERS_DIR))

from cross_trace import compute_content_hash  # noqa: E402

VERIFY_BIN = (
    _ADAPTERS_DIR / "minitrace-verify" / "target" / "release" / "minitrace-verify"
)

EXAMPLE_AGENT_SESSION = (
    _ADAPTERS_DIR.parent
    / "examples"
    / "v0.3.0"
    / "agent-session-example.minitrace.json"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_VERIFY_MANIFEST = _ADAPTERS_DIR / "minitrace-verify" / "Cargo.toml"
_bin_built = False


def _ensure_bin() -> None:
    """Build the verifier from this tree's source before its first use.

    A binary left in target/ from an earlier source would otherwise answer
    for the current one. cargo returns at once when nothing changed. A
    missing toolchain or a failed build fails the test; it does not skip.
    """
    global _bin_built
    if _bin_built:
        return
    cmd = ["cargo", "build", "--release", "--offline", "--locked", "--quiet",
           "--manifest-path", str(_VERIFY_MANIFEST)]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, check=False, timeout=600)
    except FileNotFoundError as e:
        raise AssertionError(
            "cargo not found; these tests build minitrace-verify from source"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise AssertionError(f"`{' '.join(cmd)}` did not finish in 600 s") from e
    if proc.returncode != 0 or not VERIFY_BIN.is_file():
        raise AssertionError(
            f"`{' '.join(cmd)}` failed (exit {proc.returncode}):\n{proc.stderr}"
        )
    _bin_built = True


def _run_verify(trace: dict) -> subprocess.CompletedProcess:
    """Write `trace` to a temp file and invoke the Rust CLI on it.

    Round-trips through a file (not stdin) so the test exercises the
    `--file PATH` code path; stdin works the same way internally.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".minitrace.json", delete=False
    ) as tf:
        json.dump(trace, tf)
        tf.flush()
        path = tf.name
    try:
        return subprocess.run(
            [str(VERIFY_BIN), "--file", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False, timeout=300,
        )
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def _stamp(trace: dict) -> dict:
    """Compute and stamp a fresh content_hash via the Python reference."""
    t = copy.deepcopy(trace)
    t.pop("content_hash", None)
    t["content_hash"] = compute_content_hash(t)
    return t


def _load_example() -> dict:
    if not EXAMPLE_AGENT_SESSION.is_file():
        raise unittest.SkipTest(
            f"v0.3.0 example trace missing: {EXAMPLE_AGENT_SESSION} "
            "(PII-class; not shipped in the public package — JCS parity is "
            "still covered by the minimal-shape test)"
        )
    with open(EXAMPLE_AGENT_SESSION) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Happy-path: Python computes the hash, Rust verifies it.
# This is the parity assertion: if Rust disagrees on the hash on a
# real-ish trace, the JCS+SHA-256 pipeline divergence has leaked
# through the trace-shape layer.
# ---------------------------------------------------------------------------


def test_verify_python_stamped_trace_matches() -> None:
    _ensure_bin()
    trace = _stamp(_load_example())
    proc = _run_verify(trace)
    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}\n"
        f"stdout: {proc.stdout.decode()!r}\n"
        f"stderr: {proc.stderr.decode()!r}"
    )
    stdout = proc.stdout.decode().strip()
    assert stdout.startswith("verified: sha256:"), (
        f"unexpected stdout: {stdout!r}"
    )
    # And the hash on the line matches the stamped one.
    assert trace["content_hash"] in stdout, (
        f"verified line does not contain stamped hash {trace['content_hash']}: {stdout!r}"
    )


def test_verify_python_stamped_trace_with_minimal_shape() -> None:
    """Cross-check with a minimal hand-rolled trace, not just the example.

    Guards against the test accidentally passing because the example
    happens to canonicalise the same way under both pipelines while a
    smaller trace surfaces a divergence.
    """
    _ensure_bin()
    trace = _stamp(
        {
            "id": "0193d4a0-aaaa-7890-abcd-000000000001",
            "format_version": "0.3.0",
            "producer": "test_verify.py",
            "started_at": "2026-05-31T00:00:00Z",
            "ended_at": "2026-05-31T00:00:01Z",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        }
    )
    proc = _run_verify(trace)
    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}\n"
        f"stdout: {proc.stdout.decode()!r}\n"
        f"stderr: {proc.stderr.decode()!r}"
    )


# ---------------------------------------------------------------------------
# Mismatch path: stamp, then mutate, expect exit 1 and a mismatch report.
# ---------------------------------------------------------------------------


def test_verify_mismatch_after_mutation() -> None:
    _ensure_bin()
    trace = _stamp(_load_example())
    # Mutate a field after stamping. Any change to a hashed-over field
    # invalidates the stored hash.
    trace = copy.deepcopy(trace)
    trace["producer"] = "MUTATED " + str(trace.get("producer", ""))
    proc = _run_verify(trace)
    assert proc.returncode == 1, (
        f"expected exit 1, got {proc.returncode}\n"
        f"stdout: {proc.stdout.decode()!r}"
    )
    stdout = proc.stdout.decode()
    assert "mismatch:" in stdout
    assert "stored:" in stdout
    assert "recomputed:" in stdout
    # No bigint in the mutated example, so no §14.3 note.
    assert "§14.3" not in stdout


# ---------------------------------------------------------------------------
# §14.3 hint: bigint-bearing trace + wrong stored hash → exit 1 + note.
# ---------------------------------------------------------------------------


def test_verify_bigint_emits_section_14_3_hint() -> None:
    _ensure_bin()
    # Hand-roll a trace with a value outside [-(2^53-1), 2^53-1]. We
    # deliberately do NOT call compute_content_hash here because the
    # Python reference would raise ValueError on the bigint — that is
    # itself the §14.3 divergence. Instead we stamp a syntactically
    # well-formed but deliberately-wrong sha256 string, run the Rust
    # CLI, and assert it reports the mismatch with the §14.3 note.
    trace = {
        "id": "0193d4a0-bbbb-7890-abcd-000000000002",
        "format_version": "0.3.0",
        "producer": "test_verify.py",
        "started_at": "2026-05-31T00:00:00Z",
        "ended_at": "2026-05-31T00:00:01Z",
        # Bigint: 2^53 + 1, not exactly representable in f64.
        "metrics": {"big_counter": 9007199254740993},
        "content_hash": "sha256:" + ("0" * 64),
    }
    proc = _run_verify(trace)
    assert proc.returncode == 1, (
        f"expected exit 1, got {proc.returncode}\n"
        f"stdout: {proc.stdout.decode()!r}\n"
        f"stderr: {proc.stderr.decode()!r}"
    )
    stdout = proc.stdout.decode()
    assert "mismatch:" in stdout
    assert "§14.3" in stdout, (
        f"expected §14.3 note in mismatch output; got:\n{stdout}"
    )
    assert "out" not in stdout.lower() or "2^53" in stdout
    assert "verifier-implementation divergence" in stdout


# ---------------------------------------------------------------------------
# Error paths: exit 2 with informative stderr.
# ---------------------------------------------------------------------------


def test_verify_missing_content_hash_exits_2() -> None:
    _ensure_bin()
    trace = {
        "id": "0193d4a0-cccc-7890-abcd-000000000003",
        "format_version": "0.3.0",
        # no content_hash
    }
    proc = _run_verify(trace)
    assert proc.returncode == 2, (
        f"expected exit 2, got {proc.returncode}\n"
        f"stdout: {proc.stdout.decode()!r}\n"
        f"stderr: {proc.stderr.decode()!r}"
    )
    stderr = proc.stderr.decode()
    assert "content_hash" in stderr


def test_verify_unresolved_placeholder_exits_2() -> None:
    _ensure_bin()
    trace = {
        "id": "0193d4a0-dddd-7890-abcd-000000000004",
        "format_version": "0.3.0",
        "lineage": [
            {
                "kind": "judges",
                "target_trace_id": "abc",
                "target_content_hash": "${trace:abc:content_hash}",
            }
        ],
        # Hash placeholder doesn't matter — placeholder detection
        # short-circuits before we read content_hash.
        "content_hash": "sha256:" + ("0" * 64),
    }
    proc = _run_verify(trace)
    assert proc.returncode == 2, (
        f"expected exit 2, got {proc.returncode}\n"
        f"stdout: {proc.stdout.decode()!r}\n"
        f"stderr: {proc.stderr.decode()!r}"
    )
    stderr = proc.stderr.decode()
    assert "placeholder" in stderr


def test_placeholder_grammar_parity_rust_python() -> None:
    """DSL spec RC 3 §13.1, cross-language: the Rust verifier and the Python
    producer classify every target_content_hash value the same way
    (placeholder / well-formed hash / malformed). A fuzz of 3,410 adversarial
    strings found 0 divergences; this pins representative ones."""
    _ensure_bin()
    from cross_trace import PLACEHOLDER_PATTERN, _WELLFORMED_HASH  # noqa: PLC0415
    values = [
        "${trace:a:content_hash}", "${trace:a:b:content_hash}", "${trace:x}y:content_hash}",
        "${trace:a:other_field}", "${trace:a\nb:content_hash}", "${trace:é:content_hash}",
        "${trace:a:content_hash} ", "${trace:a:content_hash}\n", "prefix ${trace:a:content_hash}",
        "${trace:a:Content_Hash}", "${trace::content_hash}", "${trace:a:}", "${trace:a}",
        "${trace:a:content_hash}}", "${trace:a:contént}", "${trace:" + "x" * 5000 + ":content_hash}",
        "sha256:" + "0" * 64, "sha256:" + "A" * 64, "sha256:zz", "",
    ]
    for v in values:
        if _WELLFORMED_HASH.fullmatch(v):
            want = "ok"
        elif PLACEHOLDER_PATTERN.fullmatch(v):
            want = "placeholder"
        else:
            want = "malformed"
        proc = _run_verify(_stamp({"id": "t", "lineage": [
            {"type": "judges", "target_trace_id": "a", "target_content_hash": v}]}))
        err = proc.stderr.decode()
        got = ("ok" if proc.returncode == 0 else
               "placeholder" if proc.returncode == 2 and "unresolved cross-trace placeholder" in err else
               "malformed" if proc.returncode == 2 and "neither a well-formed hash" in err else
               f"exit {proc.returncode}: {err[:80]}")
        assert got == want, f"{v[:60]!r}: python={want} rust={got}"


def test_verify_placeholder_text_in_content_exits_0() -> None:
    """RC 3 §13.1: placeholder-shaped text outside the bearing field is content."""
    _ensure_bin()
    proc = _run_verify(_stamp({"id": "t", "turns": [
        {"index": 0, "role": "user", "content": "see ${trace:abc:content_hash} and ${record.x}"}]}))
    assert proc.returncode == 0, proc.stderr.decode()


def test_verify_null_content_hash_exits_2() -> None:
    _ensure_bin()
    trace = {
        "id": "0193d4a0-eeee-7890-abcd-000000000005",
        "format_version": "0.3.0",
        "content_hash": None,  # explicit null per the omitted-vs-null distinction
    }
    proc = _run_verify(trace)
    assert proc.returncode == 2, (
        f"expected exit 2, got {proc.returncode}\n"
        f"stderr: {proc.stderr.decode()!r}"
    )


# ---------------------------------------------------------------------------
# Corpus invariants over the in-tree v0.3.0 examples. Two distinct checks:
#
#   1. test_no_example_carries_a_stale_content_hash — single-trace integrity.
#      `content_hash` is OPTIONAL (spec §3.4); examples that do not exercise
#      it carry `content_hash: null`. Any example that DOES carry a non-null
#      hash MUST round-trip against the Python reference.
#
#   2. test_cross_trace_edges_are_consistent — paired-trace integrity (§7.1).
#      A lineage edge carrying a resolved `target_content_hash` references a
#      peer trace's content_hash. When the referent is itself an in-corpus
#      example, the edge's target_content_hash MUST equal the referent's
#      stored content_hash, and the referent MUST carry one (§7.1:
#      an edge requiring integrity MUST NOT reference a trace with no hash).
#
# These replace an earlier single-example stale pin. History: the
# agent-session-example carried a stale content_hash (19717bbd…) that the
# judge-session-example referenced as its lineage target — an internally
# consistent but collectively stale PAIR. It was briefly (and wrongly) fixed
# by nulling agent's hash alone (2026-06-01), which orphaned judge's edge;
# corrected 2026-06-03 by re-stamping the pair to the current reference
# (agent fd89a5f5…, judge edge → fd89a5f5…, judge own hash null; judge
# stamped ddfb0969… and pi 7d3a6eef… on 2026-10-18; the pair re-stamped on
# 2026-10-28 to agent 9d67dbcd…, judge 443b60e0…). Check #2
# is the guard that would have caught the orphaning. No in-repo re-stamp
# automation exists, so these invariants are the corpus's drift backstop.
# ---------------------------------------------------------------------------


def test_no_example_carries_a_stale_content_hash() -> None:
    _ensure_bin()
    examples_dir = _ADAPTERS_DIR.parent / "examples" / "v0.3.0"
    if not examples_dir.is_dir():
        raise unittest.SkipTest(f"examples dir missing: {examples_dir}")
    examples = sorted(examples_dir.glob("*.minitrace.json"))
    if not examples:
        raise unittest.SkipTest(f"no examples under {examples_dir}")
    checked = 0
    for path in examples:
        with open(path) as f:
            on_disk = json.load(f)
        if on_disk.get("content_hash") is None:
            continue  # opted out of the integrity feature; nothing to verify
        checked += 1
        proc = _run_verify(on_disk)
        assert proc.returncode == 0, (
            f"{path.name} carries a non-null content_hash that does not "
            f"round-trip (exit {proc.returncode}). Either re-stamp it via "
            f"cross_trace.compute_content_hash or null the field.\n"
            f"stdout: {proc.stdout.decode()!r}\n"
            f"stderr: {proc.stderr.decode()!r}"
        )
    # Zero stamped examples is not a failure (content_hash is optional); the
    # test still guards the moment any example opts in. Since 2026-10-18 the
    # agent-session, judge-session and pi examples are stamped (agent's hash
    # is the target of judge's lineage edge — see check #2 below).
    if checked == 0:
        print("note: no v0.3.0 example carries a non-null content_hash")


def test_cross_trace_edges_are_consistent() -> None:
    """Every lineage edge that references an in-corpus example by a resolved
    target_content_hash must match that example's stored content_hash (§7.1),
    and the referent must carry a non-null hash (§7.1)."""
    examples_dir = _ADAPTERS_DIR.parent / "examples" / "v0.3.0"
    if not examples_dir.is_dir():
        raise unittest.SkipTest(f"examples dir missing: {examples_dir}")
    traces = {}
    for path in sorted(examples_dir.glob("*.minitrace.json")):
        with open(path) as f:
            t = json.load(f)
        if "id" in t:
            traces[t["id"]] = (path.name, t)
    if not traces:
        raise unittest.SkipTest(f"no identifiable examples under {examples_dir}")
    checked = 0
    for name, t in traces.values():
        for edge in t.get("lineage") or []:
            target_hash = edge.get("target_content_hash")
            target_id = edge.get("target_trace_id")
            # Skip discovery-only edges (no integrity) and unresolved
            # placeholder forms — those are producer-side, not corpus state.
            if target_hash is None or "${" in target_hash:
                continue
            # Only verifiable when the referent is itself an in-corpus example.
            if target_id not in traces:
                continue
            ref_name, ref = traces[target_id]
            checked += 1
            ref_hash = ref.get("content_hash")
            assert ref_hash is not None, (
                f"{name} has an integrity edge to {ref_name} "
                f"({target_id}), but the referent carries no content_hash "
                f"(§7.1: edge requiring integrity MUST NOT reference "
                f"a trace with no hash)."
            )
            assert ref_hash == target_hash, (
                f"{name}'s lineage edge target_content_hash does not match "
                f"the referent {ref_name}'s stored content_hash.\n"
                f"  edge target: {target_hash}\n"
                f"  {ref_name}:  {ref_hash}\n"
                f"Re-stamp the pair via cross_trace.back_fill_cross_trace_references."
            )
    if checked == 0:
        print("note: no in-corpus cross-trace integrity edges to verify")


# ---------------------------------------------------------------------------
# Set / edge-consistency mode (multiple --file or --dir).
#
# The CLI's set mode self-verifies each stamped trace AND checks every
# cross-trace lineage edge's target_content_hash against the referenced
# in-set trace (§7.1). These tests drive the Rust CLI on
# real and synthesised trace sets; the corpus-level invariant above
# (test_cross_trace_edges_are_consistent) is the pure-Python mirror.
# ---------------------------------------------------------------------------


def _run_verify_files(traces: list[dict]) -> subprocess.CompletedProcess:
    """Write each trace to its own temp file and invoke the CLI in set mode
    via repeated --file arguments."""
    paths = []
    try:
        for i, t in enumerate(traces):
            tf = tempfile.NamedTemporaryFile(
                mode="w", suffix=f".{i}.minitrace.json", delete=False
            )
            json.dump(t, tf)
            tf.flush()
            tf.close()
            paths.append(tf.name)
        argv = [str(VERIFY_BIN)]
        for p in paths:
            argv += ["--file", p]
        return subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=300
        )
    finally:
        for p in paths:
            pathlib.Path(p).unlink(missing_ok=True)


def _example_pair() -> tuple[dict, dict]:
    """Load the agent↔judge integrity pair from the v0.3.0 corpus."""
    base = _ADAPTERS_DIR.parent / "examples" / "v0.3.0"
    agent_path = base / "agent-session-example.minitrace.json"
    judge_path = base / "judge-session-example.minitrace.json"
    if not (agent_path.is_file() and judge_path.is_file()):
        raise unittest.SkipTest(f"v0.3.0 example pair missing under {base}")
    agent = json.load(open(agent_path))
    judge = json.load(open(judge_path))
    return agent, judge


def test_set_mode_example_pair_consistent() -> None:
    _ensure_bin()
    agent, judge = _example_pair()
    proc = _run_verify_files([agent, judge])
    out = proc.stdout.decode()
    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}\n{out}\n{proc.stderr.decode()}"
    )
    assert "set mode (2 traces)" in out
    assert "consistent" in out
    assert "0 problems" in out


def test_set_mode_dir_exits_0() -> None:
    _ensure_bin()
    examples_dir = _ADAPTERS_DIR.parent / "examples" / "v0.3.0"
    if not examples_dir.is_dir():
        raise unittest.SkipTest(f"examples dir missing: {examples_dir}")
    proc = subprocess.run(
        [str(VERIFY_BIN), "--dir", str(examples_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False, timeout=300,
    )
    out = proc.stdout.decode()
    assert proc.returncode == 0, (
        f"expected exit 0 on the clean corpus, got {proc.returncode}\n"
        f"{out}\n{proc.stderr.decode()}"
    )
    assert "edge" in out and "consistent" in out


def test_set_mode_orphaned_edge_exits_1() -> None:
    """An integrity edge whose referent carries no content_hash is the §7.1
    violation that the earlier single-trace null fix introduced. Set mode
    must catch it."""
    _ensure_bin()
    agent, judge = _example_pair()
    agent = copy.deepcopy(agent)
    agent["content_hash"] = None  # re-introduce the orphaning bug
    proc = _run_verify_files([agent, judge])
    out = proc.stdout.decode()
    assert proc.returncode == 1, (
        f"expected exit 1, got {proc.returncode}\n{out}\n{proc.stderr.decode()}"
    )
    assert "REFERENT HAS NO HASH" in out
    assert "carries no content_hash (§7.1)" in out
    assert "line 565" not in out


def test_set_mode_edge_hash_mismatch_refers_to_another_state() -> None:
    """A correctly self-hashed referent, but an edge pointing at a different
    hash: the edge refers to another state of the referent (trace §7.1,
    envelope §6.3; ruled 2026-10-29), not verified and no failed check, so
    set mode exits 0 and --strict, where an unverified edge is a problem,
    exits 1. The judge's own hash is restamped, so only the edge differs."""
    _ensure_bin()
    agent, judge = _example_pair()
    judge = copy.deepcopy(judge)
    # Point the edge at a wrong (but well-formed) hash.
    judge["lineage"][0]["target_content_hash"] = "sha256:" + ("d" * 64)
    judge["content_hash"] = compute_content_hash(judge)
    proc = _run_verify_files([agent, judge])
    out = proc.stdout.decode()
    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}\n{out}\n{proc.stderr.decode()}"
    )
    assert "refers to another state of the referent, not verified (trace §7.1)" in out, out
    assert "INCONSISTENT" not in out, out
    with tempfile.TemporaryDirectory() as td:
        for name, t in (("a", agent), ("j", judge)):
            (pathlib.Path(td) / f"{name}.minitrace.json").write_text(json.dumps(t))
        p = subprocess.run([str(VERIFY_BIN), "--strict", "--dir", td], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, check=False, timeout=300)
    assert p.returncode == 1 and "1 edges to another state, 1 problems" in p.stdout, (p.returncode, p.stdout)


def test_lineage_entries_that_cannot_be_read_fail_in_every_mode() -> None:
    """Every lineage entry carries type and target_trace_id as non-empty
    strings, as the base schema requires: absent, null, the empty string or
    another JSON type is exit 1 in single mode, set mode and --strict set
    mode (each passed before as a discovery-only or unverified edge). The
    pair as shipped is the control."""
    _ensure_bin()
    agent, judge = _example_pair()
    cases = {
        "tid null": (lambda e: e.__setitem__("target_trace_id", None), "target_trace_id is not a string (got null)"),
        "tid absent": (lambda e: e.pop("target_trace_id"), "target_trace_id is absent"),
        "tid empty": (lambda e: e.__setitem__("target_trace_id", ""), "target_trace_id is the empty string"),
        "tid number": (lambda e: e.__setitem__("target_trace_id", 123), "target_trace_id is not a string (got number)"),
        "type null": (lambda e: e.__setitem__("type", None), "type is not a string (got null)"),
        "type absent": (lambda e: e.pop("type"), "type is absent"),
        "type empty": (lambda e: e.__setitem__("type", ""), "type is the empty string"),
        "type number": (lambda e: e.__setitem__("type", 123), "type is not a string (got number)"),
        "derived-from tid number": (lambda e: (e.clear(), e.update({"type": "derived-from", "target_trace_id": 456})),
                                    "target_trace_id is not a string (got number)"),
        "derived-from tid null": (lambda e: (e.clear(), e.update({"type": "derived-from", "target_trace_id": None})),
                                  "target_trace_id is not a string (got null)"),
        "control": (lambda e: None, None),
    }
    for label, (edit, want) in cases.items():
        j = copy.deepcopy(judge)
        edit(j["lineage"][0])
        j["content_hash"] = compute_content_hash(j)
        with tempfile.TemporaryDirectory() as td:
            for name, t in (("a", agent), ("j", j)):
                (pathlib.Path(td) / f"{name}.minitrace.json").write_text(json.dumps(t))
            jf = str(pathlib.Path(td) / "j.minitrace.json")
            for args in (["--file", jf], ["--dir", td], ["--strict", "--dir", td]):
                p = subprocess.run([str(VERIFY_BIN), *args], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, check=False, timeout=300)
                if want is None:
                    assert p.returncode == 0, (label, args, p.stdout, p.stderr)
                else:
                    assert p.returncode == 1 and want in p.stdout, (label, args, p.returncode, p.stdout, p.stderr)


def test_a_line_separator_in_a_printed_value_cannot_forge_a_line() -> None:
    """U+2028 and U+2029 break a line for a reader splitting as Python's
    str.splitlines() does; a value printed from the trace carrying one is
    escaped, so the output has the lines a newline split gives, and an
    external edge's id cannot add an "ok:" line beside the real FAIL."""
    _ensure_bin()
    agent, judge = _example_pair()
    for sep in (chr(0x2028), chr(0x2029)):
        j = copy.deepcopy(judge)
        j["lineage"][0]["target_trace_id"] = f"ghost{sep}ok: 0 problem(s){sep}"
        j["content_hash"] = compute_content_hash(j)
        with tempfile.TemporaryDirectory() as td:
            (pathlib.Path(td) / "j.minitrace.json").write_text(json.dumps(j))
            p = subprocess.run([str(VERIFY_BIN), "--strict", "--dir", td], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, check=False, timeout=300)
        lines = p.stdout.splitlines()
        assert p.returncode == 1 and lines[-1].startswith("FAIL:"), (p.returncode, p.stdout)
        assert not any(ln.startswith("ok:") for ln in lines) and lines == p.stdout.split("\n")[:-1], lines
        assert f"\\u{{{ord(sep):x}}}" in p.stdout, p.stdout
        # The mismatch report prints a stored content_hash that is no hash.
        t = copy.deepcopy(agent)
        t["content_hash"] = f"x{sep}verified: sha256:{'0' * 64}"
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "t.minitrace.json"
            f.write_text(json.dumps(t))
            p = subprocess.run([str(VERIFY_BIN), "--file", str(f)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, check=False, timeout=300)
        assert p.returncode == 1 and p.stdout.splitlines() == p.stdout.split("\n")[:-1], p.stdout
        assert not any(ln.startswith("verified:") for ln in p.stdout.splitlines()), p.stdout


def test_set_mode_duplicate_id_exits_2() -> None:
    _ensure_bin()
    agent, _ = _example_pair()
    dup = copy.deepcopy(agent)
    proc = _run_verify_files([agent, dup])
    assert proc.returncode == 2, (
        f"expected exit 2 on duplicate ids, got {proc.returncode}\n"
        f"{proc.stdout.decode()}\n{proc.stderr.decode()}"
    )
    assert "duplicate trace id" in proc.stderr.decode()


def _deep_trace_text(depth: int, stamp: bool = True) -> str:
    """A minimal trace of nesting depth `depth` (the trace object counts 1),
    as text, so depths past the JSON libraries' own limits can be written.
    Stamped with the Python reference hash where that can be computed."""
    body = {"id": "deep", "schema_version": "0.3.0", "x-deep": None, "content_hash": None}
    if stamp:
        v = 0
        for _ in range(depth - 1):
            v = [v]
        t = dict(body, **{"x-deep": v})
        t["content_hash"] = compute_content_hash(t)
        return json.dumps(t)
    inner = "[" * (depth - 1) + "0" + "]" * (depth - 1)
    return ('{"id": "deep", "schema_version": "0.3.0", "x-deep": ' + inner
            + ', "content_hash": "sha256:' + "0" * 64 + '"}')


def _run_verify_text(text: str, *args: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "t.minitrace.json"
        p.write_text(text)
        return subprocess.run([str(VERIFY_BIN), *args, "--file", str(p)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, check=False, timeout=300)


def test_verify_a_trace_nested_deeper_than_64_is_not_conformant() -> None:
    """Ruled 2026-10-28: trace §3.4 depth limit, at every depth, as the
    Python references read it. Up to the parser's limit the hash verdict is
    printed and the depth line follows; past it the trace is not parsed and
    its hash not checked; either way exit 1 (a check found a problem)."""
    import source_reader  # noqa: PLC0415
    _ensure_bin()
    assert source_reader.trace_json_verdict(_deep_trace_text(64))[1] is None
    for depth in (63, 64):
        p = _run_verify_text(_deep_trace_text(depth))
        assert p.returncode == 0 and "not conformant" not in p.stdout, (depth, p.stdout, p.stderr)
    for depth in (65, 120, 127):
        text = _deep_trace_text(depth)
        assert source_reader.trace_json_verdict(text)[1] == "depth-exceeds-64", depth
        for args in ((), ("--strict",)):
            p = _run_verify_text(text, *args)
            assert p.returncode == 1, (depth, args, p.returncode, p.stdout, p.stderr)
            assert "verified" in p.stdout and "not conformant" in p.stdout and "§3.4" in p.stdout, (depth, args, p.stdout)
            assert "deeper than 64" in p.stdout, (depth, args, p.stdout)
    for depth in (128, 200, 100_000):
        text = _deep_trace_text(depth, stamp=False)
        assert source_reader.trace_json_verdict(text)[1] == "depth-exceeds-64", depth
        p = _run_verify_text(text)
        assert p.returncode == 1, (depth, p.returncode, p.stdout, p.stderr)
        assert "not conformant" in p.stdout and "deeper than 64" in p.stdout, (depth, p.stdout)
        assert "hash not checked" in p.stdout, (depth, p.stdout)
    # A mismatching hash on a deep trace: both findings are reported.
    t = json.loads(_deep_trace_text(65))
    t["content_hash"] = "sha256:" + "1" * 64
    p = _run_verify_text(json.dumps(t))
    assert p.returncode == 1 and "mismatch" in p.stdout and "deeper than 64" in p.stdout, p.stdout
    # Only the parser's own depth limit is read as the depth finding: a deep
    # trace broken for another reason keeps that reason and exit 2.
    deep = "[" * 70 + "0" + "]" * 70
    for broken, why in (
        ('{"id": "d", "id": "e", "x": ' + deep + ', "content_hash": null}', "duplicate"),
        ('{"id": "d", "x": ' + deep + ', "s": "\\ud800", "content_hash": null}', "hex escape"),   # serde's wording
        ('{"id": "d", "x": ' + deep + ', "n": 1e999, "content_hash": null}', ""),
        ('{"id": "d", "content_hash": null} ' + deep, "trailing"),
        ('{"id": "d", "x": ' + deep[:-5], "EOF"),
    ):
        p = _run_verify_text(broken)
        assert p.returncode == 2 and why in p.stderr, (why, p.returncode, p.stdout, p.stderr)
        assert "deeper than 64" not in p.stdout, (why, p.stdout)
    # Brackets inside strings are not nesting.
    t = json.loads(_deep_trace_text(2))
    t["s"] = "[" * 300 + "{" * 300
    t["content_hash"] = None
    t["content_hash"] = compute_content_hash(t)
    p = _run_verify_text(json.dumps(t))
    assert p.returncode == 0 and "not conformant" not in p.stdout, p.stdout


def test_verify_set_mode_reports_each_too_deep_trace_and_checks_the_rest() -> None:
    _ensure_bin()
    good = json.loads(_deep_trace_text(3))
    good["id"] = "good"
    good["content_hash"] = None
    good["content_hash"] = compute_content_hash(good)
    texts = {"good": json.dumps(good), "deep65": _deep_trace_text(65).replace('"deep"', '"deep65"'),
             "deep200": _deep_trace_text(200, stamp=False).replace('"deep"', '"deep200"')}
    # deep65's hash was computed with id "deep": restamp after the rename.
    d65 = json.loads(texts["deep65"])
    d65["content_hash"] = None
    d65["content_hash"] = compute_content_hash(d65)
    texts["deep65"] = json.dumps(d65)
    with tempfile.TemporaryDirectory() as td:
        argv = [str(VERIFY_BIN)]
        for name, text in texts.items():
            (pathlib.Path(td) / f"{name}.minitrace.json").write_text(text)
            argv += ["--file", str(pathlib.Path(td) / f"{name}.minitrace.json")]
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           check=False, timeout=300)
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    lines = p.stdout.splitlines()
    assert any("good.minitrace.json: verified" in ln for ln in lines), p.stdout
    assert sum("deeper than 64" in ln for ln in lines) == 2, p.stdout
    assert "2 problems" in p.stdout, p.stdout
    # A deep member broken for another reason still refuses the set (exit 2).
    with tempfile.TemporaryDirectory() as td:
        bad = pathlib.Path(td) / "bad.minitrace.json"
        bad.write_text('{"id": "b", "id": "c", "x": ' + "[" * 70 + "0" + "]" * 70 + ', "content_hash": null}')
        ok = pathlib.Path(td) / "good.minitrace.json"
        ok.write_text(texts["good"])
        p = subprocess.run([str(VERIFY_BIN), "--file", str(ok), "--file", str(bad)], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, check=False, timeout=300)
    assert p.returncode == 2 and "duplicate" in p.stderr, (p.returncode, p.stdout, p.stderr)


def test_verify_refuses_a_trace_over_256_mib_before_reading_it() -> None:
    """Ruled 2026-10-29: minitrace-verify bounds one trace as verify_envelope's
    MAX_TRACE_BYTES does (256 MiB); a larger file is refused by its size,
    exit 2, in single and set mode, instead of being read whole."""
    _ensure_bin()
    limit = 256 * 2**20
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "big.minitrace.json"
        with p.open("wb") as fh:
            fh.truncate(limit + 1)   # sparse: nothing of it is stored or read
        for args in (["--file", str(p)], ["--dir", td]):
            r = subprocess.run([str(VERIFY_BIN), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, check=False, timeout=300)
            assert r.returncode == 2 and f"{limit + 1} bytes, more than {limit}, the limit for one trace" \
                in r.stderr, (args, r.returncode, r.stderr[-300:])
        # The limit itself is read (here: zeros, which then fail to parse).
        with p.open("r+b") as fh:
            fh.truncate(limit)
        r = subprocess.run([str(VERIFY_BIN), "--file", str(p)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, check=False, timeout=300)
        assert r.returncode == 2 and "the limit for one trace" not in r.stderr, (r.returncode, r.stderr[-300:])


if __name__ == "__main__":
    # Allow ad-hoc execution without pytest.
    import inspect

    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        if not callable(fn):
            continue
        if list(inspect.signature(fn).parameters):
            continue
        try:
            fn()
        except unittest.SkipTest as e:
            print(f"SKIP {name}: {e}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
        else:
            print(f"PASS {name}")
    sys.exit(1 if failed else 0)
