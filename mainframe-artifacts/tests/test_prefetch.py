"""`Prefetcher.obtain_wave`: one level of a closure, and the type each member is asked as.

Upstream ledger batch 26, item 62. A JCL level mixes kinds - a cataloged PROC beside a
control-card member - and on a real estate one member name is often several kinds at
once, so a single hint for the whole level cannot be right for every member of it.
"""

from mainframe_artifacts.fetch import request_type
from mainframe_artifacts.prefetch import Prefetcher


def _asking():
    asked = []

    def fetcher(name, type=None, copy=None):           # noqa: A002 - the wire keyword
        asked.append((name, type))
        return {"found": True, "text": f"* {name}\n", "source_location": f"LIB({name})"}
    return fetcher, asked


def test_a_member_can_carry_its_own_type_hint(tmp_path):
    fetcher, asked = _asking()
    pf = Prefetcher(fetcher, dest=str(tmp_path))
    pf.obtain_wave([("PRC1", "why", "proc"), ("CARD1", "why", "cntl"),
                    ("ANY1", "why", None)])
    assert asked == [("PRC1", "proc"), ("CARD1", "cntl"), ("ANY1", None)]


def test_the_level_hint_still_covers_a_member_without_its_own(tmp_path):
    """COBOL's copybook closure passes pairs and one hint for the level; unchanged."""
    fetcher, asked = _asking()
    pf = Prefetcher(fetcher, dest=str(tmp_path))
    pf.obtain_wave([("CPY1", "why"), ("CPY2", "why", None)], "copybook")
    assert asked == [("CPY1", "copybook"), ("CPY2", "copybook")]


def test_a_name_asked_twice_as_one_type_is_requested_once(tmp_path):
    fetcher, asked = _asking()
    pf = Prefetcher(fetcher, dest=str(tmp_path))
    pf.obtain_wave([("SHARED", "why", "proc"), ("SHARED", "why", "proc")])
    assert asked == [("SHARED", "proc")]


def test_a_name_asked_as_two_types_is_two_requests(tmp_path):
    """Upstream ledger item 66: one name is often a PROC and a control card at once. The
    second ask is a different member, and was never requested while `seen` held names."""
    fetcher, asked = _asking()
    pf = Prefetcher(fetcher, dest=str(tmp_path))
    pf.obtain_wave([("SHARED", "why", "proc"), ("SHARED", "why", "cntl")])
    assert asked == [("SHARED", "proc"), ("SHARED", "cntl")]
    assert pf.settled("SHARED", "proc") and pf.settled("SHARED", "cntl")
    assert not pf.settled("SHARED", "copybook")


def test_a_control_member_is_asked_for_with_the_type_stage_2_uses():
    assert request_type("proc") == "proc"
    assert request_type("include-member") == request_type("control-card") == "cntl"
    assert request_type("autoedit-member") == "cntl"
    assert request_type("no-such-kind") is None


def _by_type(texts):
    """A fetcher holding a different member under each (name, type)."""
    asked = []

    def fetcher(name, type=None, copy=None):           # noqa: A002 - the wire keyword
        asked.append((name, type))
        text = texts.get((name, type))
        return None if text is None else {"found": True, "text": text,
                                          "source_location": f"{type}/{name}"}
    return fetcher, asked


def test_each_kind_of_a_name_answers_with_its_own_text(tmp_path):
    """The card used to be stored over the PROC under the one name, and the PROC's EXEC
    then expanded from the card's text (upstream ledger item 66)."""
    fetcher, _ = _by_type({("SHARED", "proc"): "PROC TEXT\n",
                           ("SHARED", "cntl"): "CARD TEXT\n"})
    pf = Prefetcher(fetcher, dest=str(tmp_path))
    pf.obtain_wave([("SHARED", "why", "proc")])
    pf.obtain_wave([("PROD.ACNTL(SHARED)", "why", "cntl")])
    resolve = pf.result.resolver()
    assert resolve("SHARED", kind="proc") == "PROC TEXT\n"
    assert resolve("PROD.ACNTL(SHARED)", kind="control-card") == "CARD TEXT\n"
    assert pf.store_text("SHARED", "proc") == "PROC TEXT\n"
    assert pf.store_text("SHARED", "cntl") == "CARD TEXT\n"
    assert sorted(pf.result.store) == [("SHARED", "cntl"), ("SHARED", "proc")]


def test_the_card_fetched_first_does_not_answer_for_the_proc(tmp_path):
    fetcher, _ = _by_type({("SHARED", "cntl"): "CARD TEXT\n"})
    pf = Prefetcher(fetcher, dest=str(tmp_path))
    pf.obtain_wave([("PROD.ACNTL(SHARED)", "why", "cntl")])
    resolve = pf.result.resolver()
    assert resolve("SHARED", kind="proc") is None        # not asked as a PROC yet
    assert not pf.settled("SHARED", "proc")
    assert resolve("PROD.ACNTL(SHARED)", kind="control-card") == "CARD TEXT\n"


def test_an_untyped_ask_reads_by_name_alone(tmp_path):
    """Asked with no type - a closure that sends none, or a resolver called without a
    kind - a member reads as it always did: by its name."""
    fetcher, asked = _by_type({("MAC1", None): "MACRO\n", ("PRC1", "proc"): "PROC\n"})
    pf = Prefetcher(fetcher, dest=str(tmp_path))
    pf.obtain_wave([("MAC1", "why"), ("PRC1", "why", "proc")])
    resolve = pf.result.resolver()
    assert resolve("MAC1") == resolve("MAC1", kind="proc") == "MACRO\n"
    assert resolve("PRC1") == "PROC\n"
    assert pf.settled("MAC1") and not pf.settled("PRC1")
    assert asked == [("MAC1", None), ("PRC1", "proc")]


def test_a_fetcher_that_cannot_type_still_fills_the_typed_ask(tmp_path):
    """The client drops `type=`; the ask was still a PROC, and reads back as one."""
    def fetcher(name, copy=None):
        return {"found": True, "text": "PROC\n", "source_location": f"LIB({name})"}
    pf = Prefetcher(fetcher, dest=str(tmp_path))
    pf.obtain_wave([("PRC1", "why", "proc")])
    assert pf.result.resolver()("PRC1", kind="proc") == "PROC\n"


def test_the_report_names_the_type_only_where_two_rows_share_a_member(tmp_path):
    fetcher, _ = _by_type({("SHARED", "proc"): "P\n", ("SHARED", "cntl"): "C\n",
                           ("ONLY", "proc"): "O\n"})
    pf = Prefetcher(fetcher, dest=str(tmp_path))
    pf.obtain_wave([("SHARED", "why", "proc"), ("ONLY", "why", "proc"),
                    ("PROD.ACNTL(SHARED)", "why", "cntl")])
    rows = pf.result.report()["members"]
    assert [(r["member"], r.get("type")) for r in rows] == [
        ("SHARED", "proc"), ("ONLY", None), ("SHARED", "cntl")]
    assert all(r["status"] == "fetched" for r in rows)
