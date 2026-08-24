"""Stage 2 - retrieve every artifact the program depends on.

``artifacts.build_artifacts`` answers *which* other things on the estate a program
touches. This stage answers the next question a migration or impact analysis asks:

    > Now go and GET them - all of them, not just the copybooks.

It walks the artifact manifest and asks the estate's artifact service
(``artifact_service``, mf-fetch by default) for every row that names something actually
retrievable: the called COBOL programs, the copybooks, the assembler modules, the control
(CNTL/PARM) members, the Db2 DDL/DCLGEN for each table, the BMS mapsets, the JCL/PROC.

**This stage fetches the program's IMMEDIATE dependencies and stops there.** It does not
parse what it fetched and walk on. That boundary is deliberate: the manifest it works
from describes *this* program's relationships, and a callee's own dependencies are a
question about the callee - answered by running the tool on the callee, with its own
prefetch, its own complete parse, and its own manifest. Walking transitively from here
would produce those rows from a parse that had never been prefetched, which is precisely
the failure ``prefetch.py`` exists to prevent.

**It runs after ``prefetch.py``, and cannot be correct before it.** The manifest is a
product of the parse, and the parse is only complete once the copybooks and control
members are in hand - a dynamic ``CALL`` whose target literal lives in a copybook that
never arrived is not a row in this manifest at all. Members prefetch already retrieved
are reported here as ``prefetched`` rather than requested a second time.

The honest part - the reason this is a projection over the manifest rather than a
``grep`` for identifiers - is that **not every artifact row names something fetchable**,
and pretending otherwise produces a directory full of wrong files:

* ``OUT-FILE`` is a name inside ONE program. There is no member called ``OUT-FILE``
  anywhere on the estate; the retrievable identity is the ddname's DSN, which lives in
  JCL. So a file row is requested by its **dataset** (when ``--bind-jcl`` resolved one),
  else its **ddname**, and if neither is known the row is *skipped with the reason*.
* A row marked ``dynamic`` names a DATA ITEM, not an artifact. Fetching ``WS-FBSPREST``
  would at best return nothing and at worst return an unrelated member that happens to
  share the name. Skipped, with the reason.
* ``CALLER``, ``SYSOUT``, ``<dynamic-sql>`` are not members of anything. Skipped.

Every skip is recorded with its reason, so the report distinguishes *"we asked and the
estate does not have it"* (a real gap) from *"this name was never fetchable"* (a
modelling fact) - the same distinction the rest of the tool insists on.

The fetcher is the caller's: this module never invents a retrieval mechanism, a search
path, or a naming convention. It passes the artifact name and a ``type`` hint (the
estate's own vocabulary - "cobol", "copybook", "ddl", ...) and accepts whatever the
service returns, via ``preprocessor.normalize_fetched``.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

from .artifact_service import (ServiceUnavailable, call_service_many,
                               canonical_type, collect)
from .categories import CATEGORY_ASM, CATEGORY_COBOL, NON_FETCHABLE

# A CALL names a load module but NOT its language: the callee may be COBOL, assembler,
# PL/I, C, ... So a program dependency is not assumed to be COBOL - it is REQUESTED by
# trying each language in likelihood order, and the one that actually retrieves it is the
# finding (COBOL and assembler source live in different libraries, so where the member is
# found proves what it is). Most common first, to keep a COBOL callee at one round-trip;
# extend for an estate that also holds PL/I or C ("pli", "c").
PROGRAM_TYPE_ORDER: Tuple[str, ...] = ("cobol", "asm")

# artifact kind -> the type HINT passed to the service, in the estate's own vocabulary.
# Only ever a hint: it is what this program's usage suggests the artifact is, which a
# service that auto-detects is free to override - and when it does, its `detected_type`
# is the answer that goes in the report. The extension a member is saved under follows
# the type, and lives in artifact_service.EXT_FOR_TYPE so both stages agree.
#
# "program" is the exception: it carries NO single language (see PROGRAM_TYPE_ORDER). Its
# presence here only marks the kind as fetchable; the request is the ordered probe.
_KIND_TYPE: Dict[str, str] = {
    # ...from a COBOL program's manifest
    "program":          "cobol",
    "copybook":         "copybook",
    "db2-table":        "ddl",
    # A stored procedure's implementation is very often a COBOL load module of the same
    # name; asking the estate for it as COBOL lets a miss say "not found" honestly.
    "db2-stored-procedure": "cobol",
    "file":             "cntl",
    "terminal-map":     "bms",
    "cics-transaction": "csd",
    "queue":            "csd",
    # ...and from a JCL job's manifest (jcl_views.build_jcl_artifacts). The control
    # members among these are normally already in hand: resolving them is what stage 1
    # had to do in order for the job to parse at all, so they report as `prefetched`.
    "proc":             "proc",
    "include-member":   "cntl",
    "control-card":     "cntl",
    "dataset":          "cntl",
}

# `PARM.LIB(SORTCRD)` - a dataset that names one member. The member is the retrievable
# identity; which library it lives in is the estate service's business, not ours.
_DSN_MEMBER = re.compile(r"^(?P<dsn>[A-Z0-9$#@.-]+)\((?P<member>[A-Z0-9$#@]{1,8})\)$",
                         re.I)

# Rows that never name a retrievable artifact, and why. Kept explicit so the report
# says WHY rather than silently shortening the list.
_NEVER_FETCHABLE = {
    "caller": "the caller is whoever invokes this program (a JCL step or a CICS "
              "transaction), not a member that can be retrieved by this name",
    "spool":  "SYSOUT/spool is a runtime destination, not a stored artifact",
}

# A member name that could plausibly be requested from a library. Anything with a
# space, a wildcard, or the tool's own placeholder brackets is a description, not a name.
_MEMBER_NAME = re.compile(r"^[A-Z0-9$#@][A-Z0-9$#@._-]{0,43}$", re.I)


def _request_name(row: dict) -> tuple:
    """(name_to_request, reason_if_not_fetchable) for one artifact row.

    A file's retrievable identity is its DATASET, not the program-local file name -
    the whole thesis of docs/mainframe-artifacts.md, applied to retrieval."""
    kind = row.get("kind", "")
    name = str(row.get("artifact", ""))

    if kind in _NEVER_FETCHABLE:
        return None, _NEVER_FETCHABLE[kind]
    # A called program the classifier positively identified as contained-here or an IBM
    # runtime API has no application source on the estate: skip it rather than probe for a
    # member that does not exist (see classify.NON_FETCHABLE).
    if row.get("classification") in NON_FETCHABLE:
        return None, (row.get("classificationReason")
                      or f"{name} is classified '{row.get('classification')}' - no "
                         f"application source to retrieve")
    if row.get("dynamic"):
        why = (f"{name} is a data item whose run-time value names the artifact, "
               f"not the artifact name itself - fetching it would retrieve the "
               f"wrong member or nothing")
        # ...but the manifest may know WHERE the run-time value comes from. Saying so
        # here turns a dead end into an instruction: this row cannot be fetched, and
        # the artifact that does name the target can.
        named = row.get("namedBy") or []
        if named:
            why += ("; the name is supplied by "
                    + ", ".join(str(n.get("dataset") or n.get("artifact"))
                                for n in named)
                    + " - fetch that instead and read the targets out of it")
        elif row.get("candidates"):
            why += (f"; nothing external writes it, so the target is one of "
                    f"{', '.join(row['candidates'])} - fetch those directly")
        return None, why
    if kind == "file":
        # dataset (resolved by --bind-jcl) is the real identity; the ddname is the
        # next-best request; the program-local file name is not fetchable at all.
        ds = row.get("dataset")
        if ds:
            return str(ds), None
        dd = row.get("ddname")
        if dd:
            return str(dd), None
        return None, (f"{name} is a program-local file name with no ddname or dataset "
                      f"known here; bind the JCL (--bind-jcl) to resolve the DSN before "
                      f"this can be fetched")
    if kind not in _KIND_TYPE:
        return None, f"artifact kind '{kind}' has no known retrieval type"
    dsn = _DSN_MEMBER.match(name)
    if dsn:
        return dsn.group("member").upper(), None
    if not _MEMBER_NAME.match(name):
        return None, f"'{name}' is not a member name that can be requested"
    return name, None


def build_fetch_plan(manifest: dict) -> List[dict]:
    """One request row per artifact in ``manifest``, in manifest order: what would be
    fetched, under which name and type, or why the row is not fetchable. Pure - makes
    no calls, so a caller can review (or print) the plan before hitting a service."""
    plan: List[dict] = []
    for row in manifest.get("artifacts", []) or []:
        kind = row.get("kind", "")
        name, reason = _request_name(row)
        entry = {
            "artifact": row.get("artifact"),
            "kind": kind,
            "dependency": row.get("dependency"),
        }
        # Carry the static classification forward so the report lists it and a successful
        # probe can refine an `unresolved` program to cobol-program / assembler-program.
        for k in ("classification", "subsystem", "classificationReason"):
            if row.get(k):
                entry[k] = row[k]
        if name is None:
            entry.update({"status": "skipped", "reason": reason})
        elif kind == "program":
            # No assumed language: request it as each of PROGRAM_TYPE_ORDER in turn; the
            # one that retrieves it is the finding. `type` stays null so nothing claims a
            # language before the estate has answered.
            entry.update({"status": "planned", "request": name,
                          "type": None, "probeTypes": list(PROGRAM_TYPE_ORDER)})
            if name != row.get("artifact"):
                entry["requestedAs"] = (
                    "dataset" if row.get("dataset") == name
                    else "member" if _DSN_MEMBER.match(str(row.get("artifact") or ""))
                    else "ddname")
        else:
            entry.update({"status": "planned", "request": name,
                          "type": _KIND_TYPE.get(kind)})
            if name != row.get("artifact"):
                # Say WHY the request name differs from the row name (a file requested
                # by its DSN, a control member requested out of `LIB(MEMBER)`), so the
                # report is auditable.
                entry["requestedAs"] = (
                    "dataset" if row.get("dataset") == name
                    else "member" if _DSN_MEMBER.match(str(row.get("artifact") or ""))
                    else "ddname")
        plan.append(entry)
    return plan


def candidate_requests(dynamic: Optional[dict]) -> List[dict]:
    """The candidate targets of unresolved dynamic calls, as fetch requests.

    A dynamic CALL's target is not a manifest row - deliberately, because the manifest's
    value is that everything in it is a proven dependency and a candidate is not one. But
    a candidate IS a concrete member name we have reason to believe the program may
    invoke, and having it locally is strictly better than not. So the candidates are
    fetched from HERE instead: retrieved and reported, without ever being asserted as
    dependencies.

    Both grades come through, carrying which they are: ``assigned`` (a MOVE or VALUE
    provably stores it) and ``declared-88`` (an 88-level names it, but nothing proves it
    is ever stored)."""
    out: List[dict] = []
    for row in ((dynamic or {}).get("dynamicCalls") or []):
        item = row.get("item")
        for names, evidence in ((row.get("candidates") or [], "assigned"),
                                (row.get("declaredCandidates") or [], "declared-88")):
            for name in names:
                clean = str(name).strip().strip("'\"").upper()
                if not clean or not _MEMBER_NAME.match(clean):
                    continue
                out.append({"artifact": clean, "kind": "program",
                            "dependency": "runtime", "forDynamicCall": item,
                            "evidence": evidence})
    return out


def _dispatch(row: dict, subject: str, seen, prefetched: dict,
              fetcher: Optional[Callable]) -> Tuple[str, Optional[str]]:
    """How one plan row is settled - decided WITHOUT touching the service.

    Written once and read twice: a pre-pass replays it to learn which names will actually
    be requested (so they can be retrieved together), and the recording pass replays it to
    build the row. Two copies of this chain would be two chances to disagree about whether
    a member is asked for, which is precisely the "one member is one round-trip" property
    the report asserts - so there is one copy, and both callers get the same answers from
    it by construction.

    ``seen`` is only ever tested for membership, which is why the pre-pass can pass a set
    of names while the recording pass passes the ``done`` map of real outcomes: the
    branch does not depend on WHICH outcome a name reached, only that it reached one.
    """
    if row["status"] == "skipped":
        return "skipped", None                # build_fetch_plan already said why
    key = str(row["request"]).upper()
    if key == subject:
        return "subject", key
    if key in seen:
        return "inherit", key
    if prefetched.get(key) is not None:
        return "prefetched", key
    if fetcher is None:
        return "no-service", key
    return "request", key


def _wanted(row: dict):
    """What to ask the service for this row: a probe ORDER for a program (whose language
    is proven by which type retrieves it), else the single type hint its kind implies."""
    return row.get("probeTypes") or row.get("type")


def fetch_dependencies(manifest: dict, fetcher: Optional[Callable],
                       dest: Optional[str] = None,
                       prefetched: Optional[Dict[str, Tuple[str, str]]] = None,
                       unavailable: Optional[str] = None,
                       dynamic: Optional[dict] = None,
                       jobs: int = 1) -> dict:
    """Fetch this program's immediate dependent artifacts.

    ``fetcher(name, type=..., copy=...)`` is the estate's artifact service - mf-fetch by
    default (see ``artifact_service``). ``prefetched`` is the store stage 1 filled: a
    member already in it is reported, not re-requested. ``dest``, when given, collects
    everything retrieved into one directory a later run can be pointed at with ``-I``.

    ``dynamic`` is the dynamic-calls report; its candidate targets are fetched too. They
    come from here rather than from the manifest because a candidate is not a proven
    dependency and must not be listed as one - but it is a real member name worth having.

    Returns a report: one row per artifact with its status (``fetched`` / ``prefetched``
    / ``not-found`` / ``error`` / ``no-service`` / ``skipped`` / ``already-fetched``),
    where it came from, and what else carried the same name. Nothing is invented: a name
    that was never fetchable is reported as ``skipped`` with the reason, not dropped."""
    prefetched = prefetched or {}
    # A COBOL manifest names its subject "program"; a JCL one names it "job" (see
    # jcl_views.build_jcl_artifacts). Reading only the first labelled every JCL run's
    # report `"program": "?"` and left the never-fetch-yourself guard holding "?", so a
    # job was requested from the estate as a dependency of itself.
    program = manifest.get("program") or manifest.get("job") or "?"
    subject = str(program).upper()
    # name -> the status the FIRST row for it actually reached. Recording the name
    # BEFORE the fetch made every later row for it claim "already-fetched ... was
    # already retrieved in this run" even when the first attempt came back not-found or
    # errored - counting failures as successes in the very report that exists to say
    # what was retrieved.
    done: Dict[str, str] = {}

    rows: List[dict] = []
    errors: List[dict] = []

    # Manifest rows first, then dynamic-call candidates. Order matters: a name that is
    # BOTH a proven dependency and a candidate should be recorded as the dependency.
    plan = build_fetch_plan(manifest) + [
        dict(entry, status="planned", request=entry["artifact"],
             type=None, probeTypes=list(PROGRAM_TYPE_ORDER))
        for entry in candidate_requests(dynamic)]

    # PASS 1 - which names actually reach the service, in plan order and each once. The
    # plan is fully known before any of it runs (build_fetch_plan is pure), so there is
    # no reason to discover the second request only after the first has come back.
    # Collapsing duplicates HERE, rather than on a `done` map consulted mid-flight, is
    # also what keeps "one member is one round-trip" true once these overlap: read-then-
    # write on `done` is a race, and two threads would both find the name absent and both
    # ask for it.
    pending: List[Tuple[str, object]] = []
    pending_keys: List[str] = []
    seen_keys: set = set()
    for entry in plan:
        kind, key = _dispatch(entry, subject, seen_keys, prefetched, fetcher)
        if kind in ("skipped", "subject", "inherit"):
            continue
        seen_keys.add(key)
        if kind == "request":
            pending.append((entry["request"], _wanted(entry)))
            pending_keys.append(key)

    # PASS 2 - retrieve them. The only concurrent step; it decides nothing.
    got_by_key: Dict[str, object] = dict(
        zip(pending_keys, call_service_many(fetcher, pending, jobs, dest))
    ) if pending else {}

    # PASS 3 - record, in plan order, so the report reads the same however the retrievals
    # happened to interleave.
    for entry in plan:
        row = dict(entry)
        row["forProgram"] = program
        kind, key = _dispatch(row, subject, done, prefetched, fetcher)
        if kind == "skipped":
            rows.append(row)
            continue

        name = row["request"]
        # Keyed on the NAME alone, not name+kind: one member is one round-trip however
        # many ways this manifest arrives at it. A JCL job that both EXECs PAYPROC and
        # names it as a called program produces two rows for one member, and asking the
        # estate twice for the same thing is waste that scales with the estate.
        if kind == "subject":
            row.update({"status": "skipped",
                        "reason": f"{name} is the artifact being analysed, not a "
                                  f"dependency of it"})
            rows.append(row)
            continue
        if kind == "inherit":
            prior = done[key]
            if prior in ("fetched", "prefetched"):
                row.update({"status": "already-fetched",
                            "reason": f"{name} was already retrieved in this run"})
            else:
                # Carry the real outcome forward: a second row must not upgrade a
                # not-found or an error into a success.
                row.update({"status": prior,
                            "reason": f"{name} was already requested in this run and "
                                      f"came back {prior}"})
            rows.append(row)
            continue

        if kind == "prefetched":
            hit = prefetched[key]
            # Stage 1 already paid for this member. Reported, because a reader tracing
            # what this run retrieved needs it in the list - but not requested twice.
            row.update({"status": "prefetched", "source": hit[1], "bytes": len(hit[0]),
                        "reason": "retrieved by prefetch (stage 1), before the parse"})
            rows.append(row)
            done[key] = row["status"]
            continue

        if kind == "no-service":
            row.update({"status": "no-service",
                        "reason": (unavailable or "no estate artifact service is "
                                   "configured, so this artifact was never looked for")})
            rows.append(row)
            done[key] = row["status"]
            continue

        probe = row.get("probeTypes")
        # Subscripted, never .get(): a missing key would otherwise read as `None`, which
        # here MEANS "the estate was asked and had nothing" - so a future edit that broke
        # the two passes' agreement about what gets requested would not fail, it would
        # quietly report every affected member absent from the estate. That is the one
        # failure this report exists to make impossible.
        got = got_by_key[key]
        if isinstance(got, ServiceUnavailable):
            # Raised in pass 2, carried here, and recorded against ITS OWN row - so one
            # member's failure still says exactly what happened to that member, and never
            # to any other.
            row.update({"status": "error", "error": str(got)})
            errors.append({"artifact": name, "error": row["error"]})
            rows.append(row)
            done[key] = row["status"]
            continue

        if got is None:
            row["status"] = "not-found"
            rows.append(row)
            done[key] = row["status"]
            continue

        got = collect(got, dest)
        row["status"] = "fetched"
        # The service's own answers - what it found, where, and what else shares the
        # name - in preference to the kind we inferred from one program's usage.
        row.update(got.row())
        # A called program's language is PROVEN, never assumed: by the estate's own
        # detected_type when it gives one, else by which probe (cobol -> asm -> ...)
        # actually retrieved it. Record it and how we know, so a reader is not left to
        # infer "cobol" from silence.
        if probe:
            lang = canonical_type(got.detected_type or got.requested_type)
            if lang:
                row["language"] = lang
                if got.detected_type:
                    row["languageBasis"] = "estate detected_type"
                else:
                    earlier = probe[:probe.index(got.requested_type)] \
                        if got.requested_type in probe else []
                    row["languageBasis"] = (
                        "retrieved as " + lang
                        + (f" ({', '.join(earlier)} not present)" if earlier else ""))
                if row.get("kind") == "program" and lang in ("cobol", "asm"):
                    # The estate resolved a formerly-`unresolved` program by producing its
                    # source: it is now classified by the language that retrieved it.
                    row["classification"] = (
                        CATEGORY_COBOL if lang == "cobol" else CATEGORY_ASM)
        rows.append(row)
        done[key] = row["status"]

    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "format": "cobol-xstate-fetch",
        "program": program,
        "note": (
            "Stage 2: one row per artifact this program depends on, with the outcome of "
            "retrieving it. These are the program's IMMEDIATE dependencies - a callee's "
            "own dependencies are a question about the callee, answered by running the "
            "tool on it. 'fetched' carries the library it came from (and alternatives, "
            "when the same name exists in more than one); 'prefetched' was already "
            "retrieved by stage 1 before the parse; 'not-found' means the estate service "
            "was asked and had nothing; 'error' means the request itself failed (a "
            "fixable condition, NOT evidence the artifact is absent); 'no-service' means "
            "no estate client was reachable, so it was never looked for; 'skipped' means "
            "the row never named a retrievable artifact and says why - a program-local "
            "file name with no ddname/DSN, a dynamic name that is really a data item, or "
            "a caller/spool destination. " + _candidate_note()
        ),
        "counts": counts,
        "artifacts": rows,
        "errors": errors,
    }


def _candidate_note() -> str:
    return (
        "Rows carrying 'forDynamicCall' are CANDIDATE targets of an unresolved dynamic "
        "call, not proven dependencies - which is why they appear here and NOT in the "
        "artifact manifest, whose value is that everything in it is real. 'evidence' "
        "grades them: 'assigned' means a MOVE or VALUE clause provably stores the name; "
        "'declared-88' means an 88-level names it but nothing proves it is ever stored. "
        "A candidate that is not on the estate is counted as 'not-found' like any other "
        "- we had a concrete name and the estate could not produce it, which is worth "
        "knowing however we came by the name."
    )
