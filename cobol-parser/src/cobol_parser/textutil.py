"""The one literal-masking rule, shared by the parse front-end and its consumers.

Moved here from ``cobol_xstate.semantics`` when the parse front-end became its own
distribution: the parser needs :func:`mask_literals` to tear statements safely, and the
front-end must not import the modelling engine. ``cobol_xstate.semantics`` re-imports
these, so its consumers are unchanged.
"""

from __future__ import annotations

import re

# A quoted alphanumeric literal is DATA, not syntax - the words inside it must never be
# read as keywords. Same rule, same idiom as `data_division._QUOTED` (a VALUE literal
# must not be read as a clause) and `naming._QUOTED` (a literal's case is data). The
# canonical failure this exists for: `MOVE 'CALL TO FRCEMAIL FAILED' TO WS-ERR-MSG`
# torn at the ` TO ` INSIDE the message, which manufactures a literal assignment
# `FRCEMAIL := 'CALL'` - and a dynamic `CALL FRCEMAIL` then "resolves", confidently, to
# a program named CALL. A doubled quote inside a literal (`'DON''T'`) parses as two
# adjacent literals here, which masks the same span, so it needs no special case.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def mask_literals(text: str) -> str:
    """A copy of ``text`` with every quoted literal replaced by same-length,
    non-whitespace filler.

    LENGTH-PRESERVING on purpose: a scan runs over the mask, and the positions it finds
    are then used to slice the ORIGINAL text - so the literal itself survives intact in
    whatever piece it belongs to. (``\\x00`` is not ``\\s`` and not a word character, so
    neither whitespace-delimited keywords nor ``\\b`` boundaries can fire inside a
    masked span.)
    """
    return _QUOTED.sub(lambda m: "\x00" * (m.end() - m.start()), text)
