#!/usr/bin/env python3
"""Shared source reader for the minitrace adapter fleet.

Implements the converter input contract (design note of 2026-07-30, §2), and
produces the per-source counts and read findings that the source-fidelity
record consumes (source-fidelity record proposal of 2026-07-30, §3).

This is one module rather than fourteen fixes because the read boundary is
per-adapter code (contract note §2.1): fourteen authors implemented it
independently and diverged, and the counts in a fidelity record only mean the
same thing across adapters if one reader produces them.

Contract as implemented here:

  * Recoverable corruption never aborts. A line that fails to decode or parse is
    skipped, counted, and recorded as a finding; the conversion continues.
  * Skipping is never silent. Every skip produces a read finding.
  * Unrecoverable conditions raise `SourceUnreadable` carrying a declared reason
    from a closed vocabulary, never a parser traceback.
  * Bounds are configured and split by scope. Whole-file bounds (size, record
    count) are fatal; per-record bounds (line length) are recoverable.
  * The line delimiter set is pinned to LF, CR and CRLF. `str.splitlines()` is
    not used anywhere in this module, because it also splits on VT, FF, FS, GS,
    RS, NEL, LS and PS, which makes `record_count_read` incomparable between an
    adapter that iterates a file handle and one that calls `splitlines()`.
  * Decoding is explicit and lossy, and each replaced byte run is recorded.
  * Reading is streaming. Peak memory is one chunk plus one line, bounded by
    `max_line_bytes`.

Byte offsets in findings are offsets into the *original source bytes*, before
decoding (proposal §3.3). That is why the file is opened in binary and decoded
line by line: a text-mode reader cannot produce raw byte offsets, and lossy
decoding shifts every position in a decoded stream.

Nothing here interprets record content. Classifying a parsed record as
discarded-by-policy is the adapter's job; it reports the class back through
`SourceRead.note_discard`.
"""
from __future__ import annotations

import codecs
import contextvars
import errno
import hashlib
import json
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from typing import Iterator

__all__ = [
    "ReadLimits",
    "SourceUnreadable",
    "SourceRead",
    "read_jsonl",
    "read_json",
    "asserted_at_now",
    "build_assertion",
    "check_count_invariant",
]

_CHUNK = 1 << 20

# Reserved bucket for a discard class the adapter does not recognise, and the
# pattern a class key must match (proposal §3.2). Raw type strings come from an
# untrusted source, so an unrecognised one is counted rather than used as a key.
_OTHER_CLASS = "other"
_CLASS_KEY = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Fatal reasons. Closed vocabulary: these reach a structured error message and
# must not carry library- or version-specific text.
FATAL_REASONS = frozenset({
    "not-a-file",
    "read-error",
    "file-too-large",
    "record-count-exceeded",
    "no-records",
    # raised by an adapter, not the reader: records were read but adapter
    # policy discards every one, so there is nothing to convert
    "no-emittable-records",
    # raised by an adapter: no record identifies the session, so no trace id
    # can be formed (e.g. a claude-code file holding only `summary` records)
    "no-session-id",
    "unsupported-encoding",
    # the file's bytes differed between the hashing pass and the parse, so the
    # recorded hash would not name what was converted
    "changed-during-read",
    "malformed-json",
})


# Nesting (DSL spec §4, minitrace-spec §3.4). MAX_DEPTH is the depth a trace
# may have: every array or object at level MAX_DEPTH (the number of arrays and
# objects enclosing it) is replaced by null and reported, so one deep value
# costs only itself and no record, container or trace is dropped for depth.
# READ_DEPTH is how deep a value must still be readable to be truncated; a
# record or element deeper than that is unreadable and skipped. Both exact,
# so two runners never disagree; together they keep every recursive step
# (surrogate repair, evaluation, RFC 8785 sealing, a verifier's parser) far
# from a recursion limit.
MAX_DEPTH = 64
READ_DEPTH = 512

@dataclass(frozen=True)
class ReadLimits:
    """Configured bounds, split by scope (contract §2).

    Whole-file bounds are unrecoverable: exceeding one means the source cannot
    be processed at all. Per-record bounds are recoverable: one oversized line
    in an otherwise good log should not cost the whole conversion.

    Defaults are deliberately generous. They exist to stop unbounded memory
    growth on a hostile or corrupt file, not to police plausible session logs:
    the largest local claude-code session logs are tens of megabytes, and a
    single line carrying a large tool result reaches a few megabytes.
    """

    max_file_bytes: int = 512 * 1024 * 1024   # whole-file, fatal
    max_records: int = 5_000_000              # whole-file, fatal
    max_line_bytes: int = 32 * 1024 * 1024    # per-record, recoverable
    max_depth: int = MAX_DEPTH                # per-record, recoverable (truncated); the specs fix it
    read_depth: int = READ_DEPTH              # per-record, recoverable (skipped); the specs fix it
    max_findings: int = 1000                  # per-source, findings are capped (the DSL §4 reporting bound)


DEFAULT_LIMITS = ReadLimits()


def truncate_deep(value: Any, level: int = MAX_DEPTH) -> list[str]:
    """Replace, in place, every array or object at `level` (the number of
    arrays and objects enclosing it; the root is level 0) with None, and
    return the JSON Pointer (RFC 6901) of each replacement. Iterative, and it
    never descends past `level`, so its cost is bounded by the kept part."""
    def seg(k: Any) -> str:
        return str(k).replace("~", "~0").replace("/", "~1")
    replaced: list[str] = []
    stack = [(value, 0, "")]
    while stack:
        node, lvl, path = stack.pop()
        items = list(node.items()) if isinstance(node, dict) else list(enumerate(node)) \
            if isinstance(node, list) else []
        for k, child in items:
            if isinstance(child, (dict, list)):
                child_path = f"{path}/{seg(k)}"
                if lvl + 1 >= level:
                    node[k] = None
                    replaced.append(child_path)
                else:
                    stack.append((child, lvl + 1, child_path))
    return sorted(replaced)


def nesting_exceeds(value: Any, limit: int) -> bool:
    """True when a JSON value nests objects and arrays deeper than `limit`
    (a scalar is depth 0, `[]` depth 1, an array or object one more than its
    deepest member). Iterative, so it cannot itself run out of stack."""
    stack = [(value, 1)]
    while stack:
        v, d = stack.pop()
        if isinstance(v, (dict, list)):
            if d > limit:
                return True
            stack.extend((x, d + 1) for x in (v.values() if isinstance(v, dict) else v))
    return False


class SourceUnreadable(Exception):
    """A source that cannot be read at all.

    Per proposal §2.3 this is not a fourth state of the fidelity record: it is
    the absence of a trace, and the reason travels with the error rather than
    with an output that was never produced.
    """

    def __init__(self, reason: str, path: Path | str, detail: str | None = None):
        if reason not in FATAL_REASONS:
            raise ValueError(f"undeclared fatal reason: {reason!r}")
        self.reason = reason
        self.path = str(path)
        self.detail = detail
        msg = f"{self.path}: {reason}"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


# --- reason vocabularies ----------------------------------------------------
#
# Parser and codec exception messages are library- and version-specific. Putting
# them in a finding would place interpreter-dependent strings inside the hashed
# canonical form (proposal §7.1), so every message is mapped to a stable string
# here and the original never leaves this module.

_DECODE_REASONS = (
    ("invalid start byte", "invalid-start-byte"),
    ("invalid continuation byte", "invalid-continuation-byte"),
    ("unexpected end of data", "unexpected-end-of-data"),
    ("truncated", "unexpected-end-of-data"),
    ("surrogates not allowed", "surrogate-not-allowed"),
    ("illegal", "invalid-encoding"),
)

_PARSE_REASONS = (
    ("Unterminated string", "unterminated-string"),
    ("Invalid control character", "invalid-control-character"),
    ("Expecting value", "expecting-value"),
    ("Expecting property name", "expecting-property-name"),
    ("Expecting ':' delimiter", "expecting-delimiter"),
    ("Expecting ',' delimiter", "expecting-delimiter"),
    ("Extra data", "extra-data"),
    ("Invalid \\uXXXX escape", "invalid-unicode-escape"),
    ("Invalid \\escape", "invalid-escape"),
    ("Out of range", "out-of-range-value"),
)


def _decode_reason(raw_reason: str) -> str:
    low = raw_reason.lower()
    for needle, mapped in _DECODE_REASONS:
        if needle in low:
            return mapped
    return "invalid-encoding"


def _parse_reason(msg: str) -> str:
    for needle, mapped in _PARSE_REASONS:
        if msg.startswith(needle):
            return mapped
    return "malformed-json"


# --- line scanning ----------------------------------------------------------


def _find_delim(buf: bytearray, scan: int, eof: bool) -> tuple[int | None, int]:
    """Find the first line delimiter at or after `scan`.

    Returns `(end, next)` where `end` is the index of the first delimiter byte
    and `next` is the index the following line starts at, or `(None, resume)`
    when no complete delimiter is present yet. Only LF, CR and CRLF terminate a
    line; a lone CR at the end of the buffer is undecided until the next byte
    arrives, which is the whole reason this returns a resume position.
    """
    n = len(buf)
    i = scan
    while i < n:
        b = buf[i]
        if b == 0x0A:
            return i, i + 1
        if b == 0x0D:
            if i + 1 < n:
                return (i, i + 2) if buf[i + 1] == 0x0A else (i, i + 1)
            if eof:
                return i, i + 1
            return None, i          # need one more byte to resolve CR vs CRLF
        i += 1
    return None, i


def _mark_allowance(buf: bytearray, base: int, more: bool = False) -> int:
    """The bytes of a UTF-8 mark at the very start of the input, which the
    line bound does not count: DSL rc3 §4 ignores the mark, so the first line
    at the bound is kept with or without one (ruled 2026-10-19). Offsets
    still count it. While `more` bytes may follow, a buffer at the start that
    is still a prefix of the mark (a chunk boundary inside it) gets the
    allowance too, so a bound shorter than the mark is decided as on a whole
    read; at the end of the input only a whole mark does."""
    mark = codecs.BOM_UTF8
    if base == 0 and (buf.startswith(mark)
                      or (more and len(buf) < len(mark) and mark.startswith(bytes(buf)))):
        return len(mark)
    return 0


def _iter_lines(fh, max_line_bytes: int, chunk_size: int = _CHUNK
                ) -> Iterator[tuple[int, bytes | None, int]]:
    """Yield `(byte_offset, line_bytes, line_length)` for each line.

    `line_bytes` is None when the line exceeded `max_line_bytes`; its bytes are
    then discarded as they arrive rather than buffered, so an unterminated
    multi-gigabyte line costs one chunk of memory. `line_length` is the length
    of the line in bytes, excluding its delimiter, in both cases.
    """
    buf = bytearray()
    base = 0            # file offset of buf[0]
    scan = 0            # next index in buf to search from
    dropping = False    # inside an over-long line
    drop_start = 0
    drop_len = 0
    eof = False

    while not eof:
        chunk = fh.read(chunk_size)
        if chunk:
            buf += chunk
        else:
            eof = True

        while True:
            end, resume = _find_delim(buf, scan, eof)
            if end is None:
                scan = resume
                break
            if dropping:
                drop_len += end
                yield drop_start, None, drop_len
                dropping = False
            elif end > max_line_bytes + _mark_allowance(buf, base):
                # Complete but over the bound. The no-delimiter branch below
                # only catches a line still growing, so a bounded line that
                # arrived whole inside one chunk has to be caught here too.
                yield base, None, end
            else:
                yield base, bytes(buf[:end]), end
            nxt = resume
            del buf[:nxt]
            base += nxt
            scan = 0

        # The line so far is buf[:scan]: a CR at the end of the buffer
        # (scan points at it) waits for the next byte to tell CR from CRLF
        # and is not part of the line, so it neither counts toward the bound
        # nor is discarded with the line's bytes while dropping.
        if not dropping and scan > max_line_bytes + _mark_allowance(buf, base, more=not eof):
            # No delimiter in sight and the line is already over the bound.
            # Start discarding, keeping the offset where the line began.
            dropping = True
            drop_start = base
            drop_len = scan
            base += scan
            del buf[:scan]
            scan = 0
        elif dropping and scan:
            drop_len += scan
            base += scan
            del buf[:scan]
            scan = 0

    if dropping:
        drop_len += len(buf)
        yield drop_start, None, drop_len
    elif buf:
        yield base, bytes(buf), len(buf)


_DECODE_RUNS: contextvars.ContextVar[list] = contextvars.ContextVar("_DECODE_RUNS")


# Runs recorded per decoded unit; beyond this the rest are summarised in one
# entry, so a line of isolated bad bytes cannot allocate a tuple per byte.
_MAX_DECODE_RUNS = 1000


# How many replacements the last, summarising run stands for.
_DECODE_FOLDED: contextvars.ContextVar[list] = contextvars.ContextVar("_DECODE_FOLDED")


def _record_and_replace(exc: UnicodeDecodeError) -> tuple[str, int]:
    runs = _DECODE_RUNS.get()
    if len(runs) < _MAX_DECODE_RUNS:
        runs.append((exc.start, exc.end - exc.start, _decode_reason(exc.reason)))
    elif len(runs) == _MAX_DECODE_RUNS:
        runs.append((exc.start, exc.end - exc.start, "further-decode-errors-suppressed"))
        _DECODE_FOLDED.get()[0] = 1
    else:
        start, length, reason = runs[-1]
        runs[-1] = (start, exc.end - start, reason)
        _DECODE_FOLDED.get()[0] += 1
    return "\ufffd", exc.end


codecs.register_error("minitrace-record-replace", _record_and_replace)


def _decode_line(raw: bytes, encoding: str) -> tuple[str, tuple[tuple[int, int, str], ...]]:
    """Decode with replacement, returning the text and every replaced byte run.

    Each run is `(offset_within_raw, length, reason)`. The strict-decode fast
    path means an undamaged line costs one decode; a damaged one costs one
    more, with an error handler that records each run. (Re-decoding the rest
    of the line after every bad byte was quadratic: a line of 0xFF bytes
    from a hostile log took seconds per 100 KB.)
    """
    text, runs, _ = _decode_line_counted(raw, encoding)
    return text, runs


def _decode_line_counted(raw: bytes, encoding: str) -> tuple[str, tuple[tuple[int, int, str], ...], int]:
    """`_decode_line` plus the number of replacements the last run stands
    for when it summarises the rest ("further-decode-errors-suppressed"),
    else 0. DSL rc3 §4 (Bytes in the input) makes each replacement an input
    failure and asks the summary past the reporting bound to name their
    count; the summarising run alone counted as one (5,000
    isolated FF bytes gave a summary of 1 further finding, not 4,000)."""
    try:
        return raw.decode(encoding), (), 0
    except UnicodeDecodeError:
        pass
    runs_ctx = _DECODE_RUNS.set([])
    folded_ctx = _DECODE_FOLDED.set([0])
    try:
        text = raw.decode(encoding, errors="minitrace-record-replace")
        return text, tuple(_DECODE_RUNS.get()), _DECODE_FOLDED.get()[0]
    finally:
        _DECODE_RUNS.reset(runs_ctx)
        _DECODE_FOLDED.reset(folded_ctx)


def _is_unusable_char(c: str) -> bool:
    cp = ord(c)
    return 0xD800 <= cp <= 0xDFFF or 0xFDD0 <= cp <= 0xFDEF or (cp & 0xFFFE) == 0xFFFE


def repair_strings(value: Any) -> tuple[Any, list[dict], list[str]]:
    """DSL spec rc3 §4 "Strings in the input" (T19b): a copy of a
    parsed JSON value in which every unpaired surrogate and every noncharacter
    in a string or member name is replaced with U+FFFD (an emitted trace is
    I-JSON, minitrace-spec §3.4); the rest of the string is kept. Returns the
    copy, one repair per affected string or member name ({"path": RFC 6901
    pointer, "reason": "lone-surrogate" or "noncharacter" (the first kind
    met), "count": characters replaced, "member_name": bool}), and the pointer
    of each member whose repaired name collided with another of its object
    (the later value wins; a value was lost, so the caller reports it). JSON
    admits "\\ud83d" (a truncated emoji escape), which no UTF-8 writer and no
    RFC 8785 canonicaliser accepts."""
    repairs: list[dict] = []
    collisions: list[str] = []

    def seg(k: str) -> str:
        return k.replace("~", "~0").replace("/", "~1")

    def fix(s: str, parent: str | None, path: str) -> str:
        # parent is set for a member name: its pointer is built from the
        # repaired name, so the report itself holds no unusable character.
        if not any(_is_unusable_char(c) for c in s):
            return s
        first = next(c for c in s if _is_unusable_char(c))
        n = sum(1 for c in s if _is_unusable_char(c))
        fixed = "".join("\ufffd" if _is_unusable_char(c) else c for c in s)
        repairs.append({"path": path if parent is None else f"{parent}/{seg(fixed)}", "count": n,
                        "member_name": parent is not None,
                        "reason": "lone-surrogate" if 0xD800 <= ord(first) <= 0xDFFF else "noncharacter"})
        return fixed

    def walk(v: Any, path: str) -> Any:
        if isinstance(v, str):
            return fix(v, None, path)
        if isinstance(v, list):
            return [walk(x, f"{path}/{i}") for i, x in enumerate(v)]
        if isinstance(v, dict):
            out: dict = {}
            for k, x in v.items():
                fk = fix(k, path, "") if isinstance(k, str) else k
                child = f"{path}/{seg(fk) if isinstance(fk, str) else fk}"
                if fk in out:
                    collisions.append(child)
                out[fk] = walk(x, child)
            return out
        return v

    return walk(value, ""), repairs, collisions


def _record_repairs(out: Any, repairs: list[dict], collision_paths: list[str],
                    offset: int, length: int, record_index: int) -> None:
    """One finding per repaired string or member name, naming record and
    path (DSL §4 "Strings in the input"), and one per member whose repaired
    name collided (a value lost, the later one wins); under the findings cap,
    the §4 reporting bound."""
    for r in repairs:
        out.add_finding({"kind": "decode-error", "byte_offset": offset, "byte_length": length,
                         "record_index": record_index, "reason": r["reason"], "path": r["path"],
                         "count": r["count"], "member_name": r["member_name"]})
    for path in collision_paths:
        out.add_finding({"kind": "decode-error", "byte_offset": offset, "byte_length": length,
                         "record_index": record_index, "reason": "repair-key-collision", "path": path})


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} (not JSON, not hashable under RFC 8785)")


# minitrace-spec §3.4 numeric domain, decided by VALUE: a number whose binary64
# value lies outside +-(2^53 - 1), in any notation (9007199254740992,
# 9007199254740992.0, 1e16, 1e300), or whose nearest binary64 value is
# infinite (1e999), cannot be a JSON number in a trace.
_MAX_SAFE_INT = 2**53 - 1
# Integer lexemes longer than this many digits lie outside the domain whatever
# they are, so int() (limited to 4300 digits) is never asked to read them.
_MAX_SAFE_DIGITS = len(str(_MAX_SAFE_INT))


def _number_in_domain(lexeme: str, is_int: bool) -> int | float | None:
    """The value of a JSON number lexeme if a trace can hold it, else None."""
    if is_int:
        if len(lexeme.lstrip("-")) > _MAX_SAFE_DIGITS:
            return None
        n = int(lexeme)
        return n if -_MAX_SAFE_INT <= n <= _MAX_SAFE_INT else None
    f = float(lexeme)
    return f if f == f and -_MAX_SAFE_INT <= f <= _MAX_SAFE_INT else None


def _strict_float(lexeme: str) -> float:
    f = _number_in_domain(lexeme, False)
    if f is None:
        if float(lexeme) in (float("inf"), float("-inf")):
            raise ValueError(f"number {lexeme[:40]} overflows binary64")
        raise ValueError(f"number {lexeme[:40]} outside the safe domain")
    return f


def _strict_int(lexeme: str) -> int:
    n = _number_in_domain(lexeme, True)
    if n is None:
        raise ValueError(f"number {lexeme[:40]} outside the safe domain")
    return n


def strict_json_loads(text: str, **kw: Any) -> Any:
    """json.loads under trace verification rules (minitrace-spec §3.4): NaN,
    Infinity, and any number outside the domain by value (an integer or float
    spelling beyond +-(2^53 - 1), an overflow such as 1e999) raise ValueError.
    For reading converter INPUT use input_json_loads, which keeps such a
    number as its source text instead (DSL spec §4)."""
    return json.loads(text, parse_constant=_refuse_constant, parse_float=_strict_float,
                      parse_int=_strict_int, **kw)


class _IJsonViolation(ValueError):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason


def is_noncharacter(cp: int) -> bool:
    """A Unicode noncharacter: U+FDD0..U+FDEF, and U+xFFFE and U+xFFFF in
    every plane (66 code points)."""
    return 0xFDD0 <= cp <= 0xFDEF or (cp & 0xFFFE) == 0xFFFE


def _ijson_string_problem(s: str) -> tuple[str, str] | None:
    for ch in s:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            return "unpaired-surrogate", f"unpaired surrogate U+{cp:04X}"
        if is_noncharacter(cp):
            return "noncharacter", f"noncharacter U+{cp:04X}"
    return None


def _ijson_object(pairs: list[tuple[str, Any]]) -> dict:
    # Member names here, string values in _ijson_walk: json.loads keeps the
    # last duplicate silently, which RFC 8785 and the Rust verifier refuse.
    out: dict = {}
    for k, v in pairs:
        if (p := _ijson_string_problem(k)):
            raise _IJsonViolation(p[0], f"{p[1]} in a member name")
        if k in out:
            raise _IJsonViolation("duplicate-key", f"duplicate member name {k[:40]!r}")
        out[k] = v
    return out


def _ijson_walk(value: Any) -> tuple[str, str] | None:
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            if (p := _ijson_string_problem(v)):
                return p
        elif isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return None


def trace_json_verdict(text: str | bytes) -> tuple[Any, str | None]:
    """A trace under the minitrace-spec §3.4 rules this reader can decide:
    (value, None) if they hold, else (None, reason) with reason
      not-utf8                   bytes that are not UTF-8 (I-JSON);
      not-json                   text that does not parse at all;
      duplicate-key              an object with two members of one name;
      unpaired-surrogate         a string or member name holding one (I-JSON);
      noncharacter               a string or member name holding a Unicode
                                 noncharacter (I-JSON);
      number-outside-safe-range  any spelling beyond ±(2^53-1) by value;
      non-finite-number          NaN, Infinity, or an overflow such as 1e999;
      depth-exceeds-64.
    The canonical vectors, the parity test, schemas/validate.py and
    verify_envelope.py take their verdicts from here, so they cannot drift."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            return None, "not-utf8"
    try:
        value = strict_json_loads(text, object_pairs_hook=_ijson_object)
    except _IJsonViolation as e:
        return None, e.reason
    except RecursionError:
        # Nesting far past the limit exhausts the parser's stack before the
        # depth check can run; it is the same verdict (Rust: recursion limit).
        return None, "depth-exceeds-64"
    except json.JSONDecodeError:
        return None, "not-json"
    except ValueError as e:
        return None, ("number-outside-safe-range" if "safe domain" in str(e) else "non-finite-number")
    if (p := _ijson_walk(value)):
        return None, p[0]
    if nesting_exceeds(value, MAX_DEPTH):
        return None, "depth-exceeds-64"
    return value, None


def _unusable(what: str, v: Any, kind: str, warnings: list | None) -> None:
    import sys  # noqa: PLC0415
    msg = f"{what}: {str(v)[:40]!r} is not a usable {kind}; null"
    print(f"warning: {msg}", file=sys.stderr)
    if warnings is not None:
        warnings.append(msg)


def _in_domain(v: Any) -> bool:
    return v == v and -_MAX_SAFE_INT <= v <= _MAX_SAFE_INT


def input_count(v: Any, what: str, warnings: list | None = None) -> int | None:
    """A count a pre-processor reads from input (token counts, step counts).
    A JSON number in the §3.4 domain is the count (as int, like the
    int(x or 0) it replaces); absent or null is 0. Anything else is not a
    usable count: a number the reader kept as its source text (DSL spec §4,
    e.g. "1e16"), a number outside ±(2^53-1) from any other source, a
    non-integral or negative number (3.7 is not truncated; a count has the
    schema's minimum 0), a boolean, a string, a container. That gives
    None, never 0, and a warning
    on stderr and in `warnings` when the caller keeps them (its
    preprocessor_warnings), never an exception that loses the whole
    conversion."""
    if v is None:
        return 0
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not _in_domain(v) or v != int(v) or v < 0:
        _unusable(what, v, "count", warnings)
        return None
    return int(v)


def count_sum(a: int | None, b: int | None, what: str, warnings: list | None = None) -> int | None:
    """a + b for counts from input_count: null when either is null (an
    unusable part is never summed as 0), and null with a warning when the sum
    leaves the §3.4 domain."""
    if a is None or b is None:
        return None
    s = a + b
    if not _in_domain(s):
        _unusable(what, f"{a} + {b}", "count (sum outside ±(2^53-1))", warnings)
        return None
    return s


def input_amount(v: Any, what: str, warnings: list | None = None) -> float | None:
    """As input_count, for an amount kept as a float (a cost)."""
    if v is None:
        return 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not _in_domain(v):
        _unusable(what, v, "amount", warnings)
        return None
    return float(v)


def _input_float(lexeme: str) -> float | str:
    f = _number_in_domain(lexeme, False)
    return lexeme if f is None else f


def _input_int(lexeme: str) -> int | str:
    n = _number_in_domain(lexeme, True)
    return lexeme if n is None else n


def input_json_loads(text: str, **kw: Any) -> Any:
    """json.loads under the DSL spec §4 input rule: a number in the §3.4
    domain is read as its nearest binary64 value; a number the trace could not
    hold as a number (outside +-(2^53 - 1) by value, in any notation, or
    overflowing binary64) is read as a string holding its source text exactly
    as written, so nothing is lost and no record is skipped for it. NaN and
    Infinity are not JSON and still raise ValueError."""
    return json.loads(text, parse_constant=_refuse_constant, parse_float=_input_float,
                      parse_int=_input_int, **kw)


def load_json_file_reporting(path: Path | str) -> Any:
    """A companion JSON file (settings sidecar, meta.json): decoded with
    replacement, parsed strictly. Returns None when unusable, and says so on
    stderr rather than dropping the file silently."""
    import sys  # noqa: PLC0415
    try:
        return input_json_loads(decode_reporting(path))
    except (ValueError, RecursionError, OSError, SourceUnreadable) as e:
        print(f"warning: {str(path)!r} unusable and not in the trace: {e}", file=sys.stderr)
        return None


_nested_refused = 0
_SAME_AS_DEFAULT = object()


def nested_json_loads(raw: Any, default: Any = None, *, count: bool = True,
                      report_malformed: bool = True,
                      refused: Any = _SAME_AS_DEFAULT,
                      truncated_out: list | None = None,
                      repaired_out: list | None = None,
                      lost_keys_out: dict | None = None) -> Any:
    """A JSON value nested in a record already read (tool arguments, a content
    or metadata column), parsed strictly. Malformed JSON or a non-string gives
    `default`, as the adapters always did. A value strict parsing refuses
    (out-of-range number, NaN, an integer too long to convert, nesting too
    deep) gives `default` too, but is counted and reported on stderr: before,
    it raised out of the pre-processor or reached the DSL input and refused
    the whole file. take_nested_refused() returns and resets the count, so a
    pre-processor reads it per session. `count=False` for a second parse of
    a value already counted or reported (it silences both). `refused`, when given, is returned instead of
    `default` for a refused value, so a caller can tell refusal (an input
    failure) from malformed JSON. `truncated_out`, when given, receives the
    JSON Pointer of every value replaced because it was nested too deep;
    `repaired_out` receives each repair_strings entry (an unpaired surrogate
    or noncharacter replaced with U+FFFD, DSL §4 "Strings in the input");
    `lost_keys_out` receives "duplicates", the count of duplicate keys, and
    "collisions", the path of each member whose repaired name collided (DSL
    §4 "Objects in the input"). A collecting caller reports these
    itself, so they are then not printed.

    Malformed JSON, or a value that is not a string (None excepted: an
    absent column), is reported with a "warning:" line on stderr (ruled
    2026-10-28), which verify_run reads as a loss; it is not counted as
    refused, since that count names strict-parsing refusals. A caller whose
    contract is a silent default (the DSL's json_parse, DSL rc3 §9), or that
    keeps the text itself (codex tool arguments), passes
    report_malformed=False."""
    global _nested_refused  # noqa: PLW0603
    import sys  # noqa: PLC0415
    refused_value = default if refused is _SAME_AS_DEFAULT else refused
    dups = 0

    def _count_dups(pairs: list[tuple[str, Any]]) -> dict:
        nonlocal dups
        out: dict = {}
        for k, v in pairs:
            if k in out:
                dups += 1
            out[k] = v
        return out

    try:
        value = input_json_loads(raw, object_pairs_hook=_count_dups)
    except (json.JSONDecodeError, TypeError) as e:
        if count and report_malformed and raw is not None:
            what = (f"not JSON ({e.msg})" if isinstance(e, json.JSONDecodeError)
                    else f"not a string ({type(raw).__name__})")
            print(f"warning: nested JSON value unreadable, not in the trace: {what}", file=sys.stderr)
        return default
    except (ValueError, RecursionError) as e:
        if count:
            _nested_refused += 1
            print(f"warning: nested JSON value refused, not in the trace: {e}", file=sys.stderr)
        return refused_value
    if nesting_exceeds(value, READ_DEPTH):
        if count:
            _nested_refused += 1
            print("warning: nested JSON value refused, not in the trace: nesting deeper than "
                  f"{READ_DEPTH}", file=sys.stderr)
        return refused_value
    # Within the read bound, a value too deep is truncated, not refused: the
    # caller gets the rest, and the JSON Pointer of each replaced value.
    truncated = truncate_deep(value, MAX_DEPTH)
    if truncated:
        if truncated_out is not None:
            truncated_out.extend(truncated)
        if count:
            print(f"warning: nested JSON value truncated at depth {MAX_DEPTH}: "
                  f"{len(truncated)} value(s) replaced with null", file=sys.stderr)
    # A "\\ud83d" escape inside the nested string decodes to a lone surrogate,
    # which the outer read could not see (it was escape text there) and which
    # the pre-processor's UTF-8 output write then refuses, losing the file;
    # a noncharacter would reach the trace (not I-JSON). Repair both as the
    # readers do for top-level values.
    try:
        value, repairs, collision_paths = repair_strings(value)
    except RecursionError:
        return default
    collisions = len(collision_paths)
    if repaired_out is not None:
        repaired_out.extend(repairs)
    if count and repairs and repaired_out is None:   # a collecting caller reports them itself
        print(f"warning: {len(repairs)} string(s) in a nested JSON value held an unpaired "
              "surrogate or a noncharacter, replaced with U+FFFD at "
              + ", ".join(r["path"][:80] or "/" for r in repairs[:5])
              + (" ..." if len(repairs) > 5 else ""), file=sys.stderr)
    if lost_keys_out is not None:
        lost_keys_out["duplicates"] = dups
        lost_keys_out["collisions"] = list(collision_paths)
    elif count and (dups or collisions):
        # Both lose a value (the later one wins); the top-level reader records
        # them as findings, so the nested reader must not drop them silently.
        print(f"warning: nested JSON value lost {dups} duplicate key(s) and "
              f"{collisions} key(s) that collided after surrogate repair", file=sys.stderr)
    return value


def take_nested_refused() -> int:
    global _nested_refused  # noqa: PLW0603
    n, _nested_refused = _nested_refused, 0
    return n


def result_as_text(v: Any) -> tuple[str | None, bool]:
    """A tool result for tool_calls[].output.result, which is a string or
    null (minitrace v0.2.0, kept by v0.3.0). A string or None is returned as
    is; any other value (a list of content blocks, an object, a number, a
    boolean) is written as its JSON text, and the second element says so,
    for the caller's warning line (ruled 2026-11-08: the structure is not
    kept in the trace, which verify_run then reads as a loss). A value too
    deep to serialise is None, also reported."""
    if v is None or isinstance(v, str):
        return v, False
    try:
        return json.dumps(v, ensure_ascii=False), True
    except (RecursionError, ValueError, TypeError):
        return None, True


def _loads_recording_duplicates(text: str) -> tuple[Any, int]:
    """json.loads that counts repeated names within an object. The last value
    wins, as in json.loads; the count lets the caller record the ambiguity
    instead of passing a silently disambiguated record on."""
    dups = 0

    def hook(pairs: list[tuple[str, Any]]) -> dict:
        nonlocal dups
        out: dict = {}
        for k, v in pairs:
            if k in out:
                dups += 1
            out[k] = v
        return out

    value = input_json_loads(text, object_pairs_hook=hook)
    return value, dups


_JSON_WS_RUN = re.compile(r"[ \t\n\r]*")
# json.scanner.NUMBER_RE: ASCII digits only, as the C scanner (\d would take any Unicode digit)
_JSON_NUMBER = re.compile(r"(-?(?:0|[1-9][0-9]*))(\.[0-9]+)?([eE][-+]?[0-9]+)?")


def _deep_elements(text: str, limit: int) -> tuple[bool, list[tuple[int, int, int]], int]:
    """Validate a JSON document without recursion and find what nests too deep.

    For a document json.loads gives up on for depth (ruled 2026-10-28).
    It refuses what json.loads refuses, with the same message, so
    a malformed document gets the reason a readable depth would give: strings
    go through json's own string scanner, a number only has to match json's
    number grammar (the input hooks read every such number, out of the §3.4
    domain as a string), NaN and Infinity are refused as input_json_loads
    refuses them, and the structure follows the C scanner's checks in its
    order.
    Returns whether the root is an array; the top-level elements (the root,
    when it is no array) nesting deeper than `limit`, as (index, start, end);
    and the number of duplicate member names inside those."""
    ws = _JSON_WS_RUN.match

    def fail(msg: str, pos: int) -> None:
        raise json.JSONDecodeError(msg, text, pos)

    def member_name(pos: int) -> int:
        # The open object's names are () before the first, the name itself
        # after one and a set from the second, so a deep chain of one-member
        # objects holds no set per level.
        nonlocal elem_dups
        name, pos = json.decoder.scanstring(text, pos + 1, True)
        names = stack[-1]
        if isinstance(names, set):
            elem_dups += name in names
            names.add(name)
        elif isinstance(names, tuple):
            stack[-1] = name
        else:
            elem_dups += name == names
            stack[-1] = {names, name}
        pos = ws(text, pos).end()
        if not text.startswith(":", pos):
            fail("Expecting ':' delimiter", pos)
        return ws(text, pos + 1).end()

    def scalar(pos: int) -> int:
        c = text[pos:pos + 1]
        if c == '"':
            return json.decoder.scanstring(text, pos + 1, True)[1]
        for word in ("null", "true", "false"):
            if text.startswith(word, pos):
                return pos + len(word)
        for word in ("NaN", "Infinity", "-Infinity"):
            if text.startswith(word, pos):
                _refuse_constant(word)
        m = _JSON_NUMBER.match(text, pos)
        if m is None:
            fail("Expecting value", pos)
        return m.end()

    pos = ws(text, 0).end()
    root_array = text.startswith("[", pos)
    base = 1 if root_array else 0   # the root array is not a level of its elements
    stack: list[tuple | str | set | None] = []   # per open container: its member names, None for an array
    deep: list[tuple[int, int, int]] = []
    deep_dups = elem_dups = elem_level = elem_start = 0
    index = -1
    have_value = False
    while True:
        if not have_value:
            if len(stack) == base:
                index += 1
                elem_start, elem_level, elem_dups = pos, 0, 0
            c = text[pos:pos + 1]
            if c == "[" or c == "{":
                stack.append(() if c == "{" else None)
                elem_level = max(elem_level, len(stack) - base)
                pos = ws(text, pos + 1).end()
                if text.startswith("]" if c == "[" else "}", pos):
                    pos += 1
                    stack.pop()
                    have_value = True
                elif c == "{":
                    if not text.startswith('"', pos):
                        fail("Expecting property name enclosed in double quotes", pos)
                    pos = member_name(pos)
                continue
            pos = scalar(pos)
            have_value = True
        if len(stack) == base:            # a top-level element (or the root) ended
            if elem_level > limit:
                deep.append((index, elem_start, pos))
                deep_dups += elem_dups
        if not stack:
            break
        pos = ws(text, pos).end()
        names = stack[-1]
        c = text[pos:pos + 1]
        if c == ("]" if names is None else "}"):
            pos += 1
            stack.pop()
            continue
        if c != ",":
            fail("Expecting ',' delimiter", pos)
        pos = ws(text, pos + 1).end()
        if names is None:
            if text.startswith("]", pos):
                fail("Illegal trailing comma before end of array", pos)
        else:
            if text.startswith("}", pos):
                fail("Illegal trailing comma before end of object", pos)
            if not text.startswith('"', pos):
                fail("Expecting property name enclosed in double quotes", pos)
            pos = member_name(pos)
        have_value = False
    end = ws(text, pos).end()
    if end != len(text):
        fail("Extra data", end)
    return root_array, deep, deep_dups


def _loads_past_the_parser(text: str, limit: int) -> tuple[Any, int, frozenset[int]]:
    """_loads_recording_duplicates for a document json.loads gives up on for
    depth (ruled 2026-10-28, DSL rc3 §4): each top-level element
    deeper than `limit` (or the root, when it is no array) is validated, put
    as null and returned as an index to skip, and the rest is parsed as
    usual. Raises as json.loads does on a malformed document, and
    RecursionError when what is left is still too deep to parse."""
    root_array, deep, deep_dups = _deep_elements(text, limit)
    if not root_array:
        if not deep:
            raise RecursionError("nesting past the parser's limit within the read bound")
        return None, deep_dups, frozenset({0})
    parts, last = [], 0
    for _, start, end in deep:
        parts += [text[last:start], "null"]
        last = end
    parts.append(text[last:])
    value, dups = _loads_recording_duplicates("".join(parts))
    return value, dups + deep_dups, frozenset(i for i, _, _ in deep)


def _hash_file(path: Path) -> tuple[str, int]:
    """sha256 over the raw bytes, plus the size, in one streaming pass.

    DSL rc3 §15 (spec a3bf9aee): this pass is the first read, so a
    file that cannot be opened or read (not permitted included) is
    SourceUnreadable here; it was an OSError traceback."""
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError:   # as the other read-error sites: no library message
        raise SourceUnreadable("read-error", path) from None
    return f"sha256:{digest.hexdigest()}", size


# --- the per-source result --------------------------------------------------


@dataclass
class SourceRead:
    """One contributing source artefact and what the reader observed of it.

    `records` and `record_indices` are parallel: `record_indices[i]` is the
    0-based index of `records[i]` among the recognised records of the source, so
    an adapter can locate a record it later drops or finds inconsistent without
    tracking positions itself.
    """

    path: Path
    role: str = "primary"
    raw_artefact_id: str | None = None
    content_hash: str = ""
    size_bytes: int = 0
    records: list = field(default_factory=list)
    record_indices: list[int] = field(default_factory=list)
    record_count_read: int = 0
    record_count_skipped: int = 0
    record_count_discarded: int = 0
    record_count_emitted: int = 0
    discarded_by_class: dict[str, int] = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    # read_json: the JSON kind of the document root ("array", "object",
    # "string", "number", "boolean", "null"); a caller that needs an array
    # (container mode, DSL rc3 §4.1) refuses any other.
    root_kind: str = ""
    _suppressed: int = 0
    _suppressed_skips: int = 0
    _first_suppressed_index: int | None = None
    _first_suppressed_path: str | None = None
    # The summary names the earliest suppressed
    # finding by this key (the caller's input order), not the first one: within
    # a line the depth truncations are added before the repairs.
    _order_key: Any = None
    _first_key: Any = None
    _skip_marks: list[bool] = field(default_factory=list)
    _limits: ReadLimits = DEFAULT_LIMITS

    # -- collection --------------------------------------------------------

    def add_finding(self, finding: dict, count: int = 1) -> None:
        """Record a read finding, capping the array per proposal §5.

        The cap exists because a badly damaged multi-megabyte log otherwise
        produces one finding per line inside the hashed form. Truncation is
        itself recorded, since silently truncating the record of silent loss
        would be the same defect one level up.

        `count` > 1: the finding stands for that many (a
        decode run summarising further replacements); listed, it is one
        finding and the others count as suppressed; past the cap, all of
        them do, so the summary names their number.
        """
        self._add(finding, skip=False)
        if count > 1:
            self._suppress(finding, count - 1, skip=False)

    def skip_record(self, finding: dict) -> None:
        """Count a record skipped as unreadable and record why. The finding is
        marked as a skip (outside the hashed form), so a caller classifies its
        loss by what happened to the record, never by finding kind."""
        self.record_count_skipped += 1
        self._add(finding, skip=True)

    def _add(self, finding: dict, *, skip: bool) -> None:
        if len(self.findings) < self._limits.max_findings:
            self.findings.append(finding)
            self._skip_marks.append(skip)
            return
        self._suppress(finding, 1, skip=skip)

    def _suppress(self, finding: dict, n: int, *, skip: bool) -> None:
        first = self._suppressed == 0
        self._suppressed += n
        self._suppressed_skips += n if skip else 0
        key = self._order_key(finding) if self._order_key is not None else None
        if first or (key is not None and key < self._first_key):   # its record_index may be None
            self._first_suppressed_index = finding.get("record_index")
            self._first_suppressed_path = finding.get("path")
            self._first_key = key

    def _seal_findings(self) -> None:
        if self._suppressed:
            self.findings.append({
                "kind": "findings-truncated",
                # None for a whole-document finding: it lies in no record.
                "record_index": self._first_suppressed_index,
                # DSL §4: the summary names the first affected record; in
                # container mode the path tells which record of the container.
                **({"path": self._first_suppressed_path} if self._first_suppressed_path else {}),
                "suppressed_count": self._suppressed,
            })
            self._skip_marks.append(self._suppressed_skips > 0)

    def findings_with_loss(self) -> list[tuple[dict, str]]:
        """Each finding with its DSL §15 loss class: "set" when it records a
        skipped record (for the cap marker: when a suppressed finding did),
        else "value" (a repair, a truncation, a duplicate key)."""
        return [(f, "set" if skip else "value")
                for f, skip in zip(self.findings, self._skip_marks, strict=True)]

    def note_discard(self, class_name: str | None, count: int = 1) -> None:
        """Count a parsed record the adapter dropped by policy.

        A class name the adapter does not recognise, or one that does not match
        the key pattern, counts under `other` rather than becoming a key: raw
        type strings come from the untrusted source and would otherwise put
        attacker-chosen keys of unbounded cardinality into the trace.
        """
        key = class_name if class_name and _CLASS_KEY.match(class_name) else _OTHER_CLASS
        self.discarded_by_class[key] = self.discarded_by_class.get(key, 0) + count
        self.record_count_discarded += count

    def note_emitted(self, count: int = 1) -> None:
        self.record_count_emitted += count

    # -- output ------------------------------------------------------------

    def to_source_entry(self) -> dict:
        entry = {
            "role": self.role,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "record_count_read": self.record_count_read,
            "record_count_skipped": self.record_count_skipped,
            "record_count_discarded": self.record_count_discarded,
            "record_count_emitted": self.record_count_emitted,
        }
        if self.raw_artefact_id:
            entry["raw_artefact_id"] = self.raw_artefact_id
        if self.discarded_by_class:
            entry["discarded_by_class"] = dict(
                sorted(self.discarded_by_class.items())
            )
        return entry


def check_count_invariant(entry: dict) -> None:
    """Assert the arithmetic invariant of proposal §3.2.

    `record_count_emitted` is deliberately not constrained to the remainder:
    preprocessors legitimately merge and split records.
    """
    total = entry["record_count_skipped"] + entry["record_count_discarded"]
    if total > entry["record_count_read"]:
        raise ValueError(
            "source-fidelity count invariant violated: "
            f"skipped+discarded={total} > read={entry['record_count_read']}"
        )


# --- readers ----------------------------------------------------------------


def _preflight(path: Path, limits: ReadLimits, encoding: str) -> tuple[str, int]:
    try:
        codecs.lookup(encoding)
    except LookupError:
        raise SourceUnreadable("unsupported-encoding", path, encoding) from None
    # One stat, not is_file(), which raised
    # PermissionError on Python 3.12 under a parent without search
    # permission and answered not-a-file on 3.14; a path that is not there
    # (a dangling link, a link loop) is not-a-file, any other failure
    # read-error.
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError, ValueError):   # ValueError: a NUL in the path
        raise SourceUnreadable("not-a-file", path) from None
    except OSError as e:   # a name too long is not-a-file, as is_file() said on 3.14
        raise SourceUnreadable("not-a-file" if e.errno in (errno.ELOOP, errno.ENAMETOOLONG) else "read-error",
                               path) from None
    if not stat.S_ISREG(st.st_mode):
        raise SourceUnreadable("not-a-file", path)
    size = st.st_size
    if size > limits.max_file_bytes:
        raise SourceUnreadable(
            "file-too-large", path, f"{size} > {limits.max_file_bytes}"
        )
    content_hash, hashed_size = _hash_file(path)
    return content_hash, hashed_size


# JSON whitespace (RFC 8259 §2): what may stand around a jsonl record and
# what a blank line may hold (DSL rc3 §15).
_JSON_WS = b" \t\n\r"


def jsonl_text_lines(text: str) -> Iterator[tuple[int, str]]:
    """(line number, line) for each line of a decoded jsonl text that is not
    blank, framed as the readers frame it (DSL rc3 §15, for
    pre-processors that read jsonl text themselves): at CR, LF or CRLF only,
    not also at U+2028, U+2029 or U+0085 as str.splitlines(), which JSON
    allows inside strings; blank is empty or JSON whitespace only, not also
    str.strip()'s VT, FF and Unicode spaces. Each line comes with its JSON
    whitespace removed, numbered from 1 with blank lines counted."""
    ws = _JSON_WS.decode("ascii")
    for n, line in enumerate(re.split(r"\r\n|\r|\n", text), 1):
        line = line.strip(ws)
        if line:
            yield n, line


def read_jsonl(path: Path | str, *,
               role: str = "primary",
               raw_artefact_id: str | None = None,
               encoding: str = "utf-8",
               limits: ReadLimits = DEFAULT_LIMITS,
               require_object: bool = True,
               require_records: bool = True,
               order_key: Any = None) -> SourceRead:
    """Read a JSONL source under the input contract.

    Returns a `SourceRead` whose `records` are the lines that parsed. Lines that
    did not are counted and recorded, not raised. Raises `SourceUnreadable` only
    for the whole-file conditions in `FATAL_REASONS`.

    `require_records=False` allows a legitimately empty source, which the
    contract otherwise treats as unrecoverable.
    """
    path = Path(path)
    content_hash, size = _preflight(path, limits, encoding)
    out = SourceRead(
        path=path, role=role, raw_artefact_id=raw_artefact_id,
        content_hash=content_hash, size_bytes=size, _limits=limits,
    )
    out._order_key = order_key   # which suppressed finding is first
    out.checks = [
        {"check": "jsonl-parse", "status": "ran"},
        {"check": "utf8-decode", "status": "ran"},
        {"check": "bounds", "status": "ran"},
    ]

    record_index = -1
    try:
        fh = path.open("rb")
    except OSError:
        raise SourceUnreadable("read-error", path) from None
    with fh:
        for offset, raw, length in _iter_lines(fh, limits.max_line_bytes):
            if raw is None:
                # Over-long line: a recognised record we could not keep.
                record_index += 1
                out.record_count_read += 1
                out.skip_record({
                    "kind": "bounds-exceeded",
                    "byte_offset": offset,
                    "byte_length": length,
                    "record_index": record_index,
                    "reason": "line-too-long",
                })
                continue

            # DSL rc3 §4 "Encoding" (ruled 2026-10-18): a UTF-8 BOM at the
            # very start of the input is not part of it; the first line is read
            # as if it were absent, unreported. Offsets still count it.
            bom = len(codecs.BOM_UTF8) if offset == 0 and raw.startswith(codecs.BOM_UTF8) else 0
            raw = raw[bom:]
            # DSL rc3 §15: blank is empty or JSON whitespace only;
            # bytes.strip() also took VT and FF, so a line of them was ignored
            # unreported and a record beside one was accepted.
            stripped = raw.strip(_JSON_WS)
            if not stripped:
                continue        # blank line: not a record in either reader

            record_index += 1
            out.record_count_read += 1
            if out.record_count_read > limits.max_records:
                raise SourceUnreadable(
                    "record-count-exceeded", path, str(limits.max_records)
                )

            lead = len(raw) - len(raw.lstrip(_JSON_WS))
            text, decode_errors, folded = _decode_line_counted(stripped, encoding)
            for n, (err_offset, err_len, reason) in enumerate(decode_errors, 1):
                out.add_finding({
                    "kind": "decode-error",
                    "byte_offset": offset + bom + lead + err_offset,
                    "byte_length": err_len,
                    "record_index": record_index,
                    "reason": reason,
                }, count=folded if n == len(decode_errors) and folded else 1)

            try:
                value, n_dup_keys = _loads_recording_duplicates(text)
            except json.JSONDecodeError as exc:
                out.skip_record({
                    "kind": "parse-error",
                    "byte_offset": offset,
                    "byte_length": length,
                    "record_index": record_index,
                    "reason": _parse_reason(exc.msg),
                })
                continue
            except RecursionError:
                # Deeply nested JSON: a per-record bound, so recoverable, and a
                # controlled rejection rather than a traceback out of the parser.
                out.skip_record({
                    "kind": "parse-error",
                    "byte_offset": offset,
                    "byte_length": length,
                    "record_index": record_index,
                    "reason": "nesting-too-deep",
                })
                continue
            except ValueError:
                # Not a JSONDecodeError: a non-finite number, or an integer
                # outside the RFC 8785 safe domain (strict_json_loads). Same
                # treatment: skip, count, record; never a traceback.
                out.skip_record({
                    "kind": "parse-error",
                    "byte_offset": offset,
                    "byte_length": length,
                    "record_index": record_index,
                    "reason": "number-out-of-range",
                })
                continue

            if nesting_exceeds(value, limits.read_depth):
                # Unreadable at any runner's bound: skipped, the others kept.
                out.skip_record({
                    "kind": "parse-error",
                    "byte_offset": offset,
                    "byte_length": length,
                    "record_index": record_index,
                    "reason": "nesting-too-deep",
                })
                continue
            for pointer in truncate_deep(value, limits.max_depth):
                # The record is kept; only the value too deep is lost.
                out.add_finding({
                    "kind": "truncated",
                    "byte_offset": offset,
                    "byte_length": length,
                    "record_index": record_index,
                    "reason": "nesting-too-deep",
                    "path": pointer,
                })

            if require_object and not isinstance(value, dict):
                out.skip_record({
                    "kind": "parse-error",
                    "byte_offset": offset,
                    "byte_length": length,
                    "record_index": record_index,
                    "reason": "not-an-object",
                })
                continue

            if n_dup_keys:
                out.add_finding({
                    "kind": "parse-error",
                    "byte_offset": offset,
                    "byte_length": length,
                    "record_index": record_index,
                    "reason": "duplicate-key",
                })
            try:
                value, repairs, collision_paths = repair_strings(value)
            except RecursionError:
                out.skip_record({
                    "kind": "parse-error",
                    "byte_offset": offset,
                    "byte_length": length,
                    "record_index": record_index,
                    "reason": "nesting-too-deep",
                })
                continue
            _record_repairs(out, repairs, collision_paths, offset, length, record_index)
            out.records.append(value)
            out.record_indices.append(record_index)

    # The hash was taken in a separate pass (_preflight); if the file changed
    # in between, the recorded hash would name bytes other than those parsed.
    if _hash_file(path)[0] != out.content_hash:
        raise SourceUnreadable("changed-during-read", path)
    out._seal_findings()
    if require_records and out.record_count_read == 0:
        raise SourceUnreadable("no-records", path)
    return out


def read_json(path: Path | str, *,
              role: str = "primary",
              raw_artefact_id: str | None = None,
              encoding: str = "utf-8",
              limits: ReadLimits = DEFAULT_LIMITS,
              require_object: bool = False,
              order_key: Any = None) -> SourceRead:
    """Read a whole-file JSON source under the same contract.

    A JSON document has no recoverable unit below the file, so a parse failure
    is fatal here where it is recoverable in `read_jsonl`. Decoding is still
    lossy and recorded: a document that decodes with replacements and then
    parses is usable, and the replacement is a finding rather than a failure.

    With `require_object` (the DSL runner, DSL rc3 §15 Unreadable and empty
    input), a record that is not an object, an element of the root
    array or a root that is no array, is skipped as unreadable, as
    `read_jsonl` skips such a line.
    """
    path = Path(path)
    content_hash, size = _preflight(path, limits, encoding)
    out = SourceRead(
        path=path, role=role, raw_artefact_id=raw_artefact_id,
        content_hash=content_hash, size_bytes=size, _limits=limits,
    )
    out._order_key = order_key   # which suppressed finding is first
    out.checks = [
        {"check": "utf8-decode", "status": "ran"},
        {"check": "bounds", "status": "ran"},
    ]

    try:
        raw = path.read_bytes()
    except OSError:
        raise SourceUnreadable("read-error", path) from None
    if f"sha256:{hashlib.sha256(raw).hexdigest()}" != out.content_hash:
        raise SourceUnreadable("changed-during-read", path)

    # DSL rc3 §4 "Encoding" (ruled 2026-10-18): a UTF-8 BOM at the very start
    # is not part of the input; it is read as if absent, unreported, and
    # offsets still count it.
    bom = len(codecs.BOM_UTF8) if raw.startswith(codecs.BOM_UTF8) else 0
    text, decode_errors, folded = _decode_line_counted(raw[bom:], encoding)
    for n, (err_offset, err_len, reason) in enumerate(decode_errors, 1):
        out.add_finding({
            "kind": "decode-error",
            "byte_offset": bom + err_offset,
            "byte_length": err_len,
            # The whole document: read_json places no finding in a record
            # before parsing, so none is named.
            "record_index": None,
            "reason": reason,
        }, count=folded if n == len(decode_errors) and folded else 1)

    too_deep: frozenset[int] = frozenset()
    try:
        try:
            value, n_dup_keys = _loads_recording_duplicates(text)
        except RecursionError:
            # Past the parser's own limit, DSL rc3 §4 still holds: the
            # over-deep elements are skipped below, not the whole input.
            value, n_dup_keys, too_deep = _loads_past_the_parser(text, limits.read_depth)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            reason = _parse_reason(exc.msg)
        elif isinstance(exc, RecursionError):
            reason = "nesting-too-deep"
        else:
            reason = "number-out-of-range"
        raise SourceUnreadable("malformed-json", path, reason) from None

    if n_dup_keys:
        out.add_finding({
            "kind": "parse-error",
            "byte_offset": 0,
            "byte_length": len(raw),
            "record_index": None,   # the whole document
            "reason": "duplicate-key",
        })
    # From the text, not the value: a number outside the §4 domain is read
    # as a string, so the value would misname it.
    first = text.lstrip(" \t\n\r")[:1]
    out.root_kind = {"[": "array", "{": "object", '"': "string", "t": "boolean", "f": "boolean",
                     "n": "null"}.get(first, "number")
    records = value if isinstance(value, list) else [value]
    if len(records) > limits.max_records:
        raise SourceUnreadable("record-count-exceeded", path, str(limits.max_records))
    # Depth is per record (top-level element): one deeper than the read bound
    # is skipped and recorded, and within it a value too deep is truncated;
    # the other records (or containers) are kept unchanged.
    out.records, out.record_indices = [], []
    for i, rec in enumerate(records):
        if i in too_deep or nesting_exceeds(rec, limits.read_depth):
            out.skip_record({
                "kind": "parse-error",
                "byte_offset": 0,
                "byte_length": len(raw),
                "record_index": i,
                "reason": "nesting-too-deep",
            })
            continue
        if require_object and not isinstance(rec, dict):
            out.skip_record({
                "kind": "parse-error",
                "byte_offset": 0,
                "byte_length": len(raw),
                "record_index": i,
                "reason": "not-an-object",
            })
            continue
        for pointer in truncate_deep(rec, limits.max_depth):
            out.add_finding({
                "kind": "truncated",
                "byte_offset": 0,
                "byte_length": len(raw),
                "record_index": i,
                "reason": "nesting-too-deep",
                "path": pointer,
            })
        rec, repairs, collision_paths = repair_strings(rec)
        _record_repairs(out, repairs, collision_paths, 0, len(raw), i)
        out.records.append(rec)
        out.record_indices.append(i)
    out.record_count_read = len(records)
    out._seal_findings()
    return out


# --- assertion assembly -----------------------------------------------------


def _finding_sort_key(f: dict) -> tuple:
    ri = f.get("record_index")   # None for a whole-document finding
    return (f.get("source_index", 0), f.get("byte_offset", -1),
            -1 if ri is None else ri, f.get("kind", ""))


def _chain_sort_key(f: dict) -> tuple:
    ri = f.get("record_index")
    return (f.get("source_index", 0), -1 if ri is None else ri,
            f.get("kind", ""), f.get("reference", ""))


def asserted_at_now() -> str | None:
    """Timestamp for the assertion, injectable for reproducibility.

    `asserted_at` is nondeterministic by construction (proposal §7.1), so it
    follows the same treatment as `provenance.converted_at`: honour
    `SOURCE_DATE_EPOCH` when set, so a third party can reproduce the exact
    bytes, read as dsl_runner reads it (timestamps.source_date_epoch).
    Returns None when that value is unusable, and the field is then omitted
    rather than invented (dsl_runner refuses such a value instead).
    """
    import datetime as _dt
    from timestamps import source_date_epoch, spec_ts  # noqa: PLC0415
    try:
        pinned = source_date_epoch()
    except ValueError:
        return None
    return pinned if pinned is not None else spec_ts(_dt.datetime.now(_dt.UTC))


def build_assertion(sources: list[SourceRead], *,
                    asserted_by: str,
                    converter_version: str,
                    asserted_at: str | None = None,
                    extra_checks: list[dict] | None = None,
                    chain_findings: list[dict] | None = None) -> dict:
    """Assemble one source-fidelity assertion from what the reader actually ran.

    The check roster is built from the `checks` each `SourceRead` recorded, not
    from anything an adapter declared: a self-declarable roster is worthless
    (proposal §2.4). `extra_checks` is for checks the *caller* genuinely ran,
    such as the chain checks over parsed records, and is the caller's assertion
    to make.

    Every array carries the normative total order of proposal §7.1, because
    RFC 8785 canonicalises object key order but not array element order, so an
    unordered array produces a different `content_hash` on every run.
    """
    ordered = sorted(
        enumerate(sources),
        key=lambda pair: (pair[1].role != "primary", pair[1].role, pair[1].content_hash),
    )

    entries: list[dict] = []
    read_findings: list[dict] = []
    checks: dict[str, str] = {}

    for new_index, (_, src) in enumerate(ordered):
        entry = src.to_source_entry()
        check_count_invariant(entry)
        entries.append(entry)
        for finding in src.findings:
            read_findings.append({**finding, "source_index": new_index})
        for check in src.checks:
            # A check is `ran` for the assertion only if it ran on every source.
            name, status = check["check"], check["status"]
            if name in checks and checks[name] != status:
                checks[name] = "incomplete"
            else:
                checks[name] = status

    roster = [{"check": name, "status": status} for name, status in checks.items()]
    if extra_checks:
        roster.extend(extra_checks)
    roster.sort(key=lambda c: c["check"])

    assertion = {
        "asserted_by": asserted_by,
        "converter_version": converter_version,
        "sources": entries,
        "checks": roster,
        "read_findings": sorted(read_findings, key=_finding_sort_key),
        "chain_findings": sorted(chain_findings or [], key=_chain_sort_key),
    }
    if asserted_at:
        assertion["asserted_at"] = asserted_at
    return assertion


def read_jsonl_records(path: Path | str, encoding: str = "utf-8",
                       findings_out: list | None = None,
                       report_out: list | None = None,
                       indices_out: list | None = None,
                       order_key: Any = None) -> list[dict]:
    """Records of a JSONL session file for pre-processors that do not (yet)
    carry a fidelity record: read under the input contract (damaged lines are
    skipped and counted, encoding damage is repaired, never a traceback), and
    anything skipped or repaired is reported on stderr, so no line is dropped
    without a word. An empty file yields no records rather than an error.
    `findings_out`, when given, receives the findings as well, so a caller
    can carry them into its own diagnostics; `report_out` receives them as
    (finding, loss class) pairs (`SourceRead.findings_with_loss`), and
    `indices_out` each kept record's input position (`record_indices`)."""
    import sys  # noqa: PLC0415
    src = read_jsonl(path, encoding=encoding, require_records=False, order_key=order_key)
    if indices_out is not None:
        indices_out.extend(src.record_indices)
    if findings_out is not None:
        findings_out.extend(src.findings)
    if report_out is not None:
        report_out.extend(src.findings_with_loss())
    if src.record_count_skipped or src.findings:
        reasons: dict[str, int] = {}
        for f in src.findings:
            key = f"{f.get('kind')}/{f.get('reason')}"
            reasons[key] = reasons.get(key, 0) + 1
        print(f"warning: {str(path)!r}: {src.record_count_skipped} of "
              f"{src.record_count_read} line(s) skipped; findings: "
              f"{', '.join(f'{k} x{v}' for k, v in sorted(reasons.items())) or 'none'}",
              file=sys.stderr)
    return src.records


def decode_reporting(path: Path | str, encoding: str = "utf-8") -> str:
    """Whole-file text for pre-processors that parse a non-JSON format
    (aider Markdown, cursor stream): invalid bytes are replaced with U+FFFD
    and counted on stderr, instead of aborting the conversion. The bound
    is the shared reader's (ruled 2026-10-29): a path that is not a regular
    file, or one over ReadLimits.max_file_bytes, raises SourceUnreadable
    before it is read, and no more than that is read from one that grows."""
    import sys  # noqa: PLC0415
    path = Path(path)
    limit = ReadLimits().max_file_bytes
    # Stat errors as _preflight maps them: a path
    # through a regular file, a symlink loop or a name too long is
    # not-a-file, never a traceback.
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError, ValueError):
        raise SourceUnreadable("not-a-file", path) from None
    except OSError as e:
        raise SourceUnreadable("not-a-file" if e.errno in (errno.ELOOP, errno.ENAMETOOLONG) else "read-error",
                               path) from None
    if not stat.S_ISREG(st.st_mode):
        raise SourceUnreadable("not-a-file", path)
    if st.st_size > limit:
        raise SourceUnreadable("file-too-large", path, f"{st.st_size} > {limit}")
    try:
        with path.open("rb") as fh:
            raw = fh.read(limit + 1)
    except OSError:
        raise SourceUnreadable("read-error", path) from None
    if len(raw) > limit:
        raise SourceUnreadable("file-too-large", path, f"more than {limit}")
    text, runs, folded = _decode_line_counted(raw, encoding)
    if runs:   # the summarising run counts what it stands for
        print(f"warning: {str(path)!r}: {len(runs) - 1 + folded if folded else len(runs)} undecodable byte run(s) "
              f"replaced with U+FFFD", file=sys.stderr)
    return text
