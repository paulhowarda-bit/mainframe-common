"""The artifact KIND vocabulary, named once, and the host spellings that reduce into it.

:mod:`mainframe_artifacts.categories` says how a target is PROVIDED - contained here, an
IBM runtime service, still unresolved. This says WHAT the target is. The two are
orthogonal, both are serialized verbatim into the artifact manifest and both retrieval
reports, and so - exactly as there - the string IS the output contract, which is why
these stay plain constants rather than an enum.

Why the words live here rather than with the front-end that assigns them: a kind is now
crossed by strangers. ``fetch`` already keys the retrieval type it asks for on kind, and
:class:`mainframe_artifacts.protocol.DependentsResolver` has to be able to say which
kinds a host may name in a dependents row. Same split rule as the categories: the shared
*vocabulary* belongs at the bottom, and the *classifier* that decides a ``WRITEQ TD``
targets a queue belongs with the language it understands.

**A host index's spellings are finer than this vocabulary in two places, deliberately.**
A CICS index distinguishes ``MAP`` from ``MAPSET`` - a program names either, and BMS
fields key on the map - and ``TSQUEUE`` from ``TDQUEUE``, which are different resources
one name can legitimately be both of. The manifest has only ``terminal-map`` and
``queue``. Collapsing at the boundary would throw away a distinction the host actually
holds; widening the manifest words would change the output contract of every row ever
written for a gain that belongs in one dependents row. So a dependents row KEEPS the fine
spelling and carries the coarse one beside it: :func:`manifest_kind` is the reduction, the
join uses it, and the row reports both.
"""

from __future__ import annotations

from typing import Dict, Optional

# -- what a manifest row can be -------------------------------------------------------
#: Application source: a program, and the textual include that carries its layouts.
KIND_PROGRAM = "program"
KIND_COPYBOOK = "copybook"
KIND_MACRO = "macro"                       # Easytrieve's include
#: Db2 catalog objects.
KIND_DB2_TABLE = "db2-table"
KIND_DB2_TYPE = "db2-type"
KIND_DB2_STORED_PROCEDURE = "db2-stored-procedure"
KIND_DB2_TABLESPACE = "db2-tablespace"
KIND_DB2_DYNAMIC_SQL = "db2-dynamic-sql"
#: Data the program names program-locally, whose real identity is bound elsewhere.
KIND_FILE = "file"
KIND_DATASET = "dataset"
KIND_IMS_SEGMENT = "ims-segment"
#: CICS resources.
KIND_CICS_TRANSACTION = "cics-transaction"
KIND_TERMINAL_MAP = "terminal-map"
KIND_QUEUE = "queue"
#: JCL members.
KIND_PROC = "proc"
KIND_INCLUDE_MEMBER = "include-member"
KIND_CONTROL_CARD = "control-card"
#: Not artifacts on a share at all: who invokes this, and where its print goes.
KIND_CALLER = "caller"
KIND_SPOOL = "spool"

#: Every word above. ``fetch._KIND_TO_TYPE`` and ``fetch._NEVER_FETCHABLE`` together key
#: on a subset of this, and ``tests/test_dependents.py`` pins that they never drift out
#: of it - the same mirror-plus-test arrangement ``categories.NON_FETCHABLE`` uses.
MANIFEST_KINDS = frozenset({
    KIND_PROGRAM, KIND_COPYBOOK, KIND_MACRO,
    KIND_DB2_TABLE, KIND_DB2_TYPE, KIND_DB2_STORED_PROCEDURE, KIND_DB2_TABLESPACE,
    KIND_DB2_DYNAMIC_SQL,
    KIND_FILE, KIND_DATASET, KIND_IMS_SEGMENT,
    KIND_CICS_TRANSACTION, KIND_TERMINAL_MAP, KIND_QUEUE,
    KIND_PROC, KIND_INCLUDE_MEMBER, KIND_CONTROL_CARD,
    KIND_CALLER, KIND_SPOOL,
})

#: The host's own words, and the manifest kind each reduces to. Keyed uppercase because
#: that is how a CICS CSD, an assembler entry-point table and a Db2 catalog all spell
#: them. Where two host words share one manifest kind the pair is the point, not an
#: oversight: see the module docstring.
HOST_KIND_TO_MANIFEST: Dict[str, str] = {
    "PROGRAM":     KIND_PROGRAM,
    "MODULE":      KIND_PROGRAM,
    "ENTRY":       KIND_PROGRAM,          # an assembler ENTRY point, named as a program
    "ENTRY-POINT": KIND_PROGRAM,
    "COPYBOOK":    KIND_COPYBOOK,
    "FILE":        KIND_FILE,
    "DATASET":     KIND_DATASET,
    "MAP":         KIND_TERMINAL_MAP,
    "MAPSET":      KIND_TERMINAL_MAP,
    "TDQUEUE":     KIND_QUEUE,
    "TSQUEUE":     KIND_QUEUE,
    "TSMODEL":     KIND_QUEUE,
    "TRANSACTION": KIND_CICS_TRANSACTION,
    "TABLE":       KIND_DB2_TABLE,
    "PROCEDURE":   KIND_DB2_STORED_PROCEDURE,
    "JOB":         KIND_PROC,             # a job that names it, reduced to its members
    "PROC":        KIND_PROC,
}


def manifest_kind(kind: Optional[str]) -> Optional[str]:
    """The manifest word for ``kind``, or ``None`` if it is not one this tool knows.

    Accepts a manifest kind verbatim (any case) and a host spelling from
    :data:`HOST_KIND_TO_MANIFEST`. ``None`` in means ``None`` out: an ask that names no
    kind is legal - a name alone is often unambiguous - and is not an unknown kind.

    Unknown is reported rather than guessed. A dependents row whose kind cannot be
    reduced has no place to attach in any package's model, so the row is refused with the
    reason instead of being joined to whatever happened to sort first.
    """
    if kind is None:
        return None
    text = str(kind).strip()
    if not text:
        return None
    if text.lower() in MANIFEST_KINDS:
        return text.lower()
    return HOST_KIND_TO_MANIFEST.get(text.upper())


__all__ = [
    "KIND_PROGRAM", "KIND_COPYBOOK", "KIND_MACRO",
    "KIND_DB2_TABLE", "KIND_DB2_TYPE", "KIND_DB2_STORED_PROCEDURE",
    "KIND_DB2_TABLESPACE", "KIND_DB2_DYNAMIC_SQL",
    "KIND_FILE", "KIND_DATASET", "KIND_IMS_SEGMENT",
    "KIND_CICS_TRANSACTION", "KIND_TERMINAL_MAP", "KIND_QUEUE",
    "KIND_PROC", "KIND_INCLUDE_MEMBER", "KIND_CONTROL_CARD",
    "KIND_CALLER", "KIND_SPOOL",
    "MANIFEST_KINDS", "HOST_KIND_TO_MANIFEST", "manifest_kind",
]
