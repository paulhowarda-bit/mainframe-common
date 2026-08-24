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
from .errors import CobolXstateError
from .prefetch import PrefetchResult, member_key

FORMAT = "cobol-xstate-estate-bundle"
VERSION = 1

_MANIFEST = "estate-bundle.json"
_MEMBERS = "members"
_SOURCE = "source"


class BundleError(CobolXstateError):
    """The bundle could not be read, or does not describe the run being asked of it."""


def _answer_key(name: str, type_hint: Optional[str]) -> str:
    """The identity of one ASK: the member, plus what we asked it to be.

    The type is part of the key because the same member is legitimately asked for twice
    with different hints, and the two answers differ - that is what a probe chain is."""
    return member_key(name) + "|" + (type_hint or "").strip().lower()


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


# ------------------------------------------------------------------------------ write

def write_bundle(root, *, subject_name: str, subject_text: str, kind: str,
                 prefetch: PrefetchResult, answers: List[dict],
                 fetch: Optional[dict] = None) -> str:
    """Write a replayable bundle to ``root`` and return the path to its manifest.

    No timestamp is recorded anywhere, so writing the same gather twice produces the
    same bytes - a bundle is evidence, and evidence that changes when nothing changed is
    hard to trust and impossible to diff.
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
        "version": VERSION,
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
        """An :class:`~cobol_xstate_core.protocol.ArtifactFetcher` backed by this bundle."""
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
