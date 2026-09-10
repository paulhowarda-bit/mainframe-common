"""The estate bundle: gather where the mainframe is reachable, model where it is not.

Retrieval needs the estate; modelling needs nothing but the text. Those two halves of a
run often want to happen on different machines - the estate-connected box may be locked
down, offline, or simply somewhere else - and today they cannot, because retrieval is
wired straight into the run.

A bundle is the hand-off. ``gather`` runs the retrieval half and keeps ONLY what came
off the estate; a later run reads the bundle instead of the network and produces the
same model, and the same two retrieval reports, that the gather run would have.

**The design rule that makes this trustworthy: offline mode introduces no new branch.**
Neither ``prefetch`` nor ``fetch`` learns about bundles. Both already take the estate
client as a parameter, so replay is the same code with a *different fetcher* - the
planning, the probe chain, the collect step, the row ordering and every reason string
run exactly as they do live. An "offline path" that reimplemented any of that would be a
second implementation to keep in step, and the first thing to drift.

**Why answers are keyed by (name, requested type) and record misses.**
``call_service_probing`` asks a program as ``cobol``, then as ``asm``, and ``fetch``
derives its ``languageBasis`` from *which probe missed first*. A bundle that stored only
the winning answer would replay a member that was found on the first try, and the report
would gain a different sentence. So every ask is recorded, including the ones that came
back empty, with a monotonic ``seq`` - and the file is written in ``seq`` order, so
``--jobs 8`` thread interleaving cannot change its bytes.

**Why a name with no record RAISES.** Returning ``None`` would mean "the estate was
asked and had nothing", which is a claim about the estate. If an offline run asks for
something the gather run never did, the truth is that the two are not the same analysis -
and saying so loudly is the entire reason this tool keeps "asked and had nothing" apart
from "could not ask".

**What a bundle does not promise.** The ``source`` of a member resolved from the local
search path names the file it was read from, which is necessarily a different path on a
different machine. Bundles reproduce the estate's ANSWERS - every status, reason,
ordering, count, detected type and alternative - not the filesystem layout of the box
that made them.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .artifact_service import ServiceUnavailable, coerce, decode_member
from .dependents import coerce_answer
from .errors import CobolXstateError
from .kinds import manifest_kind
from .prefetch import PrefetchResult, member_key

FORMAT = "cobol-xstate-estate-bundle"
#: The highest version this build understands. A bundle is stamped with the LOWEST
#: version that describes what it actually contains (see :func:`write_bundle`), so a
#: gather run that opened no new door still writes a bundle every older build can replay
#: in full, and one that did is refused by a build that would quietly ignore half of it.
VERSION = 2
_VERSION_MEMBERS_ONLY = 1
_VERSION_WITH_DEPENDENTS = 2

_MANIFEST = "estate-bundle.json"
_MEMBERS = "members"
_SOURCE = "source"
_DEPENDENTS = "dependents"


class BundleError(CobolXstateError):
    """The bundle could not be read, or does not describe the run being asked of it."""


def _answer_key(name: str, type_hint: Optional[str]) -> str:
    """The identity of one ASK: the member, plus what we asked it to be.

    The type is part of the key because the same member is legitimately asked for twice
    with different hints, and the two answers differ - that is what a probe chain is."""
    return member_key(name) + "|" + (type_hint or "").strip().lower()


def _dependents_key(name: str, kind: Optional[str]) -> str:
    """The identity of one DEPENDENTS ask: the artifact, plus the kind it was asked as.

    The kind is reduced through the shared vocabulary rather than kept verbatim, so a
    gather run that asked as ``MAPSET`` and a replay that asks as ``terminal-map`` are the
    same ask - which they are, and the manifest word is the one both sides can spell."""
    return member_key(name) + "|" + (manifest_kind(kind) or "")


# --------------------------------------------------------------------------- recording

def recording_fetcher(inner) -> Tuple[Any, List[dict]]:
    """Wrap an estate client so every ask is recorded, and return ``(wrapper, answers)``.

    Byte-neutral by construction: the wrapper returns ``inner``'s value verbatim and
    re-raises its exception verbatim, so a gather run behaves exactly like a normal one.

    Thread-safe, because retrieval overlaps at ``--jobs > 1``: the sequence number is
    handed out under a lock, and :func:`write_bundle` sorts by it, so the recorded order
    is the order the calls were MADE and never the order they happened to finish.
    """
    answers: List[dict] = []
    lock = threading.Lock()
    counter = {"n": 0}

    def wrapper(name, **kwargs):
        # Forward EXACTLY the keywords we were handed, never a reconstructed set.
        # call_service narrows its call on TypeError (dropping `copy`, then `type`) until
        # the client's signature accepts it; a wrapper that always passed both would make
        # every attempt fail identically and a one-argument client impossible to use.
        type_hint = kwargs.get("type")
        with lock:
            seq = counter["n"]
            counter["n"] += 1

        def record(**fields):
            with lock:
                answers.append({"seq": seq, "name": str(name),
                                "requestedType": type_hint, **fields})

        try:
            got = inner(name, **kwargs)
        except TypeError as exc:
            # The client's signature does not accept these keywords, so call_service
            # drops one and retries. Recorded as its own outcome and replayed by raising
            # TypeError again: without it, replay would answer the FIRST shape happily,
            # the retry would never happen, and the bundle would be asked for a
            # (name, type) pair the gather run never actually got an answer for.
            record(outcome="unsupported-kwargs", error=f"{exc.__class__.__name__}: {exc}")
            raise
        except Exception as exc:
            record(outcome="error", error=f"{exc.__class__.__name__}: {exc}")
            raise

        try:
            fetched = coerce(got, str(name), requested_type=type_hint)
        except Exception:
            # An unreadable shape is call_service's to report, in its own words. Record
            # that we could not read it and hand `got` back untouched, so the gather run
            # fails exactly as it would have without this wrapper.
            record(outcome="unreadable")
            return got

        if fetched is None:
            record(outcome="not-found")
        else:
            record(outcome="found",
                   artifactName=fetched.name,
                   source=fetched.source,
                   detectedType=fetched.detected_type,
                   alternatives=list(fetched.alternatives),
                   text=fetched.text)              # stripped out by write_bundle
        return got

    return wrapper, answers


def recording_dependents_resolver(inner) -> Tuple[Any, List[dict]]:
    """Wrap a dependents lookup so every ask is recorded; ``(wrapper, answers)``.

    Byte-neutral the same way :func:`recording_fetcher` is: the value comes back verbatim
    and an exception re-raises verbatim, so a gather run behaves exactly like a live one.

    What is recorded is the NORMALISED rows rather than the host's raw shape - unlike a
    member, where the text is recorded byte for byte. The reason is that a member's text
    is what reaches the output, whereas a dependents row's declared fields are: anything
    else the host sent was going to be dropped by
    :func:`~mainframe_artifacts.dependents.normalise_row` on the way through. Recording
    after that gate keeps a bundle to the same rule as everything else here - it holds the
    estate's ANSWERS, in the form the run actually used them.

    ``unsupported-kwargs`` is recorded and replayed for the same reason the fetcher does
    it: :class:`~mainframe_artifacts.dependents.DependentsLookup` narrows its call on
    ``TypeError`` so a one-argument lookup needs no adapter, and a replay that answered
    the first shape happily would never run that narrowing.
    """
    answers: List[dict] = []
    lock = threading.Lock()
    counter = {"n": 0}

    def wrapper(name, **kwargs):
        kind = kwargs.get("kind")
        with lock:
            seq = counter["n"]
            counter["n"] += 1

        def record(**fields):
            with lock:
                answers.append({"seq": seq, "name": str(name), "kind": kind, **fields})

        try:
            got = inner(name, **kwargs)
        except TypeError as exc:
            record(outcome="unsupported-kwargs", error=f"{exc.__class__.__name__}: {exc}")
            raise
        except Exception as exc:
            record(outcome="error", error=f"{exc.__class__.__name__}: {exc}")
            raise

        if got is None:
            # Outside what the index covers. Recorded as its own outcome, because an
            # offline run must be able to say that too, and not "nothing depends on it".
            record(outcome="not-covered")
            return got

        answer, why = coerce_answer(got)
        if why:
            # The shape is the lookup's to refuse, in its own words. Record that it could
            # not be read and hand `got` back untouched, so the gather run degrades
            # exactly as it would have without this wrapper.
            record(outcome="unreadable", error=why)
            return got
        row = {"outcome": "answered", "rows": [dict(r) for r in answer.rows]}
        if answer.truncated:
            row["truncated"] = True
            if answer.total is not None:
                row["total"] = answer.total
        record(**row)
        return got

    return wrapper, answers


# ------------------------------------------------------------------------------ write

def write_bundle(root, *, subject_name: str, subject_text: str, kind: str,
                 prefetch: PrefetchResult, answers: List[dict],
                 fetch: Optional[dict] = None,
                 dependents: Optional[List[dict]] = None) -> str:
    """Write a replayable bundle to ``root`` and return the path to its manifest.

    No timestamp is recorded anywhere, so writing the same gather twice produces the
    same bytes - a bundle is evidence, and evidence that changes when nothing changed is
    hard to trust and impossible to diff.

    ``dependents`` is :func:`recording_dependents_resolver`'s list, and a gather run that
    opened no dependents door passes none. The manifest is then stamped
    :data:`_VERSION_MEMBERS_ONLY` and is byte-identical to what this tool wrote before the
    reverse direction existed - which is the same rule the view follows: a run told
    nothing says nothing.
    """
    root = Path(root)
    (root / _MEMBERS).mkdir(parents=True, exist_ok=True)
    (root / _SOURCE).mkdir(parents=True, exist_ok=True)

    src_path = Path(_SOURCE) / subject_name
    (root / src_path).write_text(subject_text, encoding="utf-8")

    recorded: List[dict] = []
    for ans in sorted(answers, key=lambda a: a["seq"]):
        row = {k: v for k, v in ans.items() if k != "text" and v is not None}
        if ans.get("outcome") == "found":
            # One file per ASK, named by seq: the same member asked for as two different
            # types is two answers, and keying the file by name alone would collide them.
            rel = f"{_MEMBERS}/{ans['seq']:04d}.{member_key(ans['name'])[:44]}"
            (root / rel).write_text(ans["text"], encoding="utf-8")
            row["path"] = rel
            if not row.get("alternatives"):
                row.pop("alternatives", None)
        recorded.append(row)

    manifest = {
        "format": FORMAT,
        "version": (_VERSION_WITH_DEPENDENTS if dependents
                    else _VERSION_MEMBERS_ONLY),
        "subject": {"name": subject_name, "kind": kind, "path": str(src_path).replace("\\", "/")},
        "service": {
            "available": prefetch.unavailable is None,
            "unavailable": prefetch.unavailable,
        },
        # Every member the gather run settled, however it settled it. An offline run that
        # asks for something outside this set is not replaying the same analysis.
        "seen": sorted({member_key(r["member"]) for r in prefetch.rows
                        if r.get("member")}),
        "answers": recorded,
    }
    if dependents:
        # Sorted by seq for the same reason the member answers are: --jobs 8 must not be
        # able to change these bytes.
        manifest[_DEPENDENTS] = [
            {k: v for k, v in ans.items() if v is not None}
            for ans in sorted(dependents, key=lambda a: a["seq"])
        ]
    (root / _MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # The two reports travel with the bundle so the gather run's account is readable
    # without replaying it.
    (root / f"{Path(subject_name).stem}.prefetch.json").write_text(
        json.dumps(prefetch.report(), indent=2) + "\n", encoding="utf-8")
    if fetch is not None:
        (root / f"{Path(subject_name).stem}.fetch.json").write_text(
            json.dumps(fetch, indent=2) + "\n", encoding="utf-8")
    return str(root / _MANIFEST)


# ------------------------------------------------------------------------------- read

class EstateBundle:
    """A gathered estate, replayable as an artifact service."""

    def __init__(self, root: Path, manifest: dict):
        self.root = Path(root)
        self.manifest = manifest
        self.subject_name: str = manifest["subject"]["name"]
        self.kind: str = manifest["subject"].get("kind", "cobol")
        self._by_key: Dict[str, dict] = {}
        self._dependents: Dict[str, dict] = {}
        for ans in manifest.get(_DEPENDENTS, []):
            self._dependents.setdefault(
                _dependents_key(ans["name"], ans.get("kind")), ans)
        for ans in manifest.get("answers", []):
            # First answer wins for a repeated ask: within one run the estate is taken to
            # be deterministic, and a second differing answer would be a fact about the
            # estate rather than something a replay should invent a policy for.
            self._by_key.setdefault(_answer_key(ans["name"], ans.get("requestedType")),
                                    ans)

    # -- what the offline run needs -----------------------------------------
    def source(self) -> str:
        """The subject's text, exactly as the gather run read it."""
        return (self.root / self.manifest["subject"]["path"]).read_text(encoding="utf-8")

    def seen(self) -> Set[str]:
        return set(self.manifest.get("seen", ()))

    @property
    def unavailable(self) -> Optional[str]:
        """Whatever the gather run recorded about the service being unreachable - carried
        across so the replay's report says the same thing rather than claiming a healthy
        estate the gather run never had."""
        return (self.manifest.get("service") or {}).get("unavailable")

    def fetcher(self):
        """An :class:`~mainframe_artifacts.protocol.ArtifactFetcher` backed by this bundle."""
        def replay(name, type=None, copy=None):       # noqa: A002 - the wire keyword
            ans = self._by_key.get(_answer_key(name, type))
            if ans is None:
                raise ServiceUnavailable(
                    f"this estate bundle has no record of {member_key(name)!r}"
                    + (f" as {type}" if type else "")
                    + " - the offline run asked for something the gather run did not, "
                      "so the two are not the same analysis")
            outcome = ans.get("outcome")
            if outcome == "not-found":
                return {"found": False}
            if outcome == "error":
                raise ServiceUnavailable(ans.get("error", "recorded failure"))
            if outcome == "unsupported-kwargs":
                # Reproduce the gather client's narrower signature, so call_service runs
                # the same drop-a-keyword-and-retry dance and lands on the same answer.
                raise TypeError(ans.get("error", "client does not accept these keywords"))
            if outcome == "unreadable":
                raise ServiceUnavailable(
                    f"the gather run could not read the estate's answer for "
                    f"{member_key(name)!r}; there is nothing to replay")
            text = (self.root / ans["path"]).read_text(encoding="utf-8")
            # `copied_to` is deliberately NOT replayed: it would name a directory on the
            # gather box. Omitting it lets artifact_service.collect save the member into
            # THIS run's deps/, so copiedTo describes where the file actually now is.
            out = {"artifact_name": ans.get("artifactName") or str(name),
                   "text": text,
                   "source_location": ans.get("source") or f"<fetched {name}>"}
            if ans.get("detectedType"):
                out["detected_type"] = ans["detectedType"]
            if ans.get("alternatives"):
                out["alternatives"] = list(ans["alternatives"])
            return out
        return replay

    def has_dependents(self) -> bool:
        """Whether the gather run opened a dependents door at all.

        A replay of a bundle that has none must supply no lookup, so its view is absent
        exactly as the gather run's was - rather than a lookup that answers every artifact
        with a failure."""
        return bool(self._dependents)

    def dependents(self):
        """A :class:`~mainframe_artifacts.protocol.DependentsResolver` backed by this
        bundle.

        Its own accessor rather than the fetcher's path: a member fetch is keyed
        ``(name, requested type)`` and answers with bytes, a dependents ask is keyed
        ``(name, kind)`` and answers with rows, and running the second through the first
        would only be a way to confuse both.

        An ask the gather run never made RAISES, for the reason the whole module gives:
        answering it - either way - would be inventing an estate fact. Raising is also
        what the lookup's own contract calls a failed lookup, so the run completes, the
        reason is recorded, and nothing is reported as "nothing depends on this"."""
        def replay(name, kind=None):
            ans = self._dependents.get(_dependents_key(name, kind))
            if ans is None:
                raise BundleError(
                    f"this estate bundle has no dependents record for "
                    f"{member_key(name)!r}"
                    + (f" as {kind}" if kind else "")
                    + " - the offline run asked for something the gather run did not, "
                      "so the two are not the same analysis")
            outcome = ans.get("outcome")
            if outcome == "not-covered":
                return None
            if outcome == "error":
                raise BundleError(ans.get("error", "recorded failure"))
            if outcome == "unsupported-kwargs":
                # Reproduce the gather lookup's narrower signature, so DependentsLookup
                # runs the same drop-the-keyword-and-retry and lands on the same answer.
                raise TypeError(ans.get("error", "lookup does not accept these keywords"))
            if outcome == "unreadable":
                raise BundleError(
                    f"the gather run could not read the lookup's answer for "
                    f"{member_key(name)!r}; there is nothing to replay")
            out: Dict[str, Any] = {"rows": [dict(r) for r in ans.get("rows", [])]}
            if ans.get("truncated"):
                out["truncated"] = True
                if ans.get("total") is not None:
                    out["total"] = ans["total"]
            return out
        return replay


def open_bundle(path) -> EstateBundle:
    """Open a bundle written by :func:`write_bundle`; ``path`` is its directory or its
    manifest."""
    p = Path(path)
    manifest_path = p if p.is_file() else p / _MANIFEST
    if not manifest_path.is_file():
        raise BundleError(f"no estate bundle at {p} (expected {_MANIFEST})")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BundleError(f"{manifest_path} is not readable JSON: {exc}") from exc
    if manifest.get("format") != FORMAT:
        raise BundleError(f"{manifest_path} is not a {FORMAT} (got "
                          f"{manifest.get('format')!r})")
    if int(manifest.get("version", 0)) > VERSION:
        raise BundleError(
            f"{manifest_path} is version {manifest.get('version')}, but this build "
            f"understands up to {VERSION} - upgrade the tool rather than reading it "
            f"partially")
    return EstateBundle(manifest_path.parent, manifest)
