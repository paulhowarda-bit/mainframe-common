"""The estate bundle: gather against a real service, replay with none.

The claim this file has to defend is narrow and total: an offline replay produces the
SAME ANSWERS as the gather run - every status, reason, ordering, count, detected type and
alternative - because it is the same code running against a different fetcher, not a
second implementation of retrieval.
"""

import json

import pytest

from cobol_xstate_core.artifact_service import ServiceUnavailable, call_service
from cobol_xstate_core.bundle import (BundleError, open_bundle, recording_fetcher,
                                      write_bundle)
from cobol_xstate_core.prefetch import Prefetcher, PrefetchResult

_MEMBERS = {
    "ALPHA": "       01 ALPHA PIC X(4).\n",
    "BETA": "       01 BETA  PIC 9(3).\n",
}


def _estate(name, type=None, copy=None):          # noqa: A002 - the wire keyword
    """A small estate covering the outcomes that differ from one another."""
    key = str(name).upper().strip("'\"")
    if key == "BOOM":
        raise RuntimeError("share unreachable")
    if key == "GAMMA":                             # cobol misses, asm has it
        if type == "cobol":
            return {"found": False}
        return {"artifact_name": key, "found": True, "text": "GAMMA CSECT\n",
                "detected_type": "asm", "source_location": "PROD.ASM(GAMMA)"}
    if key in _MEMBERS:
        return {"artifact_name": key, "found": True, "text": _MEMBERS[key],
                "source_location": f"PROD.COPYLIB({key})",
                "alternatives": [f"TEST.COPYLIB({key})"]}
    return {"found": False}


def _gather(tmp_path, names, type_hint=None):
    """Run a closure against the recording wrapper and write a bundle."""
    fetcher, answers = recording_fetcher(_estate)
    pf = Prefetcher(fetcher, dest=str(tmp_path / "deps"))
    pf.name_source("subject.cbl")
    pf.obtain_wave([(n, "asked for") for n in names], type_hint)
    root = tmp_path / "bundle"
    write_bundle(root, subject_name="subject.cbl", subject_text="SOURCE TEXT\n",
                 kind="cobol", prefetch=pf.result, answers=answers)
    return pf.result, root


def _replay(root, tmp_path, names, type_hint=None):
    bundle = open_bundle(root)
    pf = Prefetcher(bundle.fetcher(), dest=str(tmp_path / "replay-deps"))
    pf.name_source("subject.cbl")
    pf.obtain_wave([(n, "asked for") for n in names], type_hint)
    return pf.result


def _comparable(result):
    """Every reported fact except the ones that name a local filesystem path.

    `copiedTo` and a local member's `source` describe where the file is on THIS machine;
    a bundle reproduces the estate's answers, not the layout of the box that made them.
    Everything else - status, reason, order, counts, detected type, alternatives - must
    match exactly.
    """
    return [{k: v for k, v in row.items() if k not in ("copiedTo", "source")}
            for row in result.rows]


def test_replay_reproduces_every_reported_fact(tmp_path):
    names = ["ALPHA", "BETA", "NOPE"]
    live, root = _gather(tmp_path, names)
    offline = _replay(root, tmp_path, names)
    assert _comparable(offline) == _comparable(live)
    assert offline.counts == live.counts
    assert offline.missing == live.missing


def test_replay_needs_no_estate_at_all(tmp_path):
    """The point of the exercise: the replay box has no client, and does not need one."""
    _, root = _gather(tmp_path, ["ALPHA"])
    bundle = open_bundle(root)
    text = bundle.fetcher()("ALPHA")["text"]
    assert text == _MEMBERS["ALPHA"]
    assert bundle.source() == "SOURCE TEXT\n"


def test_a_probe_chain_replays_its_misses_not_just_its_hit(tmp_path):
    """fetch derives `languageBasis` from WHICH probe missed first, so a bundle that
    stored only the winning answer would replay a different sentence."""
    fetcher, answers = recording_fetcher(_estate)
    assert call_service(fetcher, "GAMMA", "cobol") is None          # miss, recorded
    assert call_service(fetcher, "GAMMA", "asm").detected_type == "asm"
    root = tmp_path / "b"
    write_bundle(root, subject_name="s.cbl", subject_text="x", kind="cobol",
                 prefetch=PrefetchResult(), answers=answers)
    replay = open_bundle(root).fetcher()
    assert call_service(replay, "GAMMA", "cobol") is None           # the miss survives
    assert call_service(replay, "GAMMA", "asm").detected_type == "asm"


def test_a_failed_request_replays_as_a_failure_not_as_an_absence(tmp_path):
    """The distinction the whole reporting design exists to protect."""
    fetcher, answers = recording_fetcher(_estate)
    with pytest.raises(ServiceUnavailable):
        call_service(fetcher, "BOOM")
    root = tmp_path / "b"
    write_bundle(root, subject_name="s.cbl", subject_text="x", kind="cobol",
                 prefetch=PrefetchResult(), answers=answers)
    with pytest.raises(ServiceUnavailable) as exc:
        call_service(open_bundle(root).fetcher(), "BOOM")
    assert "share unreachable" in str(exc.value)


def test_asking_for_something_the_gather_run_never_asked_for_raises(tmp_path):
    """Returning None would fabricate 'the estate was asked and had nothing'."""
    _, root = _gather(tmp_path, ["ALPHA"])
    with pytest.raises(ServiceUnavailable) as exc:
        open_bundle(root).fetcher()("NEVER-ASKED")
    assert "no record" in str(exc.value)
    assert "not the same analysis" in str(exc.value)


def test_copied_to_is_not_replayed_so_the_local_copy_is_this_run_s(tmp_path):
    """Replaying the gather box's copy path would name a directory that is not here."""
    _, root = _gather(tmp_path, ["ALPHA"])
    assert "copied_to" not in open_bundle(root).fetcher()("ALPHA")


def test_alternatives_and_detected_type_survive_the_round_trip(tmp_path):
    _, root = _gather(tmp_path, ["ALPHA"])
    got = open_bundle(root).fetcher()("ALPHA")
    assert got["alternatives"] == ["TEST.COPYLIB(ALPHA)"]
    assert got["source_location"] == "PROD.COPYLIB(ALPHA)"


def test_the_bundle_is_reproducible(tmp_path):
    """No timestamp anywhere: gathering the same thing twice writes the same bytes."""
    _, root_a = _gather(tmp_path / "a", ["ALPHA", "BETA", "NOPE"])
    _, root_b = _gather(tmp_path / "b", ["ALPHA", "BETA", "NOPE"])
    assert ((root_a / "estate-bundle.json").read_text()
            == (root_b / "estate-bundle.json").read_text())


def test_answers_are_written_in_ask_order_whatever_order_they_finished(tmp_path):
    fetcher, answers = recording_fetcher(_estate)
    for n in ("BETA", "ALPHA", "NOPE"):
        call_service(fetcher, n)
    answers.reverse()                     # as if the threads had completed backwards
    root = tmp_path / "b"
    write_bundle(root, subject_name="s.cbl", subject_text="x", kind="cobol",
                 prefetch=PrefetchResult(), answers=answers)
    written = json.loads((root / "estate-bundle.json").read_text())["answers"]
    assert [a["name"] for a in written] == ["BETA", "ALPHA", "NOPE"]


def test_a_client_that_rejects_our_keywords_replays_the_same_retry(tmp_path):
    """call_service drops `copy`, then `type`, on TypeError. If replay answered the
    first shape happily the retry would never happen, and the bundle would then be asked
    for a (name, type) pair the gather run never got an answer for."""
    def narrow(name):                      # accepts neither keyword
        return _MEMBERS.get(str(name).upper())

    fetcher, answers = recording_fetcher(narrow)
    assert call_service(fetcher, "ALPHA", "copybook", str(tmp_path)).text == _MEMBERS["ALPHA"]
    root = tmp_path / "b"
    write_bundle(root, subject_name="s.cbl", subject_text="x", kind="cobol",
                 prefetch=PrefetchResult(), answers=answers)
    assert call_service(open_bundle(root).fetcher(), "ALPHA", "copybook",
                        str(tmp_path)).text == _MEMBERS["ALPHA"]


def test_a_bundle_carries_the_gather_run_s_service_outage(tmp_path):
    result = PrefetchResult()
    result.unavailable = "no estate client was reachable"
    root = tmp_path / "b"
    write_bundle(root, subject_name="s.cbl", subject_text="x", kind="cobol",
                 prefetch=result, answers=[])
    assert open_bundle(root).unavailable == "no estate client was reachable"


@pytest.mark.parametrize("content,message", [
    ('{"format": "something-else", "version": 1}', "is not a"),
    ('{"format": "cobol-xstate-estate-bundle", "version": 99}', "understands up to"),
    ("not json at all", "not readable JSON"),
])
def test_an_unreadable_bundle_says_why(tmp_path, content, message):
    root = tmp_path / "b"
    root.mkdir()
    (root / "estate-bundle.json").write_text(content, encoding="utf-8")
    with pytest.raises(BundleError) as exc:
        open_bundle(root)
    assert message in str(exc.value)


def test_a_missing_bundle_says_so(tmp_path):
    with pytest.raises(BundleError) as exc:
        open_bundle(tmp_path / "nowhere")
    assert "no estate bundle" in str(exc.value)
