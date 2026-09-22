"""Whole-program analyses over the recovered AST.

Currently: **constant propagation for dynamic CALL targets.** A `CALL identifier`
has a runtime-determined target in general, but in the common case the identifier is
only ever set to a literal in this program - a `WORKING-STORAGE VALUE 'POSTLOG'`
clause, a `MOVE 'POSTLOG' TO WS-SUBPGM`, or a `MOVE` of an item that itself carries
one. When a single literal is the *only* reaching value, the target resolves and the
"unknown target" flag can be dropped.

This is a *may*-analysis, not flow-sensitive reaching-definitions: it is honest about
that by staying flagged whenever a non-literal assignment can also reach the call, or
when more than one literal can. The skill's rule holds - resolve when provably
constant, flag (don't guess) otherwise.

It lives in the parse front-end because every input it reads is the parse's own
``Program``: a consumer that parses COBOL without the modelling engine - an estate index,
say - resolves the same CALL to the same program the statechart does, instead of writing a
second propagator that disagrees with this one. ``cobol_xstate.analysis`` re-exports it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .data_division import expand_pic, parse_pic
from .model import Action, Program, walk_statements
from .textutil import split_outside_literals

_MOVE_RE = re.compile(r"^MOVE\s+(.+)$", re.I)
# SET's pre-TO operands are condition-names/indexes - never quoted literals - so this
# split cannot land inside one and needs no literal masking.
_SET_TRUE_RE = re.compile(r"^SET\s+(.+?)\s+TO\s+TRUE\b", re.I)
_NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]*$", re.I)
# A MOVE source that names one data item: a bare name, or a name qualified by OF / IN
# (`WS-PGM OF GRP-A`). Literals are collected per unqualified name - which is how the
# TARGET side records them too - so the qualifier is read and dropped, and a name
# declared under two parents keeps both literals as candidates rather than picking one.
_SOURCE_NAME = re.compile(
    r"^([A-Z0-9][A-Z0-9-]*)(?:\s+(?:OF|IN)\s+[A-Z0-9][A-Z0-9-]*)*$", re.I)
# Runs for every MOVE / SET in the program - compiled once, like its neighbours.
_SPLIT_OPERANDS = re.compile(r"[\s,]+")
# No program name, CICS PROGRAM() operand or load module name contains a blank, and a
# `*` or `?` makes the literal a placeholder or a wildcard template, not a name.
_NOT_A_NAME = re.compile(r"[\s*?]")
# PICTURE categories whose character positions are the item's length in characters.
_CHARACTER_ITEMS = ("alphanumeric", "alphanumeric-edited", "alphabetic")


def _is_filler(literal: str) -> bool:
    """`ZZZZZZZZ`, `XXXXXXXX`, `99999999` - a fill pattern, never a load module name.

    Four is the shortest run worth treating this way: `ZZ` and `XXX` are plausible
    prefixes of a real 8-character member name, `ZZZZ` is not.
    """
    return len(literal) >= 4 and len(set(literal)) == 1


def _not_a_name(literal: str) -> Optional[str]:
    """Why ``literal`` cannot be a program or resource name, or None if it can."""
    if not literal or re.search(r"\s", literal):
        return "space"                  # an all-blank literal is SPACES by another name
    if _NOT_A_NAME.search(literal):
        return "wildcard"
    if _is_filler(literal):
        return "filler"                 # an initialiser the program overwrites
    return None


def _character_length(item) -> Optional[int]:
    """The length in characters of an elementary character item, else None."""
    pic = str(getattr(item, "pic", None) or "").strip().rstrip(".")
    if not pic or parse_pic(pic, getattr(item, "usage", None)).category \
            not in _CHARACTER_ITEMS:
        return None
    return len(expand_pic(pic))


@dataclass
class CallResolution:
    confident: bool                 # exactly one literal reaches, no variable assign
    resolved: Optional[str]         # the literal target when confident
    candidates: List[str] = field(default_factory=list)  # all literal possibilities
    has_variable_assignment: bool = False
    reason: str = ""
    # HOW GOOD the candidates are, which is not the same question as how many there are:
    #   "assigned"    - a MOVE or a VALUE clause provably stores this literal
    #   "declared-88" - an 88-level names it as a possible value, but NO SET/MOVE in the
    #                   visible source proves it is ever stored
    # Collapsing the two lets a declared-but-never-stored name be reported with the same
    # confidence as one the program demonstrably moves, which is an overclaim: the first
    # is what the program was WRITTEN to allow, the second is what it DOES.
    evidence: Optional[str] = None
    # Literals that reach the item but cannot be the name the call invokes, each as
    # {"literal", "reason"} with reason length | space | wildcard | filler. Kept rather
    # than dropped, so "no admissible literal" never reads as "no literal at all".
    rejected: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class CallAnalysis:
    literal_assigns: Dict[str, Set[str]]
    # target -> the SOURCE of every non-literal assignment to it, as written. Recording
    # only that "a variable was moved here" throws away the one thing needed to answer
    # the question: `MOVE WS-A TO WS-B` with `WS-A VALUE 'POSTLOG'` makes POSTLOG the
    # value `CALL WS-B` calls, and a chain we cannot follow is a dependency nobody
    # reports. `resolve` walks these transitively.
    var_assigns: Dict[str, Set[str]]
    # All data-item names visible to the parse, so an unresolvable name can be
    # diagnosed honestly: "declared but never assigned" is a different situation from
    # "not declared at all" - the latter usually means the item (and its VALUE) lives
    # in a copybook that was not found, which `missing_copybooks` names.
    declared: Set[str] = field(default_factory=set)
    missing_copybooks: List[str] = field(default_factory=list)
    # parent item -> the string literals its 88-level condition names carry. When no
    # assignment reaches the item at all, these are still the values the program was
    # WRITTEN to put there (via SET ... TO TRUE) - reported as candidates, not proof.
    condition_literals: Dict[str, List[str]] = field(default_factory=dict)
    # item -> its length in characters, when every declaration of it fixes one. What
    # the item cannot hold, the call cannot invoke.
    receiver_sizes: Dict[str, int] = field(default_factory=dict)

    def _admit(self, name: str, literals, operand_limit: Optional[int]):
        """(names, rejected, truncated): which literals can be what the call invokes.

        The item's own declaration bounds what it holds (``receiver_sizes``) - a
        literal longer than that is not its value. ``operand_limit`` is the verb's own
        bound - 8 for a CICS PROGRAM() operand - and CICS passes exactly that many
        characters, so a literal longer than the bound but short enough for the item is
        truncated, not discarded: `ABC40001C` in a PIC X(9) is a LINK to ABC40001.
        """
        names: Set[str] = set()
        rejected: List[Dict[str, str]] = []
        truncated: Dict[str, str] = {}
        size = self.receiver_sizes.get(name)
        for lit in sorted(set(literals)):
            passed = lit
            if operand_limit is not None and len(lit.rstrip()) > operand_limit:
                passed = lit[:operand_limit].rstrip()
            # Longer than the item is judged on the literal - the item cannot hold it.
            # The rest on what the verb actually passes. Trailing blanks are padding.
            why = ("length" if size is not None and len(lit.rstrip()) > size
                   else _not_a_name(passed.rstrip()))
            if why:
                rejected.append({"literal": lit, "reason": why})
                continue
            if passed != lit:
                truncated[lit] = passed
            names.add(passed)
        return names, rejected, truncated

    def _reaching(self, name: str):
        """(literals, opaque, through) for ``name``, following assignment chains.

        ``opaque`` is True when a value this analysis cannot reduce to a literal also
        reaches - which is what keeps the answer flagged. ``through`` names the items
        walked to get there, so a resolution can say where the literal came from.

        Breadth-first over items already visited, so a cycle (`MOVE WS-A TO WS-B` and
        `MOVE WS-B TO WS-A`) terminates instead of recursing.
        """
        lits: Set[str] = set(self.literal_assigns.get(name, set()))
        opaque = False
        through: List[str] = []
        seen = {name}
        queue = [name]
        while queue:
            item = queue.pop(0)
            if item != name:
                through.append(item)
                lits |= self.literal_assigns.get(item, set())
                if item not in self.literal_assigns and item not in self.var_assigns:
                    # Nothing in this program puts a value here - a LINKAGE item, a
                    # record a READ fills, a figurative constant, a numeric literal.
                    # Whatever reaches the chain from here is not a literal we can name.
                    opaque = True
            for src in sorted(self.var_assigns.get(item, set())):
                m = _SOURCE_NAME.match(src)
                if m is None:
                    # Subscripted, reference-modified, CORRESPONDING, a function: a real
                    # value reaches and this analysis cannot say which item holds it.
                    opaque = True
                elif m.group(1).upper() not in seen:
                    seen.add(m.group(1).upper())
                    queue.append(m.group(1).upper())
        return lits, opaque, through

    def resolve(self, name: str, operand_limit: Optional[int] = None) -> CallResolution:
        """What the call naming item ``name`` invokes.

        A literal that cannot be a name - see ``_admit`` - is never offered as a
        candidate, and never promotes one either: a call that a rejected literal also
        reaches stays flagged, however many admissible names remain.
        """
        name = name.upper()
        reached, opaque, through = self._reaching(name)
        admitted, rejected, truncated = self._admit(name, reached, operand_limit)
        lits = sorted(admitted)
        var = name in self.var_assigns
        # Named only when the chain was actually walked, so the message for a directly
        # assigned name is the one it has always been.
        path = f" (through {', '.join(through)})" if through else ""
        c88, rejected88, truncated88 = self._admit(
            name, self.condition_literals.get(name, []), operand_limit)
        if not lits and not var:
            rejected += rejected88
            truncated.update(truncated88)
        # Empty unless the filter acted, so no other message changes.
        note = ""
        if truncated:
            note += (f"; the operand passes {operand_limit} characters: "
                     + ", ".join(f"'{a}' -> '{b}'" for a, b in sorted(truncated.items())))
        if rejected:
            note += "; not a name: " + ", ".join(
                f"'{r['literal']}' ({r['reason']})" for r in rejected)
        if len(lits) == 1 and not opaque and not rejected:
            reaching = "name" if truncated else "literal"   # two literals may truncate to one
            return CallResolution(True, lits[0], lits, False,
                                  f"only {reaching} reaching {name} is '{lits[0]}'" + path
                                  + note, evidence="assigned")
        if lits and not opaque:
            return CallResolution(False, None, lits, False,
                                  f"{name} may be one of {lits}; verify reaching definition"
                                  + path + note,
                                  evidence="assigned", rejected=rejected)
        if lits and opaque:
            return CallResolution(False, None, lits, True,
                                  f"{name} set to {lits} and also to a variable; runtime-determined"
                                  + note,
                                  evidence="assigned", rejected=rejected)
        if var:
            return CallResolution(False, None, [], True,
                                  (f"{name} set from variables and from no literal that "
                                   f"can be a name" if rejected else
                                   f"{name} set only from variables")
                                  + "; target runtime-determined" + note,
                                  rejected=rejected)
        c88 = sorted(c88)
        if c88:
            return CallResolution(False, None, c88, False,
                                  f"{name} carries 88-level condition value(s) {c88} "
                                  f"but no SET ... TO TRUE or MOVE in the visible "
                                  f"source proves which reaches; verify" + note,
                                  evidence="declared-88", rejected=rejected)
        if rejected:
            return CallResolution(False, None, [], False,
                                  f"no literal that can be a name reaches {name}; "
                                  f"target runtime-determined" + note,
                                  rejected=rejected)
        if name not in self.declared:
            hint = (f" - likely defined (with its VALUE) in a missing copybook "
                    f"({', '.join(self.missing_copybooks)})"
                    if self.missing_copybooks else "")
            return CallResolution(False, None, [], False,
                                  f"{name} is not declared in the visible source{hint}; "
                                  f"target runtime-determined")
        return CallResolution(False, None, [], False,
                              f"{name} is declared but never assigned a literal; "
                              f"target runtime-determined")


def analyze_calls(program: Program) -> CallAnalysis:
    literal_assigns: Dict[str, Set[str]] = {}
    var_assigns: Dict[str, Set[str]] = {}

    # Seed from DATA DIVISION VALUE clauses (an initial literal value), read from the data
    # items rather than `working_values`: an item's VALUE is found wherever its entry
    # carries it (`PIC X(08)` \ `VALUE 'MODNAME'.` split across lines is the same clause),
    # and walking EVERY declaration - not data_by_name, where the first wins, nor
    # working_values, where the last does - keeps both literals of a name declared twice
    # with different values. Collapsed to one, `CALL WS-PGM OF GRP-A` resolved confidently
    # to GRP-B's literal; kept as two, it stays a flagged candidate list.
    # The same walk sizes each item: its length in characters when EVERY declaration of
    # the name fixes one (the longest of them), else nothing - a name declared once as a
    # group, or undeclared, is never grounds to reject a literal.
    sizes: Dict[str, Optional[int]] = {}
    for item in program.data_items:
        val = str(getattr(item, "value", None) or "")
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            literal_assigns.setdefault(str(item.name).upper(), set()).add(val[1:-1].rstrip())
        if getattr(item, "level", None) != 88:
            key, size = str(item.name).upper(), _character_length(item)
            sizes[key] = (None if size is None or (key in sizes and sizes[key] is None)
                          else max(size, sizes.get(key) or 0))

    # 88-level condition names with string VALUEs: `SET <cond> TO TRUE` stores the
    # condition's (first) VALUE into its parent item - a literal-assignment channel on
    # a par with MOVE 'lit' (the `88 DEMOC104-MODULE VALUE 'DEMOC104'` idiom for
    # dynamic CALL targets). cond -> (parent, literal-it-SETs); parent -> all its
    # 88 string literals (candidate values even when no SET is visible).
    cond_lit: Dict[str, tuple] = {}
    cond_values: Dict[str, List[str]] = {}
    for name, it in (getattr(program, "data_by_name", None) or {}).items():
        parent = getattr(it, "cond_parent", None)
        vals = [str(v).strip("'\"") for v in (getattr(it, "condition_values", None) or [])
                if str(v)[:1] in ("'", '"')]
        if parent and vals:
            cond_lit[str(name).upper()] = (str(parent).upper(), vals[0])
            cond_values.setdefault(str(parent).upper(), []).extend(vals)

    # Fold in every MOVE and SET ... TO TRUE in the procedure division.
    for para in program.paragraphs:
        for st in walk_statements(para.statements):
            if not isinstance(st, Action):
                continue
            verb = st.verb.upper()
            if verb == "MOVE":
                m = _MOVE_RE.match(st.text.strip())
                # Split at the KEYWORD TO, never at a ` TO ` inside the moved literal:
                # `MOVE 'CALL TO FRCEMAIL FAILED' TO WS-ERR-MSG` torn at the first TO
                # records the phantom assignment FRCEMAIL := 'CALL', and a dynamic
                # `CALL FRCEMAIL` then resolves - confidently - to a program named
                # CALL. The one wrong answer this whole analysis exists not to give.
                parts = split_outside_literals(m.group(1), "TO") if m else None
                if parts is None:
                    continue
                source = parts[0].strip()
                targets = [t for t in _SPLIT_OPERANDS.split(parts[1].strip())
                           if _NAME_RE.match(t)]
                if source[:1] in ("'", '"'):
                    lit = source.strip("'\"").rstrip()
                    for t in targets:
                        literal_assigns.setdefault(t.upper(), set()).add(lit)
                else:
                    for t in targets:
                        var_assigns.setdefault(t.upper(), set()).add(source.upper())
            elif verb == "SET":
                m = _SET_TRUE_RE.match(st.text.strip())
                if not m:
                    continue
                for t in _SPLIT_OPERANDS.split(m.group(1).strip()):
                    cl = cond_lit.get(t.upper())
                    if cl:
                        literal_assigns.setdefault(cl[0], set()).add(cl[1])
    declared = {str(n).upper() for n in (getattr(program, "data_by_name", None) or {})}
    missing = [str(cb.get("member", "")).upper()
               for cb in (getattr(program, "copybooks", None) or [])
               if cb.get("status") == "missing"]
    return CallAnalysis(literal_assigns=literal_assigns, var_assigns=var_assigns,
                        declared=declared, missing_copybooks=missing,
                        condition_literals=cond_values,
                        receiver_sizes={k: v for k, v in sizes.items() if v is not None})
