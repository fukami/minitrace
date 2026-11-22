#!/usr/bin/env python3
"""Harness-agnostic Axis-C drift core.

The per-harness `adapters/<h>/regression/drift_watch.py` wires this core with a
`DriftConfig` (its preprocessor, DSL, cell-signal table, and any harness-specific
extras) and calls `run_drift`. The signals that are the SAME for every harness
live here; the ones that are not (claude-code's attachment-subtype arm, its
subagent sidechain discovery, its tool-name-specific cell predicates) are passed
in as config so a new adapter is a thin wiring file, not a copied watcher.

Generic signals (see the per-harness README for the full contract):

  0. NEW RAW SHAPE     raw capture, upstream of the preprocessor.
                       • new RECORD TYPE  → FATAL (unambiguous, rare)
                       • new field PATH   → WARNING (benign run-to-run variance)
  1. DARK CELL         a manifest `annotated_cells_fired` cell that no longer
                       fires in the candidate trace → FATAL. Fired cells are
                       derived structurally from the emitted trace via the
                       harness's `cell_signals`. Self-test asserts EXACT
                       equivalence both ways (a dark cell AND an over-fire fail),
                       so a stale annotation or an always-true predicate is caught
                       at baseline.
  2. NEW UNMAPPED      a (record-type, path) the preprocessor carries into events
                       but the DSL does not consume, present in candidate and not
                       baseline → FATAL.
  3. DROPPED DEPENDENCY a field the DSL reads, present at baseline, absent at
                       candidate → FATAL.

Harness-specific arms (`DriftConfig.extra_arms`) run per scenario after signal 0
and contribute their own (problems, warnings).

Deps must be importable (pyyaml, rfc8785) — the per-harness watcher runs under uv.
Exit 0 = no drift; 1 = drift; 2 = harness/usage error.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Callable

# coverage.py lives one level up (adapters/); the per-harness watcher puts the
# adapters root on sys.path before importing this module, so this resolves.
from coverage import collect_seen_paths, _strip_path_kind

# Each adapter step (pre-processor, dsl_runner) is waited on this long; a
# hung step is a failed scenario, not a hung watch (ruled 2026-10-29).
STEP_TIMEOUT = 600


# --- Config -----------------------------------------------------------------

@dataclass
class ArmContext:
    """What a harness-specific arm needs to inspect one scenario."""
    base_input: pathlib.Path
    cand_input: pathlib.Path
    base_raw: list[dict]
    cand_raw: list[dict]
    dsl: pathlib.Path
    self_test: bool


# An arm inspects one scenario and returns (problems, warnings).
Arm = Callable[[ArmContext], "tuple[list[str], list[str]]"]


@dataclass
class DriftConfig:
    harness_name: str                      # for the header line, e.g. "claude-code"
    # Native raw -> DSL-runner input. None means the harness raw format IS the
    # DSL-runner input (pi: the DSL runner reads the native JSONL directly, the
    # original pilot design), so run_adapter feeds the raw input straight to the
    # runner with no preprocess subprocess. A Path is the preprocessor the raw
    # goes through first (claude-code/goose/opencode).
    preprocess: "pathlib.Path | None"
    dsl: pathlib.Path
    dsl_runner: pathlib.Path
    run_cwd: pathlib.Path                   # cwd for adapter subprocesses
    cell_signals: dict                      # cell -> predicate(trace) -> bool
    # How to read the main raw record list from one input path. Defaults to
    # read_jsonl (claude-code and any JSONL-native harness); a harness whose raw
    # ground truth is not JSONL (goose: a SQLite sessions.db) supplies its own
    # reader that returns the same {type: ...}-tagged record dicts.
    raw_reader: Callable[[pathlib.Path], list[dict]] | None = None
    # Extra raw records beyond the main input (e.g. a subagent sidechain). Returns
    # a list of raw record dicts; the main input is always read separately.
    extra_raw_records: Callable[[pathlib.Path], list[dict]] | None = None
    type_field: str = "type"                # record-type key in the raw format
    # The dsl_runner --operator-* arguments, from the watcher's own required
    # --operator-type (ruled 2026-10-20, as for verify_run): no default, and
    # run_drift refuses a config without one.
    operator_args: list[str] | None = None
    extra_arms: list[Arm] = field(default_factory=list)
    # Path to this adapter's drift ledger (adapters/<h>/regression/drift-ledger.yaml).
    # A ledgered drift downgrades from FATAL to an acknowledged line; a drift not
    # covered by any entry still gates. None (or a missing file) = no ledger, so
    # every drift gates (claude-code / goose behaviour is unchanged).
    ledger: "pathlib.Path | None" = None


# --- Operator ----------------------------------------------------------------

_OPERATOR_OPTIONS = ("aware", "capture_mode", "capture_purpose_legal_basis",
                     "capture_purpose_reference", "capture_purpose_authorised_at")


def add_operator_arguments(ap) -> None:
    """The operator dsl_runner stamps on every trace: --operator-type is
    required with no default (ruled 2026-10-20, as for verify_run); the rest
    pass through when given, and dsl_runner checks them (minitrace-spec
    §5.2-§5.4)."""
    ap.add_argument("--operator-type", required=True,
                    choices=["human", "ai_overseer", "scripted", "delegated"],
                    help="the operator assertion stamped on traces; required, no default")
    ap.add_argument("--operator-aware", choices=["true", "false"],
                    help="required by dsl_runner for a human operator")
    ap.add_argument("--operator-capture-mode", choices=["full"],
                    help="required by dsl_runner for a human or delegated operator; only full: dsl_runner "
                         "refuses pseudonymised, scrubbed and reference_only, since it performs none of "
                         "those transformations")
    ap.add_argument("--operator-capture-purpose-legal-basis")
    ap.add_argument("--operator-capture-purpose-reference")
    ap.add_argument("--operator-capture-purpose-authorised-at")


def operator_args_from(args) -> list[str]:
    """The dsl_runner arguments for the operator add_operator_arguments read."""
    out = ["--operator-type", args.operator_type]
    for name in _OPERATOR_OPTIONS:
        v = getattr(args, f"operator_{name}")
        if v is not None:
            out += [f"--operator-{name.replace('_', '-')}", v]
    return out


def _require_operator(cfg: "DriftConfig") -> list[str]:
    """cfg.operator_args, or ValueError: none given, or an operator dsl_runner
    would refuse. Parsed with the watchers' own options and held to
    dsl_runner's _build_operator_from_args, so a refusal is one usage error
    (exit 2) before any scenario runs, not one adapter failure per scenario
    read as drift."""
    import argparse  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import dsl_runner  # noqa: PLC0415
    where = f"drift_watch [{cfg.harness_name}]"
    if not cfg.operator_args:
        raise ValueError(f"{where}: no operator: DriftConfig.operator_args needs --operator-type "
                         f"(required, no default)")
    ap = argparse.ArgumentParser(prog=where, add_help=False, exit_on_error=False)
    add_operator_arguments(ap)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            ns = ap.parse_args(list(cfg.operator_args))
    except (argparse.ArgumentError, SystemExit) as e:
        why = err.getvalue().strip().splitlines()[-1:] or [str(e)]
        raise ValueError(f"{where}: no operator: {why[0]}") from None
    ns.operator_aware = None if ns.operator_aware is None else ns.operator_aware == "true"
    try:
        dsl_runner._build_operator_from_args(ns)
    except SystemExit as e:
        raise ValueError(f"{where}: {e}") from None
    return list(cfg.operator_args)


# --- Adapter run + coverage helpers -----------------------------------------

def run_adapter(input_path: pathlib.Path, tmp: pathlib.Path,
                cfg: DriftConfig) -> tuple[list[dict], dict]:
    """Preprocess + DSL over one input; return (traces, coverage_report)."""
    traces = tmp / "traces.json"
    coverage = tmp / "coverage.json"
    # A preprocessor-less harness (pi) feeds its native raw straight to the DSL
    # runner; otherwise the preprocessor emits events.json first.
    if cfg.preprocess is None:
        runner_input = input_path
    else:
        runner_input = tmp / "events.json"
        subprocess.run(
            [sys.executable, str(cfg.preprocess), str(input_path), str(runner_input)],
            cwd=cfg.run_cwd, check=True, capture_output=True, text=True, timeout=STEP_TIMEOUT,
        )
    r = subprocess.run(
        [sys.executable, str(cfg.dsl_runner), str(cfg.dsl), str(runner_input),
         "--output", str(traces), "--coverage", str(coverage),
         *_require_operator(cfg)],
        cwd=cfg.run_cwd, capture_output=True, text=True, timeout=STEP_TIMEOUT,
    )
    # 3 is a converted run with a set-level loss (a trace not emitted, a
    # record skipped): drift is still measured on what was converted.
    if r.returncode not in (0, 3):
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    if not traces.exists():   # the one trace of a single-trace run was not emitted
        return [], json.loads(coverage.read_text())
    # Container adapters (claude-code/goose/opencode) emit a list of traces; a
    # single-trace adapter (pi) emits one trace object. Normalize to a list so the
    # fired-cell / coverage loops downstream always iterate over traces.
    out = json.loads(traces.read_text())
    return (out if isinstance(out, list) else [out]), json.loads(coverage.read_text())


def _unmapped_set(cov: dict) -> set[str]:
    """Flatten coverage `unmapped_paths_by_type` to a {rtype::path} string set."""
    out: set[str] = set()
    for rtype, paths in (cov.get("unmapped_paths_by_type") or {}).items():
        for p in paths:
            out.add(f"{rtype}::{p}")
    return out


def _absent_set(cov: dict) -> set[str]:
    return set(cov.get("consumed_but_absent_in_record") or [])


def fired_cells(traces: list[dict], cell_signals: dict) -> set[int]:
    """Union of contract cells that fired across all traces in one scenario run."""
    fired: set[int] = set()
    for cell, pred in cell_signals.items():
        if any(pred(tr) for tr in traces):
            fired.add(cell)
    return fired


# --- Raw-format surface -----------------------------------------------------

def read_jsonl(path: pathlib.Path) -> list[dict]:
    recs: list[dict] = []
    for line in re.split(r"\r\n|\r|\n", path.read_text()):  # not splitlines(): U+2028 is valid inside JSON strings
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if isinstance(obj, dict):
            recs.append(obj)
    return recs


def raw_records(input_path: pathlib.Path, cfg: DriftConfig) -> list[dict]:
    """All raw records for a scenario: the main input plus any harness extras."""
    reader = cfg.raw_reader or read_jsonl
    recs = reader(input_path)
    if cfg.extra_raw_records is not None:
        recs.extend(cfg.extra_raw_records(input_path))
    return recs


def raw_surface(records: list[dict], type_field: str) -> tuple[set[str], set[str]]:
    """Return (record_types, {rtype::path}) for a scenario's raw records.

    The `<kind>` suffix is stripped: a candidate is a fresh live capture with
    different output than the frozen baseline, so content-driven kind variance is
    expected and benign. The faithful Axis-C signal is a new field PATH or record
    TYPE the harness ships.
    """
    seen = collect_seen_paths(records, type_field=type_field)
    rtypes = set(seen.keys())
    paths = {f"{rt}::{_strip_path_kind(p)}" for rt, ps in seen.items() for p in ps}
    return rtypes, paths


# --- Drift ledger -----------------------------------------------------------
# A per-adapter, machine-readable record of drifts the watch has already
# surfaced and triaged (adapters/<h>/regression/drift-ledger.yaml; format in
# adapters/DRIFT-LEDGER.md). Each entry names the signal class and the concrete
# surface it covers, plus a verdict/disposition. A drift whose surface an entry
# covers downgrades from FATAL to an acknowledged line; a drift no entry covers
# still gates. This is the dissector-maintenance discipline: known, triaged
# format transitions are documented and dismissed without going blind to a
# genuinely NEW shape (an un-covered record type / path still fires).


def load_ledger(path: "pathlib.Path | None") -> list[dict]:
    """Load a drift ledger's entries, or [] if there is no ledger file."""
    if path is None or not path.is_file():
        return []
    import yaml  # always importable: the per-harness watcher runs under --with pyyaml
    doc = yaml.safe_load(path.read_text()) or {}
    return doc.get("entries") or []


def _entry_covers(entry: dict, signal: str, item: str) -> bool:
    """True if a ledger entry covers one drift item of a given signal class.

    `item` is a bare record type (new_record_type / dark_cell) or a
    "recordtype::path" string (new_unmapped / dropped_dependency). Matching is
    intentionally exact on the record type — opencode versions its event types in
    the type string itself (session.created.1), so a bumped or novel type does not
    match and correctly re-fires.
    """
    if entry.get("signal") != signal:
        return False
    m = entry.get("matches") or {}
    if item in (m.get("items") or []):
        return True
    rtype = item.split("::", 1)[0]
    if rtype in (m.get("record_types") or []):
        return True
    return any(item.startswith(p) for p in (m.get("path_prefixes") or []))


def _partition_by_ledger(signal: str, items: list[str],
                         ledger: list[dict]) -> "tuple[list[tuple[str, dict]], list[str]]":
    """Split drift items into (acknowledged, remaining). acknowledged pairs each
    covered item with the entry that covers it; remaining items are un-ledgered
    and still gate."""
    acknowledged: list[tuple[str, dict]] = []
    remaining: list[str] = []
    for it in items:
        hit = next((e for e in ledger if _entry_covers(e, signal, it)), None)
        (acknowledged.append((it, hit)) if hit else remaining.append(it))
    return acknowledged, remaining


# --- Main loop --------------------------------------------------------------

def run_drift(base_dir: pathlib.Path, candidate_inputs: pathlib.Path | None,
              cfg: DriftConfig) -> int:
    """Run the drift watch for one harness. base_dir holds manifest.json + inputs/."""
    try:
        _require_operator(cfg)   # before anything is read: a usage error, exit 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    manifest_path = base_dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"drift_watch: no manifest at {manifest_path}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())
    self_test = candidate_inputs is None

    mode = "SELF-TEST (candidate = frozen baseline)" if self_test else \
        f"DRIFT (candidate = {candidate_inputs})"
    print(f"drift-watch [{cfg.harness_name}] baseline={base_dir.name} "
          f"harness={manifest['harness_version']}")
    print(f"mode: {mode}\n")

    ledger = load_ledger(cfg.ledger)
    if ledger and cfg.ledger is not None:
        print(f"ledger: {len(ledger)} known-drift entr"
              f"{'y' if len(ledger) == 1 else 'ies'} from {cfg.ledger.name}\n")

    failures: list[str] = []
    for name, spec in manifest["scenarios"].items():
        base_input = (base_dir / spec["input"]).resolve()
        input_basename = pathlib.Path(spec["input"]).name
        if self_test:
            cand_input = base_input
        else:
            cand_input = (candidate_inputs / input_basename).resolve()
            # exists(), not is_file(): a scenario input may be a directory
            # (vibe: session dir of meta.json + messages.jsonl); the harness's
            # raw_reader and preprocessor own the shape underneath.
            if not cand_input.exists():
                failures.append(f"{name}: candidate capture missing at {cand_input}")
                continue

        try:
            with tempfile.TemporaryDirectory() as td:
                _, base_cov = run_adapter(base_input, pathlib.Path(td), cfg)
            with tempfile.TemporaryDirectory() as td:
                cand_traces, cand_cov = run_adapter(cand_input, pathlib.Path(td), cfg)
            base_recs = raw_records(base_input, cfg)
            cand_recs = raw_records(cand_input, cfg)
            base_rtypes, base_raw = raw_surface(base_recs, cfg.type_field)
            cand_rtypes, cand_raw = raw_surface(cand_recs, cfg.type_field)
        except subprocess.CalledProcessError as e:
            failures.append(f"{name}: adapter run failed\n{e.stderr}")
            continue
        except subprocess.TimeoutExpired as e:
            failures.append(f"{name}: adapter run timed out after {e.timeout:g} s")
            continue
        except (json.JSONDecodeError, OSError) as e:
            print(f"drift_watch: {name}: unreadable capture: {e}", file=sys.stderr)
            return 2

        problems: list[str] = []   # fatal (→ exit 1)
        warnings: list[str] = []   # surfaced, non-fatal
        acknowledged: list[tuple[str, dict]] = []   # ledger-covered (item, entry)

        def gate(signal: str, items: list[str], header: str) -> None:
            """A fatal signal, filtered through the ledger: covered items are
            acknowledged (non-gating), the rest become a fatal problem."""
            ack, remaining = _partition_by_ledger(signal, items, ledger)
            acknowledged.extend(ack)
            if remaining:
                problems.append(header + "\n      " + "\n      ".join(remaining))

        # (0) raw-format drift — upstream of the preprocessor.
        new_rtypes = sorted(cand_rtypes - base_rtypes)
        gate("new_record_type", new_rtypes,
             "NEW RAW RECORD TYPES (harness ships a record type the preprocessor "
             "does not parse):")
        new_raw = sorted(cand_raw - base_raw)
        if new_raw:
            warnings.append("new raw field(s) vs baseline (review — benign content "
                            f"variance or a real new native field):\n        "
                            + "\n        ".join(new_raw))

        # harness-specific arms (e.g. claude-code attachment-subtype rename)
        ctx = ArmContext(base_input, cand_input, base_recs, cand_recs,
                         cfg.dsl, self_test)
        for arm in cfg.extra_arms:
            arm_problems, arm_warnings = arm(ctx)
            problems.extend(arm_problems)
            warnings.extend(arm_warnings)

        # (1) dark cells — annotated cell that no longer fires
        expected = set(spec.get("annotated_cells_fired") or [])
        if not expected:
            problems.append("no annotated_cells_fired in manifest — scenario is "
                            "un-gated for dark-cell drift")
        got = fired_cells(cand_traces, cfg.cell_signals)
        dark = sorted(expected - got)
        gate("dark_cell", [str(c) for c in dark],
             f"DARK CELLS (expected in annotated_cells_fired, did not fire; "
             f"candidate fired {sorted(got)}):")
        extra = sorted(got - expected)
        if extra and self_test:
            problems.append(f"OVER-FIRE {extra}: extractor fires cells the manifest "
                            "does not annotate — reconcile annotated_cells_fired or "
                            "fix the predicate (self-test must be exact)")

        # (2) new unmapped native shapes (preprocessor-carried, DSL does not map)
        new_unmapped = sorted(_unmapped_set(cand_cov) - _unmapped_set(base_cov))
        gate("new_unmapped", new_unmapped,
             "NEW UNMAPPED PATHS (native shape the adapter is blind to):")

        # (3) dropped DSL dependency — field the adapter reads, now absent
        new_absent = sorted(_absent_set(cand_cov) - _absent_set(base_cov))
        gate("dropped_dependency", new_absent,
             "DROPPED DEPENDENCIES (DSL reads these; candidate corpus lacks them):")

        # roll up acknowledged (ledgered) drifts, grouped by entry, for reporting
        known_lines: list[str] = []
        by_entry: dict[str, list[str]] = {}
        entry_of: dict[str, dict] = {}
        for item, entry in acknowledged:
            eid = entry.get("id", "?")
            by_entry.setdefault(eid, []).append(item)
            entry_of[eid] = entry
        for eid in sorted(by_entry):
            e = entry_of[eid]
            known_lines.append(
                f"known-drift {eid} [{e.get('disposition', '?')}/"
                f"{e.get('verdict', '?')}]: {len(by_entry[eid])} item(s) covered "
                f"({e.get('signal', '?')})")

        if problems:
            block = f"{name}:\n  " + "\n  ".join(problems)
            for k in known_lines:
                block += f"\n  {k}"
            if warnings:
                block += "\n  warn: " + "\n  warn: ".join(warnings)
            failures.append(block)
        else:
            extra_note = f" (+ unannotated fired {extra})" if extra else ""
            print(f"  OK  {name}: cells {sorted(got)} fired, no new unmapped/absent"
                  f"{extra_note}")
            for k in known_lines:
                print(f"      {k}")
            for w in warnings:
                print(f"      warn: {w}")

    if failures:
        print("\nDRIFT DETECTED:\n" + "\n".join(failures), file=sys.stderr)
        print(
            "\nRemediation (Phase 3 plan): bump the adapter / add or adjust DSL rules "
            "to cover the new shape, re-capture, then re-freeze <ver>/manifest.json "
            "(content_hashes + annotated_cells_fired) and verify.py alongside.",
            file=sys.stderr,
        )
        return 1
    print(f"\nno drift across {len(manifest['scenarios'])} scenario(s)")
    return 0
