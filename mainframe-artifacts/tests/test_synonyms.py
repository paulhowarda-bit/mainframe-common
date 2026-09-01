"""The two doors Db2 SYNONYM/ALIAS knowledge arrives by, and the one lookup over both."""

import argparse
import json
import logging

from mainframe_artifacts.cliargs import add_synonym_args, synonym_lookup
from mainframe_artifacts.synonyms import (FROM_MAP, FROM_RESOLVER, SynonymLookup,
                                          read_synonym_map)


class Recorder:
    """A resolver that remembers what it was asked."""

    def __init__(self, answers=None, raises=None):
        self.answers = answers or {}
        self.raises = raises
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        if self.raises is not None:
            raise self.raises
        return self.answers.get(name)


# --------------------------------------------------------------------------- #
# the lookup
# --------------------------------------------------------------------------- #

def test_the_map_answers_first_and_a_map_hit_never_reaches_the_resolver():
    r = Recorder({"RTAC_ACCOUNT": "T_OTHER"})
    look = SynonymLookup({"rtac_account": "t_rtac_account"}, r)
    assert look("RTAC_ACCOUNT") == ("T_RTAC_ACCOUNT", FROM_MAP)
    assert look("rtac_account") == ("T_RTAC_ACCOUNT", FROM_MAP)
    assert r.calls == []


def test_the_resolver_answers_what_the_map_does_not_hold():
    r = Recorder({"V_SMIX_ACTIVE": "MMD1DBO.T_SMIX_ACTIVE"})
    look = SynonymLookup({"RTAC_ACCOUNT": "T_RTAC_ACCOUNT"}, r)
    assert look("V_SMIX_ACTIVE") == ("MMD1DBO.T_SMIX_ACTIVE", FROM_RESOLVER)
    assert r.calls == ["V_SMIX_ACTIVE"]


def test_none_from_the_resolver_is_not_a_synonym_and_is_remembered():
    r = Recorder()
    look = SynonymLookup(None, r)
    assert look("T_PLAIN") is None
    assert look("T_PLAIN") is None
    assert look("t_plain") is None
    assert r.calls == ["T_PLAIN"]           # memoised per name, per run
    assert look.disabled_reason is None


def test_a_raising_resolver_is_a_failed_lookup_that_disables_only_the_resolver():
    r = Recorder(raises=RuntimeError("catalog down"))
    look = SynonymLookup({"RTAC_ACCOUNT": "T_RTAC_ACCOUNT"}, r)
    assert look("V_X") is None
    assert look.disabled_reason == "resolver raised RuntimeError: catalog down"
    assert look("V_Y") is None              # never asked again this run
    assert r.calls == ["V_X"]
    assert look("RTAC_ACCOUNT") == ("T_RTAC_ACCOUNT", FROM_MAP)   # the map still answers


def test_a_resolver_returning_something_other_than_a_name_is_a_failed_lookup():
    look = SynonymLookup(None, lambda n: {"real_table": "T_X"})
    assert look("V_X") is None
    assert look.disabled_reason == ("resolver returned dict for V_X, expected a table "
                                    "name or None")
    look = SynonymLookup(None, lambda n: "   ")
    assert look("V_X") is None
    assert "empty" in look.disabled_reason


def test_a_returned_base_is_uppercased_and_keeps_its_qualifier():
    look = SynonymLookup(None, lambda n: " owner.t_base ")
    assert look("OWNER.V_X") == ("OWNER.T_BASE", FROM_RESOLVER)


def test_an_empty_name_and_no_doors_are_both_no_answer():
    assert SynonymLookup()("RTAC_ACCOUNT") is None
    assert SynonymLookup({"A": "B"}, Recorder())("") is None


# --------------------------------------------------------------------------- #
# the map file
# --------------------------------------------------------------------------- #

def test_a_well_formed_map_reads(tmp_path):
    p = tmp_path / "syn.json"
    p.write_text(json.dumps({"RTAC_ACCOUNT": "T_RTAC_ACCOUNT"}), encoding="utf-8")
    assert read_synonym_map(p) == ({"RTAC_ACCOUNT": "T_RTAC_ACCOUNT"}, None)


def test_the_three_map_rejections_say_why(tmp_path):
    mapping, why = read_synonym_map(tmp_path / "absent.json")
    assert mapping is None and why.startswith("no such file: ")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    mapping, why = read_synonym_map(bad)
    assert mapping is None and "is not valid JSON" in why
    shape = tmp_path / "shape.json"
    shape.write_text(json.dumps({"A": 1}), encoding="utf-8")
    mapping, why = read_synonym_map(shape)
    assert mapping is None and "must be a JSON object" in why
    shape.write_text(json.dumps(["A", "B"]), encoding="utf-8")
    assert read_synonym_map(shape)[0] is None


# --------------------------------------------------------------------------- #
# the flags
# --------------------------------------------------------------------------- #

def _args(*argv):
    p = argparse.ArgumentParser()
    add_synonym_args(p)
    return p.parse_args(list(argv))


def test_neither_flag_opens_no_door():
    assert synonym_lookup(_args()) == (None, None)


def test_the_map_flag_reads_the_file_and_a_bad_file_is_an_error(tmp_path):
    p = tmp_path / "syn.json"
    p.write_text(json.dumps({"RTAC_ACCOUNT": "T_RTAC_ACCOUNT"}), encoding="utf-8")
    look, why = synonym_lookup(_args("--synonym-map", str(p)))
    assert why is None and look("RTAC_ACCOUNT") == ("T_RTAC_ACCOUNT", FROM_MAP)
    look, why = synonym_lookup(_args("--synonym-map", str(tmp_path / "nope.json")))
    assert look is None and why.startswith("--synonym-map: no such file")


def test_the_resolver_flag_loads_module_func_and_a_bad_spec_is_an_error():
    look, why = synonym_lookup(_args("--synonym-resolver", "json:loads"))
    assert why is None and look is not None and look.resolver is json.loads
    look, why = synonym_lookup(_args("--synonym-resolver", "no_such_mod_xyz:fn"))
    assert look is None
    assert why.startswith("--synonym-resolver no_such_mod_xyz:fn: could not import")
    look, why = synonym_lookup(_args("--synonym-resolver", "notaspec"))
    assert look is None and "is not MODULE:FUNC" in why


def test_a_resolver_with_a_doubtful_signature_is_warned_about_not_refused(caplog):
    with caplog.at_level(logging.WARNING, logger="mainframe_artifacts.cliargs"):
        look, why = synonym_lookup(_args("--synonym-resolver", "os:getcwd"))
    assert why is None and look is not None
    assert any("resolver(name)" in r.getMessage() for r in caplog.records)
