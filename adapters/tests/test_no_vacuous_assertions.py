"""Lint: no assertion in adapters/tests/ that cannot fail.

An `assert` whose condition is true whatever the code under test does checks
nothing, and passes silently. The cases recognised here are the ones that
recur: a disjunction with an always-true operand (`assert x or True`), a bare
truthy constant (`assert True`, `assert "todo"`), and a non-empty tuple
(`assert (cond, "message")`, which Python evaluates as one truthy tuple).
The check is syntactic (ast), so it sees the assertion as written, not as
any particular run evaluates it.
"""
from __future__ import annotations

import ast
import pathlib

TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _always_true(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return bool(node.value)
    # A literal is non-empty only through an element that is not unpacked:
    # [*xs], {**d} and (*xs,) are empty when what they unpack is.
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(not isinstance(e, ast.Starred) for e in node.elts)
    if isinstance(node, ast.Dict):
        return any(k is not None for k in node.keys)
    if isinstance(node, ast.JoinedStr):   # f"msg {x}": its literal text makes it non-empty
        return any(isinstance(v, ast.Constant) and v.value for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _always_false(node.operand)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return isinstance(node.operand, ast.Constant) and bool(node.operand.value)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return any(_always_true(v) for v in node.values)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        return all(_always_true(v) for v in node.values)
    return False


def _always_false(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return not node.value
    if isinstance(node, (ast.Tuple, ast.List)):
        return len(node.elts) == 0
    if isinstance(node, ast.Dict):
        return len(node.keys) == 0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _always_true(node.operand)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        return any(_always_false(v) for v in node.values)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return all(_always_false(v) for v in node.values)
    return False


def vacuous_assertions(source: str, filename: str = "<string>") -> list[tuple[int, str]]:
    """(line, assertion text) of every assert in `source` that cannot fail."""
    tree = ast.parse(source, filename=filename)
    return [(n.lineno, ast.unparse(n.test)[:120])
            for n in ast.walk(tree)
            if isinstance(n, ast.Assert) and _always_true(n.test)]


def test_the_lint_recognises_each_vacuous_form():
    for src in ("assert x or True", "assert any(y) or True, 'm'", "assert True",
                "assert 'todo'", "assert 1", "assert (x == 1, 'message')",
                "assert x or (y or True)", "assert True and 1",
                "def f():\n    assert x or True\n",
                "assert x or f'msg {x}'", "assert not False", "assert not ()", "assert -1",
                "assert [1]", "assert {'a': 1}", "assert {1}", "assert x or [1]",
                "assert not (x and False)", "assert [*xs, 1]", "assert {**d, 'k': 1}"):
        assert len(vacuous_assertions(src)) == 1, src


def test_the_lint_accepts_assertions_that_can_fail():
    for src in ("assert x", "assert x or y", "assert x and True", "assert not True",
                "assert False", "assert ()", "assert x, (1, 'm')", "assert 0",
                "assert x == (1, 2)", "assert (x)", "assert f'{x}'", "assert not x",
                "assert -x", "assert []", "assert {}", "assert x or []", "assert not True",
                "assert x or {**d}", "assert [*xs]", "assert {*xs}", "assert (*xs,)"):
        assert vacuous_assertions(src) == [], src


def test_no_test_file_asserts_something_that_cannot_fail():
    files = sorted(TESTS_DIR.glob("*.py"))
    total = 0
    hits = []
    for f in files:
        source = f.read_text(encoding="utf-8")
        total += sum(isinstance(n, ast.Assert) for n in ast.walk(ast.parse(source)))
        hits += [f"{f.name}:{line}: assert {text}" for line, text in vacuous_assertions(source, str(f))]
    # Non-vacuous itself: an empty or unparsed directory must not pass.
    assert len(files) >= 20 and total >= 500, (len(files), total)
    assert hits == [], "\n".join(hits)


# A sibling lint: every subprocess call written in a test file has
# a timeout, so a hung child fails the test instead of stalling the sweep.
# Syntactic, as above: a subprocess.run/call/check_call/check_output call
# without a timeout= keyword (Popen takes none: its wait has the timeout).
# Subprocesses a library function starts for a test (verify_run.run) are
# outside this lint.

_SUBPROCESS_CALLS = ("run", "call", "check_call", "check_output")


def _untimed_subprocess_calls(tree: ast.AST) -> list[int]:
    out = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"
                and n.func.attr in _SUBPROCESS_CALLS
                and not any(k.arg == "timeout" for k in n.keywords)):
            out.append(n.lineno)
    return out


def test_the_untimed_subprocess_lint_finds_what_it_targets():
    src = ("import subprocess\nsubprocess.run(['x'])\nsubprocess.run(['x'], timeout=5)\n"
           "subprocess.check_output(['x'])\nsubprocess.call(['x'], timeout=1)\n")
    assert _untimed_subprocess_calls(ast.parse(src)) == [2, 4]


def test_every_subprocess_in_a_test_has_a_timeout():
    files = sorted(TESTS_DIR.glob("test_*.py"))
    assert len(files) >= 20, files
    found = {f.name: lines for f in files
             if (lines := _untimed_subprocess_calls(ast.parse(f.read_text(encoding="utf-8"))))}
    assert found == {}, found
