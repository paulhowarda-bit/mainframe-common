"""`build_fetch_plan` routes a manifest row to a retrieval type, or says why it cannot.

These are unit tests over hand-built manifests, deliberately: the front-ends' own suites
already cover the kinds THEY emit, and what is checked here is the routing table itself,
including kinds no front-end in this family emits yet. A kind in neither `_KIND_TYPE` nor
`_NEVER_FETCHABLE` is the dangerous case - the row is skipped with a reason that reads as
an omission someone should fix, which is right when it IS one and wrong when the kind is
simply not a retrievable artifact.
"""

from mainframe_artifacts.fetch import build_fetch_plan


def _manifest(*rows: dict) -> dict:
    return {"format": "test-artifacts", "program": "TESTPGM", "artifacts": list(rows)}


def _row(artifact: str, kind: str, **extra) -> dict:
    row = {"artifact": artifact, "kind": kind, "dependency": "runtime",
           "identity": "global"}
    row.update(extra)
    return row


def _plan_for(row: dict) -> dict:
    plan = build_fetch_plan(_manifest(row))
    assert len(plan) == 1, plan
    return plan[0]


def test_a_db2_user_defined_type_is_planned_as_ddl():
    """A CREATE TYPE is retrieved the same way as any other DDL member.

    Upstream ledger batch 10, item 33a. A parameter declared with a user-defined type has
    no known length until that type's DDL is in hand, so a byte-offset through it is a
    guess - which makes the type a first-class dependency, not a detail of the table.
    """
    entry = _plan_for(_row("Y_CHAR32", "db2-type"))
    assert entry["status"] == "planned"
    assert entry["type"] == "ddl"


def test_a_db2_table_is_still_planned_as_ddl():
    """The neighbouring entry, pinned so the new row cannot be added by displacing it."""
    entry = _plan_for(_row("T_ACCOUNT", "db2-table"))
    assert entry["status"] == "planned"
    assert entry["type"] == "ddl"


def test_an_unknown_kind_is_skipped_and_says_why():
    """The honest fallthrough, and the reason a reader triages on."""
    entry = _plan_for(_row("WHATEVER", "not-a-kind-anyone-emits"))
    assert entry["status"] == "skipped"
    assert entry["reason"] == ("artifact kind 'not-a-kind-anyone-emits' has no known "
                               "retrieval type")
