"""The dialect decision, tested on the text alone.

cics-dependencies tests the same decision through its example members; these hold it
inline, because a second reader choosing a dialect has nothing else to go on - a member
arrives from an estate service under whatever name its library gave it.
"""

from cics_parser.detect import (KIND_BAS, KIND_BMS, KIND_BUNDLE, KIND_CSD, KIND_MACRO,
                                KIND_SIT, looks_like_csd_report, source_kind)

#: DFHCSDUP's LIST report: ASA column 1, the print stamp in the right margin, the
#: report-only DEFINETIME, and the utility's own message trailer.
REPORT = (
    " DFHCSDUP - CICS DEFINITION FILE UTILITY PROGRAM                     15.206 23:09\n"
    " LIST GROUP(RPTG) OBJECTS\n"
    " TRANSACTION(RPTA)       GROUP(RPTG)" + " " * 44 + "15.206 23:09\n"
    "                         PROGRAM(RPTMAIN)       TWASIZE(0)\n"
    "                         DEFINETIME(15/07/25 23:09:21)\n"
    " DFH5109 I END OF DFHCSDUP UTILITY JOB.  HIGHEST RETURN CODE WAS:   0\n"
)

DECK = " DEFINE FILE(CUSTMAS) GROUP(APPG) DSNAME(PROD.CUSTOMER.MASTER)\n"
BMS = ("LGSET    DFHMSD TYPE=MAP,MODE=INOUT,LANG=COBOL\n"
       "LGMAP    DFHMDI SIZE=(24,80)\n"
       "         DFHMDF POS=(1,1),LENGTH=8\n")
PCT = "         DFHPCT TYPE=ENTRY,TRANSID=LGMN,PROGRAM=LGMENU\n"


def test_a_listing_is_recognised_as_one():
    assert looks_like_csd_report(REPORT)
    assert source_kind(REPORT, "region.csd") == KIND_CSD


def test_a_deck_is_not_mistaken_for_a_listing():
    """It is the ABSENCE of a command verb that decides: a deck's own
    ``DEFINE FILE(x) GROUP(y)`` has the listing's shape once the verb is taken off."""
    assert not looks_like_csd_report(DECK)
    assert source_kind(DECK) == KIND_CSD


def test_a_capture_that_kept_no_trailer_is_still_a_listing():
    """A capture can lose either end, so the report-only times are a second signature."""
    body = "\n".join(line for line in REPORT.splitlines() if "DFH5" not in line) + "\n"
    assert looks_like_csd_report(body)


def test_bms_and_macro_decks_are_told_apart_by_their_macros_not_their_suffix():
    """Both are assembler and both start with a label in column 1."""
    assert source_kind(BMS, "lgset.pct") == KIND_BMS
    assert source_kind(PCT, "lgset.bms") == KIND_MACRO


def test_a_listing_that_names_a_macro_is_still_a_listing():
    """A region dump's free text can name a BMS macro; one such line must not send the
    whole member to the BMS reader, which recovers nothing from it."""
    named = REPORT.replace(
        "                         DEFINETIME",
        "                         DESCRIPTION(GENERATED DFHMDF FIELDS)\n"
        "                         DEFINETIME")
    assert "DFHMDF" in named
    assert source_kind(named) == KIND_CSD


def test_a_bundle_a_bas_deck_and_a_sit_are_recognised():
    assert source_kind('<?xml version="1.0"?><manifest/>') == KIND_BUNDLE
    assert source_kind("CREATE TRANDEF NAME(LGMN)\n") == KIND_BAS
    assert source_kind("         DFHSIT TYPE=CSECT,GRPLIST=APPLIST\n") == KIND_SIT


def test_what_looks_like_nothing_goes_to_the_reader_that_says_so():
    assert source_kind("just some text\n") == KIND_CSD
