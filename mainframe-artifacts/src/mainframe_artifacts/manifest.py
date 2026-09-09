"""The artifact manifest's shared core, named once.

Five front-ends produce manifests - COBOL, JCL, Easytrieve, assembler and CICS - and
``fetch`` consumes rows it did not produce, from any of them. The row vocabulary is
already a contract crossed by strangers; this writes it down, in the same spirit as
``categories.py``, whose comment makes the same argument for the category strings:

    Kept as plain string constants rather than an enum on purpose - they are serialized
    verbatim into the artifact manifest and both retrieval reports, so the string IS the
    output contract, and an enum would only add a way to write a different one by accident.

**Written from the producers, not from a design.** Every key and value below was measured
across all five packages' example corpora rather than decided. Two consequences a reader
should not be surprised by:

* ``io`` is NOT core. Copybook rows have none, and neither do JCL ``db2-tablespace`` rows -
  a tablespace has no direction because a utility works on the space.
* ``identity`` is core, and ``cics-dependencies`` does not yet emit it. That is a real gap
  this module exists to surface, not a reason to weaken the core to fit.

**Per-kind extras stay with the producer that understands them.** ``ddname`` on a COBOL
file row, ``generations``/``gdg`` on a JCL dataset, ``evidence`` on an assembler row,
``installed``/``relation`` on a CICS one - all real, all useful, none of a consumer's
business unless it knows that producer. What is here is what every producer must emit and
every consumer may rely on.

**The occurrence list is deliberately NOT core.** COBOL spells it ``lines`` and carries
source line numbers; JCL, assembler and Easytrieve spell it ``touchedBy`` and carry dicts;
JCL ``program`` rows carry ``steps`` instead, and ``proc``/``include-member`` rows carry
none at all. They are not one concept in four spellings - the payloads differ - so naming
one of them in the core would either lose information or force three producers to invent
an occurrence they do not have. A consumer reads the producer's own key, and this says so.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Bumped when this vocabulary changes in a way a consumer must notice. Additive keys do
#: NOT bump it; a removed or re-meaning key does.
MANIFEST_SCHEMA_VERSION = 1

#: Keys every row carries, whatever produced it. Measured as the intersection over the
#: five producers' corpora, with `identity` held to the four that emit it - see the module
#: docstring.
CORE_KEYS = ("artifact", "kind", "dependency", "identity")

#: Keys a row carries when the producer knows the answer. Absent, never empty, and never
#: `false` - a boolean flag in this family is present-and-true or gone.
OPTIONAL_KEYS = ("io", "needs", "resolvedBy", "flags")

#: What `dependency` may say. Compile-time means the artifact was assembled or copied into
#: the source before it could be read at all.
DEPENDENCY_VALUES = ("runtime", "compile-time")

#: What `identity` may say - whether the name in `artifact` is already an estate-wide
#: identity, or still needs an external binding to join on.
IDENTITY_VALUES = (
    "global",          # a Db2 table, a load-module name: catalog- or estate-global
    "program-local",   # a ddname: needs the JCL DD statement to resolve
    "job-scoped",      # meaningful within one job
    "library-member",  # a member name, resolved against a library concatenation
    "internal",        # contained in this source (a nested PROGRAM-ID)
    "runtime",         # supplied by an IBM subsystem runtime library
)

#: What `io` may say. TWO spellings of both-ways access, deliberately and per kind: a
#: `db2-table` row says `read+write` because the JCL side does, and every other kind says
#: `read-write` because the Easytrieve `file` row does. Written down here because it is a
#: real divergence a consumer must handle, not one to normalise away behind its back.
IO_VALUES = ("read", "write", "read-write", "read+write", "unknown")

#: Occurrence lists, by the producer that emits each. Not core - see the module docstring.
OCCURRENCE_KEYS = ("lines", "touchedBy", "steps", "usedAt")


def validate_manifest(manifest: dict) -> List[str]:
    """Complaints about ``manifest``, or ``[]``.

    ADVISORY, and advisory in the specific sense ``describe_fetcher`` means it: written to
    stderr as a diagnostic, never into an output file. A complaint about a manifest's
    shape is not a finding about the estate, and must never be mistaken for one.

    It also never raises and never rejects on ignorance: a key this module has not heard
    of is a per-kind extra until proven otherwise, and unknown is not the same as wrong.
    """
    out: List[str] = []
    if not isinstance(manifest, dict):
        return [f"manifest is {type(manifest).__name__}, not a dict"]

    rows = manifest.get("artifacts")
    if rows is None:
        return ["manifest has no 'artifacts' key"]
    if not isinstance(rows, list):
        return [f"'artifacts' is {type(rows).__name__}, not a list"]

    for i, row in enumerate(rows):
        where = f"row {i}"
        if not isinstance(row, dict):
            out.append(f"{where} is {type(row).__name__}, not a dict")
            continue
        name = row.get("artifact")
        if isinstance(name, str) and name:
            where = f"row {i} ({name})"

        for key in CORE_KEYS:
            if key not in row:
                out.append(f"{where} has no '{key}'")

        out += _value_complaints(where, row)

    return out


def _value_complaints(where: str, row: dict) -> List[str]:
    """Vocabulary checks, each skipped when the key is absent - a missing core key is
    already reported once and must not be reported twice."""
    out: List[str] = []
    for key, allowed in (("dependency", DEPENDENCY_VALUES),
                         ("identity", IDENTITY_VALUES),
                         ("io", IO_VALUES)):
        value = row.get(key)
        if value is not None and value not in allowed:
            out.append(f"{where} has {key}={value!r}, outside "
                       f"{{{', '.join(allowed)}}}")

    for key in ("needs", "resolvedBy"):
        if key in row and row[key] == "":
            out.append(f"{where} has an empty '{key}' - absent, never empty")

    for key in OCCURRENCE_KEYS:
        if key in row and row[key] == []:
            out.append(f"{where} has an empty '{key}' - absent, never empty")

    return out


def report_manifest(log: Any, source_name: str, manifest: dict) -> List[str]:
    """Validate and say so on ``log``, the way ``report.report_stages`` reports retrieval.

    Returns the complaints so a caller can assert on them. Silence when there are none:
    a conforming manifest is the normal case and does not deserve a line.
    """
    complaints = validate_manifest(manifest)
    for complaint in complaints:
        log.warning("%s: manifest shape: %s", source_name, complaint)
    return complaints
