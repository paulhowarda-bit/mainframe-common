"""Which kind of definition source this is - the dialect decision, made on content.

A second reader of the same members needs this before it can lex anything: a DFHCSDUP
deck, DFHCSDUP's printed listing, a macro table deck and a BMS mapset are four different
physical formats, and none of them reliably declares which it is. Deciding it by suffix
sends a whole region dump to the wrong reader, which recovers nothing and says so in a
flag per line.

Where inside a JCL job an instream deck starts is NOT decided here - that stays with the
extractor that strips it (``cics_dependencies.detect``).
"""

from __future__ import annotations

import re

KIND_CSD = "csd"
KIND_MACRO = "macro-table"
KIND_BMS = "bms"
KIND_BAS = "bas"
KIND_BUNDLE = "bundle"
KIND_SIT = "sit"

# The prefix alternation is not decoration: KICKS spells the same tables KIKPCT / KIKPPT /
# KIKFCT / KIKSIT, and a shop that wraps IBM's macros in its own does the same thing again.
# Keyed on DFH alone, a whole estate's tables detect as "not a macro deck".
_MACRO_NAMES = re.compile(
    r"^\s*\S*\s+((DFH|KIK)(PCT|PPT|FCT|DCT|TCT|TST|PLT|XLT)|DSNCRCT)\b", re.I | re.M)
_BMS_NAMES = re.compile(r"^\s*\S*\s+(DFHMSD|DFHMDI|DFHMDF)\b", re.I | re.M)
_SIT_NAMES = re.compile(r"^\s*\S*\s+(DFH|KIK)SIT\b|^\s*GRPLIST\s*=", re.I | re.M)
_BAS_CREATE = re.compile(r"^\s*CREATE\s+[A-Z0-9]+DEF\b", re.I | re.M)
_CSD_DEFINE = re.compile(r"^\s*(DEFINE|ADD|USERDEFINE)\s", re.I | re.M)

# DFHCSDUP's printed listing, which carries no command verb at all. Two independent
# signatures, because a capture can lose either end: the utility's own messages (a listing
# saved from the job's SYSPRINT keeps them; one saved by cut-and-paste may not), and the
# report-only timestamps it prints on essentially every object. `_CSD_REPORT_OBJECT`
# alone is deliberately NOT enough - a deck's `DEFINE FILE(x) GROUP(y)` matches the same
# shape once the verb is stripped, so it is the ABSENCE of a verb that decides.
_CSD_REPORT_MESSAGE = re.compile(r"^\s*DFH5\d{3}\s*[A-Z]?\s", re.M)
_CSD_REPORT_TIMES = re.compile(r"^\s*(DEFINETIME|CHANGETIME)\(", re.I | re.M)
_CSD_REPORT_OBJECT = re.compile(r"^\s?[A-Z][A-Z0-9$#@]*\([^)]*\)\s+GROUP\(", re.I | re.M)


def looks_like_csd_report(text: str) -> bool:
    """Is this DFHCSDUP's printed object listing rather than a definition deck?

    The distinction is not cosmetic: handed to the deck lexer, a listing produces ZERO
    resources and one "text before the first command" flag per line - a flag list longer
    than the file, wrapped around an empty region that looks exactly like a region whose
    deck defined nothing.
    """
    if _CSD_DEFINE.search(text):
        return False
    return bool(_CSD_REPORT_MESSAGE.search(text)
                or (_CSD_REPORT_OBJECT.search(text) and _CSD_REPORT_TIMES.search(text)))


def source_kind(text: str, source_name: str = "") -> str:
    """Which parser this source belongs to.

    By CONTENT first and name second, because members arrive from an estate service under
    whatever name the library gave them. The order matters: a BMS deck and a macro table
    deck are both assembler and both start with a label in column 1, so they are told apart
    by which macros they invoke, never by their suffix.

    A source that looks like none of these comes back ``KIND_CSD``, which is the one whose
    parser reports what it did not understand rather than silently producing nothing.
    """
    if text.lstrip().startswith("<?xml") or "<manifest" in text[:2000]:
        return KIND_BUNDLE
    # Before the assembler checks, not after: a listing of MAPSETs and PROGRAMs can
    # legitimately name a resource `DFHMDF` or `DFHPCT`, and one line of that would send a
    # whole region dump to the BMS or macro-table parser, which recovers nothing from it.
    if looks_like_csd_report(text):
        return KIND_CSD
    if _BMS_NAMES.search(text):
        return KIND_BMS
    if _MACRO_NAMES.search(text):
        return KIND_MACRO
    if _BAS_CREATE.search(text):
        return KIND_BAS
    if _CSD_DEFINE.search(text):
        return KIND_CSD
    # The SIT is checked BEFORE the JCL fallback: KICKS ships its SIT as an assemble job,
    # and a job with no DEFINE in it would otherwise be handed to the CSD parser, which
    # would find no commands and report a deck that contributed nothing.
    if _SIT_NAMES.search(text):
        return KIND_SIT
    return KIND_CSD
