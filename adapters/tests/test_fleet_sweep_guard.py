"""The fleet sweep's spine verdict (run_fleet_sweep.run_spine).

Each case is a synthetic spine file run through run_spine itself, the path the
sweep takes. The four PASS-that-asserts-nothing shapes kill-tested on
2026-10-17 must read FAIL; a file with one passing test must read PASS, so
the guard is not vacuous in the other direction.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADAPTERS))

from run_fleet_sweep import run_spine, spine_verdict  # noqa: E402

CASES = {
    "pytest_fail": (False, "def test_a():\n    assert False\n"),
    "no_tests": (False, "X = 1\n"),
    "self_runner_runs_nothing": (False, (
        "import sys\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(0)\n"
    )),
    "stub_main_over_failing_test": (False, (
        "def test_a():\n    assert False\n"
        "if __name__ == '__main__':\n    pass\n"
    )),
    "all_skipped": (False, (
        "import pytest\n"
        "@pytest.mark.skip(reason='x')\n"
        "def test_a():\n    assert False\n"
    )),
    "one_skipped_one_passed": (False, (
        "import pytest\n"
        "def test_a():\n    assert True\n"
        "@pytest.mark.skip(reason='x')\n"
        "def test_b():\n    assert False\n"
    )),
    "xfail": (False, (
        "import pytest\n"
        "@pytest.mark.xfail\n"
        "def test_a():\n    assert False\n"
    )),
    "fixture_test_fails": (False, (
        "def test_a(tmp_path):\n    assert False\n"
    )),
    # One pass beside the failure, so the zero-passed check cannot stand in
    # for the exit-code check (reviewer mutation M1, 2026-10-17).
    "pass_and_fail": (False, (
        "def test_a():\n    assert True\n"
        "def test_b():\n    assert False\n"
    )),
    "pytest_exit_zero_mid_run": (False, (
        "import pytest\n"
        "def test_a():\n    assert True\n"
        "def test_b():\n    pytest.exit('stop', returncode=0)\n"
        "def test_c():\n    assert False\n"
    )),
    "one_passing_test": (True, "def test_a():\n    assert True\n"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_run_spine_verdict(name, tmp_path):
    expected, body = CASES[name]
    test = tmp_path / f"test_{name}.py"
    test.write_text(body, encoding="utf-8")
    _, ok, _, why = run_spine(test, cwd=tmp_path)
    assert ok is expected, f"{name}: {why}"


@pytest.mark.parametrize("addopts", ["-k test_a", "--deselect test_pf.py::test_b"])
def test_inherited_addopts_cannot_drop_a_failure(addopts, tmp_path, monkeypatch):
    test = tmp_path / "test_pf.py"
    test.write_text(CASES["pass_and_fail"][1], encoding="utf-8")
    monkeypatch.setenv("PYTEST_ADDOPTS", addopts)
    _, ok, _, why = run_spine(test, cwd=tmp_path)
    assert not ok, why


# A config file beside the spine file reaches the collect-only pass and the run
# alike, so a deselection there shrinks both counts and a failure reads PASS.
CONFIGS = {
    "pytest_ini_addopts": ("pytest.ini", "[pytest]\naddopts = -k test_a\n"),
    "pytest_ini_python_functions": ("pytest.ini", "[pytest]\npython_functions = test_a\n"),
    "pyproject_addopts": ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-k test_a"\n'),
    "tox_ini_addopts": ("tox.ini", "[pytest]\naddopts = -k test_a\n"),
    "setup_cfg_addopts": ("setup.cfg", "[tool:pytest]\naddopts = -k test_a\n"),
}

_DROP_B = (
    "def pytest_collection_modifyitems(config, items):\n"
    "    items[:] = [i for i in items if not i.name.endswith('test_b')]\n"
)


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_a_config_file_cannot_drop_a_failure(name, tmp_path):
    fname, body = CONFIGS[name]
    (tmp_path / fname).write_text(body, encoding="utf-8")
    test = tmp_path / "test_pf.py"
    test.write_text(CASES["pass_and_fail"][1], encoding="utf-8")
    _, ok, _, why = run_spine(test, cwd=tmp_path)
    assert not ok, f"{name}: {why}"


def test_a_conftest_cannot_drop_a_failure(tmp_path):
    (tmp_path / "conftest.py").write_text(_DROP_B, encoding="utf-8")
    test = tmp_path / "test_pf.py"
    test.write_text(CASES["pass_and_fail"][1], encoding="utf-8")
    _, ok, _, why = run_spine(test, cwd=tmp_path)
    assert not ok, why


def test_an_autoloaded_plugin_cannot_drop_a_failure(tmp_path, monkeypatch):
    # An installed pytest11 entry point, simulated by a dist-info on PYTHONPATH.
    site = tmp_path / "site"
    (site / "dropb-0.dist-info").mkdir(parents=True)
    (site / "dropb-0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: dropb\nVersion: 0\n", encoding="utf-8")
    (site / "dropb-0.dist-info" / "entry_points.txt").write_text("[pytest11]\ndropb = dropb_plugin\n", encoding="utf-8")
    (site / "dropb_plugin.py").write_text(_DROP_B, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(site))
    test = tmp_path / "work" / "test_pf.py"
    test.parent.mkdir()
    test.write_text(CASES["pass_and_fail"][1], encoding="utf-8")
    _, ok, _, why = run_spine(test, cwd=test.parent)
    assert not ok, why


def test_missing_report_is_a_failure(tmp_path):
    ok, why = spine_verdict(0, tmp_path / "absent.xml", 1)
    assert not ok, why


def test_a_timed_out_step_has_everything_it_started_killed(tmp_path, monkeypatch):
    # Ruled 2026-10-29: a fleet step runs in a session of its own and a
    # timeout kills its whole group, so a process the step started (a rung's
    # pre-processor or dsl_runner) does not keep running.
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415
    import time  # noqa: PLC0415
    import run_fleet_sweep  # noqa: PLC0415
    if os.name != "posix":
        pytest.skip("process groups are POSIX")
    monkeypatch.setattr(run_fleet_sweep, "STEP_TIMEOUT", 2)
    beat, pid = tmp_path / "beat", tmp_path / "pid"
    (tmp_path / "child.py").write_text(
        "import pathlib, sys, time\n"
        "while True:\n"
        "    with pathlib.Path(sys.argv[1]).open('a') as fh:\n"
        "        fh.write('x')\n"
        "    time.sleep(0.1)\n")
    (tmp_path / "step.py").write_text(
        "import subprocess, sys, time\n"
        f"c = subprocess.Popen([sys.executable, {str(tmp_path / 'child.py')!r}, {str(beat)!r}],\n"
        "                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pid)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(30)\n")
    try:
        label, ok, dur, tail = run_fleet_sweep.run_step("hang", [sys.executable, str(tmp_path / "step.py")], tmp_path)
        assert not ok and tail == "TIMEOUT after 2s" and dur < 10, (ok, tail, dur)
        size = beat.stat().st_size
        time.sleep(1)
        assert beat.stat().st_size == size, "a process the timed-out step started kept running"
    finally:
        if pid.exists():
            try:
                os.kill(int(pid.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass


def test_drift_core_and_every_rung_wait_on_their_steps_with_a_timeout(monkeypatch):
    # Ruled 2026-10-29: drift_core's pre-processor and dsl_runner runs, and
    # every subprocess.run of a rung's verify.py, carry a timeout, and a
    # rung reports a timed-out run as a failed scenario.
    import re  # noqa: PLC0415
    import drift_core  # noqa: PLC0415
    seen = []

    def recording(*args, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise drift_core.subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))
    monkeypatch.setattr(drift_core.subprocess, "run", recording)
    for preprocess in (None, _ADAPTERS / "goose" / "preprocess_goose.py"):
        cfg = type("Cfg", (), {"preprocess": preprocess, "run_cwd": _ADAPTERS, "dsl_runner": _ADAPTERS / "dsl_runner.py",
                               "dsl": _ADAPTERS / "goose" / "goose.dsl.yaml", "harness_name": "probe",
                               "operator_args": ("--operator-type", "scripted")})()
        with pytest.raises(drift_core.subprocess.TimeoutExpired):
            drift_core.run_adapter(_ADAPTERS / "absent.json", pathlib.Path("/nonexistent"), cfg)
    assert seen == [drift_core.STEP_TIMEOUT, drift_core.STEP_TIMEOUT], seen
    rungs = sorted(_ADAPTERS.glob("*/regression/*/verify.py"))
    assert rungs, "no rung verify.py found"
    for f in rungs:
        text = f.read_text()
        calls = re.findall(r"subprocess\.run\((.*?)\n    \)", text, re.S)
        assert calls and all("timeout=" in c for c in calls), (f, len(calls))
        assert "except subprocess.TimeoutExpired" in text, f
