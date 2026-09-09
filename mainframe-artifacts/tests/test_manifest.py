"""The artifact manifest's shared core, and the advisory validator over it.

Upstream ledger batch 10, item 31: five packages produce a manifest and `fetch` consumes
rows it did not produce, so "the same shape" was three prose claims and a table in the
COBOL package, with nothing for a fourth or fifth producer to conform to.
"""

import logging

from mainframe_artifacts.manifest import (CORE_KEYS, DEPENDENCY_VALUES, IDENTITY_VALUES,
                                          IO_VALUES, MANIFEST_SCHEMA_VERSION,
                                          OCCURRENCE_KEYS, report_manifest,
                                          validate_manifest)


def _row(**over) -> dict:
    row = {"artifact": "T_ACCOUNT", "kind": "db2-table", "dependency": "runtime",
           "identity": "global"}
    row.update(over)
    return row


def _manifest(*rows: dict) -> dict:
    return {"format": "test-artifacts", "artifacts": list(rows)}


def test_validate_accepts_a_minimal_row():
    assert validate_manifest(_manifest(_row())) == []


def test_validate_names_a_missing_core_key():
    row = _row()
    del row["identity"]
    complaints = validate_manifest(_manifest(row))
    assert len(complaints) == 1
    assert "no 'identity'" in complaints[0]
    assert "T_ACCOUNT" in complaints[0], "the complaint must name the row"


def test_validate_rejects_an_identity_outside_the_vocabulary():
    complaints = validate_manifest(_manifest(_row(identity="whatever")))
    assert len(complaints) == 1 and "identity='whatever'" in complaints[0]


def test_both_spellings_of_both_ways_access_are_accepted():
    """A `db2-table` row says `read+write` because the JCL side does; every other kind
    says `read-write` because the Easytrieve `file` row does. The divergence is real and
    is written down rather than normalised away behind a producer's back."""
    assert validate_manifest(_manifest(_row(io="read+write"))) == []
    assert validate_manifest(_manifest(_row(kind="file", io="read-write"))) == []


def test_io_is_not_core():
    """Copybook rows have none, and neither do JCL db2-tablespace rows - a tablespace has
    no direction because a utility works on the space, not on a named artifact."""
    assert "io" not in CORE_KEYS
    assert validate_manifest(_manifest(_row(kind="db2-tablespace"))) == []


def test_the_occurrence_list_is_not_core():
    """COBOL spells it `lines` and carries line numbers; three others spell it
    `touchedBy` and carry dicts; JCL `proc` rows carry none at all. Not one concept in
    four spellings, so the core names none of them."""
    for key in OCCURRENCE_KEYS:
        assert key not in CORE_KEYS
    assert validate_manifest(_manifest(_row())) == []


def test_a_per_kind_extra_is_not_a_complaint():
    """Unknown is not the same as wrong. A key this module has not heard of is a per-kind
    extra until proven otherwise - the same rule `describe_fetcher` follows."""
    assert validate_manifest(_manifest(_row(ddname="CUSTFILE", gdg="+1",
                                            evidence="literal", installed=True))) == []


def test_an_empty_optional_is_a_complaint_because_absent_is_the_rule():
    assert "never empty" in validate_manifest(_manifest(_row(needs="")))[0]
    assert "never empty" in validate_manifest(_manifest(_row(touchedBy=[])))[0]


def test_validate_is_advisory_and_never_raises():
    """Every shape a caller could hand it, including ones that are not manifests."""
    for junk in (None, [], "", 7, {"artifacts": "not a list"}, {"artifacts": [None]},
                 {"artifacts": [{"artifact": None}]}, {}):
        assert isinstance(validate_manifest(junk), list)


def test_report_manifest_writes_to_the_log_and_returns_the_complaints(caplog):
    """The live call path. A validator with no call site is `describe_fetcher` again -
    documented, correct, and never run."""
    with caplog.at_level(logging.WARNING):
        complaints = report_manifest(logging.getLogger("t"), "x.cbl",
                                     _manifest(_row(identity="nope")))
    assert len(complaints) == 1
    assert "manifest shape" in caplog.text and "nope" in caplog.text


def test_a_conforming_manifest_says_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        assert report_manifest(logging.getLogger("t"), "x.cbl", _manifest(_row())) == []
    assert caplog.text == ""


def test_the_version_is_an_int_not_a_string():
    """A consumer compares it numerically; '10' < '9' as a string."""
    assert isinstance(MANIFEST_SCHEMA_VERSION, int)


def test_the_vocabularies_are_tuples_of_plain_strings():
    """Serialized verbatim into the manifest and both retrieval reports, so the string IS
    the output contract - the argument categories.py already makes."""
    for vocab in (CORE_KEYS, DEPENDENCY_VALUES, IDENTITY_VALUES, IO_VALUES):
        assert isinstance(vocab, tuple)
        assert all(isinstance(v, str) for v in vocab)
