"""The estate artifact service, as a typed contract.

Until now this contract existed only as the branch list inside
:func:`cobol_xstate_core.artifact_service.coerce` - a reader had to infer the interface
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


def describe_fetcher(fn: Any) -> Optional[str]:
    """Explain why ``fn`` cannot work as an :class:`ArtifactFetcher`, or ``None`` if it
    looks usable.

    Signature-inspected and advisory ONLY. It is written to stderr as a diagnostic and
    never into an output file, because a guess about a client's shape is not a finding
    about the estate. When inspection is not possible (a C callable, a wrapped builtin)
    this returns ``None`` rather than complaining: unknown is not the same as wrong.
    """
    if fn is None:
        return "no estate client was supplied"
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
        return ("takes no positional argument, so it cannot be called as "
                "fetcher(name) - the member name is always passed positionally")
    required = [p for p in positional[1:] if p.default is inspect.Parameter.empty]
    if required:
        names = ", ".join(p.name for p in required)
        return (f"requires argument(s) {names} that this tool does not supply - a client "
                f"is called as fetcher(name), optionally with type= and copy=")
    return None
