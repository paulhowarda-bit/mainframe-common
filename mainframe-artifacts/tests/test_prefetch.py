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


def test_a_name_asked_twice_in_one_level_is_requested_once_with_the_first_hint(tmp_path):
    fetcher, asked = _asking()
    pf = Prefetcher(fetcher, dest=str(tmp_path))
    pf.obtain_wave([("SHARED", "why", "proc"), ("SHARED", "why", "cntl")])
    assert asked == [("SHARED", "proc")]


def test_a_control_member_is_asked_for_with_the_type_stage_2_uses():
    assert request_type("proc") == "proc"
    assert request_type("include-member") == request_type("control-card") == "cntl"
    assert request_type("autoedit-member") == "cntl"
    assert request_type("no-such-kind") is None


def test_the_store_resolver_accepts_a_kind_and_ignores_it(tmp_path):
    fetcher, _ = _asking()
    pf = Prefetcher(fetcher, dest=str(tmp_path))
    pf.obtain_wave([("PRC1", "why", "proc")])
    resolve = pf.result.resolver()
    assert resolve("PRC1", kind="proc") == resolve("PRC1") == "* PRC1\n"
