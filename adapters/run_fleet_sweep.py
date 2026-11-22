#!/usr/bin/env python3
"""Fleet sweep — the committed release-gate harness.

One entrypoint that runs every gate a release reviewer needs green:

  1. RUNGS   — every adapter's every regression rung verify.py
               (content_hash freeze + unmapped_path_count; each runs with
               cwd = its rung dir, exactly as documented there. Schema:
               a rung calls its own preprocess + dsl_runner directly rather
               than adapters/verify_run.py, so it carries its own schema gate
               (adapters/schema_gate.py, the validator verify_run.py uses).
               Wired 2026-09-25 into every rung, so a green RUNGS step
               is a schema claim. Before this, no rung schema-validated,
               a gap found in review.)
  2. SPINE   — every adapters/tests/test_*.py (includes jcs-oracle parity,
               minitrace-verify cross-language parity, dispatch predicates,
               the coverage walker). Every file goes through pytest, and the
               verdict is read from pytest's own counts, not from the exit
               code alone: a file passes only if it ran every test a
               collect-only pass found, at least one, and none failed, errored
               or was skipped (an expected failure counts as skipped; a
               non-strict xfail test that passes counts as passed). The run
               ignores an inherited PYTEST_ADDOPTS and PYTEST_PLUGINS.
               Running a pytest-style file as a plain script defines its test
               functions, calls none of them and exits 0; a
               file's own `__main__` runner was trusted until 2026-10-17, and
               a runner that ran nothing, a stub `__main__`, an all-skipped
               file and a runner skipping fixture-taking tests all read PASS.
               Self-runners stay for ad-hoc use; the sweep does
               not call them.
  3. LINT    — lint_dispatch over the shipped adapter DSL corpus.
  4. COVERAGE DECLS — every adapter with regression rungs must declare
               coverage.tested in its drift-ledger.yaml, and the declaration
               must equal the actual regression/<version>/ rung dirs
               (adapters/DRIFT-LEDGER.md "Coverage declaration").

Trace/DSL coverage has NO separate step: the per-rung unmapped_path_count assertion
gates the seen-vs-consumed surface per adapter, and test_coverage_walk.py
pins the walker. The Axis-C coverage arm belongs to drift_watch
deltas between rungs, which need a baseline/candidate pair and are not part
of the static sweep.

Discovery is dynamic (adapters/*/regression/*/verify.py, tests/test_*.py) —
a new adapter or rung joins the sweep by existing. Finding zero of either
is a sweep ERROR (exit 2), not a pass: an empty sweep proves nothing.

Run (needs the adapter venv for PyYAML + rfc8785 + jsonschema + pytest, and both Rust
binaries):
    .venv/bin/python adapters/run_fleet_sweep.py
    cargo build --release --manifest-path adapters/jcs-oracle/Cargo.toml
    cargo build --release --manifest-path adapters/minitrace-verify/Cargo.toml

Exit 0 = all gates green; 1 = at least one failure; 2 = preflight/discovery
error (missing deps, missing binaries, nothing discovered).
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import time

ADAPTERS_DIR = pathlib.Path(__file__).resolve().parent
PY = sys.executable
STEP_TIMEOUT = 600  # seconds; a hung gate is a failed gate, not a hung sweep

RUST_BINARIES = [
    ADAPTERS_DIR / "jcs-oracle" / "target" / "release" / "jcs-oracle",
    ADAPTERS_DIR / "minitrace-verify" / "target" / "release" / "minitrace-verify",
]


def spine_verdict(
    returncode: int, report: pathlib.Path, collected: int | None
) -> tuple[bool, str]:
    """Pass/fail for one spine file from pytest's exit code and JUnit report.

    The exit code alone is not enough: pytest exits 0 when every test is
    skipped, and after pytest.exit(returncode=0) in mid-run, when the report
    lists only the tests that ran. So the report's test count must equal the
    count of a separate collect-only pass (`collected`; None when that pass
    could not be read). A missing or unreadable report is a failure, not a
    pass. The zero-passed and no-testsuite branches are defensive: every
    zero-test shape probed on 2026-10-17 already exits nonzero.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError) as e:
        return False, f"no readable pytest report ({e}); exit {returncode}"
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        return False, f"pytest report has no testsuite; exit {returncode}"
    counts = {k: sum(int(s.get(k, 0)) for s in suites)
              for k in ("tests", "failures", "errors", "skipped")}
    passed = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    summary = (f"{passed} passed, {counts['failures']} failed, "
               f"{counts['errors']} errors, {counts['skipped']} skipped")
    if returncode != 0:
        return False, f"pytest exit {returncode}: {summary}"
    if collected is None:
        return False, f"collect-only count unreadable: {summary}"
    if counts["tests"] != collected:
        return False, f"ran {counts['tests']} of {collected} collected: {summary}"
    if counts["skipped"]:
        return False, f"skipped tests are not a pass: {summary}"
    if passed < 1:
        return False, f"no test executed: {summary}"
    return True, summary


def collected_count(stdout: str) -> int | None:
    """Sum of the `<file>: N` lines that `pytest --collect-only -qq` prints."""
    import re

    counts = [int(m.group(1)) for m in re.finditer(r"^\S.*: (\d+)$", stdout, re.M)]
    return sum(counts) if counts else None


def run_bounded(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output, text) waited on for STEP_TIMEOUT,
    with the step in a session of its own, so a timeout or an interrupt
    kills everything it started (a rung's pre-processor and dsl_runner),
    not only the direct child (ruled 2026-10-29); TimeoutExpired is raised
    as subprocess.run raised it."""
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415
    posix = os.name == "posix"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=posix, **kwargs)
    try:
        out, err = proc.communicate(timeout=STEP_TIMEOUT)
    except BaseException:
        try:
            if posix:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass   # gone, or (macOS) only an unreaped zombie leader is left
        for pipe in (proc.stdout, proc.stderr):
            if pipe:
                pipe.close()
        proc.wait()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run_spine(test: pathlib.Path, cwd: pathlib.Path) -> tuple[str, bool, float, str]:
    """Run one spine file through pytest and judge it by spine_verdict."""
    import os
    import tempfile

    label = f"spine {test.name}"
    # An inherited PYTEST_ADDOPTS ("-k", "--deselect") or PYTEST_PLUGINS can
    # drop failing tests before the report sees them; the gate runs without.
    # The same holds for configuration the run would pick up on its own, which
    # reaches the collect-only pass and the run alike: a pytest.ini,
    # pyproject.toml, tox.ini or setup.cfg (addopts, python_functions), a
    # conftest.py (pytest_collection_modifyitems) and an installed plugin's
    # pytest11 entry point. The gate runs on an empty ini of its own, without
    # conftest files and without plugin autoload.
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    start = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        ini = pathlib.Path(tmp) / "pytest.ini"
        ini.write_text("[pytest]\n", encoding="utf-8")
        base = [PY, "-m", "pytest", "-p", "no:cacheprovider",
                "-c", str(ini), "--rootdir", str(cwd), "--noconftest"]
        report = pathlib.Path(tmp) / "report.xml"
        try:
            co = run_bounded(base + ["--collect-only", "-qq", str(test)], cwd=cwd, env=env)
            collected = collected_count(co.stdout) if co.returncode == 0 else None
            proc = run_bounded(base + ["-q", f"--junitxml={report}", str(test)], cwd=cwd, env=env)
            ok, why = spine_verdict(proc.returncode, report, collected)
            tail = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            ok, why, tail = False, f"TIMEOUT after {STEP_TIMEOUT}s", ""
    dur = time.monotonic() - start
    print(f"  {'PASS' if ok else 'FAIL'}  {label}  ({dur:.1f}s; {why})")
    if not ok:
        for line in tail.strip().splitlines()[-15:]:
            print(f"        {line}")
    return label, ok, dur, why


def preflight() -> list[str]:
    problems = []
    # jsonschema: the rungs' schema gate reports a missing validator as a
    # failure, so without it every rung would fail for a reason unrelated to
    # the adapter. Catch it once here instead. pytest runs every spine file.
    for mod in ["yaml", "rfc8785", "jsonschema", "pytest"]:
        try:
            __import__(mod)
        except ImportError:
            if mod == "pytest":
                problems.append(
                    "python module 'pytest' not importable; every spine test "
                    "runs through it. Install it into the adapter venv "
                    "(adapters/requirements.txt)."
                )
            else:
                problems.append(
                    f"python module '{mod}' not importable — run under the adapter "
                    f"venv (.venv/bin/python) or: uv run --with pyyaml --with rfc8785 "
                    f"--with jsonschema --with pytest"
                )
    for binary in RUST_BINARIES:
        if not binary.is_file():
            rel = binary.relative_to(ADAPTERS_DIR.parent)
            manifest = binary.parents[2] / "Cargo.toml"
            problems.append(
                f"{rel} not built — cargo build --release "
                f"--manifest-path {manifest.relative_to(ADAPTERS_DIR.parent)}"
            )
    return problems


def discover() -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    rungs = sorted(ADAPTERS_DIR.glob("*/regression/*/verify.py"))
    tests = sorted((ADAPTERS_DIR / "tests").glob("test_*.py"))
    return rungs, tests


def check_coverage_decl(adapter_dir: pathlib.Path) -> tuple[bool, str]:
    """One adapter's coverage declaration vs its actual rung dirs."""
    import yaml

    rung_dirs = sorted(
        p.name for p in (adapter_dir / "regression").iterdir()
        if p.is_dir() and (p / "verify.py").is_file()
    )
    ledger = adapter_dir / "regression" / "drift-ledger.yaml"
    if not ledger.is_file():
        return False, f"rungs exist but no {ledger.name}"
    doc = yaml.safe_load(ledger.read_text()) or {}
    cov = doc.get("coverage")
    if not isinstance(cov, dict):
        return False, "no coverage: block in drift-ledger.yaml"
    tested = sorted(str(v) for v in (cov.get("tested") or []))
    if tested != rung_dirs:
        return False, f"coverage.tested {tested} != rung dirs {rung_dirs}"
    if not cov.get("policy"):
        return False, "coverage.policy missing"
    return True, ""


def run_step(label: str, cmd: list[str], cwd: pathlib.Path) -> tuple[str, bool, float, str]:
    start = time.monotonic()
    try:
        proc = run_bounded(cmd, cwd=cwd)
        ok, tail = proc.returncode == 0, (proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired:
        ok, tail = False, f"TIMEOUT after {STEP_TIMEOUT}s"
    dur = time.monotonic() - start
    status = "PASS" if ok else "FAIL"
    print(f"  {status}  {label}  ({dur:.1f}s)")
    if not ok:
        for line in tail.strip().splitlines()[-15:]:
            print(f"        {line}")
    return label, ok, dur, tail


def main() -> int:
    problems = preflight()
    if problems:
        print("fleet sweep: PREFLIGHT FAILED")
        for p in problems:
            print(f"  - {p}")
        return 2

    rungs, tests = discover()
    if not rungs or not tests:
        print(
            f"fleet sweep: DISCOVERY ERROR — {len(rungs)} rungs, "
            f"{len(tests)} spine tests found under {ADAPTERS_DIR} "
            "(an empty sweep proves nothing)"
        )
        return 2

    results = []

    print(f"[1/4] rungs ({len(rungs)})")
    for verify in rungs:
        rung_dir = verify.parent
        label = f"rung {rung_dir.parent.parent.name}/{rung_dir.name}"
        results.append(run_step(label, [PY, "verify.py"], cwd=rung_dir))

    print(f"[2/4] spine tests ({len(tests)})")
    for test in tests:
        results.append(run_spine(test, cwd=ADAPTERS_DIR.parent))

    print("[3/4] lint")
    results.append(
        run_step(
            "lint_dispatch",
            [PY, str(ADAPTERS_DIR / "lint_dispatch.py")],
            cwd=ADAPTERS_DIR.parent,
        )
    )

    rung_adapters = sorted({v.parent.parent.parent for v in rungs}, key=lambda p: p.name)
    print(f"[4/4] coverage declarations ({len(rung_adapters)})")
    for adapter_dir in rung_adapters:
        ok, why = check_coverage_decl(adapter_dir)
        status = "PASS" if ok else "FAIL"
        print(f"  {status}  coverage {adapter_dir.name}" + (f"  ({why})" if why else ""))
        results.append((f"coverage {adapter_dir.name}", ok, 0.0, why))

    failed = [label for label, ok, _, _ in results if not ok]
    total_dur = sum(dur for _, _, dur, _ in results)
    print(
        f"\nfleet sweep: {len(results) - len(failed)}/{len(results)} pass "
        f"({len(rungs)} rungs, {len(tests)} spine tests, 1 lint, "
        f"{len(rung_adapters)} coverage decls; {total_dur:.0f}s)"
    )
    if failed:
        for label in failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
