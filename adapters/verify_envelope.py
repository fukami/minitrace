"""Check an envelope manifest against the files it lists (envelope spec §3.2, §6).

    python verify_envelope.py path/to/manifest.json

What is checked, and what failing each means:

- Paths (§3.2): every `path` is relative, uses forward slashes, has no `.`
  or `..` segment, no control or bidi character, and resolves to a regular
  file inside the envelope root without passing through a symlink. A path
  that cannot be examined (a name too long, say), or a listed file that
  cannot be read, is a failed check; a path the sweep for unlisted files
  cannot examine is noted (ruled 2026-10-26).
- Traces (§6.1, §6.2): at least one; each file's SHA-256 equals
  `file_content_hash`; the trace parses (duplicate keys refused, as in
  minitrace-verify); its `id` equals `trace_id`; its stored `content_hash`
  and the manifest's `trace_content_hash` both equal the hash recomputed over
  its canonical form. No two entries share a path; a trace id held by two
  or more entries fails step 2 for every one of them, and a reference to it
  (lineage, binding, witness scope) names no one trace (ruled 2026-10-20). The
  trace's own `schema_version` decides its version (§12, ruled 2026-10-20):
  from 0.1.1 only v0.3.x (`0.3.<n>`) may be bundled; under 0.1.0 a v0.2.x
  trace ("minitrace-v0.2.0") is also admitted, has only its listing hash
  checked, and any `content_hash` it carries is noted as unchecked. Any
  other version is a failed check; a listing `schema_version` that differs
  from the trace's is noted.
- Lineage (§6.3): a hashed lineage edge to a trace bundled in the same
  envelope is compared with that trace's `trace_content_hash`; a difference
  is not a failed check (the edge refers to another state of the target,
  as a permitted redaction or addition leaves it) and is noted as not
  verified against the bundled target. Applied to 0.1.0 envelopes as well:
  0.1.0 states the match only as a producer duty, and the after-assembly
  redaction that §6.3 describes can happen to them too.
- Raw artefacts and bindings: each file's SHA-256 equals `content_hash`
  (and `size_bytes`, when given). A `produces_trace_id` that equals no
  listing's `trace_id`, or that of two or more, is noted and the entry
  applies to no trace, removed entries included; it is not a failed check
  (§4.7, §4.11, ruled 2026-10-21).
  A raw artefact removed as a whole (§10.4: redaction operation "remove",
  target_kind "witness" or absent) lists no file, or under 0.1.0 a path
  whose file is absent; that is noted, not a problem. A file that is
  present is checked as it stands (§7.4).
- Witnesses (§6.2 steps 3 and 4, §7): each listed payload path obeys the
  path rules and is hashed against `path_hashes` (in an envelope declaring
  0.1.1 or later every listed path must be a key and no key may name an
  unlisted path; under 0.1.0 a witness without `path_hashes` has
  unverifiable payload files); the record's own `content_hash` is checked
  against its canonical form (RFC 8785, the field itself excluded; under
  0.1.1 or later the record must meet the §3.4 rules). A trace-scoped
  witness whose target_trace_id equals no listing's trace_id, or that of two
  or more, is noted and applies to no bundled trace, with no failed check
  (§7.3, ruled 2026-10-21); an event-scoped one names exactly one event of
  its type in one bundled trace by its minitrace-spec §4.2 identifier, a
  turn as "turn-<index>", else it fails (§7.3, ruled 2026-10-20). A witness removed
  as a whole lists no payload, is reported as removed, and its record hash
  is still checked.
- Signature (§6.2 step 5, §9): read from signature.json at the envelope
  root; verification is not implemented in this package, so a signed
  envelope has an unverified step 5 and is informational only (§6.2). An
  unsigned envelope has no step 5; it lacks producer authenticity (§5.2).
  An empty `signatures` list is unsigned; a signature.json that does not
  parse, is not an object, or whose `signatures` is not a list fails step 5
  (§9.5: failure is not "unsigned"; ruled 2026-10-21). Each entry is held to
  the schema's SignatureFile shape, and one whose `covers` includes
  `manifest` has its `manifest_hash` compared with the SHA-256 of the
  manifest bytes (§9.1); a malformed entry or a differing hash fails step 5,
  a matching one leaves it unsupported, and an entry not covering the
  manifest is noted (ruled 2026-10-26).
- Clocks (§8, §14.5, ruled 2026-10-21): a manipulated clock is named on the
  verdict line and in the report, without changing the verdict class (a
  manipulated-clock envelope is valid, §8.3). Noted: a trace clock_id or a
  producer host_clock_id naming no declared clock (the envelope's
  timestamps are then host_system, unattested, §8.6), and, with two or
  more clocks declared, a trace listing without clock_id (§8.5).
- Trace schema (ruled 2026-10-21): each bundled v0.3.x trace is validated
  against the base schema and its trace_type schema, a check labelled
  "trace schema" (not a §6.2 step); a v0.2.x trace under 0.1.0 has no
  schema here and the check is skipped.
- x-* witnesses (ruled 2026-10-21): every record of an x-tls-keylog-trace
  payload (NDJSON, §7.6) must carry redaction_class "amber" or "red"
  (§7.6.7); a record missing it, carrying "green", or a line that is not a
  JSON object fails the check labelled "x-tls-keylog-trace". An x-* witness
  whose metadata.capture_status is present and not "captured" is noted
  (§7.6.1; an absent declaration reads as captured).

Every check is recorded with its §6.2 step and a status: passed, failed,
skipped (not performed: unverifiable), unsupported (not implemented here)
or removed (an entry removed as a whole: nothing to verify, §6.2 step 3).
Each witness also gets one summary record (step "witness"): passed only
when every check of it passed, otherwise its worst status; nothing is
reported as verified that a check did not pass. The summaries restate their
witness's checks, so they are counted on a line of their own and the check
counts leave them out: the failed checks, the verdict's failed count and the
problems are one number (ruled 2026-10-26). The verdict (ruled
2026-10-20) counts §6.2's steps: an envelope with a skipped or unsupported
step (a present signature is one: step 5 is not implemented), or with any
failed check (a step, the schema or a scope), is informational only, not
evidence (§6.2); otherwise, unsigned, it has integrity only, and whether
that is evidence is for the consumer's evidence policy (§5.2). Removed
entries do not decide the verdict. The verdict line ends with the number of
notes, and every report names the SHA-256 of the manifest bytes it read
(ruled 2026-10-26). --json prints the full report.

Before any of these, the manifest is checked against the envelope schema of
the revision it declares (§12: an envelope is held to no requirement of a
later revision): minitrace-envelope-v0.1.0 for "0.1.0", v0.1.1 for any
other. A manifest that fails it is reported and nothing else is read
(ruled 2026-10-20).
Limits, each a bounded refusal: the manifest file, the entries in each
listing and a trace file (parsed in memory) have a size or count limit,
changeable by flag; raw artefacts and bindings are hashed by streaming.

Exit (ruled 2026-10-21) 0: integrity only, every step passed. 1: a check
failed. 3: informational only with no failed check (a step skipped or
unsupported, a signature present but not verified). 2: the manifest is
unreadable or a limit refused it (or a usage error).
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import unicodedata
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from cross_trace import compute_content_hash  # noqa: E402
from input_tree import field, install_output_sanitiser, within_tree  # noqa: E402
from source_reader import _ijson_string_problem, _in_domain, trace_json_verdict  # noqa: E402

SCHEMAS = pathlib.Path(__file__).resolve().parent.parent / "schemas"
SCHEMA = SCHEMAS / "minitrace-envelope-v0.1.1.schema.json"
SCHEMA_V010 = SCHEMAS / "minitrace-envelope-v0.1.0.schema.json"
MAX_SCHEMA_ERRORS = 50
MAX_MANIFEST_BYTES = 64 * 2**20
MAX_ITEMS = 100_000
MAX_TRACE_BYTES = 256 * 2**20
_LISTINGS = ("traces", "raw_artefacts", "witnesses", "annotations", "bindings", "clocks")


class EnvelopeRefused(ValueError):
    """A limit refused the envelope before it was read in full (exit 2)."""


# A trace's own schema_version (envelope §12): the v0.3.x line as the base
# schema spells it, and v0.2.0, the only published v0.2 release
# (minitrace-spec v0.3.0 §1.4), as v0.2.0 spells it (ruled 2026-10-20).
_TRACE_V03 = re.compile(r"0\.3\.(0|[1-9][0-9]*)")
_TRACE_V02 = "minitrace-v0.2.0"
_BAD_PATH_CHARS = re.compile(r"[\\\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")


def _refuse_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate object key {k!r}")
        out[k] = v
    return out


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON value")


def _finite_float(s: str) -> float:
    # An overflow such as 1e999 reads as inf; refused like NaN (§3.4 reading).
    v = float(s)
    if v in (float("inf"), float("-inf")):
        raise ValueError(f"number {s[:40]} is not finite")
    return v


def _read_manifest(p: pathlib.Path, max_bytes: int, what: str = "manifest") -> str:
    """The manifest's (or signature.json's, `what`) text, read once: see _read_bytes."""
    return _read_bytes(p, max_bytes, what).decode("utf-8")


def _read_bytes(p: pathlib.Path, max_bytes: int, what: str = "manifest") -> bytes:
    """The file's bytes, read once (ruled 2026-10-21): opened without
    blocking and checked on the open descriptor, so a FIFO or device, or a
    file swapped in after a check by name, is refused before any read, and
    every later view parses (and the report hashes) these same bytes."""
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        # On the raw descriptor, before fdopen: fdopen itself refuses a
        # directory, and the descriptor is closed on every path.
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{what} is not a regular file: {field(p)}")
        if st.st_size > max_bytes:
            raise EnvelopeRefused(f"{what} is {st.st_size} bytes, over the limit of {max_bytes}")
        fh = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise EnvelopeRefused(f"{what} is over the limit of {max_bytes} bytes")
    return data


def _finite_int(s: str) -> int:
    # Envelope §3.3: a number whose nearest binary64 value is infinite
    # refuses the manifest, an integer spelling included (1 then 400 zeros).
    try:
        n = int(s)
    except ValueError:   # CPython's limit on the digits it converts
        raise ValueError(f"an integer of {len(s)} digits is beyond this reader") from None
    try:
        float(n)
    except OverflowError:
        raise ValueError(f"number {s[:40]}... is not finite as binary64") from None
    return n


def _load_json(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_refuse_duplicates, parse_constant=_refuse_constant,
                          parse_float=_finite_float, parse_int=_finite_int)
    except RecursionError:
        # A deeply nested manifest is unreadable, not a crash.
        raise ValueError("manifest is nested too deeply to read") from None


class _OutOfDomain:
    """A number whose value lies outside ±(2^53-1), kept as its lexeme."""
    __slots__ = ("lexeme",)

    def __init__(self, lexeme: str):
        self.lexeme = lexeme


def _domain_int(s: str) -> Any:
    return int(s) if _in_domain(int(s)) else _OutOfDomain(s)


def _domain_float(s: str) -> Any:
    f = _finite_float(s)
    return f if _in_domain(f) else _OutOfDomain(s)


def _pointer_step(base: str, name: str) -> str:
    return f"{base}/{name.replace('~', '~0').replace('/', '~1')}"


def _manifest_value_problems(text: str, skip: tuple[str, ...] = ("witnesses",)) -> list[str]:
    """Where the manifest departs from the minitrace-spec §3.4 numeric
    domain and I-JSON string rules (ruled 2026-10-21), by JSON path, at most
    MAX_SCHEMA_ERRORS. The top-level members in `skip` are left out:
    witness records are held to these rules by step 4 (§7.1), not here.
    Duplicate names and non-finite numbers are refused when the manifest is
    read, before this. The hooks here add Python frames the reading in
    _load_json does not, so a manifest nested just short of that refusal
    can still exhaust the stack here; it is refused the same way."""
    try:
        value = json.loads(text, parse_int=_domain_int, parse_float=_domain_float)
    except RecursionError:
        raise ValueError("manifest is nested too deeply to read") from None
    out: list[str] = []
    # Paths are JSON Pointers (RFC 6901): "~" and "/" in a member name are
    # escaped, so two locations never share a name.
    stack: list[tuple[str, str | None, Any]] = [
        (_pointer_step("", k), k, v) for k, v in reversed(list(value.items())) if k not in skip] \
        if isinstance(value, dict) else []
    while stack and len(out) <= MAX_SCHEMA_ERRORS:
        here, name, v = stack.pop()
        if name is not None and (p := _ijson_string_problem(name)):
            out.append(f"{field(here)}: member name: {p[1]}")
        if isinstance(v, _OutOfDomain):
            out.append(f"{field(here)}: number {field(v.lexeme, 40)} lies outside ±(2^53-1)")
        elif isinstance(v, str):
            if (p := _ijson_string_problem(v)):
                out.append(f"{field(here)}: {p[1]}")
        elif isinstance(v, dict):
            stack.extend((_pointer_step(here, k), k, x) for k, x in reversed(list(v.items())))
        elif isinstance(v, list):
            stack.extend((f"{here}/{i}", None, x) for i, x in reversed(list(enumerate(v))))
    if len(out) > MAX_SCHEMA_ERRORS:
        out = out[:MAX_SCHEMA_ERRORS] + [f"and more (stopped after {MAX_SCHEMA_ERRORS})"]
    return out


def _sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _member_sha256(p: pathlib.Path) -> tuple[str | None, str | None]:
    """(hash, None), or (None, why) when a listed file cannot be read: a
    failed check, not an unreadable envelope (ruled 2026-10-26)."""
    try:
        return _sha256(p), None
    except OSError as e:
        return None, f"cannot be read: {e.strerror or e.__class__.__name__}"


def _schema_problems(m: dict) -> list[str]:
    """How the manifest departs from the schema of the revision it
    declares, located by JSON path, at most MAX_SCHEMA_ERRORS of them (the
    validator is stopped there, so a manifest of many bad entries costs no
    more); a missing validator is itself a problem, never a skip."""
    import itertools  # noqa: PLC0415
    schema = SCHEMA_V010 if m.get("envelope_version") == "0.1.0" else SCHEMA
    try:
        import jsonschema  # noqa: PLC0415
        validator = jsonschema.Draft202012Validator(json.loads(schema.read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001 - any failure to load fails the check
        return [f"schema: validator unavailable ({e.__class__.__name__}: {field(e)})"]
    try:
        errs = list(itertools.islice(validator.iter_errors(m), MAX_SCHEMA_ERRORS + 1))
    except RecursionError:
        return ["schema: manifest nested too deeply to validate"]
    # The path carries manifest keys (path_hashes, original_path_hashes), so
    # it is escaped and bounded as the message is: a key cannot forge a line.
    out = [f"schema: {field('/'.join(str(x) for x in e.absolute_path), 300) or '(manifest)'}: {field(e.message, 200)}"
           for e in sorted(errs[:MAX_SCHEMA_ERRORS], key=lambda e: [str(x) for x in e.absolute_path])]
    if len(errs) > MAX_SCHEMA_ERRORS:
        out.append(f"schema: and more (stopped after {MAX_SCHEMA_ERRORS})")
    return out


def _lineage_entry_readable(e: Any, v03: bool) -> bool:
    """An edge the §6.3 checks can read: for a v0.3.x trace both type and
    target_trace_id are non-empty strings; for a v0.2.x trace, whose
    specification defines no lineage, a target_trace_id is a string where
    present (ruled 2026-11-12)."""
    if not isinstance(e, dict):
        return False
    if v03:
        return all(isinstance(e.get(k), str) and e.get(k) for k in ("type", "target_trace_id"))
    return e.get("target_trace_id") is None or isinstance(e.get("target_trace_id"), str)


def _lineage_shape_problems(lineage: Any, v03: bool = True) -> list[str]:
    """Why a trace's `lineage` cannot be read for the §6.3 checks: not a
    list, an edge that is not an object, a type or target_trace_id that is
    not a non-empty string (absent, null, empty or another JSON type; the
    v0.3.0 base schema requires both on every edge), or a target_content_hash
    that is present but not a string (ruled 2026-10-21; absent, null and
    empty type and target_trace_id added 2026-11-12, as minitrace-verify).
    For a v0.2.x trace (`v03` false), whose specification defines no
    lineage, only a target_trace_id or target_content_hash that is present
    but not a string is a problem, as before. An absent lineage is none."""
    if lineage is None:
        return []
    if not isinstance(lineage, list):
        return [f"lineage is not a list (a JSON {_json_type(lineage)})"]
    out = []
    for j, e in enumerate(lineage):
        if not isinstance(e, dict):
            out.append(f"lineage[{j}] is not an object (a JSON {_json_type(e)})")
            continue
        if not v03:
            if e.get("target_trace_id") is not None and not isinstance(e["target_trace_id"], str):
                out.append(f"lineage[{j}].target_trace_id is not a string "
                           f"(a JSON {_json_type(e['target_trace_id'])})")
        for key in ("type", "target_trace_id") if v03 else ():
            v = e.get(key)
            if isinstance(v, str) and v:
                continue
            what = ("absent" if key not in e else "the empty string" if v == ""
                    else f"not a string (a JSON {_json_type(v)})")
            out.append(f"lineage[{j}].{key} is {what}; a non-empty string is required")
        if e.get("target_content_hash") is not None and not isinstance(e["target_content_hash"], str):
            out.append(f"lineage[{j}].target_content_hash is not a string "
                       f"(a JSON {_json_type(e['target_content_hash'])})")
    return out


def _json_type(v: Any) -> str:
    return {dict: "object", list: "array", str: "string", bool: "boolean",
            int: "number", float: "number", type(None): "null"}.get(type(v), type(v).__name__)


def resolve_member(root: pathlib.Path, rel: Any) -> tuple[pathlib.Path | None, str | None]:
    """(file, None) for a §3.2-conformant path to a regular file, else (None, why)."""
    if not isinstance(rel, str) or not rel:
        return None, "path missing or not a string"
    if rel.startswith("/") or _BAD_PATH_CHARS.search(rel) or re.match(r"^[A-Za-z]:", rel):
        return None, "path not relative with forward slashes"
    if any(seg in (".", "..", "") for seg in rel.split("/")):
        return None, "path has an empty, '.' or '..' segment"
    f = root / rel
    # A path that cannot be examined (a name over the file system's limit
    # raises ENAMETOOLONG before Python 3.13) is a failed check, not an
    # unreadable envelope (ruled 2026-10-26).
    try:
        if not within_tree(f, root):
            return None, "path passes through a symlink or leaves the envelope"
        if not f.is_file():
            return None, "no such regular file"
    except OSError as e:
        return None, f"path cannot be examined: {e.strerror or e.__class__.__name__}"
    return f, None


def _listed_before(seen: set[Any], root: pathlib.Path, rel: Any) -> bool:
    """True when `rel` was listed before: as the same string, or as a
    spelling that differs from it only in letter case or Unicode
    normalisation and names the same file (st_dev, st_ino), which a
    case-insensitive or normalising file system (APFS, NTFS) resolves to one
    file (ruled 2026-10-29). Two hard links with different names are two
    paths. `rel` is recorded either way; `seen` also holds the listed
    strings the unlisted-file check reads."""
    key = None
    if isinstance(rel, str):
        try:
            # lstat: an alias names the same directory entry, and a symlink
            # is not followed before resolve_member refuses it.
            st = os.lstat(root / rel)
            key = (st.st_dev, st.st_ino, unicodedata.normalize("NFC", rel).casefold())
        except (OSError, ValueError):
            pass
    before = rel in seen or (key is not None and key in seen)
    seen.add(rel)
    if key is not None:
        seen.add(key)
    return before


def _nothing_at(p: pathlib.Path) -> bool:
    """True only when nothing exists at p. A path that cannot be examined
    (no permission, say) is not taken as absent."""
    try:
        os.lstat(p)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _removed_as_a_whole(redaction: Any) -> bool:
    """§7.4, §10.4: operation remove applied to the record as a whole
    (target_kind witness, or absent as v0.1.0 could only express)."""
    return (isinstance(redaction, dict) and redaction.get("operation") == "remove"
            and redaction.get("target_kind", "witness") == "witness")


STATUSES = ("passed", "failed", "skipped", "unsupported", "removed")
_STEPS = ("1", "2", "3", "4", "5")


@dataclasses.dataclass
class Report:
    """One envelope's result: the problems (failed checks) and notes as
    text, and every check as {step, subject, status, detail}, status one of
    STATUSES; step "witness" is one summary per witness record."""
    problems: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    checks: list[dict] = dataclasses.field(default_factory=list)
    signed: bool = False
    manipulated: list[str] = dataclasses.field(default_factory=list)   # "<clock id>: <notes>"
    manifest_sha256: str = ""   # "sha256:<hex>" over the manifest bytes as read

    def record(self, step: str, subject: str, status: str, detail: str = "") -> None:
        self.checks.append({"step": step, "subject": subject, "status": status, "detail": detail})

    def fail(self, step: str, subject: str, message: str) -> None:
        self.problems.append(message)
        self.record(step, subject, "failed", message)

    def counts(self) -> dict[str, int]:
        """Every check but the per-witness summaries, which restate the checks
        of their witness (ruled 2026-10-26): its failed count is the number
        of problems."""
        return self._count(lambda c: c["step"] != "witness")

    def witness_counts(self) -> dict[str, int]:
        """The per-witness summaries alone."""
        return self._count(lambda c: c["step"] == "witness")

    def _count(self, keep) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for c in self.checks:
            if keep(c):
                out[c["status"]] += 1
        return out

    def verdict(self) -> str:
        # Only §6.2's steps decide (ruled 2026-10-20): a removed entry is not
        # a skipped step (§6.2: nothing to verify, reported as removed), and
        # the §7.3 scope check and the per-witness summary are not steps.
        # Any failed check, a step or not (schema, scope), is never integrity
        # only: it is also what makes the exit status 1.
        n = {s: sum(1 for c in self.checks if c["step"] in _STEPS and c["status"] == s)
             for s in ("skipped", "unsupported")}
        n["failed"] = self.counts()["failed"]
        reasons = [f"{n[s]} {s}" for s in ("failed", "skipped", "unsupported") if n[s]]
        if self.signed:
            reasons.append("signature present but not verified (no signature profile in this package)")
        # §14.5 (ruled 2026-10-21): manipulated clocks are surfaced on the
        # verdict line itself, whatever its class; the note count ends it
        # (ruled 2026-10-26).
        tail = ("; manipulated clock(s), untrusted for wall time and not monotonic (§8.3): "
                + "; ".join(self.manipulated)) if self.manipulated else ""
        if self.notes:
            tail += f"; {len(self.notes)} note(s)"
        if reasons:
            return "informational only, not evidence (§6.2): " + "; ".join(reasons) + tail
        return ("integrity only: steps 1 to 4 passed; unsigned, so producer authenticity is not "
                "established (§5.2); whether this is evidence is for the consumer's evidence policy" + tail)

    def exit_status(self) -> int:
        """1 a failed check; 3 informational only without one; 0 integrity
        only (ruled 2026-10-21)."""
        if self.problems:
            return 1
        return 3 if self.verdict().startswith("informational only") else 0


def check(manifest_path: pathlib.Path, **limits: int) -> tuple[list[str], list[str]]:
    """(problems, notes) for one manifest: the text view of verify()."""
    r = verify(manifest_path, **limits)
    return r.problems, r.notes


def verify(manifest_path: pathlib.Path, *, max_manifest_bytes: int = MAX_MANIFEST_BYTES,
           max_items: int = MAX_ITEMS, max_trace_bytes: int = MAX_TRACE_BYTES) -> Report:
    """The full report for one manifest. Raises EnvelopeRefused when a
    limit is exceeded, ValueError or OSError when the manifest is unreadable."""
    rep = Report()
    root = manifest_path.parent
    # A FIFO or device would block the read (ruled 2026-10-21): only a
    # regular file, a symlink to one included, is read, and only once.
    data = _read_bytes(manifest_path, max_manifest_bytes)
    rep.manifest_sha256 = "sha256:" + hashlib.sha256(data).hexdigest()
    text = data.decode("utf-8")
    m = _load_json(text)
    if not isinstance(m, dict):
        raise ValueError("manifest is not a JSON object")
    for section in _LISTINGS:
        if isinstance(m.get(section), list) and len(m[section]) > max_items:
            raise EnvelopeRefused(f"{section} lists {len(m[section])} entries, over the limit of {max_items}")
    schema_problems = _schema_problems(m)
    for sp in schema_problems:
        rep.fail("schema", "manifest", sp)
    if schema_problems:
        # Nothing below may assume a type the schema did not confirm.
        return rep
    rep.record("schema", "manifest", "passed")
    version = m.get("envelope_version")
    v011 = version != "0.1.0"   # 0.1.1 or a later revision (§12)
    # The §3.4 numeric domain and I-JSON string rules on the manifest, its
    # witness records left to step 4 (ruled 2026-10-21): a failed step-1
    # check from 0.1.1, a note under 0.1.0.
    for vp in _manifest_value_problems(text):
        if v011:
            # Envelope §3.3: a failed check outside §6.2's steps, labelled as such.
            rep.fail("manifest", "manifest", f"manifest: outside the §3.4 rules: {vp}")
        else:
            rep.notes.append(f"manifest (0.1.0): outside the §3.4 rules: {vp}")
    _clock_checks(rep, m)

    traces = m.get("traces") or []
    by_id: dict[str, dict] = {}
    seen_paths: set[Any] = set()
    # A trace_id held by two or more listings (ruled 2026-10-20): every
    # holder fails step 2 and none is verified, as R5 treats shared trace
    # ids; counted before any file is read, and a reference to the id
    # (lineage, binding, witness scope) resolves to no trace.
    held: dict[Any, int] = {}
    for t in traces:
        held[t.get("trace_id")] = held.get(t.get("trace_id"), 0) + 1
    shared = {k: n for k, n in held.items() if n > 1}
    for i, t in enumerate(traces):
        where = f"traces[{i}]"
        tid, rel = t.get("trace_id"), t.get("path")
        step1 = len(rep.checks)
        if _listed_before(seen_paths, root, rel):
            rep.fail("1", where, f"{where}: path {field(rel)} listed twice")
        if tid in shared:
            rep.fail("2", where, f"{where}: trace_id {field(tid)} is held by {shared[tid]} trace listings; "
                                 f"none is verified")
        f, why = resolve_member(root, rel)
        if f is None:
            rep.fail("1", where, f"{where}: {why}: {field(rel)}")
            if tid not in shared:   # a holder has already failed step 2
                rep.record("2", where, "skipped", "no trace file to read")
            continue
        if f.stat().st_size > max_trace_bytes:
            raise EnvelopeRefused(f"{where}: trace file is {f.stat().st_size} bytes, "
                                  f"over the limit of {max_trace_bytes}")
        digest, unread = _member_sha256(f)
        if unread is not None:
            rep.fail("1", where, f"{where}: {unread}: {field(rel)}")
            if tid not in shared:
                rep.record("2", where, "skipped", "no trace file to read")
            continue
        if digest != t.get("file_content_hash"):
            rep.fail("1", where, f"{where}: file_content_hash does not match the file bytes")
        elif not any(c["step"] == "1" and c["subject"] == where for c in rep.checks[step1:]):
            # Not beside a step-1 failure of this entry (its path listed twice).
            rep.record("1", where, "passed")
        # A trace is read under the §3.4 rules (I-JSON, numeric domain, depth),
        # the same verdict as validate.py and the vectors.
        try:
            raw = f.read_bytes()
        except OSError as e:
            rep.fail("2", where, f"{where}: cannot be read: {e.strerror or e.__class__.__name__}")
            continue
        doc, why = trace_json_verdict(raw)
        if why is not None:
            rep.fail("2", where, f"{where}: trace not conformant: {why}")
            continue
        if not isinstance(doc, dict):
            rep.fail("2", where, f"{where}: trace is not a JSON object")
            continue
        step2 = len(rep.problems)
        if doc.get("id") != tid:
            rep.fail("2", where, f"{where}: trace id {field(doc.get('id'))} != trace_id {field(tid)}")
        # §12: the trace's own schema_version declares its version; the
        # listing only records it, so a difference is noted, not failed.
        sv = doc.get("schema_version")
        if t.get("schema_version") != sv:
            rep.notes.append(f"{where}: listing schema_version {field(t.get('schema_version'))} "
                             f"differs from the trace's {field(sv)}; the trace's decides (§12)")
        v03 = isinstance(sv, str) and _TRACE_V03.fullmatch(sv) is not None
        listing_only = False
        if not v03 and v011:
            rep.fail("2", where, f"{where}: trace schema_version {field(sv)} is not v0.3.x; an envelope "
                                 f"declaring {field(version)} bundles only v0.3.x traces (§12)")
        elif not v03 and sv != _TRACE_V02:
            rep.fail("2", where, f"{where}: trace schema_version {field(sv)} is neither v0.3.x nor "
                                 f"v0.2.x (§12)")
        elif not v03:
            # A v0.2.x trace under 0.1.0 defines no content_hash: only the
            # listing's hash is checked (§6.2 step 2, §12).
            listing_only = True
            if "content_hash" in doc:
                rep.notes.append(f"{where}: v0.2.x trace under 0.1.0: its content_hash member is not "
                                 f"checked (v0.2.x defines none; §12)")
        try:
            recomputed = compute_content_hash(doc)
        except (ValueError, TypeError) as e:
            rep.fail("2", where, f"{where}: content_hash cannot be recomputed: {field(e)}")
            recomputed = None
        if recomputed is not None:
            if not listing_only and doc.get("content_hash") != recomputed:
                rep.fail("2", where, f"{where}: stored content_hash does not match the trace content")
            if t.get("trace_content_hash") != recomputed:
                rep.fail("2", where, f"{where}: trace_content_hash does not match the trace content")
        # A lineage the §6.3 checks below cannot read is a failed check by
        # name, not a crash (ruled 2026-10-21).
        for why in _lineage_shape_problems(doc.get("lineage"), v03):
            rep.fail("2", where, f"{where}: {why}")
        if len(rep.problems) == step2 and tid not in shared:
            rep.record("2", where, "passed",
                       "v0.2.x trace under 0.1.0: listing hash only (§12)" if listing_only else "")
        # A listing that states a trace_type other than the trace's own is
        # noted, not a failed check: the manifest describes, the trace
        # decides (ruled 2026-10-29). A trace that states none (v0.2.x has
        # no trace_type) is not compared.
        if "trace_type" in t and "trace_type" in doc and t.get("trace_type") != doc.get("trace_type"):
            rep.notes.append(f"{where}: the listing's trace_type {field(t.get('trace_type'))} differs from "
                             f"the trace's {field(doc.get('trace_type'))}")
        _trace_schema_check(rep, where, doc, v03)
        if isinstance(tid, str) and tid not in shared:
            by_id[tid] = {"entry": t, "doc": doc, "v03": v03}

    # §6.3: an edge to a bundled trace whose hash differs refers to another
    # state of the target and is not verified against it (not a failed check).
    for tid, rec in by_id.items():
        lineage = rec["doc"].get("lineage")
        for j, e in enumerate(lineage if isinstance(lineage, list) else []):
            if not _lineage_entry_readable(e, rec["v03"]):
                continue   # failed in step 2 (_lineage_shape_problems)
            target, h = e.get("target_trace_id"), e.get("target_content_hash")
            if isinstance(target, str) and target in shared:
                rep.notes.append(f"§6.3: {field(tid)} lineage[{j}] -> {field(target)}: the target id is held "
                                 f"by {shared[target]} trace listings; the edge names no one bundled trace")
            elif isinstance(h, str) and target in by_id and by_id[target]["entry"].get("trace_content_hash") != h:
                rep.notes.append(f"§6.3: {field(tid)} lineage[{j}] -> {field(target)}: target_content_hash "
                                 f"differs from the manifest: the edge refers to a state of the target other "
                                 f"than the one bundled, not verified against it")

    for section in ("raw_artefacts", "bindings"):
        for i, a in enumerate(m.get(section) or []):
            where = f"{section}[{i}]"
            rel = a.get("path")
            # §4.7, §4.11 (ruled 2026-10-21): a produces_trace_id names the
            # trace_id of exactly one listing, counted over every listing; else
            # it is noted and the entry applies to no trace, not to the bundle.
            # Not a step-3 failure (step 3 checks bytes); checked before the
            # file, so a removed or missing entry is noted too.
            ptid = a.get("produces_trace_id")
            if ptid is not None and held.get(ptid, 0) != 1:
                n = held.get(ptid, 0)
                rep.notes.append(f"{where}: produces_trace_id {field(ptid)} "
                                 + (f"is held by {n} trace listings" if n else "names no trace listing")
                                 + "; the entry applies to no trace, and not to the bundle as a whole (§4.7)")
            removed = section == "raw_artefacts" and _removed_as_a_whole(a.get("redaction"))
            if removed and rel is None:
                rep.notes.append(f"{where}: {field(a.get('id'))} removed as a whole (§10.4): no file listed")
                rep.record("3", where, "removed", "removed as a whole: nothing to verify")
                continue
            before = len(rep.problems)   # a path listed twice is this entry's failure too
            if _listed_before(seen_paths, root, rel):
                rep.fail("3", where, f"{where}: path {field(rel)} already listed")
            f, why = resolve_member(root, rel)
            if f is None:
                # A removed entry may list a path whose file is absent (0.1.0
                # still required path); the path rules still hold (§3.2,
                # §14.1), and only nothing at all at that path is absence;
                # a path that cannot be examined is not.
                if removed and why == "no such regular file" and _nothing_at(root / rel):
                    rep.notes.append(f"{where}: {field(a.get('id'))} removed as a whole (§10.4): "
                                     f"its listed file {field(rel)} is absent")
                    rep.record("3", where, "removed", "removed as a whole: nothing to verify")
                    continue
                rep.fail("3", where, f"{where}: {why}: {field(rel)}")
                continue
            digest, unread = _member_sha256(f)
            if unread is not None:
                rep.fail("3", where, f"{where}: {unread}: {field(rel)}")
                continue
            if digest != a.get("content_hash"):
                rep.fail("3", where, f"{where}: content_hash does not match the file bytes")
            if "size_bytes" in a and a["size_bytes"] != f.stat().st_size:
                rep.fail("3", where, f"{where}: size_bytes does not match the file")
            if len(rep.problems) == before:
                rep.record("3", where, "passed")

    for i, w in enumerate(m.get("witnesses") or []):
        _check_witness(rep, root, f"witnesses[{i}]", w, v011, by_id, seen_paths, shared=shared, held=held)

    # Completeness is not a manifest property the format can prove, but files
    # present and unlisted carry no integrity, and a trace removed from the
    # manifest would otherwise go unmentioned.
    signature = root / "signature.json"
    found, unexamined = _envelope_files(root)
    unlisted = sorted(str(f.relative_to(root)) for f in found
                      if f not in (manifest_path, signature) and str(f.relative_to(root)) not in seen_paths)
    if unlisted:
        rep.notes.append(f"{len(unlisted)} file(s) in the envelope directory are not listed in "
                         f"the manifest and carry no integrity: "
                         f"{', '.join(field(u, 80) for u in unlisted[:10])}")
    if unexamined:
        rep.notes.append(f"{len(unexamined)} path(s) in the envelope directory could not be examined: "
                         f"{', '.join(field(u, 80) for u in sorted(unexamined)[:10])}; unlisted files "
                         f"there are not accounted for")
    # §9: signature data lives in signature.json at the envelope root; §6.2
    # step 5 applies only when a signature is present.
    if signature.is_symlink() or (signature.exists() and not signature.is_file()):
        rep.signed = True
        rep.notes.append("signature.json is not a regular file at the envelope root (a symlink, a "
                         "directory or another special file): not read")
        rep.record("5", "signature.json", "unsupported", "not a regular file; not read")
    elif not signature.is_file():
        rep.notes.append("unsigned: there is no step 5 to perform; producer authenticity is not "
                         "established (§5.2)")
    else:
        _signature_check(rep, signature, max_manifest_bytes, m.get("envelope_version"))
    return rep


def _envelope_files(root: pathlib.Path) -> tuple[list[pathlib.Path], list[str]]:
    """Every regular file and symlink under root (symlinked directories are
    not entered), and the paths, relative to root, that could not be listed
    or examined: noted, not an unreadable envelope (ruled 2026-10-26)."""
    found: list[pathlib.Path] = []
    unexamined: list[str] = []

    def failed(e: OSError) -> None:
        name = pathlib.Path(e.filename) if e.filename else root
        try:
            unexamined.append(str(name.relative_to(root)) or ".")
        except ValueError:
            unexamined.append(str(name))

    for dirpath, dirnames, filenames in os.walk(root, onerror=failed):
        for name in dirnames + filenames:
            p = pathlib.Path(dirpath) / name
            try:
                mode = os.lstat(p).st_mode
            except OSError as e:
                failed(e)
                continue
            if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                found.append(p)
    return found, unexamined


def _signature_check(rep: Report, signature: pathlib.Path, max_bytes: int, version: Any) -> None:
    """signature.json is read (ruled 2026-10-21): an empty `signatures` list
    is unsigned, with no step 5; one that does not parse, is not an object,
    or whose `signatures` is not a list fails step 5 (§9.5: failure is
    failure, never "unsigned"); a non-empty list is present and unverified,
    step 5 unsupported (no signature profile in this package). Each entry
    (ruled 2026-10-26) is held to the schema's SignatureFile shape, and one
    whose covers includes manifest has its manifest_hash compared with the
    SHA-256 of the manifest bytes (§9.1); a malformed entry or a differing
    hash fails step 5, a match leaves it unsupported."""
    try:
        doc = _load_json(_read_manifest(signature, max_bytes, what="signature.json"))
    except (OSError, ValueError, EnvelopeRefused) as e:
        rep.signed = True
        rep.fail("5", "signature.json", f"signature.json: unreadable: {field(e)}")
        return
    sigs = doc.get("signatures") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or not isinstance(sigs, list):
        rep.signed = True
        rep.fail("5", "signature.json", "signature.json: "
                 + ("not a JSON object" if not isinstance(doc, dict) else "signatures is not a list")
                 + " (§9.1)")
        return
    if not sigs:
        rep.notes.append("unsigned: signature.json lists no signature, so there is no step 5 to "
                         "perform; producer authenticity is not established (§5.2)")
        return
    rep.signed = True
    rep.notes.append(f"signature.json lists {len(sigs)} signature(s), NOT verified (no signature "
                     f"profile in this package)")
    shape = _signature_shape_problems(doc, version)
    # A problem outside any entry (no validator, the error cap reached) fails
    # the file with every located problem, and no entry is read unchecked.
    if any(not p.startswith("signatures/") for p in shape):
        for p in shape:
            rep.fail("5", "signature.json", f"signature.json: {p}")
        return
    for i, s in enumerate(sigs):
        subject = f"signature.json signatures[{i}]"
        mine = [p for p in shape if p.startswith(f"signatures/{i}:") or p.startswith(f"signatures/{i}/")]
        for p in mine:
            rep.fail("5", subject, f"signature.json: {p}")
        if mine:
            continue
        who = f"signatures[{i}] ({field(s.get('id'), 80)})"
        if "manifest" not in s["covers"]:
            rep.notes.append(f"{who}: covers does not include manifest; manifest_hash not compared")
        elif s["manifest_hash"] != rep.manifest_sha256:
            rep.fail("5", subject, f"signature.json: {who}: manifest_hash does not match the manifest's "
                                   f"SHA-256 (§9.1, §9.3)")
            continue
        else:
            rep.notes.append(f"{who}: manifest_hash matches the manifest; the signature itself is not verified")
        rep.record("5", subject, "unsupported", "no signature profile in this package")


def _signature_schema(version: Any) -> pathlib.Path:
    """The schema whose SignatureFile $def signature.json is held to: that of
    the envelope revision the manifest declares."""
    return SCHEMA_V010 if version == "0.1.0" else SCHEMA


def _signature_shape_problems(doc: dict, version: Any) -> list[str]:
    """How signature.json departs from the SignatureFile $def of the schema
    of the envelope revision the manifest declares, located by JSON path
    ("signatures/0/manifest_hash: ..."), at most MAX_SCHEMA_ERRORS of them;
    a missing validator is itself a problem."""
    import itertools  # noqa: PLC0415
    try:
        import jsonschema  # noqa: PLC0415
        root = json.loads(_signature_schema(version).read_text(encoding="utf-8"))
        # The $defs alone: the manifest's own top-level rules do not apply.
        validator = jsonschema.Draft202012Validator({"$defs": root["$defs"], "$ref": "#/$defs/SignatureFile"})
        errs = list(itertools.islice(validator.iter_errors(doc), MAX_SCHEMA_ERRORS + 1))
    except RecursionError:
        return ["(file): nested too deeply to validate"]
    except Exception as e:  # noqa: BLE001 - any failure to load fails the check
        return [f"(file): validator unavailable ({e.__class__.__name__}: {field(e)})"]
    out = [f"{field('/'.join(str(x) for x in e.absolute_path), 300) or '(file)'}: {field(e.message, 200)}"
           for e in sorted(errs[:MAX_SCHEMA_ERRORS], key=lambda e: [str(x) for x in e.absolute_path])]
    if len(errs) > MAX_SCHEMA_ERRORS:
        out.append(f"(file): and more (stopped after {MAX_SCHEMA_ERRORS})")
    return out


_TRACE_VALIDATOR: list = []   # the schemas/validate.py module, loaded once, or the error loading it


def _trace_validator():
    if not _TRACE_VALIDATOR:
        import importlib.util  # noqa: PLC0415
        try:
            spec = importlib.util.spec_from_file_location(
                "minitrace_schema_validate", SCHEMAS / "validate.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _TRACE_VALIDATOR.append(mod)
        except Exception as e:  # noqa: BLE001 - any failure to load fails the check, never a skip
            _TRACE_VALIDATOR.append(e)
    return _TRACE_VALIDATOR[0]


def _trace_schema_check(rep: Report, where: str, doc: dict, v03: bool) -> None:
    """The trace schema check (ruled 2026-10-21), labelled "trace schema",
    not a §6.2 step: a v0.3.x trace against the base schema and its
    trace_type schema, at most MAX_SCHEMA_ERRORS errors reported; a v0.2.x
    trace has no schema here and is skipped."""
    if not v03:
        sv = doc.get("schema_version")
        rep.record("trace schema", where, "skipped", "v0.2.x trace: no schema in this package" if sv == _TRACE_V02
                   else f"schema_version {field(sv)} is not v0.3.x: no schema applied")
        return
    v = _trace_validator()
    if isinstance(v, Exception):
        rep.fail("trace schema", where, f"{where}: trace schema: validator unavailable "
                                        f"({v.__class__.__name__}: {field(v)})")
        return
    try:
        # Bounded: the validator stops after one error more than reported.
        errors, warnings = v.validate_doc(doc, max_errors=MAX_SCHEMA_ERRORS + 1)
    except Exception as e:  # noqa: BLE001 - a validator crash fails the check
        errors, warnings = [f"validator raised {e.__class__.__name__}: {field(e)}"], []
    for e in errors[:MAX_SCHEMA_ERRORS]:
        rep.fail("trace schema", where, f"{where}: trace schema: {field(e, 200)}")
    if len(errors) > MAX_SCHEMA_ERRORS:
        rep.fail("trace schema", where, f"{where}: trace schema: and more (stopped after {MAX_SCHEMA_ERRORS})")
    if not errors:
        # An unknown trace_type with a schema_uri is valid but uninterpreted
        # (minitrace-spec §3.2): base schema only, noted (ruled 2026-10-21).
        # Base schema only is not a pass of the trace schema check: its
        # type schema was not applied (ruled 2026-10-29).
        for w in warnings:
            rep.notes.append(f"{where}: trace schema: {field(w, 200)}")
        rep.record("trace schema", where, "unsupported" if warnings else "passed",
                   "base schema only" if warnings else "")


def _clock_checks(rep: Report, m: dict) -> None:
    """§8 and §14.5 (ruled 2026-10-21): a manipulated clock is surfaced on
    the verdict line; a clock reference naming no declared clock is noted,
    and so is a trace without clock_id when two or more clocks are declared.
    None of these is a failed check. Types are the schema's, checked before."""
    clocks = m.get("clocks") or []
    counts = collections.Counter(c.get("id") for c in clocks)   # one pass: up to MAX_ITEMS clocks
    ids = set(counts)
    for cid, n in sorted(counts.items()):
        if n > 1:   # ruled 2026-10-21
            rep.notes.append(f"clocks: id {field(cid)} declared {n} times; a binding to it names no one clock (§8.5)")
    for c in clocks:
        if c.get("manipulated") is True:
            entry = f"{field(c.get('id'))}: {field(c.get('manipulation_notes'), 200)}"
            rep.manipulated.append(entry)
            rep.notes.append(f"clocks: {entry}: manipulated; untrusted for wall-time inferences and not "
                             f"to be presumed monotonic (§8.3)")
    for i, t in enumerate(m.get("traces") or []):
        if "clock_id" in t and t["clock_id"] not in ids:
            rep.notes.append(f"traces[{i}]: clock_id {field(t['clock_id'])} names no declared clock (§8.5)")
        elif "clock_id" not in t and len(clocks) >= 2:
            rep.notes.append(f"traces[{i}]: no clock_id while {len(clocks)} clocks are declared; its "
                             f"timestamps cannot be bound to one (§8.5)")
    host = (m.get("producer") or {}).get("host_clock_id")
    if host is not None and host not in ids:
        rep.notes.append(f"producer.host_clock_id {field(host)} names no declared clock: the envelope's "
                         f"own timestamps are host_system, unattested (§8.6)")


_EVENT_ARRAYS = (("tool_call", "tool_calls"), ("segment", "segment_markers"), ("policy_trigger", "policy_triggers"))


def _event_ids(doc: dict) -> dict[str, list[str]]:
    """Every event identifier of a trace (minitrace-spec §4.2) -> the event
    types holding it: a turn as "turn-<index>" with the integer index in
    plain decimal, tool calls, segment markers and policy triggers by their
    id, verbatim. Entries that carry no usable identifier name nothing."""
    out: dict[str, list[str]] = {}
    turns = doc.get("turns")
    for t in turns if isinstance(turns, list) else []:
        ix = t.get("index") if isinstance(t, dict) else None
        if isinstance(ix, (int, float)) and not isinstance(ix, bool) and float(ix).is_integer() and ix >= 0:
            out.setdefault(f"turn-{int(ix)}", []).append("turn")
    for kind, key in _EVENT_ARRAYS:
        events = doc.get(key)
        for e in events if isinstance(events, list) else []:
            if isinstance(e, dict) and isinstance(e.get("id"), str):
                out.setdefault(e["id"], []).append(kind)
    return out


def _event_scope_problem(doc: dict, stype: str, target_id: Any) -> str | None:
    """None if target_id names exactly one event of type stype in doc, else
    why not (ruled 2026-10-20): a turn only as "turn-<index>"; no event, an
    event of another type, or an id held by more than one event (§4.2: ids
    are unique within the trace) is a failed scope check."""
    # Both envelope schemas require a string target_id here.
    holders = _event_ids(doc).get(target_id, []) if isinstance(target_id, str) else []
    if not holders:
        return f"names no {stype} in the bundled trace"
    if len(holders) > 1:
        return f"is held by {len(holders)} events ({', '.join(holders)}); §4.2 ids are unique within a trace"
    if holders[0] != stype:
        return f"names a {holders[0]}, not a {stype}"
    return None


def _check_witness(rep: Report, root: pathlib.Path, where: str, w: dict, v011: bool,
                   by_id: dict[str, dict], seen_paths: set[Any], shared: dict | None = None,
                    held: dict | None = None) -> None:
    """§6.2 steps 3 and 4 and §7.3 scope for one witness record, then one
    summary record for the witness (step "witness")."""
    first = len(rep.checks)
    _witness_checks(rep, root, where, w, v011, by_id, seen_paths, shared, held)
    _x_witness_checks(rep, root, where, w)
    # A scope failure, an event target_id included, keeps a witness from
    # being verified.
    mine = [c["status"] for c in rep.checks[first:]]
    for status in ("failed", "skipped", "unsupported", "removed"):
        if status in mine:
            break
    else:
        status = "passed"
    rep.record("witness", where, status,
               "verified" if status == "passed" else "not verified")


_CAPTURE_STATUSES = ("captured", "unsupported_platform", "attach_failed")
_KEYLOG = "x-tls-keylog-trace"
_KEYLOG_REPORTED = 10   # bad records reported one by one per witness; the rest are counted
_KEYLOG_LINE_BYTES = 1 << 20   # a keylog record is a handful of fields (ruled 2026-10-21)


def _x_witness_checks(rep: Report, root: pathlib.Path, where: str, w: dict) -> None:
    """§7.6 consumer duties (ruled 2026-10-21). An x-* witness whose
    metadata.capture_status is present and not "captured" is noted (§7.6.1:
    an assembler must not emit one; absent reads as captured). Every record
    of an x-tls-keylog-trace payload, read as NDJSON (§7.6), must carry
    redaction_class "amber" or "red" (§7.6.7); a record missing it, carrying
    "green" or anything else, or a line that is not a JSON object fails the
    check labelled "x-tls-keylog-trace". A payload removed or absent is
    skipped."""
    kind = w.get("kind")
    if not (isinstance(kind, str) and kind.startswith("x-")):
        return
    meta = w.get("metadata")
    if isinstance(meta, dict) and "capture_status" in meta and meta["capture_status"] != "captured":
        status = meta["capture_status"]
        known = isinstance(status, str) and status in _CAPTURE_STATUSES
        rep.notes.append(f"{where}: {field(kind)} capture_status {field(status)}"
                         + ("" if known else " (outside the v0.1.x set)")
                         + ": the capture did not run as declared, and an assembler must not emit "
                           "this record (§7.6.1)")
    if kind != _KEYLOG:
        return
    if _removed_as_a_whole(w.get("redaction")):
        rep.record(_KEYLOG, where, "removed", "removed as a whole: no records to check")
        return
    listed = ([w["primary_path"]] if w.get("primary_path") is not None else []) + list(w.get("auxiliary_paths") or [])
    bad: list[str] = []
    checked = records = 0
    for rel in dict.fromkeys(listed):   # a path listed twice is read once (step 3 reports the duplicate)
        f, _why = resolve_member(root, rel)
        if f is None:
            rep.record(_KEYLOG, f"{where} {field(rel)}", "skipped", "payload file absent or unreadable")
            continue
        checked += 1
        try:
            for lineno, raw in _bounded_lines(f, _KEYLOG_LINE_BYTES):
                if raw is None:
                    bad.append(f"{field(rel)} line {lineno}: longer than {_KEYLOG_LINE_BYTES} bytes, not read")
                    continue
                if not raw.strip():
                    continue
                records += 1
                why = _keylog_record_problem(raw)
                if why:
                    bad.append(f"{field(rel)} line {lineno}: {why}")
        except (OSError, ValueError) as e:
            bad.append(f"{field(rel)}: unreadable ({field(e)})")
    for b in bad[:_KEYLOG_REPORTED]:
        rep.fail(_KEYLOG, where, f"{where}: {_KEYLOG} record rejected (§7.6.7): {b}")
    if len(bad) > _KEYLOG_REPORTED:
        rep.fail(_KEYLOG, where, f"{where}: {_KEYLOG}: and {len(bad) - _KEYLOG_REPORTED} more rejected record(s)")
    if checked and not bad:
        rep.record(_KEYLOG, where, "passed", "every record amber or red" if records else "no records")


def _bounded_lines(f: pathlib.Path, limit: int):
    """(line number, bytes) for each line of a regular file, opened without
    following a final symlink or blocking and checked on the descriptor
    (as the manifest is read); a line longer than `limit` bytes yields None
    and is skipped unread, so memory stays bounded (ruled 2026-10-21)."""
    fd = os.open(f, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        fh = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with fh:
        lineno = 0
        while True:
            line = fh.readline(limit + 1)
            if not line:
                return
            lineno += 1
            if len(line) > limit and not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):   # skip the rest of the long line
                    line = fh.readline(limit + 1)
                yield lineno, None
            else:
                yield lineno, line


def _keylog_record_problem(raw: bytes) -> str | None:
    try:
        rec = json.loads(raw.decode("utf-8"), object_pairs_hook=_refuse_duplicates,
                         parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as e:
        return f"not a JSON object ({field(e, 80)})"
    if not isinstance(rec, dict):
        return f"not a JSON object (a JSON {_json_type(rec)})"
    if "redaction_class" not in rec:
        return "redaction_class missing"
    if rec["redaction_class"] not in ("amber", "red"):
        return f"redaction_class {field(rec['redaction_class'], 40)}, not amber or red"
    return None


def _witness_checks(rep: Report, root: pathlib.Path, where: str, w: dict, v011: bool,
                    by_id: dict[str, dict], seen_paths: set[Any], shared: dict | None = None,
                    held: dict | None = None) -> None:
    shared = shared or {}   # trace id -> the number of listings holding it, two or more
    if held is None:        # every listing's trace_id -> its count; a direct caller passes by_id only
        held = {**{k: 1 for k in by_id}, **shared}
    # Step 4: the record's own content_hash over its canonical form,
    # content_hash excluded (§7.1). From 0.1.1 the record must meet the §3.4
    # rules, a failed check otherwise; under 0.1.0 a record without a
    # reproducible canonical form is unverifiable, a skipped check (§7.1).
    canonical = True
    try:
        _, why = trace_json_verdict(json.dumps(w).encode("utf-8"))
    except RecursionError:   # nested past the serialiser's reach, so far past 64
        why = "depth-exceeds-64"
    if why is not None:
        canonical = False
        if v011:
            rep.fail("4", where, f"{where}: witness record outside the §3.4 rules: {why}")
        else:
            rep.notes.append(f"{where}: record outside the §3.4 rules (0.1.0): {why}; its "
                             f"content_hash is unverifiable")
            rep.record("4", where, "skipped", "0.1.0 record outside the §3.4 rules")
    if canonical:
        try:
            recomputed = compute_content_hash(w)
        except (ValueError, TypeError, RecursionError) as e:
            recomputed = None
            if v011:
                rep.fail("4", where, f"{where}: witness content_hash cannot be recomputed: {field(e)}")
            else:
                rep.notes.append(f"{where}: record has no reproducible canonical form (0.1.0): its "
                                 f"content_hash is unverifiable")
                rep.record("4", where, "skipped", "0.1.0 record without a reproducible canonical form")
        if recomputed is not None:
            if w.get("content_hash") != recomputed:
                rep.fail("4", where, f"{where}: witness content_hash does not match its record")
            else:
                rep.record("4", where, "passed")

    # §7.3 (ruled 2026-10-20, 2026-10-21): a trace scope names one listing's
    # trace_id, else it is noted; a sub-trace scope resolves to one event in
    # one bundled trace, else it fails.
    scope = w.get("scope") or {}
    stype = scope.get("type")
    n_listed = held.get(scope.get("target_trace_id"), 0)
    if stype == "trace" and n_listed != 1:
        # §7.3 (ruled 2026-10-21): a trace scope naming no listing, or two or
        # more, is noted and applies to no bundled trace; no scope record, not
        # a failed check. One listing resolves, read or not.
        rep.notes.append(f"{where}: scope target_trace_id {field(scope.get('target_trace_id'))} "
                         + (f"is held by {n_listed} trace listings" if n_listed else "names no trace listing")
                         + "; the witness applies to no bundled trace (§7.3)")
    elif stype == "trace" and scope.get("target_trace_id") not in by_id:
        rep.record("scope", where, "passed", "target trace listed (its file absent or not parsed)")
    elif stype is not None and stype != "envelope":
        target = scope.get("target_trace_id")
        if target in shared:
            rep.fail("scope", where, f"{where}: scope target_trace_id {field(target)} is held by "
                                     f"{shared[target]} trace listings; it names no one trace")
        elif target not in by_id and n_listed == 1:
            rep.fail("scope", where, f"{where}: scope target_trace_id {field(target)}: the trace's file is "
                                     f"absent or cannot be parsed, so it resolves no event (§7.3)")
        elif target not in by_id:
            rep.fail("scope", where, f"{where}: scope target_trace_id {field(target)} is not a bundled trace")
        else:
            rep.record("scope", where, "passed", "target trace bundled")
            if stype != "trace":
                # Resolved against the bundled file even when it failed step 2;
                # step 2 decides that (ruled 2026-10-20).
                why = _event_scope_problem(by_id[target]["doc"], stype, scope.get("target_id"))
                if why:
                    rep.fail("scope", where, f"{where}: scope target_id {field(scope.get('target_id'))} "
                                             f"in trace {field(target)}: {why}")
                else:
                    rep.record("scope", where, "passed", f"target_id names one {stype}")

    hashes = w.get("path_hashes")
    if hashes is not None and not isinstance(hashes, dict):
        # Undefined in the 0.1.0 schema, so any type reaches here.
        rep.fail("3", where, f"{where}: path_hashes is not an object")
        return

    # Step 3, removed as a whole (§7.4): no payload listed, nothing to verify.
    if _removed_as_a_whole(w.get("redaction")):
        problems = []
        if v011 and "size_bytes" in w:
            # A 0.1.1 rule (§7.4); 0.1.0 left it unstated (§12).
            problems.append("removed as a whole but carries size_bytes (§7.4)")
        if hashes:
            problems.append("removed as a whole but path_hashes is not empty (§7.1)")
        listed = ([w["primary_path"]] if w.get("primary_path") is not None else []) \
            + list(w.get("auxiliary_paths") or [])
        for rel in listed:
            # 0.1.0 still required primary_path; nothing may be at it (§7.4).
            if _listed_before(seen_paths, root, rel):
                rep.fail("3", where, f"{where}: path {field(rel)} already listed")
            f, why = resolve_member(root, rel)
            if f is not None or not (why == "no such regular file" and _nothing_at(root / rel)):
                problems.append(f"removed as a whole but {field(rel)} {'is present' if f is not None else why}")
        for problem in problems:
            rep.fail("3", where, f"{where}: {problem}")
        if problems:
            return
        rep.notes.append(f"{where}: {field(w.get('id'))} removed as a whole (§7.4): no payload listed")
        rep.record("3", where, "removed", "removed as a whole: nothing to verify")
        return

    # Step 3: payload files against path_hashes.
    listed = ([w["primary_path"]] if w.get("primary_path") is not None else []) + list(w.get("auxiliary_paths") or [])
    files: dict[str, pathlib.Path] = {}
    for rel in listed:
        if _listed_before(seen_paths, root, rel):
            rep.fail("3", where, f"{where}: path {field(rel)} already listed")
        f, why = resolve_member(root, rel)
        if f is None:
            rep.fail("3", where, f"{where}: {why}: {field(rel)}")
        else:
            files[rel] = f
    if "size_bytes" in w and w.get("primary_path") in files \
            and w["size_bytes"] != files[w["primary_path"]].stat().st_size:
        rep.fail("3", where, f"{where}: size_bytes does not match the primary file")
    if hashes is None:
        if v011:
            rep.fail("3", where, f"{where}: path_hashes is missing (§7.1, required from 0.1.1)")
        elif listed:
            rep.notes.append(f"{where}: no path_hashes (0.1.0): its payload files are unverifiable "
                             f"and the witness is not verified")
            rep.record("3", where, "skipped", "0.1.0 witness without path_hashes: payload unverifiable")
        return
    for rel in listed:
        subject = f"{where} {field(rel)}"
        if rel not in hashes:
            if v011:
                rep.fail("3", where, f"{where}: path_hashes has no key for listed path {field(rel)}")
            else:
                rep.record("3", subject, "skipped", "no path_hashes entry (0.1.0): unverifiable")
            continue
        f = files.get(rel)
        if f is None:
            continue   # its path problem is already reported
        digest, unread = _member_sha256(f)
        if unread is not None:
            rep.fail("3", where, f"{where}: {unread}: {field(rel)}")
        elif digest != hashes[rel]:
            rep.fail("3", where, f"{where}: path_hashes[{field(rel)}] does not match the file bytes")
        else:
            rep.record("3", subject, "passed")
    for key in hashes:
        if key not in listed:
            if v011:
                rep.fail("3", where, f"{where}: path_hashes names {field(key)}, which the record does not list")
            else:
                # §6.2 step 3 covers every entry; one naming no listed path
                # cannot be checked against a listed file.
                rep.notes.append(f"{where}: path_hashes names {field(key)}, which the record does not list")
                rep.record("3", f"{where} {field(key)}", "skipped", "entry names a path the record does not list")


def _positive(s: str) -> int:
    n = int(s)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {s}")
    return n


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="verify_envelope.py", description=(__doc__ or "").split("\n\n", 1)[0])
    ap.add_argument("manifest", type=pathlib.Path)
    ap.add_argument("--max-manifest-bytes", type=_positive, default=MAX_MANIFEST_BYTES)
    ap.add_argument("--max-items", type=_positive, default=MAX_ITEMS, help="entries per listing")
    ap.add_argument("--max-trace-bytes", type=_positive, default=MAX_TRACE_BYTES)
    ap.add_argument("--json", action="store_true", help="print the full report (every check) as JSON")
    try:
        args = ap.parse_args(argv[1:])
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        rep = verify(args.manifest, max_manifest_bytes=args.max_manifest_bytes,
                     max_items=args.max_items, max_trace_bytes=args.max_trace_bytes)
    except EnvelopeRefused as e:
        print(f"envelope refused (limit): {field(e)}", file=sys.stderr)
        return 2
    except (OSError, ValueError, UnicodeDecodeError) as e:
        print(f"envelope unreadable: {field(e)}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"manifest_sha256": rep.manifest_sha256, "problems": rep.problems,
                          "notes": rep.notes, "checks": rep.checks, "counts": rep.counts(),
                          "witness_summaries": rep.witness_counts(), "verdict": rep.verdict()},
                         indent=2, ensure_ascii=False))
    else:
        print(f"  manifest: {rep.manifest_sha256}")
        for p in rep.problems:
            print(f"  PROBLEM {p}")
        for n in rep.notes:
            print(f"  note: {n}")
        print("  checks: " + ", ".join(f"{k} {v}" for k, v in rep.counts().items()))
        if any(c["step"] == "witness" for c in rep.checks):
            print("  witness summaries: " + ", ".join(f"{k} {v}" for k, v in rep.witness_counts().items()))
        print(f"  verdict: {rep.verdict()}")
        print(f"{'FAIL' if rep.problems else 'ok'}: {len(rep.problems)} problem(s)")
    return rep.exit_status()


if __name__ == "__main__":
    install_output_sanitiser()
    sys.exit(main(sys.argv))
