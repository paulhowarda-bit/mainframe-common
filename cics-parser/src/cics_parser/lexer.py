"""Physical text -> statements.

Three dialects will live here, because a deck's dialect is not reliably declared and the
caller should not have to know it before reading it. This module implements the first:

**CSD command syntax** - a ``DFHCSDUP`` SYSIN deck, and the ``DEFINE``-shaped output of a
``DFHCSDUP EXTRACT``. Free-form to column 72, columns 73-80 ignored as sequence numbers,
``*`` in column 1 a comment. Operands are ``KEYWORD(value)`` or a bare ``KEYWORD``.

**The report dialect** - ``DFHCSDUP LIST ... OBJECTS`` output, which is not a deck at all:
it has no command verb anywhere, an object is introduced by its own type
(``FILE(name)   GROUP(group)``), its attributes follow indented, column 1 is ASA carriage
control and the right margin of each header carries the report's print timestamp. It is
read here because it is what a site's whole-region CSD dump usually IS - the deck that
built the region is long gone, and refusing the listing means recovering nothing from it.

Still to come: the assembler column rules for the macro table decks (1-71, column-72
continuation resuming at 16).

Two rules here were wrong until real source was read, and both are the kind of thing that
produces plausible-looking output rather than an error:

**A value can contain blanks and commas.** ``DEFINETIME(22/06/10 20:03:53)`` and
``WAITTIME(0,0,0)`` are both real, from the CardDemo CSD. Splitting operands on whitespace
or on commas truncates the first and shatters the second, and neither failure raises
anything. Values are read by scanning for the balanced closing parenthesis.

**Indentation means nothing.** A continuation line is usually indented to column 9, but
``DESCRIPTION(...)`` routinely sits in column 2 - the same column ``DEFINE`` sits in. A
lexer that continued on indentation would drop every description and, worse, would treat a
description as a new statement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

#: The DFHCSDUP command verbs. A line whose first token is one of these starts a new
#: statement - subject to the disambiguation rule below.
COMMANDS = frozenset({
    "ADD", "ALTER", "APPEND", "COPY", "DEFINE", "DELETE", "EXTRACT", "INITIALIZE",
    "LIST", "MIGRATE", "PROCESS", "REMOVE", "SCAN", "SERVICE", "UPGRADE", "USERDEFINE",
    "VERIFY",
})

#: The last column DFHCSDUP reads. 73-80 are a sequence field and are ignored.
MARGIN = 72

#: A card image is 80 columns wide. Content past column 80 cannot be sitting in one, so a
#: source that has any is not a deck of card images and the margin rule does not describe
#: it - it is read at full width instead. The file decides, once, rather than each line
#: deciding for itself: a deck is card images or it is not.
CARD_COLUMNS = 80

_NAME_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789$#@_-.")


@dataclass
class Operand:
    """One ``KEYWORD(value)``, or a bare ``KEYWORD`` with ``value`` None.

    ``DELETE GROUP(CARDDEMO) ALL`` has two operands, and the second has no value. That is
    not a degenerate case to normalise away - ``ALL`` changes what the command does.
    """

    keyword: str
    value: Optional[str] = None
    line: int = 0


@dataclass
class Statement:
    """One complete DFHCSDUP command, however many lines it was written across."""

    verb: str
    operands: List[Operand] = field(default_factory=list)
    line: int = 0
    flags: List[str] = field(default_factory=list)
    #: Why this statement is known to be INCOMPLETE - text cut at the margin, or an
    #: operand whose parenthesis never closed and which therefore ate the rest of it.
    #: A region flag says a line was damaged; this is what carries that onto the row the
    #: damaged statement produced, so a missing attribute cannot read as an absent one.
    damaged: List[str] = field(default_factory=list)

    def first(self, keyword: str) -> Optional[str]:
        """The value of the first operand with this keyword, or None.

        First rather than only: a deck that names an attribute twice is a real thing, and
        the duplicate is reported by the parser rather than silently merged here.
        """
        want = keyword.upper()
        for op in self.operands:
            if op.keyword == want:
                return op.value
        return None


# --------------------------------------------------------------------------- #
# physical lines
# --------------------------------------------------------------------------- #

def margin_for(text: str, flags: List[str]) -> Optional[int]:
    """The column this source is cut at, or ``None`` when it is not card images at all.

    Reported once per source, not once per line: it is one fact about the file, and one
    flag per offending line is how a 320,000-line region dump produced a flag list longer
    than itself.
    """
    longest = max((len(line) for line in text.splitlines()), default=0)
    if longest <= CARD_COLUMNS:
        return MARGIN
    flags.append(
        "this source is not a deck of 80-column card images - its longest line is %d "
        "characters - so the column-%d margin was not applied and every line was read at "
        "its full width" % (longest, MARGIN))
    return None


def _strip_margin(line: str, lineno: int, flags: List[str],
                  margin: Optional[int], cut: Set[int]) -> str:
    """Cut at column 72 and decide whether what was cut mattered.

    Columns 73-80 hold sequence numbers on most decks and nothing at all on many. But a
    deck edited in a free-form editor can genuinely run a value past the margin, and CICS
    would not see it either - so the truncation is real, and it is REPORTED rather than
    performed quietly. A tail that looks like a sequence field is not worth a flag; one
    containing command punctuation is.
    """
    if margin is None or len(line) <= margin:
        return line
    tail = line[margin:]
    if tail.strip() and ("(" in tail or ")" in tail or "=" in tail):
        cut.add(lineno)
        flags.append(
            "line %d: content past column %d was ignored, as CICS would ignore it: %r"
            % (lineno, margin, tail.rstrip()))
    return line[:margin]


#: CICSPlex SM BATCHREP verbs. The same command syntax with a different vocabulary, which
#: is why ``lex_csd`` takes the set rather than hard-coding it: a BAS deck lexed against
#: the CSD verbs is entirely "text before the first command".
BAS_COMMANDS = frozenset({"CREATE", "UPDATE", "REMOVE", "DELETE", "CONTEXT", "SCOPE",
                          "ADD", "DISCARD", "INSTALL", "LIST"})


#: BATCHREP verbs that ARE commands even when written ``CONTEXT(EYUPLX01)``. The
#: paren rule below exists because ADD, DELETE and LIST are CSD attribute keywords as well
#: as CSD commands; nothing in a BAS deck writes CONTEXT or SCOPE as an attribute, so
#: applying the rule to them turns a real command into ignored text.
BAS_PAREN_COMMANDS = frozenset({"CONTEXT", "SCOPE"})


def _starts_a_command(text: str, commands=COMMANDS, paren_commands=frozenset()) -> bool:
    """Is this line the start of a new statement rather than a continuation?

    The rule that matters: a command verb IMMEDIATELY followed by ``(`` is an operand, not
    a verb. ``ADD``, ``DELETE`` and ``LIST`` are all three DFHCSDUP commands AND ordinary
    attribute keywords - a FILE definition carries ``ADD(YES) DELETE(YES)``, and an
    ``ADD GROUP(g) LIST(l)`` can be written with ``LIST(l)`` on its own line. Without this
    rule a continuation line beginning ``DELETE(YES)`` silently becomes a DELETE command
    and everything after it is attached to the wrong statement.
    """
    stripped = text.lstrip()
    if not stripped:
        return False
    word = ""
    for ch in stripped:
        if ch in _NAME_CHARS:
            word += ch
        else:
            break
    if word.upper() not in commands:
        return False
    if word.upper() in paren_commands:
        return True
    rest = stripped[len(word):]
    return not rest.startswith("(")


# --------------------------------------------------------------------------- #
# operands
# --------------------------------------------------------------------------- #

def _join(parts: List[Tuple[int, str]],
          sep: str = " ") -> Tuple[str, List[Tuple[int, int]]]:
    """Join a statement's physical lines, keeping an offset -> line-number map.

    The map is what lets a manifest row say which line an attribute was written on, across
    a statement that ran to twelve lines. Without it every operand of a DEFINE reports the
    DEFINE's own line, and provenance on a 500-line deck stops being useful.
    """
    text, spans, at = [], [], 0
    for lineno, chunk in parts:
        spans.append((at, lineno))
        text.append(chunk)
        at += len(chunk) + len(sep)
    return sep.join(text), spans


def _line_at(spans: List[Tuple[int, int]], offset: int) -> int:
    lineno = spans[0][1] if spans else 0
    for start, n in spans:
        if start > offset:
            break
        lineno = n
    return lineno


def _split_operands(text: str, spans: List[Tuple[int, int]], flags: List[str],
                    damaged: Optional[List[str]] = None) -> List[Operand]:
    """``KEYWORD(value) KEYWORD(value) KEYWORD`` -> operands, values read by balance.

    The scan is over the whole joined statement, so a value is allowed to contain blanks,
    commas and nested parentheses - all three occur in real decks.
    """
    ops: List[Operand] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] not in _NAME_CHARS:
            i += 1
            continue
        start = i
        while i < n and text[i] in _NAME_CHARS:
            i += 1
        keyword = text[start:i].upper()
        lineno = _line_at(spans, start)
        if i < n and text[i] == "(":
            depth = 0
            vstart = i + 1
            while i < n:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if depth != 0:
                flags.append("line %d: %s( is never closed; the rest of the statement "
                             "was read as its value" % (lineno, keyword))
                if damaged is not None:
                    damaged.append(
                        "%s( is never closed, so every attribute after it on line %d was "
                        "read as part of its value" % (keyword, lineno))
                ops.append(Operand(keyword, text[vstart:].strip(), lineno))
                return ops
            ops.append(Operand(keyword, text[vstart:i].strip(), lineno))
            i += 1
        else:
            ops.append(Operand(keyword, None, lineno))
    return ops


# --------------------------------------------------------------------------- #
# the entry point
# --------------------------------------------------------------------------- #

def lex_csd(text: str, commands=COMMANDS,
            paren_commands=frozenset()) -> Tuple[List[Statement], List[str]]:
    """Split a DFHCSDUP deck into statements. Returns (statements, flags).

    Flags are file-level: a truncated line, an unclosed parenthesis, text before the first
    command. Anything a statement itself is unhappy about rides on the statement.
    """
    flags: List[str] = []
    statements: List[Statement] = []
    current: Optional[Statement] = None
    pending: List[Tuple[int, str]] = []
    margin = margin_for(text, flags)
    cut: Set[int] = set()

    def close() -> None:
        if current is not None:
            joined, spans = _join(pending)
            current.operands.extend(
                _split_operands(joined, spans, current.flags, current.damaged))
            # Which of THIS statement's physical lines lost text. The region flag already
            # names the line; this is what carries it onto the definition that line
            # belongs to, so an attribute that was cut cannot read as one never written.
            lost = sorted(({lineno for lineno, _ in pending} | {current.line}) & cut)
            if lost:
                current.damaged.append(
                    "text past column %d was cut from line(s) %s of this definition"
                    % (MARGIN, ", ".join(str(n) for n in lost)))

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_margin(raw.rstrip("\r\n"), lineno, flags, margin, cut)
        if line[:1] == "*":
            continue
        if not line.strip():
            continue
        if _starts_a_command(line, commands, paren_commands):
            close()
            stripped = line.lstrip()
            # The verb is the leading run of name characters, NOT everything up to the
            # first blank: a BATCHREP `CONTEXT(EYUPLX01)` has no blank in it, and split on
            # one the verb becomes `CONTEXT(EYUPLX01)` and matches nothing.
            end = len(stripped)
            for index, ch in enumerate(stripped):
                if ch not in _NAME_CHARS:
                    end = index
                    break
            verb, rest = stripped[:end], stripped[end:]
            current = Statement(verb=verb.upper(), line=lineno)
            statements.append(current)
            pending = [(lineno, rest)]
        elif current is None:
            flags.append("line %d: text before the first command was ignored: %r"
                         % (lineno, line.strip()))
        else:
            pending.append((lineno, line.strip()))

    close()
    return statements, flags


# --------------------------------------------------------------------------- #
# the report dialect: DFHCSDUP's printed listing, which is not a deck
# --------------------------------------------------------------------------- #

#: DFHCSDUP's own messages, printed around the listing it produces: the CSD it opened and
#: closed, how many commands ran, the highest return code. They are the utility talking
#: about the job, not definitions.
_REPORT_MESSAGE = re.compile(r"^\s*DFH\d{4}\s*[A-Z]?\s")

#: The job's outcome, which decides whether the listing can be trusted to be complete.
_REPORT_RC = re.compile(r"HIGHEST RETURN CODE WAS:\s*(\d+)", re.I)

#: The REPORT's own print timestamp, in the right margin of every object header line:
#: Julian `15.206 23:09`. The definition's own times are `DEFINETIME`/`CHANGETIME` beside
#: it, which are kept - this one belongs to the piece of paper, so it is dropped. Left in,
#: it splits into bare operands named `15.206` and `23:09` on every object in the member.
_REPORT_STAMP = re.compile(r"\s{2,}\d\d\.\d{3}\s+\d\d:\d\d\s*$")

#: An object header: its own TYPE introduces it, where a deck would have said DEFINE.
#: `GROUP(` has to be an operand of its own - an attribute line carrying `NSRGROUP()` is
#: not a header, and the anchor plus the whitespace is what keeps them apart.
_REPORT_OBJECT = re.compile(r"^[A-Z][A-Z0-9$#@]*\([^)]*\)\s+GROUP\(", re.I)

#: ASA carriage control: column 1 is the printer's, not the data's. Blank single-spaces,
#: `-` triple-spaces, `0` double-spaces, `+` overprints, `1` ejects to a new page.
_ASA_CONTROL = frozenset(" -0+1")


def uses_asa_control(lines: List[str]) -> bool:
    """Is column 1 carriage control rather than data?

    Judged on every non-blank line, because being wrong either way is silent: read as data
    the control character joins the first keyword (`1FILE(...)` matches nothing), and
    stripped when it was data every object loses the first letter of its type.
    """
    seen = {line[:1] for line in lines if line.strip()}
    return bool(seen) and seen <= _ASA_CONTROL


def lex_csd_report(text: str) -> Tuple[List[Statement], List[str]]:
    """``DFHCSDUP LIST ... OBJECTS`` output -> the statements a deck would have produced.

    The listing is not a deck and cannot be read as one: there is no command verb in it at
    all, so every line of it is "text before the first command" and nothing is defined.
    What it has instead is one header line per object - its TYPE and its GROUP - followed
    by its attributes, indented, until the next header.

    Emitted as ``DEFINE`` statements on purpose, rather than as a second kind of object:
    everything downstream (the resource types, the attribute table, the edges, the
    conflict rule) is then the same code reading the same shape, and a resource recovered
    from a listing is indistinguishable from the same resource recovered from the deck
    that made it - which is the point, because for most regions the deck is long gone.
    """
    flags: List[str] = []
    statements: List[Statement] = []
    current: Optional[Statement] = None
    pending: List[Tuple[int, str]] = []
    lines = [line.rstrip("\r\n") for line in text.splitlines()]
    asa = uses_asa_control(lines)

    def close() -> None:
        if current is not None:
            joined, spans = _join(pending)
            current.operands.extend(
                _split_operands(joined, spans, current.flags, current.damaged))

    for lineno, raw in enumerate(lines, start=1):
        line = raw[1:] if asa else raw
        if not line.strip():
            continue
        if _REPORT_MESSAGE.match(line):
            # A message ENDS the object above it. A member is very often several jobs
            # concatenated, and the banner and command echo that start the next one sit
            # right after this trailer: absorbed as attributes they would hand the last
            # object of each segment the GROUP named in the next segment's LIST command -
            # a definition reported, with no flag, as living in a group it is not in.
            close()
            current = None
            rc = _REPORT_RC.search(line)
            if rc and rc.group(1).lstrip("0"):
                flags.append(
                    "line %d: DFHCSDUP reported highest return code %s for this listing, "
                    "so what it lists may be partial: %r"
                    % (lineno, rc.group(1), line.strip()))
            continue
        stripped = _REPORT_STAMP.sub("", line).strip()
        if _REPORT_OBJECT.match(stripped):
            close()
            current = Statement(verb="DEFINE", line=lineno)
            statements.append(current)
            pending = [(lineno, stripped)]
        elif current is None or not line[:1].isspace():
            # An attribute of the object above is indented under it; the report's own
            # furniture - its banner, the command it is echoing - starts where a header
            # would. Reported rather than dropped, because a header shape this reader does
            # not recognise would land here and must not disappear quietly.
            flags.append("line %d: text outside any object was ignored: %r"
                         % (lineno, stripped))
        else:
            pending.append((lineno, stripped))

    close()
    return statements, flags


# --------------------------------------------------------------------------- #
# the assembler dialect: macro table decks and BMS
# --------------------------------------------------------------------------- #

#: HLASM's fixed fields. A continuation resumes at 16 - not at the first non-blank, and
#: not at the previous operand's column.
_ASM_MARGIN = 71
_ASM_CONTINUE = 71          # the index of column 72
_ASM_RESUME = 15            # the index of column 16


@dataclass
class MacroStatement:
    """One assembler macro invocation: ``LABEL  DFHPCT TYPE=ENTRY,TRANSID=MENU,...``

    ``operands`` keeps keyword operands; ``positional`` keeps the bare ones, in order.
    Both matter: ``DFHMDF POS=(1,1),LENGTH=8`` is all keyword, while a site's own table
    macros routinely take a positional name first.
    """

    label: Optional[str]
    operation: str
    operands: List[Operand] = field(default_factory=list)
    positional: List[str] = field(default_factory=list)
    line: int = 0
    flags: List[str] = field(default_factory=list)

    def first(self, keyword: str) -> Optional[str]:
        want = keyword.upper()
        for op in self.operands:
            if op.keyword == want:
                return op.value
        return None


def _operand_field(text: str) -> str:
    """The operand field only: everything up to the first blank outside quotes and parens.

    HLASM has a REMARK field, and a table deck uses it:

        CSGM     KIKPCT TYPE=ENTRY,TRANSID=CSGM,PROGRAM=KSGMPGM NO REFRESH

    ``NO REFRESH`` is a comment. Read as part of the operands, the program becomes
    ``KSGMPGM NO REFRESH`` - a name that resolves to nothing, in a row that looks
    completely ordinary. Found in KICKS's own PCT, not in anything written here.
    """
    depth, quoted = 0, False
    for index, ch in enumerate(text):
        if ch == "'":
            quoted = not quoted
        elif not quoted:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == " " and depth == 0:
                return text[:index]
    return text


def _split_asm_operands(text: str, lineno: int) -> Tuple[List[Operand], List[str]]:
    """Split on commas at parenthesis depth 0, then on the first ``=``.

    Depth matters: ``ACCMETH=(VSAM,KSDS)`` and ``SERVREQ=(GET,PUT,BROWSE)`` are single
    operands whose values contain the same comma that separates operands. Splitting
    naively turns one file definition into four nonsense ones, and raises nothing.
    """
    parts, depth, current = [], 0, []
    quoted = False
    for ch in _operand_field(text):
        if ch == "'":
            quoted = not quoted
        if not quoted:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append("".join(current).strip())
                current = []
                continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)

    keywords: List[Operand] = []
    positional: List[str] = []
    for part in parts:
        if not part:
            continue
        name, sep, value = part.partition("=")
        if sep and name.strip().replace("&", "").isidentifier():
            keywords.append(Operand(name.strip().upper(), value.strip(), lineno))
        else:
            positional.append(part)
    return keywords, positional


def lex_macro(text: str) -> Tuple[List[MacroStatement], List[str]]:
    """Split an assembler deck into macro statements. Returns (statements, flags).

    Only the physical format is handled here - which columns are read, how a statement
    continues, what is a comment. Which macros mean what is ``tables.py``'s and ``bms.py``'s
    business, and neither should have to know about column 72.

    Conditional assembly is NOT evaluated. A table deck inside an ``AIF`` is the same
    problem ``asm-dependencies`` solves with ``&SYSPARM``, and pretending to decide it here
    would produce a deck that never assembles. Such statements are flagged instead.
    """
    flags: List[str] = []
    statements: List[MacroStatement] = []
    pending: List[Tuple[int, str]] = []
    label: Optional[str] = None
    operation = ""
    start = 0
    joining = False

    def close() -> None:
        if not operation:
            return
        stmt = MacroStatement(label=label, operation=operation, line=start)
        # Joined with NOTHING between the parts: an assembler continuation resumes the
        # operand field exactly, so a separator would insert a blank into it - and a blank
        # outside quotes and parens ENDS the operand field, which would truncate every
        # continued statement at its first line.
        joined, spans = _join(pending, "")
        stmt.operands, stmt.positional = _split_asm_operands(
            joined, _line_at(spans, 0) if spans else start)
        statements.append(stmt)

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r\n")
        if line[:1] in ("*", ".") and not joining:
            continue
        if not line.strip():
            continue
        continues = len(line) > _ASM_CONTINUE and line[_ASM_CONTINUE:72].strip() != ""
        # A continuation mark that MISSED column 72 is the classic hand-edited-deck
        # failure, and it is silent: the mark is past the margin so nothing continues, the
        # next line is read as a fresh statement, its operation is not a known macro, and
        # every operand on it disappears. Found while writing this repository's own
        # example, which had its X in column 74.
        if not continues and len(line) > 72 and line[72:].strip():
            flags.append(
                "line %d: there is content past column 72 but column 72 itself is blank, "
                "so this statement does NOT continue. A continuation mark must be in "
                "column 72 exactly; everything on the next line is being read as a new "
                "statement: %r" % (lineno, line[72:].rstrip()))
        body = line[:_ASM_MARGIN]

        if joining:
            pending.append((lineno, body[_ASM_RESUME:].strip()))
            joining = continues
            if not continues:
                close()
                operation = ""
            continue

        close()
        operation = ""
        if body[:1] not in (" ", "\t"):
            label, _, rest = body.partition(" ")
            label = label or None
        else:
            label, rest = None, body
        parts = rest.strip().split(None, 1)
        if not parts:
            continue
        operation = parts[0].upper()
        if operation in ("AIF", "AGO", "ANOP", "SETA", "SETB", "SETC", "MACRO", "MEND"):
            flags.append(
                "line %d: conditional assembly (%s) is not evaluated - which statements "
                "this deck contributes may depend on &SYSPARM, and deciding it here would "
                "model a deck that never assembles" % (lineno, operation))
            operation = ""
            continue
        pending = [(lineno, parts[1] if len(parts) > 1 else "")]
        start = lineno
        joining = continues

    close()
    return statements, flags
