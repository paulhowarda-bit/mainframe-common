"""Stage 1 - the shared prefetch engine: one cache, one report, two closures.

Prefetch exists because of a circle. The dependency fetch (``fetch.py``) works from the
artifact manifest, and the manifest comes from a parse - so a run that parses whatever
text it happens to have builds its manifest from an incomplete model, and both of the
things a migration most needs are exactly what that loses:

* **Dynamic ``CALL`` targets.** ``CALL WS-SUBPGM`` resolves only when the single literal
  reaching ``WS-SUBPGM`` is visible, and that literal is almost always a ``VALUE`` clause
  in a copybook. Copybook absent -> the data item is not declared -> the target stays
  unresolved -> the called program is never fetched. Nothing errors; the answer is just
  quietly short, which is the worst possible failure for an impact analysis.
* **Calls inside control files.** A cataloged PROC, an ``INCLUDE`` member and a
  control-card dataset each carry ``EXEC PGM=`` steps that exist nowhere in the JCL file
  itself. Unresolved, those steps do not appear as programs, as datasets, or at all.

So stage 1 runs FIRST and closes over the text, transitively - a copybook COPYs
copybooks, a PROC INCLUDEs members - because the point is a source text with no holes in
it, and a hole one level down is still a hole.

**What lives here, and what does not.** The two languages discover their dependencies by
completely different means: COBOL is scanned lexically (``COPY X.`` names its member
right there), JCL by record-and-replay (only the JCL parser resolves symbols, so the
parse itself is replayed until it stops asking). Those two closures live with their
languages, in ``cobol_xstate.prefetch`` and ``jcl_dependencies.prefetch``. What lives
HERE is everything they do identically: deciding whether a name can be requested at all,
checking the local search path before the network, calling the estate, and - above all -
keeping the several distinct reasons a member did not arrive distinct from one another.

That last part is why the report's note text is one shared string covering both
languages rather than one per package: the note IS output, and splitting it would change
the bytes of every ``.prefetch.json`` for no gain.

**What this stage will not do:** invent a location. Where a member lives - which SYSLIB,
which library concatenation, which share - is knowledge this tool does not have and does
not model. It asks the estate's own artifact service by name, and reports what came back.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .artifact_service import (Fetched, ServiceUnavailable, call_service,
                               call_service_many, collect, decode_member)

# A member name that can plausibly be requested from a library. A control-card DD may
# instead name a full dataset (`PARM.LIB(SORTCRD)`), handled by _DSN_MEMBER below.
_MEMBER_NAME = re.compile(r"^[A-Z0-9$#@][A-Z0-9$#@._-]{0,43}$", re.I)
_DSN_MEMBER = re.compile(r"^(?P<dsn>[A-Z0-9$#@.-]+)\((?P<member>[A-Z0-9$#@]{1,8})\)$",
                         re.I)
# Extensions tried when looking for a member already on the local search path.
_LOCAL_EXTS = ("", ".cpy", ".CPY", ".cbl", ".cob", ".copy", ".CBL",
               ".jcl", ".JCL", ".prc", ".PRC", ".proc", ".txt")


def member_key(name: str) -> str:
    """The canonical form a member is cached and reported under: unquoted, uppercased."""
    return str(name).strip().strip("'\"").upper()


@dataclass
class PrefetchResult:
    """What stage 1 retrieved, and the honest account of what it could not.

    ``store`` is the payload the rest of the run consumes: pass it to
    ``CopybookResolver(store=...)`` for the COBOL parse and :meth:`resolver` to
    ``parse_jcl(resolver=...)``. Both then read members already paid for, so stage 2
    re-fetches nothing.

    One object is deliberately shared ACROSS languages: a COBOL run with ``--bind-jcl``
    passes the program's result into the JCL closure, so a member either of them
    retrieved is paid for once and reported once. That sharing only works while both
    packages see the same class, which is why this lives in core rather than being
    duplicated into each front-end."""

    source_name: str = "<source>"
    store: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    fetched: Dict[str, Fetched] = field(default_factory=dict)
    rows: List[dict] = field(default_factory=list)
    # Set when the estate client itself could not be reached. NOT the same as any
    # individual member being absent, and never reported as if it were.
    unavailable: Optional[str] = None

    def resolver(self) -> Callable[[str], Optional[str]]:
        """A ``resolver(name) -> text | None`` over the store, for ``parse_jcl``."""
        def _resolve(name: str) -> Optional[str]:
            hit = self.store.get(member_key(name))
            if hit is None:
                # A control-card DD names a full DSN; the member is what was retrieved.
                m = _DSN_MEMBER.match(member_key(name))
                if m:
                    hit = self.store.get(m.group("member").upper())
            return hit[0] if hit else None
        return _resolve

    @property
    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for row in self.rows:
            out[row["status"]] = out.get(row["status"], 0) + 1
        return out

    @property
    def missing(self) -> List[str]:
        """Members the source text needs and this run does not have - the holes that
        remain. Anything downstream that looks short should be read against this list."""
        return [r["member"] for r in self.rows
                if r["status"] in ("not-found", "error", "no-service")]

    def report(self) -> dict:
        return {
            "format": "cobol-xstate-prefetch",
            "source": self.source_name,
            "note": (
                "Stage 1: the members needed to COMPLETE THE SOURCE TEXT before it is "
                "parsed - COPY/EXEC SQL INCLUDE members for COBOL, cataloged PROCs, "
                "INCLUDE members and control-card datasets for JCL - retrieved "
                "transitively. This runs before the parse because the parse is what "
                "produces the dependency manifest: a copybook missing here takes its "
                "VALUE clauses out of the model, which turns a resolvable dynamic CALL "
                "target into an unresolved one, and an unresolved PROC hides every EXEC "
                "PGM= step inside it. 'local' means the member was already on the "
                "copybook search path and cost no round-trip; 'fetched' came from the "
                "estate service and carries the library it came from; 'not-found' means "
                "the service was asked and had nothing; 'error' means the request "
                "failed, which is fixable and is NOT evidence the member is absent; "
                "'no-service' means no estate client was reachable at all, so the "
                "member was never actually looked for."
            ),
            "serviceAvailable": self.unavailable is None,
            **({"serviceUnavailable": self.unavailable} if self.unavailable else {}),
            "counts": self.counts,
            "members": self.rows,
        }


class Prefetcher:
    """Shared machinery for the COBOL and JCL closures: one cache, one report."""

    def __init__(self, fetcher: Optional[Callable], paths: Optional[List[str]] = None,
                 dest: Optional[str] = None, unavailable: Optional[str] = None,
                 result: Optional[PrefetchResult] = None,
                 exts: Optional[Tuple[str, ...]] = None,
                 seen: Optional[Iterable[str]] = None):
        self.fetcher = fetcher
        self.paths = list(paths or [])
        self.dest = dest
        # The extensions to try on disk. The caller's `--copybook-ext` values come FIRST,
        # then the built-in list. Without threading them here, stage 1 tried only the
        # defaults, reported a member saved under a custom extension as MISSING, and - with
        # a live service - fetched the estate's copy instead, shadowing the very local file
        # the flag pointed at. The parse (stage 2) resolved it fine, so the two disagreed.
        seen_ext: dict = {}
        for e in (*(exts or ()), *_LOCAL_EXTS):
            seen_ext.setdefault(e, None)
        self.exts: Tuple[str, ...] = tuple(seen_ext)
        self.result = result or PrefetchResult()
        # Only ever RECORD an outage, never clear one. A second stage called with
        # unavailable=None (the COBOL run's --bind-jcl loop does exactly this) was
        # erasing a recorded outage, and report() then said serviceAvailable: true for
        # a run in which the estate was never reachable - conflating "asked and had
        # nothing" with "never asked", which this report exists to keep apart.
        if unavailable:
            self.result.unavailable = unavailable
        # Seed from the shared result, not empty: when a caller passes an existing
        # PrefetchResult (prefetch_jcl(..., result=pre)), members already in the store
        # were paid for and must not be requested - or reported - a second time.
        #
        # `seen` is an ADDITIONAL seed for a caller that already knows some names are
        # settled (an offline replay knows exactly what the gather run asked for). It is
        # deliberately NOT persisted on PrefetchResult: today a second Prefetcher
        # re-plans - and re-rows - members the first one recorded as not-found, and
        # changing that would change --bind-jcl's report.
        self.seen: set = set(self.result.store) | {member_key(n) for n in (seen or ())}

    def name_source(self, source_name: str) -> None:
        """Name the run, but only if the shared result has not been named already.

        A COBOL run with --bind-jcl calls the JCL closure once per JCL file against the
        PROGRAM's result; overwriting made the program's own prefetch report attribute
        its copybooks to the last JCL file on the command line."""
        if self.result.source_name == "<source>":
            self.result.source_name = source_name

    def note_closure_bound(self, max_rounds: int) -> None:
        """Record that a closure stopped at its round bound rather than converging.

        Bounded, and said so: a member set this deep is more likely a resolver loop than
        a real job, and silently stopping would look like a complete closure."""
        self.result.rows.append({
            "member": "<closure>", "status": "skipped",
            "reason": (f"stopped after {max_rounds} resolution rounds - the job's "
                       f"PROC/INCLUDE nesting is deeper than this bound, so members "
                       f"beyond it were not retrieved"),
        })

    # -- retrieval ----------------------------------------------------------
    def _local(self, name: str) -> Optional[Tuple[str, str]]:
        """A member already on the search path. Checked first, always: a member on disk
        must never cost a network round-trip."""
        for base in self.paths:
            for ext in self.exts:
                candidate = os.path.join(base, name + ext)
                if os.path.isfile(candidate):
                    # Explicit decode: without encoding= this uses the platform default
                    # (cp1252 on Windows), which maps almost every byte to SOMETHING, so
                    # a member saved as UTF-8 by save_member reads back mojibaked with no
                    # error - and one extra character shifts every column of a
                    # fixed-format line.
                    with open(candidate, "rb") as fh:
                        return decode_member(fh.read()), candidate
        return None

    def _plan(self, raw: str, why: str = "") -> Tuple[str, Optional[dict],
                                                      str, Optional[tuple]]:
        """Settle one member as far as can be settled WITHOUT asking the service:
        already-seen, not a requestable name, already on disk, or nothing to ask.

        Returns ``(kind, row, member, local)``. The row is built but deliberately NOT
        appended - the report's row order is part of its output, and a wave that recorded
        its locally-satisfied members as it planned them would file them ahead of the
        members either side that had to be fetched. Recording happens in :meth:`_record`,
        in request order, for every kind alike."""
        name = member_key(raw)
        if name in self.seen:
            return "seen", None, name, None
        self.seen.add(name)

        row: dict = {"member": name, "status": "", "for": why} if why else \
                    {"member": name, "status": ""}
        if raw != name:
            row["requested"] = str(raw)

        member = name
        dsn = _DSN_MEMBER.match(name)
        if dsn:
            # `PARM.LIB(SORTCRD)`: the retrievable identity is the MEMBER; the dataset
            # is where it lives, which is the service's business, not ours.
            member = dsn.group("member").upper()
            row["member"] = member
            row["dataset"] = name
        elif not _MEMBER_NAME.match(name):
            row["status"] = "skipped"
            row["reason"] = (f"'{name}' is not a member name that can be requested - "
                             f"it names something inside this job, not a stored member")
            return "skipped", row, member, None

        local = self._local(member)
        if local is not None:
            return "local", row, member, local

        if self.fetcher is None:
            row["status"] = "no-service"
            row["reason"] = (self.result.unavailable
                             or "no estate artifact service is configured, so this "
                                "member was never looked for")
            return "no-service", row, member, None

        return "request", row, member, None

    def _record(self, planned: tuple, got=None) -> Optional[str]:
        """Append the planned row, update the store, and return the member's text.

        ``got`` is the service's answer for a ``request`` - a ``Fetched``, ``None`` when
        it was asked and had nothing, or a ``ServiceUnavailable`` it raised. Every
        distinct reason for coming back empty stays distinct - that separation is the
        point of the report, because "the estate does not have it" and "we could not ask"
        lead to completely different next actions."""
        kind, row, member, local = planned
        if kind == "local":
            row.update({"status": "local", "source": local[1], "bytes": len(local[0])})
            self.result.rows.append(row)
            self.result.store[member] = local
            return local[0]

        if kind in ("skipped", "no-service"):
            self.result.rows.append(row)
            return None

        if isinstance(got, ServiceUnavailable):
            row.update({"status": "error", "error": str(got),
                        "reason": "the request itself failed - this is fixable, and is "
                                  "NOT evidence the member is absent from the estate"})
            self.result.rows.append(row)
            return None

        if got is None:
            row["status"] = "not-found"
            row["reason"] = "the estate service was asked and had nothing under this name"
            self.result.rows.append(row)
            return None

        # Collected HERE rather than in whatever thread retrieved it: this writes a file
        # into the run directory, and the run directory should fill in the order the
        # report lists, not in the order the estate happened to answer.
        got = collect(got, self.dest)
        row["status"] = "fetched"
        row.update(got.row())
        self.result.rows.append(row)
        self.result.fetched[member] = got
        self.result.store[member] = (got.text, got.source)
        return got.text

    def obtain(self, raw: str, type_hint: Optional[str] = None,
               why: str = "") -> Optional[str]:
        """Retrieve one member, record a row, return its text (or ``None``)."""
        planned = self._plan(raw, why)
        if planned[0] == "seen":
            # Via the resolver, not the raw store: a name first seen as
            # `PARM.LIB(SORTCRD)` is stored under the member it resolved to.
            return self.result.resolver()(planned[2])
        got = None
        if planned[0] == "request":
            try:
                got = call_service(self.fetcher, planned[2], type_hint, self.dest)
            except ServiceUnavailable as exc:
                got = exc
        return self._record(planned, got)

    def obtain_wave(self, items: List[Tuple[str, str]],
                    type_hint: Optional[str] = None,
                    jobs: int = 1) -> List[Tuple[str, str]]:
        """Retrieve one LEVEL of the closure at once; returns ``[(member, text)]``.

        A level is the largest set of members known to be needed before any of them has
        been read, which is exactly what can be asked for together - the level below it
        is not knowable until these arrive, since a copybook names its own COPYs only in
        its text. So the closure still costs one round of latency per level of nesting,
        and no longer one per member.

        Planning the whole level first also collapses a name requested twice within it:
        :meth:`_plan` marks it seen on the first, so the second is ``seen`` and never
        becomes a second request. Rows are appended in the level's own order, whatever
        order the answers arrived in."""
        planned = [p for p in (self._plan(raw, why) for raw, why in items)
                   if p[0] != "seen"]
        requests = [(p[2], type_hint) for p in planned if p[0] == "request"]
        answers = iter(call_service_many(self.fetcher, requests, jobs, self.dest)
                       if requests else ())
        out: List[Tuple[str, str]] = []
        for p in planned:
            text = self._record(p, next(answers) if p[0] == "request" else None)
            if text:
                out.append((p[2], text))
        return out

    def store_text(self, name: str) -> Optional[str]:
        hit = self.result.store.get(member_key(name))
        return hit[0] if hit else None
