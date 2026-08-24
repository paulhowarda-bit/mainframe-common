"""The category vocabulary, named once so every stage and every front-end spells it
identically.

This is the WORDS, not the judgement. Deciding that ``MQPUT`` is an IBM MQ verb is COBOL
domain knowledge and lives with the COBOL classifier (``cobol_xstate.classify``); knowing
that the string for "provided by a subsystem, no application source to chase" is
``ibm-runtime`` is shared, because the retrieval stage in core has to recognise it in a
manifest row it did not produce.

That is the split rule generally: a shared *vocabulary* belongs at the bottom, the
*classifier* that produces it belongs with the language it understands. Core consumes
these categories (``fetch`` must not chase a target that has no application source); it
never assigns them.

Kept as plain string constants rather than an enum on purpose - they are serialized
verbatim into the artifact manifest and both retrieval reports, so the string IS the
output contract, and an enum would only add a way to write a different one by accident.
"""

from __future__ import annotations

#: The target is a program CONTAINED in this source (a nested ``PROGRAM-ID``); a CALL to
#: it is internal, not an external dependency.
CATEGORY_INTERNAL = "internal-nested"

#: The target is a standard IBM subsystem entry point (MQI verb, Db2 language interface,
#: an LE ``CEE*`` service, a CICS ``DFH*`` module). Provided by the runtime; there is no
#: application source to retrieve.
CATEGORY_IBM = "ibm-runtime"

#: NONE of the above could be positively established. An HONEST default, not a failure:
#: the tool never guesses a provider it cannot prove. The fetch stage then PROBES the
#: estate, and what it finds refines this.
CATEGORY_UNRESOLVED = "unresolved"

#: Refinements the fetch stage assigns to a formerly-``unresolved`` target once the estate
#: answers - kept here so the fetch report and any summary name them the same way.
CATEGORY_COBOL = "cobol-program"
CATEGORY_ASM = "assembler-program"

#: Categories with NO application source to retrieve, so the fetch stage must not chase
#: them (mirrors ``fetch._NEVER_FETCHABLE``, but keyed on classification, not endpoint
#: kind).
NON_FETCHABLE = frozenset({CATEGORY_INTERNAL, CATEGORY_IBM})

__all__ = [
    "CATEGORY_INTERNAL", "CATEGORY_IBM", "CATEGORY_UNRESOLVED",
    "CATEGORY_COBOL", "CATEGORY_ASM", "NON_FETCHABLE",
]
