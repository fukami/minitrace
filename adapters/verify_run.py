#!/usr/bin/env python3
"""Per-adapter verify driver — the canonical minitrace adapter pipeline.

Runs the full pipeline a release reviewer should reproduce:

    preprocess (optional) -> dsl_runner -> single-trace cross_trace.back_fill ->
    split array into per-trace *.minitrace.json -> minitrace-verify --dir

Usage:
    verify_run.py <adapter.dsl.yaml> <input> [<preprocess.py>|-] --operator-type TYPE
                  [--operator-aware true|false] [--operator-capture-mode MODE]
                  [--operator-capture-purpose-legal-basis B --operator-capture-purpose-reference R
                   --operator-capture-purpose-authorised-at T]
                  [--allow-incomplete] [--diagnostics-out PATH] [--timeout SECONDS]

  <adapter.dsl.yaml>  the adapter DSL (e.g. adapters/pi/0.58.1.dsl.yaml)
  <input>             a native-format session corpus (file or dir, per adapter)
  <preprocess.py>     optional preprocessor; pass "-" to skip
  --operator-type     the operator assertion stamped on traces, REQUIRED (ruled
                      2026-10-20: no default, so a human session never becomes
                      scripted by omission); the other --operator-* options pass
                      the rest of the operator object to dsl_runner, which checks
                      what each type requires (DSL spec §7.1)

Outcome (ruled 2026-10-20, 2026-10-21): complete (every trace verified and
schema-valid, with no loss reported: no set-level or value-level loss
diagnostic from dsl_runner and no preprocessor warning), incomplete (a
set-level loss: a record skipped or a trace not emitted, or nothing
converted; a value-level loss: a dsl_runner diagnostic with loss "value";
or a preprocessor output line starting with "warning:", a loss whose level
the preprocessor does not state; or, in the fidelity record a preprocessor
writes beside its output (pre.fidelity.json), a skipped record or any read
finding, or a record that cannot be read; records the adapter discarded by
policy are not loss; whatever was emitted verified) or failed. Exit status: 0 complete; 3 incomplete, or 0
with --allow-incomplete (analysis mode, which still reports it); 1 failed;
2 a usage error. --diagnostics-out keeps the runner's diagnostics
(set_level_loss and its entries), the preprocessor's warnings
(preprocessor_warnings) and its fidelity record (preprocessor_fidelity,
null when it wrote none or it could not be read) after the temporary
workdir is removed; it holds null when the runner wrote none, the
preprocessor did not warn and wrote no fidelity record (a preprocessor
failure, say). Each step is killed after --timeout seconds
(600 by default), and a step that times out fails the run (ruled
2026-10-21). --diagnostics-out is written atomically;
a destination that is the input, the adapter or the preprocessor (by path,
symlink or hardlink), or lies inside an input directory, is a usage error
before anything runs, and a write that fails makes the run failed (exit 1).

Notes / gotchas this encodes:
  - dsl_runner emits a JSON ARRAY; minitrace-verify wants per-trace
    *.minitrace.json files and a --dir, so we split.
  - dsl_runner only auto-stamps content_hash for MULTI-trace adapters
    (the is_multi_trace gate). Single-trace adapters emit content_hash=null,
    so we run cross_trace.back_fill explicitly here.

All paths are relative to this file's location, so the driver is portable:
it expects minitrace-verify built at adapters/minitrace-verify/target/release/.
Build it with: (cd adapters/minitrace-verify && cargo build --release)
"""
import argparse
import dataclasses
import json
import math
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ADAPTERS_DIR = pathlib.Path(__file__).resolve().parent
PY = sys.executable
VERIFY = ADAPTERS_DIR / "minitrace-verify" / "target" / "release" / "minitrace-verify"


@dataclasses.dataclass
class VerifyResult:
    """One run's outcome: "complete", "incomplete" or "failed" (see the
    module docstring). diagnostics is the runner's diagnostics file
    (set_level_loss and its entries), read before the workdir is removed;
    None when the runner did not get as far as writing one. True only when
    complete, so `if run(...)` keeps its meaning."""
    outcome: str
    diagnostics: dict | None = None
    reasons: list[str] = dataclasses.field(default_factory=list)

    def __bool__(self) -> bool:
        return self.outcome == "complete"


STEP_TIMEOUT = 600   # seconds per step (ruled 2026-10-21)


class _StepTimedOut(Exception):
    def __init__(self, step: str, seconds: float, output: str = ""):
        super().__init__(f"{step} timed out after {seconds:g} s")
        self.output = output   # what the step printed before it was killed


def _step(info: dict, step: str, cmd: list[str]) -> subprocess.CompletedProcess:
    """One pipeline step, killed after info["timeout"] seconds (ruled
    2026-10-21: a step that times out fails the run). On POSIX the step runs
    in its own session, and a timeout kills its whole process group, so
    nothing it started outlives the run; the output it printed until then is
    kept on the exception. A step that exits but leaves a descendant holding
    its output open is waited on until the timeout too, and reads as timed
    out. Any other exception while waiting (Ctrl-C, which the step's own
    session no longer receives) also kills the group, then propagates.
    With info["step_tmp"] the step runs with TMPDIR set to it, so what a
    killed step leaves there (sqlite_source's store copy, removed only at
    exit) goes with the run (ruled 2026-10-28). On POSIX the group is also
    killed after a step that exits, so no descendant left in its process
    group keeps writing there; one that started a group or session of its
    own is out of reach (ruled 2026-10-28)."""
    posix = os.name == "posix"
    env = {**os.environ, "TMPDIR": info["step_tmp"]} if info.get("step_tmp") else None
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=posix, env=env)

    def kill_group() -> None:
        try:
            if posix:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            # Gone, or (macOS: EPERM) only an unreaped zombie leader is left
            # in the group, as when the step exited and a descendant in a
            # session of its own holds the pipes; it is reaped below.
            pass

    try:
        out, err = proc.communicate(timeout=info["timeout"])
    except subprocess.TimeoutExpired:
        kill_group()
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired as e:
            # A descendant left the group and holds the pipes: keep what
            # was read so far, then stop reading.
            out, err = _text(e.output), _text(e.stderr)
            proc.kill()
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()
            proc.wait()
        raise _StepTimedOut(step, info["timeout"], (out or "") + "\n" + (err or "")) from None
    except BaseException:
        kill_group()
        proc.wait()
        raise
    if posix:
        kill_group()
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _remove_step_tmp(path: str) -> None:
    """Remove the run's step directory, retrying while something still
    writes into it; one that cannot be removed is a warning line, never an
    exception that would replace the run's result (ruled 2026-10-28)."""
    err: OSError | None = None
    for _ in range(5):
        try:
            shutil.rmtree(path)
            return
        except OSError as e:
            # An entry that vanished under rmtree (older Pythons raise for
            # it) is not the directory being gone.
            if not os.path.lexists(path):
                return
            err = e
            time.sleep(0.2)
    if os.path.lexists(path):
        print(f"  warning: step directory {path} not removed ({err})")


def _workdir_in_input(workdir: pathlib.Path, inp) -> str | None:
    """"the same as" when `workdir` is the input directory, "inside" when
    it lies in one, else None; by device and inode as
    diagnostics_out_problem compares, so a case variant on a
    case-insensitive file system is caught too; else None."""
    try:
        if not os.path.isdir(inp):
            return None
        here = os.stat(inp)
        target = pathlib.Path(workdir).resolve()
        for n, a in enumerate((target, *target.parents)):
            try:
                st = os.stat(a)
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == (here.st_dev, here.st_ino):
                return "inside" if n else "the same as"
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def _text(b) -> str:
    """TimeoutExpired's output so far: bytes even with text=True."""
    if b is None:
        return ""
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else b


def run(adapter_dsl, inp, preprocess=None, *, operator_type, operator_aware=None,
        capture_mode=None, capture_purpose=None, workdir=None, timeout=STEP_TIMEOUT) -> VerifyResult:
    """operator_type is required and has no default (ruled 2026-10-20);
    operator_aware, capture_mode and capture_purpose (a mapping of
    legal_basis, reference and authorised_at) complete the operator object
    and go to dsl_runner, which checks what the type requires. Without a
    workdir, intermediates (converted session data) go to a temporary
    directory that is removed afterwards, not left in $TMPDIR. Each step
    (preprocessor, dsl_runner, minitrace-verify) is killed after `timeout`
    seconds, and the run is then failed (ruled 2026-10-21). Every step runs
    with TMPDIR set to a directory of the run's own, never the workdir,
    removed when the run ends, whatever its outcome, so a step killed before
    its exit handlers ran leaves nothing in $TMPDIR either (ruled
    2026-10-28). Not covered: verify_run itself killed; a step's
    descendant in a process group or session of its own that keeps writing
    there, or a directory a step leaves that cannot be removed, each of
    which may leave the directory, with a warning line (outcome unchanged)."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0:
        raise ValueError(f"timeout must be a positive number of seconds, got {timeout!r}")
    if operator_aware is not None and not isinstance(operator_aware, bool):
        # A consent assertion is never coerced: "false" is truthy.
        raise TypeError(f"operator_aware must be a bool or None, got {type(operator_aware).__name__}")
    operator_args = ["--operator-type", operator_type]
    if operator_aware is not None:
        operator_args += ["--operator-aware", "true" if operator_aware else "false"]
    if capture_mode is not None:
        operator_args += ["--operator-capture-mode", capture_mode]
    for key in ("legal_basis", "reference", "authorised_at"):
        if capture_purpose and capture_purpose.get(key) is not None:
            operator_args += [f"--operator-capture-purpose-{key.replace('_', '-')}", capture_purpose[key]]
    info: dict = {"warnings": [], "runner_exit": None, "timeout": timeout, "diagnostics": None,
                  "fidelity": None, "workdir_given": workdir is not None}
    step_tmp = info["step_tmp"] = tempfile.mkdtemp(prefix="verify-steps-")
    try:
        if workdir is None:
            with tempfile.TemporaryDirectory(prefix="verify-") as tmp:
                result = _run(adapter_dsl, inp, preprocess, operator_args, tmp, info)
        else:
            result = _run(adapter_dsl, inp, preprocess, operator_args, workdir, info)
    except _StepTimedOut as e:
        print(f"  TIMED OUT: {e}")
        result = VerifyResult("failed", info["diagnostics"], [str(e)])
    finally:
        _remove_step_tmp(step_tmp)
    _account_value_loss(result, info["warnings"], info["runner_exit"], info["fidelity"])
    print(f"  outcome: {result.outcome}" + (f" ({'; '.join(result.reasons)})" if result.reasons else ""))
    return result


def _account_value_loss(result: VerifyResult, pre_warnings: list[str], runner_exit: int | None,
                        fidelity: tuple[dict | None, str | None] | None = None) -> None:
    """Ruled 2026-10-21: a value-level loss diagnostic from dsl_runner and a
    preprocessor warning each make a run that would be complete incomplete,
    with the source and count in the reasons (and the level for the
    runner's; a preprocessor warning does not say whether it lost values or
    records); a failed run stays failed. The preprocessor's warnings go into
    the diagnostics under preprocessor_warnings ([] when there are none), and
    the diagnostics are an object whenever the preprocessor warned, even if
    the runner wrote none; set_level_loss is then true when the runner exited
    3, else null.

    `fidelity` is _read_fidelity's (record, problem), or None when the
    preprocessor wrote no fidelity record. Ruled 2026-10-21: a skipped
    record (a set-level loss) or any read finding in it, or a record that
    cannot be read, makes the run incomplete; discarded records are adapter
    policy, not loss, and chain findings are carried, not counted. The
    record goes into the diagnostics under preprocessor_fidelity (null when
    there is none or it could not be read)."""
    record, problem = fidelity if fidelity is not None else (None, None)
    if result.diagnostics is not None:
        result.diagnostics["preprocessor_warnings"] = list(pre_warnings)
        result.diagnostics["preprocessor_fidelity"] = record
    elif pre_warnings or fidelity is not None:
        result.diagnostics = {"set_level_loss": True if runner_exit == 3 else None, "diagnostics": None,
                              "preprocessor_warnings": list(pre_warnings),
                              "preprocessor_fidelity": record}
    if result.outcome == "failed":
        return
    entries = (result.diagnostics or {}).get("diagnostics")
    value = sum(1 for d in entries if isinstance(d, dict) and d.get("loss") == "value") \
        if isinstance(entries, list) else 0
    added = ([f"value-level loss: {value} diagnostic(s) from dsl_runner"] if value else []) \
        + ([f"loss: {len(pre_warnings)} preprocessor warning(s)"] if pre_warnings else [])
    if problem:
        added.append(f"loss unknown: the preprocessor's fidelity record {problem}")
    elif record is not None:
        skipped = sum(s["record_count_skipped"] for s in record["sources"])
        findings = len(record["read_findings"])
        added += ([f"set-level loss: {skipped} record(s) skipped in the preprocessor's fidelity record"]
                  if skipped else []) \
            + ([f"loss: {findings} read finding(s) in the preprocessor's fidelity record"] if findings else [])
    if added:
        result.outcome = "incomplete"
        result.reasons = result.reasons + added


FIDELITY_MAX_BYTES = 16 * 1024 * 1024
# A count at or above this lacks the counts (ruled 2026-10-21): the int64
# range, and a sum over the capped counts always formats.
FIDELITY_MAX_COUNT = 2 ** 63


def diagnostics_text(diagnostics: dict | None) -> str:
    """What --diagnostics-out holds: standard JSON only, so a non-finite
    value raises ValueError rather than being written as NaN or Infinity."""
    return json.dumps(diagnostics, indent=2, allow_nan=False) + "\n"


def _refuse_constant(name: str):
    raise ValueError(f"{name} is not JSON")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} has no finite value")
    return value


def _read_fidelity(path: pathlib.Path) -> tuple[dict | None, str | None] | None:
    """The fidelity record a preprocessor wrote beside its output: None when
    there is none, else (record, None), or (None, what is wrong) when it
    cannot be read or lacks the counts the loss accounting reads (no
    sources, a source without an integer record_count_skipped from 0 below
    FIDELITY_MAX_COUNT,
    read_findings not a list; NaN, Infinity or a number with no finite
    binary64 value, anywhere, is not JSON). Opened without following a
    symlink and without blocking, read only if it is a regular file of at
    most FIDELITY_MAX_BYTES."""
    import stat  # noqa: PLC0415
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        return None, f"could not be opened ({e.strerror or e.__class__.__name__})"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "is not a regular file"
        with os.fdopen(fd, "rb", closefd=True) as f:
            fd = -1
            data = f.read(FIDELITY_MAX_BYTES + 1)
    except OSError as e:
        return None, f"could not be read ({e.strerror or e.__class__.__name__})"
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > FIDELITY_MAX_BYTES:
        return None, f"is larger than {FIDELITY_MAX_BYTES} bytes"
    try:
        # NaN and Infinity are not JSON, and would reach --diagnostics-out as
        # such; 1e999 parses to inf through parse_float, not parse_constant.
        record = json.loads(data.decode("utf-8"), parse_constant=_refuse_constant,
                            parse_float=_finite_float)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "is not JSON"
    sources = record.get("sources") if isinstance(record, dict) else None
    if not isinstance(sources, list) or not sources or not isinstance(record.get("read_findings"), list) or any(
            not isinstance(s, dict) or isinstance(s.get("record_count_skipped"), bool)
            or not isinstance(s.get("record_count_skipped"), int)
            or not 0 <= s["record_count_skipped"] < FIDELITY_MAX_COUNT
            for s in sources):
        return None, "lacks the counts it must carry"
    return record, None


def _read_diagnostics(path: pathlib.Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _preprocessor_warnings(output: str) -> list[str]:
    """Every line of a preprocessor's output that starts, after whitespace,
    with `warning:` (ruled 2026-10-21: each one reports input not carried
    into the trace)."""
    return [line.strip() for line in output.splitlines() if line.lstrip().startswith("warning:")]


def _run(adapter_dsl, inp, preprocess, operator_args, workdir, info: dict) -> VerifyResult:
    """info["warnings"] collects the preprocessor's warning lines and
    info["runner_exit"] dsl_runner's exit status, for run() to account."""
    pre_warnings: list[str] = info["warnings"]
    if not VERIFY.exists():
        print(f"  minitrace-verify not built at {VERIFY}")
        print("  build it: (cd adapters/minitrace-verify && cargo build --release)")
        return VerifyResult("failed", reasons=["minitrace-verify not built"])
    workdir = pathlib.Path(workdir)
    # A workdir that is an input directory, or lies in one, would have its
    # intermediates replace or remove files of the corpus being verified:
    # refused before anything is written or removed.
    # Only a workdir the caller gave: the run's own temporary one is new and
    # empty, wherever the input lies.
    if info.get("workdir_given", True) and (inside := _workdir_in_input(workdir, inp)) is not None:
        print(f"  FAILED: the workdir is {inside} the input directory")
        return VerifyResult("failed", reasons=[f"the workdir is {inside} the input directory, whose files a "
                                               f"run would replace; nothing was written"])
    workdir.mkdir(parents=True, exist_ok=True)
    src = inp
    shipped_pre = sorted(pathlib.Path(adapter_dsl).resolve().parent.glob("preprocess_*.py"))
    if not preprocess and shipped_pre:
        # Most adapters read the preprocessor's container, not the native file;
        # without it the DSL runs on the wrong shape (gemini-cli: every trace
        # lacks its id and back_fill raises). Say so before that happens.
        print(f"  note: this adapter ships {shipped_pre[0].name} and none was given; "
              f"pass it as the 3rd argument, with the input it reads (AgentDojo: the runs "
              f"directory), unless the DSL reads native input directly")
    if preprocess and not pathlib.Path(preprocess).is_file():
        # Also catches an operator type left in the preprocess slot.
        print(f"  PREPROCESS FAILED: no preprocessor file {preprocess}")
        return VerifyResult("failed", reasons=[f"preprocessor not found: {preprocess}"])
    if preprocess:
        pre = workdir / "pre.json"
        # Where a preprocessor writes its fidelity record (claude-code does).
        # A reused workdir must not lend this run an earlier run's record,
        # nor an earlier run's output (ruled 2026-10-29).
        fidelity_path = pre.with_suffix(".fidelity.json")
        # The input, the adapter or the preprocessor kept at one of those
        # names is the caller's file, not an earlier run's: refused before
        # anything is removed.
        for earlier in (pre, fidelity_path):
            for name, given in (("input", inp), ("adapter", adapter_dsl), ("preprocessor", preprocess)):
                try:
                    clash = os.path.exists(earlier) and os.path.exists(given) and os.path.samefile(earlier, given)
                except OSError:
                    clash = False
                if clash:
                    print(f"  PREPROCESS FAILED: the {name} is the workdir's {earlier.name}")
                    return VerifyResult("failed", reasons=[f"the {name} is the workdir's {earlier.name}, "
                                                           f"which a run replaces; nothing was removed"])
        for earlier, what in ((pre, "preprocessor output"), (fidelity_path, "fidelity record")):
            try:
                earlier.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                return VerifyResult("failed", reasons=[f"an earlier {what} could not be removed from "
                                                       f"the workdir ({e.strerror or e.__class__.__name__})"])
        try:
            r = _step(info, "the preprocessor", [PY, str(preprocess), str(inp), str(pre)])
        except _StepTimedOut as e:
            # What it reported before it was killed is kept (ruled 2026-10-21).
            pre_warnings.extend(_preprocessor_warnings(e.output))
            raise
        tail = (r.stdout + r.stderr).strip().splitlines()
        print("  preprocess:", tail[-1] if tail else "ok")
        pre_warnings.extend(_preprocessor_warnings(r.stdout + "\n" + r.stderr))
        if pre_warnings:
            print(f"  preprocessor warnings: {len(pre_warnings)} (loss)")
            for w in pre_warnings[:10]:
                print("    WARN:", w)
        info["fidelity"] = _read_fidelity(fidelity_path)
        if info["fidelity"] is not None:
            record, problem = info["fidelity"]
            if problem:
                print(f"  preprocessor fidelity record {problem} (loss unknown)")
            else:
                skipped = sum(s["record_count_skipped"] for s in record["sources"])
                findings = len(record["read_findings"])
                print(f"  preprocessor fidelity record: skipped={skipped} read_findings={findings}"
                      + (" (loss)" if skipped or findings else ""))
        if r.returncode != 0:
            print("  PREPROCESS FAILED")
            return VerifyResult("failed", reasons=[f"preprocessor exited {r.returncode}"])
        if not os.path.lexists(pre):
            print("  PREPROCESS FAILED: no output")
            return VerifyResult("failed", reasons=["the preprocessor exited 0 and wrote no output"])
        src = pre
    out = workdir / "traces.json"
    cov = workdir / "cov.json"
    r = _step(info, "dsl_runner", [PY, str(ADAPTERS_DIR / "dsl_runner.py"), str(adapter_dsl), str(src),
                                   *operator_args, "--output", str(out), "--coverage", str(cov)])
    diag = [l for l in (r.stdout + r.stderr).splitlines()
            if any(w in l.lower() for w in ("warning", "diagnostic", "error"))]
    # Read now: a temporary workdir is gone by the time the caller looks.
    diagnostics = _read_diagnostics(workdir / f"{out.name}.diagnostics.json")
    info["diagnostics"] = diagnostics   # kept if a later step times out
    info["runner_exit"] = r.returncode
    lossy = r.returncode == 3
    entries = (diagnostics or {}).get("diagnostics")
    # Source and count, as for the value-level reasons (ruled 2026-10-21).
    loss_reasons = ([f"set-level loss: {sum(1 for d in entries if isinstance(d, dict) and d.get('loss') == 'set')}"
                     f" diagnostic(s) from dsl_runner"] if isinstance(entries, list)
                    else ["set-level loss from dsl_runner (its diagnostics file unreadable)"])
    if lossy:
        # Converted, but a trace was not emitted or a record was skipped.
        print("  SET-LEVEL LOSS: see the diagnostics (in the result; --diagnostics-out keeps them)")
        if not out.exists():
            print("  no trace was emitted")
            return VerifyResult("incomplete", diagnostics, loss_reasons + ["no trace was emitted"])
    elif r.returncode != 0:
        print("  DSL_RUNNER FAILED:", (r.stdout + r.stderr)[-500:])
        if not preprocess and shipped_pre:
            print(f"  likely cause: the DSL expects {shipped_pre[0].name}'s output; "
                  f"re-run with it as the 3rd argument")
        return VerifyResult("failed", diagnostics, [f"dsl_runner exited {r.returncode}"])
    if not out.exists():
        # Exit 0 with no trace: nothing to convert (DSL rc3 §15). Nothing was
        # verified, so not complete (ruled 2026-10-20).
        print("  no trace written (nothing to convert); nothing to verify")
        return VerifyResult("incomplete", diagnostics, ["nothing converted"])
    try:
        traces = json.loads(out.read_text())
    except (OSError, ValueError) as e:
        return VerifyResult("failed", diagnostics, [f"the runner's output unreadable ({e.__class__.__name__})"])
    if not isinstance(traces, list):
        traces = [traces]
    if not traces:
        # Container and multi-trace mode write [] when every trace is
        # dropped: the same as no output, not a verify of nothing.
        print("  no trace was emitted")
        if lossy:
            return VerifyResult("incomplete", diagnostics, loss_reasons + ["no trace was emitted"])
        return VerifyResult("incomplete", diagnostics, ["nothing converted"])
    sys.path.insert(0, str(ADAPTERS_DIR))
    from cross_trace import back_fill_cross_trace_references
    if any(not isinstance(t, dict) for t in traces):
        return VerifyResult("failed", diagnostics, ["the runner's output holds a trace that is not an object"])
    if any(t.get("content_hash") is None for t in traces):
        try:
            traces = back_fill_cross_trace_references(traces)
        except (ValueError, RuntimeError, TypeError, KeyError, AttributeError) as e:
            from input_tree import field  # noqa: PLC0415
            print(f"  BACK_FILL FAILED: {field(e)}")
            if not preprocess and shipped_pre:
                print(f"  likely cause: the DSL expects {shipped_pre[0].name}'s output; "
                      f"re-run with it as the 3rd argument")
            return VerifyResult("failed", diagnostics, ["cross-trace back-fill failed"])
    splitdir = workdir / "split"
    splitdir.mkdir(exist_ok=True)
    # A reused workdir must not verify an earlier run's extra traces.
    for stale in splitdir.glob("trace-*.minitrace.json"):
        stale.unlink()
    for i, t in enumerate(traces):
        (splitdir / f"trace-{i}.minitrace.json").write_text(json.dumps(t))

    # SCHEMA GATE.
    # minitrace-verify checks content-hash and lineage integrity and performs no
    # JSON-schema validation, so before this every "verify green" in the fleet
    # was silent about schema validity — which is how a schema-invalid
    # claim_type shipped behind a green pipeline on 2026-08-31.
    #
    # A MISSING jsonschema FAILS THE GATE rather than skipping it. Skipping
    # would restore precisely the state this closes: a green that does not mean
    # what a reader takes it to mean. Install it into the invocation:
    #   uv run --with pyyaml --with rfc8785 --with jsonschema python ...
    schema_errors: list[str] = []
    try:
        import importlib.util
        vspec = importlib.util.spec_from_file_location(
            "minitrace_schema_validate",
            ADAPTERS_DIR.parent / "schemas" / "validate.py",
        )
        vmod = importlib.util.module_from_spec(vspec)
        vspec.loader.exec_module(vmod)
    except Exception as e:  # noqa: BLE001 — any import failure is a failed gate
        schema_errors.append(f"schema validator unavailable ({e.__class__.__name__}: {e})")
        vmod = None
    if vmod is not None:
        for i, t in enumerate(traces):
            try:
                errs, _warns = vmod.validate_doc(t)
            except Exception as e:  # noqa: BLE001 — a validator crash is a failed gate
                errs = [f"validator raised {e.__class__.__name__}: {e}"]
            schema_errors.extend(f"trace-{i}: {e}" for e in errs)
    print(f"  schema={'FAIL' if schema_errors else 'ok'} "
          f"({len(schema_errors)} error(s) over {len(traces)} trace(s))")
    for e in schema_errors[:10]:
        print("    SCHEMA:", e)

    # --strict: the verdict requires every trace and edge to have been verified,
    # not merely no mismatch among the ones that could be checked.
    vr = _step(info, "minitrace-verify", [str(VERIFY), "--strict", "--dir", str(splitdir)])
    unmapped = 0
    try:
        c = json.loads(cov.read_text())
        ub = c.get("unmapped_paths_by_type", {})
        unmapped = sum(len(v) for v in ub.values()) if isinstance(ub, dict) else 0
    except Exception:
        pass
    print(f"  traces={len(traces)}  verify_exit={vr.returncode}  "
          f"unmapped={unmapped}  diagnostics={len(diag)} (advisory)")
    for d in diag[:10]:
        print("    DIAG:", d.strip())
    vout = (vr.stdout + vr.stderr).strip()
    print("  verify:", (vout[-800:] if vr.returncode != 0
                         else (vout.splitlines()[-1] if vout else "ok")))
    # Release bar is integrity AND schema. minitrace-verify exit 0 means the
    # content_hash reproduces over the JCS-canonical body and lineage edges
    # bind; the schema gate above means the trace validates against the base
    # schema plus its trace_type schema (spec §13). Before 2026-08-31 only the
    # first half existed and the comment here said so; both are now required,
    # so a green from this driver covers both claims and nothing more.
    # Coverage diagnostics (unmapped fields, catch-all warnings) stay advisory
    # drift signal, not release blockers.
    #
    # The regression rungs under adapters/*/regression/*/ run their own
    # verify.py and do not call this driver; they carry the same check through
    # adapters/schema_gate.py (wired 2026-09-25 into all 23).
    #
    # A set-level loss makes a verified run incomplete, never complete: the
    # traces that were emitted check out, but the conversion is not whole.
    reasons = ([f"minitrace-verify exited {vr.returncode}"] if vr.returncode != 0 else []) \
        + ([f"{len(schema_errors)} schema error(s)"] if schema_errors else [])
    if reasons:
        return VerifyResult("failed", diagnostics, reasons)
    if lossy:
        return VerifyResult("incomplete", diagnostics, loss_reasons)
    return VerifyResult("complete", diagnostics)


def _positive_seconds(s: str) -> float:
    import math  # noqa: PLC0415
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {s!r}") from None
    if not (v > 0 and math.isfinite(v)):
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {s!r}")
    return v


def diagnostics_out_problem(dest, inp, adapter_dsl, preprocess=None) -> str | None:
    """Why --diagnostics-out may not write to dest, or None (ruled
    2026-10-21): dest must not be the input, the adapter DSL or the
    preprocessor, by path, symlink or hardlink, nor lie inside an input
    directory, where it would join the corpus being verified."""
    dest = pathlib.Path(dest)
    sources = [("input", inp), ("adapter", adapter_dsl)]
    if preprocess is not None:
        sources.append(("preprocessor", preprocess))
    try:
        target = dest.resolve()
    except (OSError, RuntimeError) as e:
        return f"--diagnostics-out {dest}: cannot be resolved ({e})"
    for name, src in sources:
        src = pathlib.Path(src)
        try:
            # samefile follows symlinks and compares device and inode, so
            # a path, a symlink and a hardlink to the source all match.
            if dest.exists() and src.exists() and dest.samefile(src):
                return f"--diagnostics-out {dest} is the {name} ({src}); it would be overwritten"
        except (OSError, RuntimeError):
            continue
    # Inside an input directory: each existing ancestor of the target is
    # compared by device and inode, not by string, so a case variant on a
    # case-insensitive file system (CORPUS/ for corpus/) is caught too.
    inp = pathlib.Path(inp)
    try:
        if inp.is_dir():
            here = os.stat(inp)
            for a in (target, *target.parents):
                try:
                    st = os.stat(a)
                except OSError:
                    continue
                if (st.st_dev, st.st_ino) == (here.st_dev, here.st_ino):
                    return f"--diagnostics-out {dest} lies inside the input directory ({inp})"
    except (OSError, RuntimeError):
        pass
    return None


def write_atomically(dest, text: str) -> None:
    """Write text to dest through a temporary file in its directory and
    os.replace, so dest holds the old content or the new, never part of
    either (ruled 2026-10-21). A symlink at dest is replaced, not written
    through, and a hardlinked dest is split from its other names. An
    existing dest must be writable (for a symlink, the file it names, as
    write_text judged it; root passes) and keeps its permission bits (not its
    owner, ACLs, extended attributes or setuid/setgid/sticky); a new one
    gets the mode write_text would give. The directory needs write
    permission. Raises OSError; the temporary file is removed on failure.
    diagnostics_out_problem runs before the conversion, so a directory
    renamed or replaced by a symlink in between is not seen, nor is dest
    made read-only between the writability check and the replace (accepted,
    ruled 2026-10-21: whoever can do that can change the input too)."""
    dest = pathlib.Path(dest)
    # Ruled 2026-10-21: an existing destination that is not writable fails
    # as write_text did (a read-only file is a guard), and only the
    # permission bits carry over, not setuid, setgid or sticky.
    if dest.exists() and not os.access(dest, os.W_OK):
        raise PermissionError(f"{dest} is not writable")
    try:
        mode = os.stat(dest).st_mode & 0o777       # an existing file keeps its permissions
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask                      # a new one gets write_text's
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    from input_tree import install_output_sanitiser
    install_output_sanitiser()
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0],
                                 epilog="Exit status: 0 complete; 3 incomplete (0 with "
                                        "--allow-incomplete); 1 failed; 2 a usage error.")
    ap.add_argument("adapter_dsl")
    ap.add_argument("input")
    ap.add_argument("preprocess", nargs="?", default="-", help='a preprocessor, or "-" for none')
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
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="analysis mode: an incomplete conversion exits 0, still reported as such")
    ap.add_argument("--diagnostics-out", type=pathlib.Path,
                    help="write the runner's diagnostics (set_level_loss and its entries), the "
                         "preprocessor's warnings (preprocessor_warnings) and its fidelity record "
                         "(preprocessor_fidelity) here; null when the runner wrote none, the "
                         "preprocessor did not warn and wrote no fidelity record")
    ap.add_argument("--timeout", type=_positive_seconds, default=STEP_TIMEOUT, metavar="SECONDS",
                    help=f"kill a step (preprocessor, dsl_runner, minitrace-verify) after this long; "
                         f"the run is then failed (default {STEP_TIMEOUT})")
    args = ap.parse_args()
    if args.diagnostics_out is not None:
        problem = diagnostics_out_problem(args.diagnostics_out, args.input, args.adapter_dsl,
                                          None if args.preprocess == "-" else args.preprocess)
        if problem:
            ap.error(problem)
    purpose = {k: getattr(args, f"operator_capture_purpose_{k}")
               for k in ("legal_basis", "reference", "authorised_at")}
    result = run(args.adapter_dsl, args.input, None if args.preprocess == "-" else args.preprocess,
                 operator_type=args.operator_type,
                 operator_aware=None if args.operator_aware is None else args.operator_aware == "true",
                 capture_mode=args.operator_capture_mode,
                 capture_purpose={k: v for k, v in purpose.items() if v is not None} or None,
                 timeout=args.timeout)
    if args.diagnostics_out is not None:
        # Always written, null when the runner wrote none, so an earlier
        # run's file at that path is never read as this one's.
        # A failed write fails the run (ruled 2026-10-21): the record asked
        # for was not kept, whatever the conversion's outcome.
        try:
            write_atomically(args.diagnostics_out, diagnostics_text(result.diagnostics))
        except (OSError, ValueError) as e:
            print(f"  could not write --diagnostics-out: {e}")
            print("  outcome: failed (the diagnostics were not written)")
            sys.exit(1)
    if result.outcome == "complete":
        sys.exit(0)
    if result.outcome == "incomplete":
        if args.allow_incomplete:
            print("  INCOMPLETE (analysis mode, --allow-incomplete): exit 0")
            sys.exit(0)
        sys.exit(3)
    sys.exit(1)
