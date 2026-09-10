"""The estate artifact service, as a typed contract.

Until now this contract existed only as the branch list inside
:func:`mainframe_artifacts.artifact_service.coerce` - a reader had to infer the interface
from the shapes that happened to be accepted. With the code split across three
distributions the contract is now crossed by strangers, so it is written down.

Be honest about what this buys. ``@runtime_checkable`` on a callable Protocol only ever
checks that ``__call__`` exists, so ``isinstance`` against it is close to worthless as a
validation. The value is elsewhere: a name to annotate with across three packages, a
place for type checkers and IDEs to hang, one authoritative statement of the calling
convention, and - for actual runtime help - :func:`describe_fetcher`, which inspects a
signature and explains why a client will not work.
"""

from __future__ import annotations

import inspect
import os
from typing import (Any, Mapping, Optional, Sequence, Tuple, Union,
                    runtime_checkable)

try:                                    # pragma: no cover - 3.8+ has it; kept explicit
    from typing import Protocol
except ImportError:                     # pragma: no cover
    from typing_extensions import Protocol   # type: ignore

#: Everything :func:`artifact_service.coerce` knows how to read. A client may return any
#: of these; none is preferred, because each corresponds to a real client style already
#: in the wild (return the text, return a path it copied to, return the service's own
#: record).
FetchedShape = Union[
    None, bool,                       # not found  (also {"found": False})
    str, bytes, bytearray,            # the member text
    "os.PathLike",                    # a file to read
    Tuple[str, str],                  # (text, source)
    Sequence[Any],                    # [text, source, ...]
    Mapping[str, Any],                # the full mf-fetch dict
]


@runtime_checkable
class ArtifactFetcher(Protocol):
    """The estate's artifact service, as this tool calls it.

    ``type`` shadows the builtin because that is the wire keyword the default mf-fetch
    client uses; it is not negotiable here.

    **Both keywords are optional.** :func:`artifact_service.call_service` drops ``copy``,
    then ``type``, and retries on ``TypeError``, so a client whose signature is just
    ``f(name)`` is perfectly valid and needs no adapter.

    **The one invariant, and the only one that really matters:**

        RAISING means THE REQUEST FAILED - fixable: bad credentials, service down, share
        unreachable.

        Returning ``None`` / ``False`` / ``{"found": False}`` means THE ESTATE WAS ASKED
        AND HAD NOTHING.

    These are never interchangeable. Every report this tool writes keeps them apart,
    because they lead to completely different next actions, and a client that returns
    ``None`` on a connection error will make an entire estate read as empty - silently,
    and with a report that says so with full confidence.
    """

    def __call__(self, name: str, *, type: Optional[str] = ...,      # noqa: A002
                 copy: Optional[str] = ...) -> FetchedShape:
        ...


@runtime_checkable
class SynonymResolver(Protocol):
    """A Db2 SYNONYM/ALIAS lookup against the catalog, as this tool calls it.

    The host supplies it, the same way it supplies the artifact service: the catalog is
    the only place the synonym->base-table join exists, and this tool never derives it.
    Called with one positional argument, the table name as the statement wrote it,
    uppercased (``OWNER.T`` stays qualified). Returns the base table's name - which may
    itself be schema-qualified - or ``None``.

    **The same invariant as** :class:`ArtifactFetcher`, **and it matters just as much:**

        RAISING means THE LOOKUP FAILED - fixable: catalog down, bad connection.

        Returning ``None`` means THE CATALOG WAS ASKED AND THE NAME IS NOT A SYNONYM.

    A consumer keeps them apart: a failed lookup is flagged and the site stays
    unresolved for a fixable reason; ``None`` reads exactly as an absent map entry. A
    resolver that returns ``None`` on a connection error would make every synonym in
    the run read as a plain table - silently, and with a report that says so with
    full confidence.
    """

    def __call__(self, name: str) -> Optional[str]:
        ...


#: What a dependents lookup may answer with. The rows themselves, or an object carrying
#: them with a REPORTED fan-out cap beside them (``{"rows": [...], "truncated": true,
#: "total": 9182}``) - a host holding ten thousand callers may send the first hundred, but
#: a shortened list that does not say so reads as a complete one.
DependentsShape = Union[
    None,                             # not answerable for this artifact
    Sequence[Mapping[str, Any]],      # the rows, possibly empty
    Mapping[str, Any],                # {"rows": [...], "truncated": ..., "total": ...}
]


@runtime_checkable
class DependentsResolver(Protocol):
    """*What depends on this artifact* - the reverse direction, which only a host holds.

    Every extractor in this family answers the forward direction, because that is what an
    artifact's own source can support: it names what it references. The reverse is not
    unimplemented here, it is *underivable* - which programs issue ``EXEC CICS READ FILE``
    against a file definition, which modules ``CALL`` an assembler entry point, which
    members reference a mapset. Those facts live in an estate-wide index, so they arrive
    the same way the catalog does: the host supplies them, and this tool never guesses
    them.

    Called with the artifact name positionally and its kind by keyword - the kind of the
    artifact being ASKED ABOUT, from
    :data:`~mainframe_artifacts.kinds.MANIFEST_KINDS` or a host spelling that reduces into
    it. Both keywords are optional in the same way :class:`ArtifactFetcher`'s are: a
    resolver whose signature is just ``f(name)`` is valid and needs no adapter, because
    :class:`~mainframe_artifacts.dependents.DependentsLookup` drops ``kind`` and retries
    on ``TypeError``.

    **The same invariant as the two contracts above, and a third distinction this one
    cannot do without:**

        RAISING means THE LOOKUP FAILED - fixable: index unreachable, bad connection.

        Returning an EMPTY sequence means THE INDEX WAS ASKED AND NOTHING DEPENDS ON
        THIS. That is a claim about the estate, and a strong one.

        Returning ``None`` means THIS ARTIFACT IS NOT ONE THE INDEX CAN ANSWER FOR - it is
        outside what was ingested. Not the same claim, and never reported as if it were.

    A lookup that returned an empty list on a connection error would make an entire estate
    read as though nothing depends on anything - silently, and with a view that says so
    with full confidence. A row's shape is declared in
    :data:`~mainframe_artifacts.dependents.ROW_FIELDS`, and one that does not fit is
    refused with the reason, exactly as a non-string synonym result is.
    """

    def __call__(self, name: str, *, kind: Optional[str] = ...) -> DependentsShape:
        ...


def _describe(fn: Any, *, absent: str, called_as: str, subject: str,
              convention: str) -> Optional[str]:
    """The signature inspection the three ``describe_*`` helpers share; the words differ
    per contract."""
    if fn is None:
        return absent
    if not callable(fn):
        return f"{fn!r} is not callable"
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None                      # not introspectable; assume it is fine

    params = list(sig.parameters.values())
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return None                      # *args swallows anything we would pass

    positional = [p for p in params
                  if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    if not positional:
        return (f"takes no positional argument, so it cannot be called as "
                f"{called_as} - {subject} is always passed positionally")
    required = [p for p in positional[1:] if p.default is inspect.Parameter.empty]
    if required:
        names = ", ".join(p.name for p in required)
        return (f"requires argument(s) {names} that this tool does not supply - "
                f"{convention}")
    return None


def describe_fetcher(fn: Any) -> Optional[str]:
    """Explain why ``fn`` cannot work as an :class:`ArtifactFetcher`, or ``None`` if it
    looks usable.

    Signature-inspected and advisory ONLY. It is written to stderr as a diagnostic and
    never into an output file, because a guess about a client's shape is not a finding
    about the estate. When inspection is not possible (a C callable, a wrapped builtin)
    this returns ``None`` rather than complaining: unknown is not the same as wrong.
    """
    return _describe(fn, absent="no estate client was supplied",
                     called_as="fetcher(name)", subject="the member name",
                     convention="a client is called as fetcher(name), optionally with "
                                "type= and copy=")


def describe_synonym_resolver(fn: Any) -> Optional[str]:
    """Explain why ``fn`` cannot work as a :class:`SynonymResolver`, or ``None`` if it
    looks usable. Advisory only, exactly like :func:`describe_fetcher`."""
    return _describe(fn, absent="no synonym resolver was supplied",
                     called_as="resolver(name)", subject="the table name",
                     convention="a resolver is called as resolver(name) and nothing "
                                "else")


def describe_dependents_resolver(fn: Any) -> Optional[str]:
    """Explain why ``fn`` cannot work as a :class:`DependentsResolver`, or ``None`` if it
    looks usable. Advisory only, exactly like :func:`describe_fetcher`."""
    return _describe(fn, absent="no dependents lookup was supplied",
                     called_as="lookup(name)", subject="the artifact name",
                     convention="a lookup is called as lookup(name), optionally with "
                                "kind=")
