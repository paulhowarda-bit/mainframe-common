"""What depends on an artifact: estate-wide knowledge supplied as input, never derived.

Every extractor in this family answers one question about the artifact it is given -
*what does this artifact name?* - because that is the question a single artifact's source
can answer. The reverse, *what depends on this artifact*, is not derivable from the
source at all: which programs issue ``EXEC CICS READ FILE`` against a file definition,
which modules ``CALL`` an assembler entry point, which members reference a mapset. Those
facts live in an estate-wide index only the host can read. So the reverse direction
arrives the way catalog knowledge already does - through a door, by one of two forms,
both the host's to supply:

* a **map** - a JSON file of ``{"NAME|KIND": [row, ...]}`` (``--dependents-map``), the
  operator's explicit, reviewed answer and the only door available to someone running a
  CLI by hand; or
* a **resolver** - a callable ``(name, kind=...) -> rows`` (``--dependents-resolver``,
  see :class:`mainframe_artifacts.protocol.DependentsResolver`) asked at the point of
  need, so a host running a tool once per program over tens of thousands of programs
  never hands each spawn its own copy of index state that a re-ingest silently makes
  stale.

:class:`DependentsLookup` folds both into one question, with the map consulted first and
a map hit never reaching the resolver - the file is the operator's override, exactly as
:class:`~mainframe_artifacts.synonyms.SynonymLookup` treats it.

**Three answers, kept apart, and this is the whole point of the module.**

    ``None`` from the lookup - NOT ANSWERED. No door holds this artifact, or the resolver
    has already broken its contract in this run.

    an answer whose ``rows`` are empty - ASKED, AND NOTHING DEPENDS ON IT. A claim about
    the estate, and a strong one.

    ``disabled_reason`` set - THE LOOKUP BROKE. Fixable, and the reason is recorded.

Collapsing the first two would let an unsupplied - or a broken - run report "nothing in
the estate depends on this", which is a claim no such run is entitled to make. A view
built on this must report the three distinctly, and must never fill the first with the
second.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .kinds import manifest_kind
from .prefetch import member_key
from .synonyms import FROM_MAP, FROM_RESOLVER

#: How well the host matched the name it was asked about, as its OWN field and never
#: folded into prose. A host that aggregated this into a sentence once reported 168
#: fully-qualified calls as bare-name matches, because both non-qualified cases began
#: with the same word.
MATCH_STRENGTHS = ("qualified", "schema-unconstrained", "bare")

#: The declared row. ``via`` is how it depends - the site or verb the host matched on,
#: ``READ FILE``, ``WRITEQ TD``, ``CALL``, ``LINK``, ``START TRANSID`` - free text,
#: because it is the host's evidence rather than this tool's vocabulary and nothing here
#: branches on it. Anything a host sends that is not named here is ignored rather than
#: passed through: what gets written out is exactly what is declared here, so a field a
#: host adds for itself cannot silently become part of another package's output.
ROW_FIELDS = ("name", "kind", "manifest_kind", "via", "match_strength", "detail")


@dataclass(frozen=True)
class DependentsAnswer:
    """One artifact's dependents, as one door answered it.

    ``rows`` may be empty - that is the "asked, and nothing depends on it" answer, and it
    is not the same as :class:`DependentsLookup` returning ``None``. ``truncated`` with
    ``total`` is a REPORTED fan-out cap: a host that holds ten thousand callers may send
    the first hundred, and saying so is required, because a silently shortened list reads
    as a complete one.
    """

    rows: Tuple[Dict[str, Any], ...]
    door: str
    truncated: bool = False
    total: Optional[int] = None


def normalise_row(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """One host row, reduced to :data:`ROW_FIELDS`. ``(row, None)`` or ``(None, why)``.

    ``name`` and ``kind`` are required: a dependent with no name is not a fact, and a
    kind that :func:`~mainframe_artifacts.kinds.manifest_kind` cannot reduce has nowhere
    to attach in any package's model. Names are compared and reported the way every
    member name in this tool is - unquoted and uppercased, by :func:`member_key`.
    """
    if not isinstance(raw, Mapping):
        return None, f"expected a row object, got {type(raw).__name__}"
    name = str(raw.get("name", "") or "").strip()
    if not name:
        return None, "row has no name"
    kind = raw.get("kind")
    coarse = manifest_kind(kind)
    if coarse is None:
        return None, (f"row for {member_key(name)} has kind {kind!r}, which is not an "
                      f"artifact kind this tool knows")
    strength = raw.get("match_strength")
    if strength is not None and strength not in MATCH_STRENGTHS:
        return None, (f"row for {member_key(name)} has match_strength {strength!r}, "
                      f"expected one of {', '.join(MATCH_STRENGTHS)}")
    row: Dict[str, Any] = {"name": member_key(name),
                           # The host's own spelling is kept: MAP and MAPSET are a
                           # distinction it holds and the manifest word does not.
                           "kind": str(kind).strip().upper(),
                           "manifest_kind": coarse}
    for field in ("via", "match_strength", "detail"):
        value = raw.get(field)
        if value is not None and str(value).strip():
            row[field] = str(value).strip()
    return row, None


def coerce_answer(got: Any) -> Tuple[Optional[DependentsAnswer], Optional[str]]:
    """A resolver's return value, as an answer. ``(answer, None)`` or ``(None, why)``.

    Two shapes, both real: the rows themselves, or a mapping carrying them with the
    fan-out cap beside them. ``None`` is the third and means NOT ANSWERED - an artifact
    outside what the index covers - never "nothing depends on it"; an index that was
    asked and holds nothing returns an empty sequence and says so.
    """
    if got is None:
        return None, None
    rows_in: Any = got
    truncated = False
    total: Optional[int] = None
    if isinstance(got, Mapping):
        rows_in = got.get("rows")
        if rows_in is None:
            return None, "answer object has no rows"
        truncated = bool(got.get("truncated"))
        raw_total = got.get("total")
        if raw_total is not None:
            try:
                total = int(raw_total)
            except (TypeError, ValueError):
                return None, f"total {raw_total!r} is not a number"
    if isinstance(rows_in, (str, bytes, bytearray)) or not isinstance(rows_in, Sequence):
        return None, (f"expected rows, or an object with rows, got "
                      f"{type(got).__name__}")
    rows: List[Dict[str, Any]] = []
    for raw in rows_in:
        row, why = normalise_row(raw)
        if why:
            return None, why
        rows.append(row)                       # type: ignore[arg-type]
    return DependentsAnswer(tuple(rows), FROM_RESOLVER, truncated, total), None


def read_dependents_map(path) -> Tuple[Optional[Dict[str, List[dict]]], Optional[str]]:
    """Read a ``--dependents-map`` file. ``(mapping, None)`` or ``(None, why)``.

    The shape is a JSON object keyed ``"NAME|KIND"`` - or ``"NAME"`` where the name alone
    is unambiguous - whose values are lists of rows. Every row is validated here rather
    than at the point of use, so a malformed file is refused whole, with the reason, and
    never partially read: half a dependents map would answer some artifacts and silently
    leave others looking as though nothing depends on them.

    An entry may be an empty list, and that is a deliberate statement - "asked, and
    nothing depends on this" - which is exactly what a name absent from the file does NOT
    say.
    """
    sp = Path(path)
    if not sp.exists():
        return None, f"no such file: {sp}"
    try:
        raw = json.loads(sp.read_text(encoding="utf-8"))
    except ValueError as exc:
        return None, f"{sp} is not valid JSON: {exc}"
    if not isinstance(raw, dict):
        return None, (f"{sp} must be a JSON object keyed \"NAME|KIND\" whose values are "
                      f"lists of dependents rows")
    out: Dict[str, List[dict]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            return None, f"{sp} has a key that is not a name: {key!r}"
        if not isinstance(value, list):
            return None, (f"{sp}: {key!r} must be a list of dependents rows, got "
                          f"{type(value).__name__}")
        rows: List[dict] = []
        for item in value:
            row, why = normalise_row(item)
            if why:
                return None, f"{sp}: {key!r} {why}"
            rows.append(row)                   # type: ignore[arg-type]
        out[_map_key(key)] = rows
    return out, None


def _map_key(key: str) -> str:
    """A map file's key, canonicalised: ``NAME`` or ``NAME|manifest-kind``.

    The host writes the kind in its own spelling, so it is reduced here exactly as an ask
    is, and the two meet whichever word each used."""
    name, _, kind = str(key).partition("|")
    coarse = manifest_kind(kind) if kind.strip() else None
    return member_key(name) + (f"|{coarse}" if coarse else "")


def _ask_keys(name: str, kind: Optional[str]) -> Tuple[str, ...]:
    """The map keys one ask can be answered by, most specific first."""
    base = member_key(name)
    coarse = manifest_kind(kind)
    return (f"{base}|{coarse}", base) if coarse else (base,)


class DependentsLookup:
    """One question - "what depends on this artifact?" - over both doors.

    ``__call__(name, kind=None)`` returns a :class:`DependentsAnswer`, or ``None`` when
    neither door holds this artifact. It never raises.

    ``disabled_reason`` is set the first time the resolver breaks its contract - it
    raised, or answered with a shape the protocol does not allow - and from then on only
    the map answers. Every artifact the resolver did not reach then stays NOT ANSWERED for
    a fixable reason, which is not the same as being answered "nothing depends on it": a
    consumer reads the reason at the end of a run and says so in its view.

    ``supplied`` is what a front-end's ``dependents()`` view branches on. False means no
    door was opened at all, and the view is then ``None`` rather than empty.
    """

    def __init__(self, mapping: Optional[Mapping[str, Sequence[dict]]] = None,
                 resolver: Optional[Callable[..., Any]] = None) -> None:
        self.resolver = resolver
        self.disabled_reason: Optional[str] = None
        self.map_warning: Optional[str] = None
        self.mapping: Dict[str, Tuple[Dict[str, Any], ...]] = {}
        self._by_name: Dict[str, List[str]] = {}
        self._memo: Dict[str, Optional[DependentsAnswer]] = {}
        self._accepts_kind = True
        for key, rows in (mapping or {}).items():
            # A mapping from read_dependents_map is already normalised and every row of
            # it already valid; one handed in directly by a host embedding this package
            # has been through neither, so it goes through the same gate. A bad row is
            # dropped with the reason recorded rather than silently kept or silently
            # discarded - the entry then answers with what was valid, and the run says
            # the map was degraded. Its own field, not ``disabled_reason``: a map this
            # tool could not fully read is no reason to stop asking a resolver that is
            # answering perfectly well.
            good: List[Dict[str, Any]] = []
            for row in rows:
                got, why = normalise_row(row)
                if why:
                    if self.map_warning is None:
                        self.map_warning = f"dependents map: {why}"
                    continue
                good.append(got)               # type: ignore[arg-type]
            canonical = _map_key(key)
            self.mapping[canonical] = tuple(good)
            self._by_name.setdefault(canonical.partition("|")[0], []).append(canonical)

    @property
    def supplied(self) -> bool:
        """Whether any door was opened. A run with none behaves exactly as it did before
        this contract existed."""
        return bool(self.mapping) or self.resolver is not None

    def describe(self) -> str:
        """How the answers were supplied, for the view to carry. A view that reports
        dependents without saying where they came from is asking to be trusted twice."""
        doors = []
        if self.mapping:
            doors.append(f"map ({len(self.mapping)} entries)")
        if self.resolver is not None:
            doors.append("resolver")
        if not doors:
            return "no dependents lookup was supplied"
        text = " + ".join(doors)
        for note in (self.disabled_reason, self.map_warning):
            if note:
                text += f", degraded: {note}"
        return text

    def __call__(self, name: str, kind: Optional[str] = None) -> Optional[DependentsAnswer]:
        if not member_key(name or ""):
            return None
        keys = _ask_keys(name, kind)
        for key in keys:
            rows = self.mapping.get(key)
            if rows is not None:
                return DependentsAnswer(rows, FROM_MAP)
        if manifest_kind(kind) is None:
            # An ask that named no kind, against a map keyed by one. Answered when the
            # name has exactly ONE entry, because then there is nothing to choose; two
            # entries for one name is a question only the caller can settle, so it is
            # left unanswered rather than guessed at.
            candidates = self._by_name.get(member_key(name), ())
            if len(candidates) == 1:
                return DependentsAnswer(self.mapping[candidates[0]], FROM_MAP)
        if self.resolver is None or self.disabled_reason is not None:
            return None
        memo_key = keys[0]
        if memo_key in self._memo:
            return self._memo[memo_key]
        try:
            got = self._call_resolver(member_key(name), kind)
        except Exception as exc:  # noqa: BLE001 - the contract: raising IS the failure
            self.disabled_reason = f"resolver raised {type(exc).__name__}: {exc}"
            return None
        answer, why = coerce_answer(got)
        if why:
            self.disabled_reason = (f"resolver answered for {member_key(name)} with a "
                                    f"shape this contract does not allow: {why}")
            return None
        self._memo[memo_key] = answer
        return answer

    def _call_resolver(self, name: str, kind: Optional[str]) -> Any:
        """Ask the resolver, narrowing the call the way ``call_service`` does.

        A resolver whose signature is just ``f(name)`` is perfectly valid and needs no
        adapter, so a ``TypeError`` from passing ``kind`` is retried without it - once,
        and then remembered, because retrying on every ask would double the calls for the
        rest of the run."""
        assert self.resolver is not None
        if self._accepts_kind and kind is not None:
            try:
                return self.resolver(name, kind=kind)
            except TypeError:
                self._accepts_kind = False
        return self.resolver(name)


__all__ = ["MATCH_STRENGTHS", "ROW_FIELDS", "DependentsAnswer", "DependentsLookup",
           "coerce_answer", "normalise_row", "read_dependents_map"]
