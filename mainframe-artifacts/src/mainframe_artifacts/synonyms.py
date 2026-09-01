"""Db2 SYNONYM/ALIAS -> base table: catalog knowledge supplied as input, never guessed.

A Db2 ALIAS or SYNONYM is an alternate name for a table, and the join from one to its
base table exists only in the Db2 catalog. No front-end here derives it. It arrives by
one of two doors, both the host's to supply:

* a **map** - a JSON file of ``{"SYNONYM": "BASE_TABLE"}`` (``--synonym-map``), the
  operator's explicit, reviewed answer, the only door available to someone running a
  CLI by hand; or
* a **resolver** - a callable ``(name) -> base | None`` (``--synonym-resolver``, see
  :class:`mainframe_artifacts.protocol.SynonymResolver`) asked at the point of need, so
  a host that runs a tool once per program over tens of thousands of programs never
  hands each spawn its own copy of index state that a catalog re-ingest silently makes
  stale.

:class:`SynonymLookup` folds both into one question. The map is consulted first and a
map hit never reaches the resolver: the file is the operator's override, exactly as a
member on the local search path is never asked of the estate. The resolver answers what
the map does not hold. The one invariant the resolver protocol states is enforced here:
a resolver that RAISES has failed (``disabled_reason`` records why, and it is never
asked again in this run), which is a different fact from ``None`` - the catalog was
asked and the name is not a synonym - and a consumer must keep the two apart in what
it reports.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

#: Which door answered, as :class:`SynonymLookup` reports it.
FROM_MAP = "map"
FROM_RESOLVER = "resolver"


def read_synonym_map(path) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Read a ``--synonym-map`` file. Returns ``(mapping, None)`` or ``(None, why)``.

    The shape is deliberately the flat object ``mfdep catalog export-synonym-map``
    emits: a JSON object whose keys and values are all strings. Anything else is
    refused with the reason, never partially read."""
    sp = Path(path)
    if not sp.exists():
        return None, f"no such file: {sp}"
    try:
        raw = json.loads(sp.read_text(encoding="utf-8"))
    except ValueError as exc:
        return None, f"{sp} is not valid JSON: {exc}"
    if (not isinstance(raw, dict)
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in raw.items())):
        return None, (f"{sp} must be a JSON object of \"SYNONYM\": \"BASE_TABLE\" "
                      f"strings")
    return raw, None


class SynonymLookup:
    """One question - "is this name a synonym, and of what?" - over both doors.

    ``__call__(name)`` returns ``(base_table, door)`` with ``door`` one of
    :data:`FROM_MAP` / :data:`FROM_RESOLVER`, or ``None`` when neither door has an
    answer. It never raises. Names are compared uppercased; a schema qualifier written
    on the name stays on it, and a base the resolver returns is taken as given (it may
    be ``OWNER.T``).

    ``disabled_reason`` is set the first time the resolver breaks its contract - it
    raised, or returned something that is neither a string nor ``None`` - and from then
    on only the map answers. A consumer reads it once at the end of a run and flags it:
    everything the resolver did not reach stays unresolved for a fixable reason, which
    is not the same as being resolved as "not a synonym".
    """

    def __init__(self, mapping: Optional[Mapping[str, str]] = None,
                 resolver: Optional[Callable[[str], Optional[str]]] = None) -> None:
        self.mapping: Dict[str, str] = {str(k).upper(): str(v).upper()
                                        for k, v in (mapping or {}).items()}
        self.resolver = resolver
        self.disabled_reason: Optional[str] = None
        self._memo: Dict[str, Optional[str]] = {}

    def __call__(self, name: str) -> Optional[Tuple[str, str]]:
        key = (name or "").upper()
        if not key:
            return None
        base = self.mapping.get(key)
        if base is not None:
            return base, FROM_MAP
        if self.resolver is None or self.disabled_reason is not None:
            return None
        if key in self._memo:
            hit = self._memo[key]
            return (hit, FROM_RESOLVER) if hit else None
        try:
            got = self.resolver(key)
        except Exception as exc:  # noqa: BLE001 - the contract: raising IS the failure
            self.disabled_reason = f"resolver raised {type(exc).__name__}: {exc}"
            return None
        if got is None:
            self._memo[key] = None
            return None
        if not isinstance(got, str) or not got.strip():
            self.disabled_reason = (
                f"resolver returned {type(got).__name__} "
                f"{'(empty) ' if isinstance(got, str) else ''}for {key}, expected a "
                f"table name or None")
            return None
        base = got.strip().upper()
        self._memo[key] = base
        return base, FROM_RESOLVER
