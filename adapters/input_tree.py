"""Which files found by walking an input directory may be read.

A pre-processor that walks a harness's session directory reads whatever it
finds there. Whoever can write into that directory can plant a symlink to any
file the analyst can read (another session, a key file), and its content would
enter the trace attributed to the symlink's path. So a file discovered by a
walk is read only if no path component between the walked root and the file
is a symlink and the file resolves inside the root. A path the analyst names
on the command line is read as given.

Skipped files are reported on stderr, never dropped silently; the path is
printed with repr(), so control characters in a crafted file name cannot
reach the terminal as escape sequences.
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path


def within_tree(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    p = root
    for part in rel.parts:
        p = p / part
        if p.is_symlink():
            return False
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def keep_within_tree(paths: list[Path], root: Path) -> list[Path]:
    """`paths` minus those outside `root` or reached through a symlink,
    each skipped one reported on stderr."""
    kept = []
    for p in paths:
        if within_tree(p, root):
            kept.append(p)
        else:
            print(f"warning: skipped {str(p)!r}: symlink or outside the input "
                  f"directory {str(root)!r}", file=sys.stderr)
    return kept


# Characters that act on a terminal or reorder what a reader sees: C0 controls
# other than \t and \n (so also \r, which can overwrite a verdict line),
# DEL and C1, the line and paragraph separators U+2028 and U+2029 (a reader
# splitting on them, as Python's str.splitlines() does, would see a new
# line), bidi overrides/isolates, zero-width characters, BOM; and lone
# surrogates, which a JSON \uXXXX escape can carry but no UTF-8 output can
# encode, so writing one crashed the CLI (ruled 2026-10-21: rendered escaped).
_UNPRINTABLE = re.compile(
    "[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u2069\\ufeff"
    "\ud800-\udfff]")


def printable(s: str) -> str:
    """`s` with every character above replaced by a \\uXXXX escape."""
    return _UNPRINTABLE.sub(lambda m: f"\\u{ord(m.group()):04x}", s)


def field(s: object, limit: int = 300) -> str:
    """One untrusted value for a human-readable line: printable(), plus
    newline and tab escaped (so a value cannot start a forged line such as
    "verified: ..."), and bounded in length. A value nested too deep for
    str() to render (an object or array of a hostile document, many
    thousands of levels deep) is named by its type instead of ending the
    tool with a RecursionError."""
    try:
        text = str(s)
    except RecursionError:
        text = f"<{type(s).__name__} nested too deep to print>"
    t = printable(text).replace("\n", "\\u000a").replace("\t", "\\u0009")
    return t if len(t) <= limit else t[:limit] + f"... [{len(t)} chars]"


class _SanitisingStream(io.TextIOBase):
    """`one_line_writes`: escape newlines inside a write and keep only a
    trailing one. print() sends its message and its end separately, so each
    diagnostic stays one line and a value cannot forge another. Used for
    stderr, which carries only human diagnostics; stdout keeps newlines
    because JSON is written there, and callers escape values with field()."""

    def __init__(self, inner, one_line_writes: bool = False):
        self._inner = inner
        self._one_line = one_line_writes

    def write(self, s: str) -> int:
        out = printable(s)
        if self._one_line:
            body, end = (out[:-1], "\n") if out.endswith("\n") else (out, "")
            out = body.replace("\n", "\\u000a") + end
        self._inner.write(out)
        return len(s)

    def flush(self) -> None:
        self._inner.flush()

    def isatty(self) -> bool:
        return self._inner.isatty()

    @property
    def encoding(self):
        return self._inner.encoding


def install_output_sanitiser() -> None:
    """Route stdout and stderr through printable(). Values from a session log
    (ids, block types, error text) reach both, and must not act on the
    analyst's terminal. JSON written to stdout stays equivalent: inside a JSON
    string, \\uXXXX denotes the same character."""
    if not isinstance(sys.stdout, _SanitisingStream):
        sys.stdout = _SanitisingStream(sys.stdout)
    if not isinstance(sys.stderr, _SanitisingStream):
        sys.stderr = _SanitisingStream(sys.stderr, one_line_writes=True)
    # argparse's usage and help text is the CLI's own: it keeps its line
    # breaks, its colour codes removed and every character still through
    # printable() (ruled 2026-10-21). The message of an argparse error echoes
    # argv, which a sweep may build from walked names, so a newline in it is
    # escaped (ruled 2026-10-21): no line is forged through error(). A direct
    # parser.exit(status, message) is not wrapped; no CLI here calls it with
    # a message. Every other stderr write keeps the one-line rule.
    import argparse  # noqa: PLC0415
    try:
        from _colorize import decolor  # noqa: PLC0415  (Python 3.13+, colour on by default from 3.14)
    except ImportError:
        def decolor(text: str) -> str:
            return text
    if not getattr(argparse.ArgumentParser._print_message, "_keeps_lines", False):
        def _print_message(self, message, file=None):
            if message:
                f = file if file is not None else sys.stderr
                try:
                    if isinstance(f, _SanitisingStream):
                        f._inner.write(printable(decolor(message)))
                    else:
                        f.write(message)
                except (AttributeError, OSError):
                    pass
        _print_message._keeps_lines = True
        argparse.ArgumentParser._print_message = _print_message
        error = argparse.ArgumentParser.error

        def _error(self, message):
            return error(self, str(message).replace("\n", "\\u000a"))
        argparse.ArgumentParser.error = _error
