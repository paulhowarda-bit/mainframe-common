"""The reverse direction: what depends on an artifact, and the three answers.

The claim this file defends is the one the contract is FOR: "nobody told us", "the index
was asked and nothing depends on this" and "the lookup broke" never collapse into one
another - not through either door, not through a bundle, and not when a host sends a shape
the contract does not allow.
"""

import argparse
import json

import pytest

from mainframe_artifacts.bundle import (BundleError, open_bundle,
                                        recording_dependents_resolver, write_bundle)
from mainframe_artifacts.cliargs import dependents_lookup
from mainframe_artifacts.dependents import (DependentsLookup, read_dependents_map)
from mainframe_artifacts.fetch import _KIND_TYPE, _NEVER_FETCHABLE
from mainframe_artifacts.kinds import MANIFEST_KINDS, manifest_kind
from mainframe_artifacts.prefetch import Prefetcher

_ROWS = [
    {"name": "pgm1", "kind": "PROGRAM", "via": "READ FILE",
     "match_strength": "qualified", "detail": "PGM1 reads CUSTFILE at line 412"},
    {"name": "PGM2", "kind": "MODULE", "via": "CALL"},
]


def _resolver(rows, calls=None):
    """A lookup that answers ``rows`` for anything, counting its calls."""
    def lookup(name, kind=None):
        if calls is not None:
            calls.append((name, kind))
        return rows
    return lookup


# -- the row, and the join ------------------------------------------------------------

def test_rows_are_normalised_and_carry_both_kinds():
    lookup = DependentsLookup(resolver=_resolver(_ROWS))
    answer = lookup("custfile", "file")
    assert answer is not None
    first, second = answer.rows
    # The name is uppercased the way every member name in this tool is...
    assert first["name"] == "PGM1"
    # ...the host's own spelling of the kind is KEPT, and the manifest word carried
    # beside it, because that is what the join attaches to.
    assert (first["kind"], first["manifest_kind"]) == ("PROGRAM", "program")
    assert (second["kind"], second["manifest_kind"]) == ("MODULE", "program")
    assert first["via"] == "READ FILE"
    assert first["match_strength"] == "qualified"
    # An absent optional field is absent, not present-and-empty.
    assert "detail" not in second


def test_a_field_the_host_invented_does_not_reach_the_row():
    lookup = DependentsLookup(resolver=_resolver(
        [{"name": "PGM1", "kind": "PROGRAM", "confidence": 0.8}]))
    answer = lookup("CUSTFILE", "file")
    assert answer is not None
    assert "confidence" not in answer.rows[0]


def test_the_fine_host_kinds_reduce_and_are_not_lost():
    # The two places a CICS index is finer than the manifest vocabulary. Both reduce for
    # the join; neither is collapsed on the way in.
    assert manifest_kind("MAP") == manifest_kind("MAPSET") == "terminal-map"
    assert manifest_kind("TSQUEUE") == manifest_kind("TDQUEUE") == "queue"
    lookup = DependentsLookup(resolver=_resolver(
        [{"name": "MAP01", "kind": "MAP", "via": "SEND MAP"}]))
    row = lookup("MAPSET1", "MAPSET").rows[0]
    assert (row["kind"], row["manifest_kind"]) == ("MAP", "terminal-map")


def test_kind_vocabulary_is_one_source():
    """``fetch``'s two tables key on kind; they may not drift out of the vocabulary."""
    assert set(_KIND_TYPE) <= MANIFEST_KINDS
    assert set(_NEVER_FETCHABLE) <= MANIFEST_KINDS


# -- the three answers ----------------------------------------------------------------

def test_no_lookup_supplied_is_none_at_the_boundary():
    args = argparse.Namespace(dependents_map=None, dependents_resolver=None)
    lookup, why = dependents_lookup(args)
    assert (lookup, why) == (None, None)
    assert DependentsLookup().supplied is False


def test_asked_and_empty_is_not_the_same_as_not_covered():
    asked_and_empty = DependentsLookup(resolver=_resolver([]))("CUSTFILE", "file")
    assert asked_and_empty is not None and asked_and_empty.rows == ()
    # None from the resolver means the artifact is outside what the index covers, and
    # that must NOT arrive as an empty list of dependents.
    assert DependentsLookup(resolver=_resolver(None))("CUSTFILE", "file") is None


def test_a_lookup_that_raises_is_recorded_and_never_asked_again():
    calls = []

    def boom(name, kind=None):
        calls.append(name)
        raise RuntimeError("index unreachable")

    lookup = DependentsLookup(resolver=boom)
    assert lookup("CUSTFILE", "file") is None
    assert lookup("OTHERFILE", "file") is None
    assert calls == ["CUSTFILE"]                      # asked once, then disabled
    assert "index unreachable" in lookup.disabled_reason
    assert "degraded" in lookup.describe()


@pytest.mark.parametrize("got, expected", [
    ("PGM1", "expected rows"),                        # a bare string is not rows
    ({"truncated": True}, "no rows"),                 # an answer object without them
    ([{"kind": "PROGRAM"}], "no name"),
    ([{"name": "PGM1", "kind": "TRIGGER"}], "not an artifact kind"),
    ([{"name": "PGM1", "kind": "PROGRAM", "match_strength": "probably"}],
     "expected one of"),
])
def test_a_shape_the_contract_disallows_is_refused_with_the_reason(got, expected):
    lookup = DependentsLookup(resolver=_resolver(got))
    assert lookup("CUSTFILE", "file") is None
    assert expected in lookup.disabled_reason
    # Refused the way a non-string synonym result is: the lookup stops, it does not
    # answer "nothing depends on this".
    assert lookup.mapping == {}


def test_the_reported_fan_out_cap_survives():
    lookup = DependentsLookup(resolver=_resolver(
        {"rows": _ROWS, "truncated": True, "total": 9182}))
    answer = lookup("CUSTFILE", "file")
    assert (answer.truncated, answer.total) == (True, 9182)


def test_a_one_argument_lookup_needs_no_adapter():
    seen = []

    def narrow(name):                                 # no kind= at all
        seen.append(name)
        return _ROWS

    lookup = DependentsLookup(resolver=narrow)
    assert lookup("CUSTFILE", "file") is not None
    assert lookup("OTHER", "file") is not None
    assert seen == ["CUSTFILE", "OTHER"]              # narrowed once, then remembered


# -- the two doors --------------------------------------------------------------------

def test_the_map_wins_and_the_resolver_is_never_asked():
    calls = []
    lookup = DependentsLookup({"CUSTFILE|file": [{"name": "PGM9", "kind": "PROGRAM"}]},
                              _resolver(_ROWS, calls))
    answer = lookup("custfile", "FILE")
    assert [r["name"] for r in answer.rows] == ["PGM9"]
    assert answer.door == "map"
    assert calls == []
    # ...and the resolver still answers what the map does not hold.
    assert lookup("OTHERFILE", "file").door == "resolver"
    assert calls == [("OTHERFILE", "file")]


def test_a_map_entry_may_say_nothing_depends_on_it():
    lookup = DependentsLookup({"CUSTFILE|file": []})
    asked = lookup("CUSTFILE", "file")
    assert asked is not None and asked.rows == ()
    # ...whereas a name the file never mentions says nothing at all.
    assert lookup("OTHERFILE", "file") is None


def test_a_map_key_meets_an_ask_whichever_word_each_used():
    lookup = DependentsLookup({"MAPSET1|MAPSET": [{"name": "PGM1", "kind": "PROGRAM"}]})
    assert lookup("MAPSET1", "terminal-map") is not None
    assert lookup("MAPSET1") is not None              # name alone: the only entry for it


def test_a_kindless_ask_is_not_guessed_when_the_name_is_ambiguous():
    lookup = DependentsLookup({"WORK1|file": [{"name": "PGM1", "kind": "PROGRAM"}],
                               "WORK1|queue": [{"name": "PGM2", "kind": "PROGRAM"}]})
    assert lookup("WORK1", "file").rows[0]["name"] == "PGM1"
    # Two entries for one name is a question only the caller can settle.
    assert lookup("WORK1") is None


@pytest.mark.parametrize("text, expected", [
    ("{not json", "not valid JSON"),
    ('["PGM1"]', "must be a JSON object"),
    ('{"CUSTFILE|file": {"name": "PGM1"}}', "must be a list"),
    ('{"CUSTFILE|file": [{"kind": "PROGRAM"}]}', "no name"),
])
def test_read_dependents_map_refuses_whole_never_partially(tmp_path, text, expected):
    path = tmp_path / "dep.json"
    path.write_text(text, encoding="utf-8")
    mapping, why = read_dependents_map(path)
    assert mapping is None
    assert expected in why


def test_read_dependents_map_reads_the_shape_a_host_exports(tmp_path):
    path = tmp_path / "dep.json"
    path.write_text(json.dumps({"CUSTFILE|file": _ROWS, "MAPSET1|MAPSET": []}),
                    encoding="utf-8")
    mapping, why = read_dependents_map(path)
    assert why is None
    lookup = DependentsLookup(mapping)
    assert len(lookup("CUSTFILE", "file").rows) == 2
    assert lookup("MAPSET1", "terminal-map").rows == ()


def test_a_map_file_that_will_not_open_is_an_operator_error(tmp_path):
    args = argparse.Namespace(dependents_map=str(tmp_path / "missing.json"),
                              dependents_resolver=None)
    lookup, why = dependents_lookup(args)
    assert lookup is None
    assert "--dependents-map" in why and "no such file" in why


# -- the bundle: gather with a lookup, replay with none -------------------------------

def _gather(tmp_path, asks, resolver):
    """Ask a lookup for every artifact in ``asks`` and write the bundle that records it."""
    recorded, answers = recording_dependents_resolver(resolver)
    lookup = DependentsLookup(resolver=recorded)
    live = {(n, k): lookup(n, k) for n, k in asks}
    pf = Prefetcher(lambda name, **kw: {"found": False},
                    dest=str(tmp_path / "deps"))
    pf.name_source("subject.cbl")
    root = tmp_path / "bundle"
    write_bundle(root, subject_name="subject.cbl", subject_text="SOURCE\n", kind="cobol",
                 prefetch=pf.result, answers=[], dependents=answers)
    return live, root


def test_a_bundle_replays_every_dependents_answer(tmp_path):
    def estate(name, kind=None):
        if name == "CUSTFILE":
            return {"rows": _ROWS, "truncated": True, "total": 9182}
        if name == "EMPTYFILE":
            return []                                  # asked, and nothing depends on it
        return None                                    # outside what the index covers

    asks = [("CUSTFILE", "file"), ("EMPTYFILE", "file"), ("UNKNOWNFILE", "file")]
    live, root = _gather(tmp_path, asks, estate)

    bundle = open_bundle(root)
    assert bundle.has_dependents()
    replayed = DependentsLookup(resolver=bundle.dependents())
    for name, kind in asks:
        want, got = live[(name, kind)], replayed(name, kind)
        if want is None:
            assert got is None                         # not-covered stays not-covered
        else:
            assert got is not None
            assert (got.rows, got.truncated, got.total) == (want.rows, want.truncated,
                                                            want.total)
    assert replayed.disabled_reason is None


def test_an_ask_the_gather_run_never_made_raises(tmp_path):
    live, root = _gather(tmp_path, [("CUSTFILE", "file")], _resolver(_ROWS))
    replay = open_bundle(root).dependents()
    with pytest.raises(BundleError) as exc:
        replay("OTHERFILE", "file")
    assert "no dependents record" in str(exc.value)
    # Through the lookup, the same miss is a recorded failure rather than an empty answer.
    lookup = DependentsLookup(resolver=replay)
    assert lookup("OTHERFILE", "file") is None
    assert "BundleError" in lookup.disabled_reason


def test_a_recorded_failure_replays_as_a_failure(tmp_path):
    def boom(name, kind=None):
        raise RuntimeError("index unreachable")

    recorded, answers = recording_dependents_resolver(boom)
    lookup = DependentsLookup(resolver=recorded)
    assert lookup("CUSTFILE", "file") is None
    pf = Prefetcher(lambda name, **kw: {"found": False}, dest=str(tmp_path / "deps"))
    pf.name_source("subject.cbl")
    root = tmp_path / "bundle"
    write_bundle(root, subject_name="subject.cbl", subject_text="S\n", kind="cobol",
                 prefetch=pf.result, answers=[], dependents=answers)
    with pytest.raises(BundleError):
        open_bundle(root).dependents()("CUSTFILE", "file")


def test_a_gather_that_opened_no_door_writes_what_it_always_did(tmp_path):
    pf = Prefetcher(lambda name, **kw: {"found": False}, dest=str(tmp_path / "deps"))
    pf.name_source("subject.cbl")
    root = tmp_path / "bundle"
    write_bundle(root, subject_name="subject.cbl", subject_text="S\n", kind="cobol",
                 prefetch=pf.result, answers=[])
    manifest = json.loads((root / "estate-bundle.json").read_text(encoding="utf-8"))
    # No dependents key at all, and the version an older build can still read in full.
    assert "dependents" not in manifest
    assert manifest["version"] == 1
    assert open_bundle(root).has_dependents() is False


def test_a_one_argument_lookup_replays_through_the_same_narrowing(tmp_path):
    """The reason ``unsupported-kwargs`` is recorded rather than smoothed over."""
    def narrow(name):                                  # no kind= at all
        return _ROWS

    live, root = _gather(tmp_path, [("CUSTFILE", "file")], narrow)
    manifest = json.loads((root / "estate-bundle.json").read_text(encoding="utf-8"))
    outcomes = [ans["outcome"] for ans in manifest["dependents"]]
    assert outcomes == ["unsupported-kwargs", "answered"]
    replayed = DependentsLookup(resolver=open_bundle(root).dependents())
    got = replayed("CUSTFILE", "file")
    assert got is not None and got.rows == live[("CUSTFILE", "file")].rows
    assert replayed.disabled_reason is None
