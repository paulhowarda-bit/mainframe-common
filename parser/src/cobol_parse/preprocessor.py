"""Stage 2 - the preprocessor: COPY / REPLACE / EXEC SQL INCLUDE.

A COBOL "parser" is really a pipeline, and most of the COBOL-specific work lives
here, before the grammar runs (references/parsing-cobol.md, Stage 2). Without it the
parser cannot see copybook-defined data items or procedure code - they are silently
missing, which is worse than flagged. This stage runs after format normalization and
rewrites the ``CodeLine`` stream:

* **COPY** member [(OF|IN) library] [REPLACING ==a== BY ==b== ...] - textual inclusion
  with a configurable resolver (search paths, extension list, missing-copybook policy)
  and pseudo-text / word substitution.
* **EXEC SQL INCLUDE** member END-EXEC - behaves like COPY.
* standalone **REPLACE ==a== BY ==b==** ... **REPLACE OFF**.

Copybooks are expanded recursively (with a cycle guard); expanded lines carry their
``origin`` member name for provenance, and the set of expanded/missing members is
reported so coverage is measurable, never silently dropped.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, List, Optional, Tuple

from cobol_xstate_core.artifact_service import (decode_member,
                               normalize_fetched)  # re-exported: see artifact_service
from .normalizer import CodeLine, SourceFormat, normalize

logger = logging.getLogger(__name__)

__all__ = ["normalize_fetched", "CopybookResolver", "PreprocessResult", "preprocess",
           "scan_copy_members"]


@dataclass
class CopybookResolver:
    """Locate copybooks. ``paths`` are searched in order, each combined with every
    extension in ``exts`` (plus the bare name).

    ``fetcher`` is an optional caller-supplied callable tried when the local search
    finds nothing - the hook for an estate's own artifact service (a network share
    client, a source-control API, a member-retrieval library). It mirrors the JCL
    reader's ``resolver(name) -> text`` contract, and accepts whatever shape that
    service already returns:

        fetcher(name) -> None                       # not found
                      -> "IDENTIFICATION DIVI..."   # the member text
                      -> (text, source_label)
                      -> {"text"|"content"|"source": ..., "path"|"source_path": ...}
                      -> {"copied_to": "data/X.cpy"} / {"path": ...}  # a file to read

    A dict carrying only a path (the common shape for a fetch-to-disk client) is read
    from that path, so a client that copies the member locally needs no adapter. A
    ``found: False`` dict is honored as "not found". Results are cached per name, so a
    member COPYed twice costs one fetch. An exception from the fetcher is swallowed
    into "not found" and noted - a flaky external service must not crash a batch run,
    and the missing copybook is already flagged loudly downstream.

    ``store`` is the prefetch stage's result: members already retrieved from the estate
    before the parse began (``prefetch.py``). It is consulted FIRST - ahead of the local
    paths and the fetcher - because it is the same member, already paid for. This is
    what makes the parse that drives the dependency manifest a parse with no holes in
    it, which is the whole reason prefetch runs first."""

    paths: List[str] = field(default_factory=list)
    exts: Tuple[str, ...] = ("", ".cpy", ".CPY", ".cbl", ".cob", ".copy", ".CBL")
    missing: str = "continue"  # 'continue' (stub + note) | 'error'
    fetcher: Optional[Callable[[str], object]] = None
    # UPPERCASED member name -> (text, source), from the prefetch stage.
    store: dict = field(default_factory=dict)
    # name -> (text, source) | None, so a repeated COPY does not re-hit the service.
    _cache: dict = field(default_factory=dict, repr=False)
    # Fetcher failures, surfaced by the caller if it wants them (name, message).
    fetch_errors: List[Tuple[str, str]] = field(default_factory=list)

    def resolve(self, name: str) -> Optional[Tuple[str, str]]:
        name = name.strip().strip("'\"")
        key = name.upper()
        hit = self.store.get(key)
        if hit is not None:
            return hit
        # Check the cache BEFORE the path sweep, and populate it from a local hit too.
        # It used to cover only the fetcher branch, so a member COPYed 40 times was
        # stat'd len(paths) x len(exts) times and re-read from disk on every site -
        # the dominant cost of a parse when the copybook library is a network share,
        # and contrary to the docstring's promise above.
        if key in self._cache:
            return self._cache[key]
        for base in self.paths or ["."]:
            for ext in self.exts:
                candidate = os.path.join(base, name + ext)
                if os.path.isfile(candidate):
                    # Explicit decode - the platform default is cp1252 on
                    # Windows, which mojibakes a UTF-8 copybook silently.
                    with open(candidate, "rb") as f:
                        got = (decode_member(f.read()), candidate)
                    self._cache[key] = got
                    return got
        if self.fetcher is None:
            return None
        try:
            got = self._normalize_fetched(self.fetcher(name), name)
        except Exception as exc:                      # a flaky service is not fatal
            self.fetch_errors.append((key, f"{type(exc).__name__}: {exc}"))
            logger.debug("copybook fetcher raised for %r", name, exc_info=True)
            got = None
        self._cache[key] = got
        return got

    def _normalize_fetched(self, got, name: str) -> Optional[Tuple[str, str]]:
        return normalize_fetched(got, name)


@dataclass
class PreprocessResult:
    lines: List[CodeLine]
    expanded: List[str] = field(default_factory=list)  # members successfully copied
    missing: List[str] = field(default_factory=list)   # members not found
    notes: List[str] = field(default_factory=list)
    # Structured record of every COPY / EXEC SQL INCLUDE seen, in source order:
    # {member, status: expanded|missing|skipped-cyclic, via: COPY|EXEC SQL INCLUDE,
    #  replacing: bool}. The lists above stay for the notes they already feed; this
    # carries the copybook dependency out as data (the related-artifact manifest reads it).
    copybooks: List[dict] = field(default_factory=list)


# `(?<![A-Z0-9-])`: `\b` alone fires after a hyphen, so `\bCOPY\b` matched the tail of a
# data name - `ADD 1 TO CTR-COPY TOTALS.` read as `COPY TOTALS.`, which both discovered
# a phantom copybook (prefetched from the estate by name) and CONSUMED the ADD statement.
# Same defect and same guard as the PROGRAM-ID fix (5f787a9): a hyphen is a word
# character in COBOL even though the regex engine says otherwise.
_COPY_RE = re.compile(
    r"(?<![A-Z0-9-])COPY\b\s+([A-Z0-9$#@_.-]+|'[^']*'|\"[^\"]*\")"
    r"(?:\s+(?:OF|IN)\s+[A-Z0-9$#@_-]+)?"
    r"(?:\s+REPLACING\s+(?P<rep>.*?))?\s*\.\s*$",
    re.I | re.S,
)
_SQL_INCLUDE_RE = re.compile(
    r"\bEXEC\s+SQL\s+INCLUDE\s+([A-Z0-9$#@_-]+)\s+END-EXEC\s*\.?\s*$", re.I | re.S)

# Listing directives: they lay out the compiler listing and have no runtime meaning.
# Matched only as a WHOLE line so a data item or paragraph called TITLE is not eaten.
_LISTING_DIRECTIVE = re.compile(
    r"\s*(?:(?:EJECT|SKIP[123])\s*\.?|TITLE\s+(?:'[^']*'|\"[^\"]*\")\s*\.?)\s*$", re.I)
# Hot per-line tests, compiled once: `preprocess` runs these over every line of every
# program (and every copybook), so an uncompiled literal here is a corpus-scale cost.
_REPLACE_START = re.compile(r"\s*REPLACE\b", re.I)
_REPLACE_OFF = re.compile(r"\bREPLACE\s+(?:OFF|LAST\s+OFF)\b", re.I)
_REPLACE_HEAD = re.compile(r"^\s*REPLACE\b", re.I)
_SQL_INCLUDE_PROBE = re.compile(r"\bEXEC\s+SQL\s+INCLUDE\b", re.I)


def _parse_replacing(clause: str) -> List[Tuple[str, str]]:
    """Parse REPLACING pairs: ==a== BY ==b==  or  word BY word."""
    pairs: List[Tuple[str, str]] = []
    # Pseudo-text pairs first.
    for m in re.finditer(r"==(.*?)==\s+BY\s+==(.*?)==", clause, re.I | re.S):
        pairs.append((m.group(1).strip(), m.group(2).strip()))
    if pairs:
        return pairs
    for m in re.finditer(r"(\S+)\s+BY\s+(\S+)", clause, re.I):
        pairs.append((m.group(1), m.group(2)))
    return pairs


@lru_cache(maxsize=512)
def _replacing_pattern(a: str):
    """Compiled, whitespace-tolerant matcher for one REPLACING operand.

    Cached because the substitution runs per LINE while a REPLACE is active and for
    every line of every expanded copybook: recompiling the same handful of patterns
    per line is pure waste at corpus scale."""
    return re.compile(re.escape(a).replace(r"\ ", r"\s+"), re.I)


# A quoted literal is ONE text-word to COPY REPLACING, exactly as it is one token to
# the compiler - shared spelling of the rule data_division/_naming/semantics already
# apply: the words inside a literal are data, not syntax.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _apply_replacing(text: str, pairs: List[Tuple[str, str]]) -> str:
    """Apply REPLACING pairs the way the compiler does: to text-words, never inside a
    quoted literal.

    ``REPLACING ABC BY XYZ`` over the copybook line ``05 X PIC X(9) VALUE 'ABC MSG'.``
    must leave the literal alone - substituting inside it rewrote the VALUE, and since
    VALUE literals feed dynamic-CALL constant propagation, that could make a CALL
    "resolve", confidently, to a program that does not exist. An operand that is
    ITSELF a literal (``REPLACING 'ABC' BY 'XYZ'``) still works: quotes included, it
    can only ever match a whole literal, so it runs over the full text.
    """
    for a, b in pairs:
        if not a:
            continue
        pat = _replacing_pattern(a)
        rb = b.replace("\\", r"\\")
        if a[:1] in ("'", '"'):
            text = pat.sub(rb, text)
            continue
        out: List[str] = []
        pos = 0
        for m in _QUOTED.finditer(text):
            out.append(pat.sub(rb, text[pos:m.start()]))
            out.append(m.group(0))          # the literal, verbatim
            pos = m.end()
        out.append(pat.sub(rb, text[pos:]))
        text = "".join(out)
    return text


def _gather_statement(lines: List[CodeLine], i: int):
    """From line i, collect lines until one carries a period; return (text, next_i)."""
    parts = [lines[i].text]
    j = i
    while "." not in lines[j].text and j + 1 < len(lines):
        j += 1
        parts.append(lines[j].text)
    return " ".join(parts), j + 1


def scan_copy_members(text: str, fmt: Optional[SourceFormat] = None) -> List[str]:
    """Every member a ``COPY`` / ``EXEC SQL INCLUDE`` in ``text`` names, uppercased and
    de-duplicated, in source order.

    This is the prefetch stage's discovery pass, and it deliberately shares
    :data:`_COPY_RE` / :data:`_SQL_INCLUDE_RE` and :func:`_gather_statement` with
    :func:`preprocess` rather than re-implementing them. A second copy of this grammar
    would drift, and the failure mode of drift here is the quiet one: a COPY form the
    scanner misses is a member never prefetched, hence a data item never declared, hence
    a dynamic CALL that silently does not resolve.

    It resolves nothing and fetches nothing - it only reads the text it was given, which
    is what lets it run BEFORE anything has been retrieved."""
    lines = normalize(text, fmt)
    found: List[str] = []
    seen: set = set()
    i = 0
    while i < len(lines):
        up = lines[i].text.upper()
        if "COPY" in up or ("INCLUDE" in up and _SQL_INCLUDE_PROBE.search(up)):
            stmt, nxt = _gather_statement(lines, i)
            m = _COPY_RE.search(stmt) or _SQL_INCLUDE_RE.search(stmt)
            if m:
                member = m.group(1).strip().strip("'\"").upper()
                if member and member not in seen:
                    seen.add(member)
                    found.append(member)
                i = nxt
                continue
        i += 1
    return found


def preprocess(lines: List[CodeLine], resolver: Optional[CopybookResolver] = None,
               _seen: Optional[set] = None,
               fmt: Optional[SourceFormat] = None) -> PreprocessResult:
    resolver = resolver or CopybookResolver()
    _seen = _seen if _seen is not None else set()
    out: List[CodeLine] = []
    res = PreprocessResult(lines=out)

    active_replace: List[Tuple[str, str]] = []

    def emit(cl: CodeLine) -> None:
        if active_replace:
            cl = CodeLine(text=_apply_replacing(cl.text, active_replace), line=cl.line,
                          area_a=cl.area_a, origin=cl.origin)
        out.append(cl)

    i = 0
    while i < len(lines):
        line = lines[i]
        up = line.text.upper()
        if _LISTING_DIRECTIVE.match(up):
            # EJECT / SKIP1|2|3 / TITLE are listing directives: they format the compiler
            # listing and have NO runtime behavior. Left in the stream they parse as
            # statements, so the model grows phantom actions (an `EJECT` effect in the
            # statechart, an unknown op in the emitted module) for something that does
            # not exist at run time.
            i += 1
            continue
        if _REPLACE_START.match(up):
            # standalone REPLACE ==a== BY ==b== ... / REPLACE OFF: text substitution
            # active on every following line until turned off.
            stmt, nxt = _gather_statement(lines, i)
            if _REPLACE_OFF.search(stmt):
                active_replace = []
                i = nxt
                continue
            prs = _parse_replacing(_REPLACE_HEAD.sub("", stmt))
            if prs:
                active_replace = prs
                i = nxt
                continue
        # "INCLUDE" gates the second (expensive) probe: without it the regex ran on
        # essentially every line of every program, since `COPY` short-circuits rarely.
        if "COPY" in up or ("INCLUDE" in up and _SQL_INCLUDE_PROBE.search(up)):
            stmt, nxt = _gather_statement(lines, i)
            copy_m = _COPY_RE.search(stmt)
            m = copy_m or _SQL_INCLUDE_RE.search(stmt)
            if m:
                # Code preceding the COPY in the same gathered sentence (e.g.
                # ``MOVE 1 TO WS-IDX. COPY FOO.``) is real code - keep it.
                prefix = stmt[:m.start()].strip()
                if prefix:
                    emit(CodeLine(text=prefix, line=line.line,
                                  area_a=line.area_a, origin=line.origin))
                member = m.group(1)
                rep = m.groupdict().get("rep") if copy_m else None
                pairs = _parse_replacing(rep or "")
                _expand_member(member, pairs + active_replace, resolver, res, _seen, fmt,
                               via="COPY" if copy_m else "EXEC SQL INCLUDE",
                               replacing=bool(rep))
                i = nxt
                continue
        emit(line)
        i += 1
    return res


def _expand_member(member, pairs, resolver, res: PreprocessResult, seen: set,
                   fmt: Optional[SourceFormat] = None, via: str = "COPY",
                   replacing: bool = False) -> None:
    key = member.strip().strip("'\"").upper()

    def record(status: str, source: Optional[str] = None) -> None:
        row = {"member": key, "status": status, "via": via, "replacing": replacing}
        if source:
            # WHERE this member actually came from - a local path or the label an
            # external fetcher reported. Two programs "using DC01104" are only the
            # same dependency if the same member resolved, so the source is evidence.
            row["source"] = source
        res.copybooks.append(row)

    if key in seen:
        record("skipped-cyclic")
        res.notes.append(f"COPY {key}: recursive/cyclic include skipped")
        return
    found = resolver.resolve(member)
    if found is None:
        record("missing")
        res.missing.append(key)
        err = dict(getattr(resolver, "fetch_errors", None) or {}).get(key)
        res.notes.append(
            f"COPY {key}: copybook not found on search path"
            + (f" and the fetcher failed ({err})" if err else "")
            + " - members/logic it defines are NOT in the model")
        return
    text, path = found
    record("expanded", path)
    res.expanded.append(key)
    # A copybook inherits the including program's source format - it is a fragment,
    # far too small to auto-detect reliably on its own.
    sub = normalize(text, fmt)
    seen = seen | {key}
    # Recursively preprocess the copybook (nested COPY), then apply REPLACING.
    inner = preprocess(sub, resolver, seen, fmt)
    # Set-guarded: `x not in res.expanded` on a growing list is O(n^2), and a
    # copybook-heavy program pulls in hundreds of members transitively.
    already = set(res.expanded)
    for x in inner.expanded:
        if x not in already:
            already.add(x)
            res.expanded.append(x)
    res.missing.extend(inner.missing)
    res.notes.extend(inner.notes)
    res.copybooks.extend(inner.copybooks)   # nested COPY inside this member
    for cl in inner.lines:
        new_text = _apply_replacing(cl.text, pairs) if pairs else cl.text
        res.lines.append(CodeLine(text=new_text, line=cl.line,
                                  area_a=cl.area_a, origin=cl.origin or key))
