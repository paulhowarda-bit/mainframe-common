"""DATA DIVISION recovery - the typed data dictionary.

In a Harel/STATEMATE statechart the data items are first-class: actions assign to
them and conditions test them, and the items are *typed*. For COBOL that type is the
``PICTURE`` + ``USAGE`` + sign, which is exactly what determines numeric behavior
(packed vs. zoned vs. binary, digit count, decimal scale, signedness - the things
that cause S0C7s). This module recovers that dictionary so the emitted statechart can
carry the real types, not just names.

It parses level-numbered data description entries (handling multi-line entries and the
decimal point inside a ``VALUE`` literal by splitting on the *next level number*, not
on periods), derives a type descriptor from each PICTURE/USAGE, and resolves 88-level
condition-names to the (parent == value) test they stand for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .normalizer import CodeLine

_LEVEL_START = re.compile(r"^\s*(\d{1,2})[\s.]", )
_SECTIONS = ("FILE", "WORKING-STORAGE", "LOCAL-STORAGE", "LINKAGE", "REPORT", "SCREEN")


@dataclass
class PicType:
    category: str            # 'numeric' | 'numeric-edited' | 'alphanumeric' |
                             # 'alphabetic' | 'alphanumeric-edited' | 'group' | 'unknown'
    digits: int = 0          # number of digit positions (numeric)
    scale: int = 0           # digits after the implied decimal point (V)
    signed: bool = False
    usage: str = "DISPLAY"   # DISPLAY | COMP | COMP-3 | COMP-1 | COMP-2 | ...
    pic: Optional[str] = None

    def describe(self) -> str:
        if self.category == "group":
            return "group"
        if self.category.startswith("numeric"):
            s = f"{'signed ' if self.signed else ''}numeric({self.digits}"
            if self.scale:
                s += f",{self.scale}"
            return s + f") usage {self.usage}"
        return f"{self.category} usage {self.usage}"

    def to_dict(self) -> dict:
        d = {"category": self.category, "usage": self.usage}
        if self.pic:
            d["pic"] = self.pic
        if self.category.startswith("numeric"):
            d.update({"digits": self.digits, "scale": self.scale, "signed": self.signed})
        return d


@dataclass
class DataItem:
    level: int
    name: str
    line: int
    origin: Optional[str] = None   # copybook member name if from a COPY-expanded line
    section: Optional[str] = None
    pic: Optional[str] = None
    usage: Optional[str] = None
    value: Optional[str] = None
    # FILE SECTION only: the FD/SD file this record belongs to (record <-> file link,
    # so the external interface can attribute record fields to the physical file).
    file: Optional[str] = None
    occurs: Optional[int] = None
    # OCCURS min TO max DEPENDING ON var: `occurs` holds the MAXIMUM (the table is
    # seeded at full size); the dynamic length variable is kept here and flagged.
    occurs_depending: Optional[str] = None
    redefines: Optional[str] = None
    parent: Optional[str] = None
    is_group: bool = False
    # Two clauses that change BYTE LAYOUT and nothing else, which is why they are kept
    # even though no other stage reads them. SYNCHRONIZED inserts slack bytes so a COMP
    # item starts on a halfword/fullword boundary; SIGN IS SEPARATE gives a signed
    # DISPLAY number an extra byte for its sign. Without them, a computed field offset
    # can be silently wrong - and a wrong offset looks exactly like a right one, which
    # is the failure `storage.py` refuses to risk (see its `provable` contract).
    sync: bool = False
    sign_separate: bool = False
    condition_values: List[str] = field(default_factory=list)  # 88-level singleton VALUE(s)
    condition_ranges: List[List[str]] = field(default_factory=list)  # 88-level [lo, hi] THRU
    cond_parent: Optional[str] = None                          # 88-level's data item
    type: Optional[PicType] = None


_USAGES = {
    "COMP-3": "COMP-3", "COMPUTATIONAL-3": "COMP-3", "PACKED-DECIMAL": "COMP-3",
    "COMP-1": "COMP-1", "COMPUTATIONAL-1": "COMP-1",
    "COMP-2": "COMP-2", "COMPUTATIONAL-2": "COMP-2",
    "COMP-4": "COMP-4", "COMPUTATIONAL-4": "COMP-4",
    "COMP-5": "COMP-5", "COMPUTATIONAL-5": "COMP-5",
    "COMP": "COMP", "COMPUTATIONAL": "COMP", "BINARY": "COMP-4",
    "DISPLAY": "DISPLAY", "INDEX": "INDEX", "POINTER": "POINTER",
}


# A quoted VALUE literal is DATA, not syntax - its text must never be read as a clause.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
# Clauses whose operand is a data-NAME the programmer chose. The name is not syntax
# either, so `REDEFINES INDEX-TAB` must not make this item USAGE INDEX.
_NAME_OPERAND = re.compile(r"\b(?:REDEFINES|RENAMES|DEPENDING\s+ON)\s+[A-Z0-9-]+", re.I)
# One compiled alternation instead of a per-keyword loop that re-escaped and re-looked-up
# 18 patterns for every data entry in the program. LONGEST FIRST so COMP-3 still wins
# over COMP. Both regexes are built once, at import.
_USAGE_RE = re.compile(
    r"(?<![A-Z0-9-])("
    + "|".join(re.escape(k) for k in sorted(_USAGES, key=len, reverse=True))
    + r")(?![A-Z0-9-])")
# Every key contains one of these, so a miss here is a guaranteed miss above.
_USAGE_HINT = re.compile(r"COMP|BINARY|DISPLAY|INDEX|POINTER|PACKED")


def _find_usage(text: str) -> Optional[str]:
    """The USAGE clause of one data entry, or None.

    Two kinds of text in the entry are DATA, not syntax, and matching a USAGE keyword
    inside either one gives the item a wrong size that storage.py then reports as a
    provable byte offset - shifting every later field in the record:

    * a quoted VALUE literal - ``PIC X(30) VALUE 'INDEX OUT OF RANGE'`` sized as a
      4-byte INDEX instead of 30 bytes;
    * a data-name operand - ``REDEFINES INDEX-TAB``, ``DEPENDING ON COMP-CNT``.

    Both are blanked first. The keyword match then requires that no hyphen or
    alphanumeric touch either end: COBOL data-names may contain hyphens, so a plain
    ``\\bINDEX\\b`` still matches inside ``INDEX-TAB``.
    """
    up = text.upper()
    # Cheap gate first: this runs once per data entry, and the commonest entry by far
    # (a plain PIC X(n) with no USAGE clause) would otherwise pay a full scan to learn
    # it has nothing to find.
    if not _USAGE_HINT.search(up):
        return None
    up = _NAME_OPERAND.sub(" ", _QUOTED.sub(" ", up))
    m = _USAGE_RE.search(up)
    return _USAGES[m.group(1)] if m else None


def expand_pic(pic: str) -> str:
    """Expand repetition counts: 9(3)V99 -> 999V99, X(4) -> XXXX."""
    def rep(m):
        return m.group(1) * int(m.group(2))
    return re.sub(r"([9AXZSPV0BspnN.,/$+\-*])\((\d+)\)", rep, pic, flags=re.I)


def parse_pic(pic: Optional[str], usage: Optional[str]) -> PicType:
    u = usage or "DISPLAY"
    if not pic:
        return PicType(category="group", usage=u, pic=None)
    raw = pic
    exp = expand_pic(pic).upper()
    signed = exp.startswith("S") or "S" in exp[:1] or exp.endswith(("CR", "DB")) or "+" in exp or "-" in exp
    body = exp[1:] if exp.startswith("S") else exp
    # Editing characters (display-only numeric or alphanumeric edited).
    has_edit = any(c in exp for c in "ZB*$,/CRDB") or exp.count(".") > 0 and "V" not in exp
    if "X" in exp:
        cat = "alphanumeric-edited" if has_edit else "alphanumeric"
        return PicType(category=cat, usage=u, pic=raw, signed=False)
    if "A" in exp and "9" not in exp:
        return PicType(category="alphabetic", usage=u, pic=raw)
    if "9" in exp or "V" in exp or "P" in exp:
        digit_part = body
        edited = any(c in exp for c in "ZB*$,/") or ("." in exp and "V" not in exp)
        # Digit positions are `9`, plus the suppression symbols `Z`/`*` in an EDITED
        # picture (they hold a digit that may print as a space or star). `P` is a
        # SCALING position: it moves the implied decimal point but is neither a digit
        # position nor a byte - IBM: "P is not counted in the size of the data item".
        # Counting P inflated the packed-decimal byte count (S9(5)PPP sized as 8 digits
        # -> 5 bytes instead of 5 digits -> 3) and the runtime truncation modulus.
        digit_syms = "9Z*" if edited else "9"
        scale = 0
        if "V" in digit_part:
            after = digit_part.split("V", 1)[1]
            scale = sum(1 for c in after if c in digit_syms or c == "P")
        elif edited and "." in digit_part:
            after = digit_part.split(".", 1)[1]
            scale = sum(1 for c in after if c in digit_syms)
        else:
            # No point written: leading Ps put it left of the digits, trailing Ps right.
            lead = len(digit_part) - len(digit_part.lstrip("P"))
            scale = lead if lead else -(len(digit_part) - len(digit_part.rstrip("P")))
        digits = sum(1 for c in digit_part if c in digit_syms)
        cat = "numeric-edited" if edited else "numeric"
        return PicType(category=cat, digits=digits, scale=scale,
                       signed=bool(re.match(r"^S", exp)) or signed and cat == "numeric",
                       usage=u, pic=raw)
    return PicType(category="unknown", usage=u, pic=raw)


def _data_region(lines: List[CodeLine]) -> List[CodeLine]:
    start = end = None
    for i, cl in enumerate(lines):
        if start is None and re.search(r"\bDATA\s+DIVISION\b", cl.text, re.I):
            start = i
        elif start is not None and re.search(r"\bPROCEDURE\s+DIVISION\b", cl.text, re.I):
            end = i
            break
    if start is None:
        return []
    return lines[start + 1:end if end is not None else len(lines)]


def _entries(region: List[CodeLine]):
    """Yield (text, line, section, origin, fd) for each level-numbered data entry,
    where ``fd`` is the enclosing FD/SD file name inside the FILE SECTION (None
    elsewhere) - the record <-> file association the external interface needs."""
    section = None
    fd = None
    buf: List[str] = []
    first_line = 0
    first_origin = None
    first_fd = None
    for cl in region:
        t = cl.text.strip()
        up = t.upper()
        # One or more spaces: COBOL lets any number of blanks separate the words, and
        # aligned source routinely uses two. Matching only `NAME SECTION` left every
        # item in a `LINKAGE  SECTION.` untagged, which silently drops the whole
        # COMMAREA/caller perimeter from the recovered interface.
        sec = next((s for s in _SECTIONS
                    if re.match(rf"{re.escape(s)}\s+SECTION\b", up)), None)
        if sec:
            if buf:
                yield " ".join(buf), first_line, section, first_origin, first_fd
                buf = []
            section = sec
            fd = None
            continue
        fm = re.match(r"^(?:FD|SD)\s+([A-Z0-9][A-Z0-9-]*)", up)
        if fm:
            fd = fm.group(1)  # records that follow belong to this file
            continue
        if up.startswith(("FD ", "SD ", "RD ", "FD.", "01 FD")) or up in ("FD", "SD"):
            # File/sort descriptions - skip the FD line itself; its 01 follows.
            continue
        # A data entry runs until its terminating period, and a level number only starts a
        # NEW entry at that boundary. A clause continued onto a line that happens to begin
        # with digits - the standard `OCCURS` \ `n TIMES` wrap, or a level number pushed
        # to its own line - was read as a fresh item: `05 WS-TAB OCCURS` split from
        # `10 TIMES PIC X(5)` invented a phantom item named TIMES and dropped WS-TAB's
        # OCCURS. Only break when the entry so far is terminated (its last line ends in a
        # period) or nothing is buffered yet.
        terminated = (not buf) or buf[-1].rstrip().endswith(".")
        if terminated and _LEVEL_START.match(cl.text) and re.match(r"^\s*\d", cl.text):
            if buf:
                yield " ".join(buf), first_line, section, first_origin, first_fd
            buf = [t]
            first_line = cl.line
            first_origin = cl.origin
            first_fd = fd
        else:
            if buf:
                buf.append(t)
    if buf:
        yield " ".join(buf), first_line, section, first_origin, first_fd


def parse_data_division(lines: List[CodeLine]):
    """Return (items, by_name) recovered from the DATA DIVISION."""
    items: List[DataItem] = []
    region = _data_region(lines)
    parent_stack: List[DataItem] = []  # (group items by level)
    last_elementary: Dict[int, DataItem] = {}

    for text, line, section, origin, fd in _entries(region):
        m = re.match(r"^(\d{1,2})\s+([A-Z0-9][A-Z0-9-]*|FILLER)\b(.*)$", text, re.I)
        if not m:
            continue
        level = int(m.group(1))
        name = m.group(2).upper()
        rest = m.group(3)
        item = DataItem(level=level, name=name, line=line, section=section, origin=origin,
                        file=fd if section == "FILE" else None)

        # Every CLAUSE scan below runs over `clauses`, the entry with its VALUE literal
        # masked out - the same rule _find_usage already applies for USAGE (line ~103):
        # a quoted VALUE literal is DATA, not syntax. Unmasked, `VALUE 'OUT OF SYNC'`
        # made the item SYNCHRONIZED, and `VALUE 'TABLE OCCURS 10 TIMES'` made it a
        # ten-element table - shifting the byte offset of every field after it. Only
        # the VALUE scan itself reads the raw text, because its operand IS the literal
        # (and it is safe there: a literal can appear in an entry only as a VALUE
        # operand, so the first `\bVALUE\b` is always the real keyword).
        clauses = _QUOTED.sub(" ", rest)
        pm = re.search(r"\bPIC(?:TURE)?\b\s+(?:IS\s+)?(\S+)", clauses, re.I)
        if pm:
            item.pic = pm.group(1).rstrip(".")
        item.usage = _find_usage(rest)
        vm = re.search(r"\bVALUE\b\s+(?:IS\s+)?('(?:[^']*)'|\"(?:[^\"]*)\"|\S+)", rest, re.I)
        if vm:
            item.value = vm.group(1).rstrip(".")
        om = re.search(r"\bOCCURS\b\s+(\d+)(?:\s+TO\s+(\d+))?", clauses, re.I)
        if om:
            # A variable-length table (OCCURS min TO max) is sized at its MAXIMUM.
            item.occurs = int(om.group(2) or om.group(1))
            dm = re.search(r"\bDEPENDING\s+(?:ON\s+)?([A-Z0-9][A-Z0-9-]*)", clauses, re.I)
            if dm:
                item.occurs_depending = dm.group(1).upper()
        rm = re.search(r"\bREDEFINES\b\s+([A-Z0-9-]+)", clauses, re.I)
        if rm:
            item.redefines = rm.group(1).upper()
        # Layout-only clauses. Matched on the whole-word abbreviations COBOL allows
        # (SYNC / SYNCHRONIZED, and SIGN [IS] {LEADING|TRAILING} SEPARATE [CHARACTER]).
        item.sync = bool(re.search(r"\bSYNC(?:HRONIZED)?\b", clauses, re.I))
        item.sign_separate = bool(re.search(r"\bSEPARATE\b(?:\s+CHARACTER)?", clauses, re.I))

        if level == 88:
            # condition-name: collect its VALUE(s); parent is the last elementary item.
            # `lo THRU hi` is a range (kept as a pair, not flattened into two singletons).
            toks = re.findall(
                r"'[^']*'|\"[^\"]*\"|[+-]?\d+(?:\.\d+)?|THRU|THROUGH|[A-Za-z][A-Za-z0-9-]*",
                re.sub(r"^88\s+[A-Z0-9-]+\s+VALUES?\s+(?:ARE\s+|IS\s+)?", "",
                       text, flags=re.I))
            toks = [t.rstrip(".") for t in toks if t.rstrip(".")]
            singles: List[str] = []
            ranges: List[List[str]] = []
            i = 0
            while i < len(toks):
                if (i + 2 < len(toks) and toks[i + 1].upper() in ("THRU", "THROUGH")):
                    ranges.append([toks[i], toks[i + 2]])
                    i += 3
                elif toks[i].upper() in ("THRU", "THROUGH"):
                    i += 1  # stray keyword, skip
                else:
                    singles.append(toks[i])
                    i += 1
            item.condition_values = singles
            item.condition_ranges = ranges
            if parent_stack:
                item.cond_parent = parent_stack[-1].name
            items.append(item)
            continue

        # Maintain the group/parent stack by level number.
        while parent_stack and parent_stack[-1].level >= level:
            parent_stack.pop()
        item.parent = parent_stack[-1].name if parent_stack else None
        item.is_group = item.pic is None and level not in (66, 77)
        item.type = parse_pic(item.pic, item.usage)
        items.append(item)
        parent_stack.append(item)
        last_elementary[level] = item

    by_name: Dict[str, DataItem] = {}
    for it in items:
        by_name.setdefault(it.name, it)  # first wins on duplicate (qualified) names
    # Resolve 88 parents' types onto the condition for convenience.
    return items, by_name


#: A Db2 VARCHAR host variable is declared as a group with a 2-byte length item and a
#: character item, both at level 49. The precompiler treats that group as ONE host
#: variable filling ONE column and never sends its subordinates.
_VARCHAR_LEVEL = 49


def _varchar_pair_at(items: List[DataItem], start: int) -> bool:
    """True when ``items[start]`` is a group whose ONLY subordinates are a Db2 VARCHAR
    length/data pair - so the group is ONE host variable filling ONE column, and the
    precompiler never sends its two subordinates.

    The gate is the LEVEL NUMBER, because that is what makes the declaration a VARCHAR
    rather than a coincidence of shape. A two-field group at 05/10 is a plain group and
    MUST keep expanding, or a group that really does fill two columns is silently
    collapsed - which is the one way this could invent lineage rather than recover it.

    Only ONE child need sit at 49: three declarations in two Db2 stored procedures pair
    a level-49 length item with a level-05 or level-10 character item and are real
    VARCHAR parameters (FBSPMG04.CBL:133-135, FBSPMG08.CBL:296-298, both `01` groups).

    **Deliberately not gated on the PICTURE.** A `S9(4) COMP` + `X(n)` test would miss
    real VARCHARs whose length item carries no USAGE at all (IXPI0009.CBL:39
    `49 SUSR-LENGTH PIC S9(4).`, FTNR119.CBL:744 `49 WS-SEC-DS-LEN PIC 9(4) VALUE
    ZEROES.`). Any picture test is a separate opt-in signal, and would have to accept
    DISPLAY as well as COMP/COMP-4/COMP-5/BINARY.

    **Known limitation:** the scan breaks on ``it.level <= g.level``, so the non-49 child
    must sit strictly BELOW the group's own level. A `10 GRP.` / `49 ...-LEN` /
    `05 ...-TEXT` member is NOT detected - the `05` ends the run before the pair is
    complete. Both estate declarations above are at `01`, where `05`/`10` are genuinely
    subordinate, so neither is missed; the shape is left as a stated gap rather than
    papered over, because widening the run past a lower level number would break the
    positional walk everything else here depends on.

    Cheap despite being called from inside the walk: it returns on the FIRST subordinate
    group and on the THIRD elementary child, so it reads at most three items for anything
    that is not a VARCHAR pair. The walk does not become quadratic.
    """
    g = items[start]
    kids: List[DataItem] = []
    for it in items[start + 1:]:
        if it.level in (66, 77) or it.level <= g.level:
            break
        if it.level == 88:
            continue                        # a condition name on the length or the text
        if it.is_group or it.redefines:
            return False                    # a real structure, not a length/data pair
        kids.append(it)
        if len(kids) > 2:
            return False
    return len(kids) == 2 and _VARCHAR_LEVEL in (kids[0].level, kids[1].level)


def elementary_subordinates(items: List[DataItem], name: str) -> Optional[List[str]]:
    """The host variables DB2 expands a GROUP-level host variable into, in declaration
    order - or ``None`` when ``name`` is not a group, or is itself one host variable, so
    a caller leaves it as written.

    Every name returned is elementary EXCEPT a Db2 VARCHAR length/data pair, which is
    emitted as the GROUP's own name because that is the one host variable the
    precompiler sends for it (see ``_varchar_pair_at``).

    `EXEC SQL FETCH c INTO :BSTI-TRNF-INIT` names a 01-level group, and the DB2
    precompiler rewrites it into every elementary item under that group before the
    statement ever reaches the database. A recovery that keeps the source spelling sees
    ONE host variable where the cursor has fifty columns, so the count gate refuses the
    correlation and the fifty fields map to nothing. This performs the same expansion,
    which is a documented precompiler transformation rather than a guess: the shape is in
    the DATA DIVISION, in the source, in front of us.

    The walk is POSITIONAL over the flat item list, not over ``data_by_name`` (first-wins
    on duplicate names - a DCLGEN copied twice would expand to the wrong copy) and not
    over ``DataItem.parent`` (a level-77 item following a group is handed that group as
    its parent by the level-stack rule, so a parent-chain walk drags standalone items in).

    The exclusions are DB2's, not ours:

    - **FILLER** is not a nameable host variable.
    - **REDEFINES** occupies its original's storage - DB2 expands the original only - and
      everything subordinate to a REDEFINES goes with it.
    - **88-level** condition names are not storage; the walk steps over them.
    - **66/77** are always top-level, so either ENDS the group's run (a bare ``level >
      group.level`` test reads `77` as a subordinate of an `01`).
    - A **nested group** is recursed into, never itself emitted - UNLESS it is a VARCHAR
      length/data pair, which is one host variable and is emitted under its own name
      while its two subordinates are skipped.
    - An **OCCURS** item is one host variable, not N copies (DB2 takes the first element).

    Returns None rather than an empty list when nothing survives the exclusions (a group
    of pure FILLER): the caller keeps the source spelling and the count gate flags it,
    which is the honest answer - an empty expansion would silently delete the reference.
    """
    target = (name or "").upper()
    start = next((i for i, it in enumerate(items)
                  if it.name == target and it.is_group
                  and it.level not in (66, 77, 88)), None)
    if start is None:
        return None
    group = items[start]
    # `SELECT CMT INTO :BE-CMT-X` names ONE host variable and fills ONE column. None is
    # the existing "keep the source spelling" answer, which both callers already handle
    # (parser._expand_structures, parser._values_slots).
    if _varchar_pair_at(items, start):
        return None
    out: List[str] = []
    # The level of a REDEFINES item whose whole subtree is being skipped.
    redefined_at: Optional[int] = None
    # The level of a VARCHAR group whose pair is being skipped. Modelled on
    # `redefined_at` because this walk is a FLAT positional scan; "do not descend" has to
    # be a level tracker, there being no recursion to return from.
    varchar_at: Optional[int] = None
    for i, it in enumerate(items[start + 1:], start + 1):
        if it.level == 88:
            continue                       # a condition name, not storage
        if it.level in (66, 77):
            break                          # always top-level: the group's run ended
        if it.level <= group.level:
            break
        if redefined_at is not None:
            if it.level > redefined_at:
                continue                   # still inside the REDEFINES subtree
            redefined_at = None
        if varchar_at is not None:
            if it.level > varchar_at:
                continue                   # the length/data pair, already accounted for
            varchar_at = None
        if it.redefines:
            redefined_at = it.level
            continue
        if it.is_group and _varchar_pair_at(items, i):
            # A VARCHAR MEMBER of a bigger record: `SELECT ... INTO :BE-REC` sends this
            # group's OWN name for one column, and its subordinates for none.
            varchar_at = it.level
            if it.name != "FILLER":
                out.append(it.name)
            continue
        if it.name == "FILLER" or it.is_group:
            continue
        out.append(it.name)
    return out or None
