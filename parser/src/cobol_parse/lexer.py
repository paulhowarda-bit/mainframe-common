"""Stage 2 (light) - tokenization.

A faithful COBOL lexer needs a configurable, dialect-dependent reserved-word table
and context-sensitive soft keywords (see references/parsing-cobol.md). This tool
recovers *control flow*, not full semantics, so the tokenizer is deliberately
modest: it splits normalized code into words, numbers, string literals, the period,
and the punctuation/operators that matter for conditions and statement structure.
Every token keeps the source line it came from for provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .normalizer import CodeLine


@dataclass
class Token:
    text: str   # original spelling
    line: int   # 1-based source line
    kind: str   # 'word' | 'number' | 'string' | 'period' | 'punct'
    origin: Optional[str] = None  # copybook member name if from a COPY-expanded line
    # Upper-cased spelling, computed ONCE. COBOL is case-insensitive, so the parser
    # compares against `up` constantly - it is the single hottest expression in the
    # pipeline (dozens of reads per token, ~10^9 tokens over a large corpus). As a
    # property it re-uppercased the text on every read.
    up: str = ""

    def __post_init__(self) -> None:
        if not self.up:
            self.up = self.text.upper()

    def is_word(self, *words: str) -> bool:
        """True when this is a word token spelled as one of ``words``.

        ``words`` are ASCII-uppercase literals at every call site, so they are
        compared directly - the previous version built a fresh set and re-uppercased
        each literal on every call, per token."""
        return self.kind == "word" and self.up in words


# Multi-character operators recognized before single chars.
_TWO_CHAR = (">=", "<=", "<>")
_SINGLE_PUNCT = set("()=><+-*/,;:")

# Characters allowed inside a COBOL word (names allow letters, digits, hyphen).
def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "-" or ch == "_"


def tokenize(lines: List[CodeLine]) -> List[Token]:
    """Tokenize normalized ``CodeLine``s into a flat token stream."""
    tokens: List[Token] = []
    for cl in lines:
        s = cl.text

        def emit(text: str, kind: str) -> None:
            tokens.append(Token(text, cl.line, kind, cl.origin))

        i, n = 0, len(s)
        while i < n:
            ch = s[i]
            if ch.isspace():
                i += 1
                continue
            # String literal: '...' or "..." with doubled-quote escape.
            if ch in ("'", '"'):
                quote = ch
                j = i + 1
                buf = [quote]
                while j < n:
                    buf.append(s[j])
                    if s[j] == quote:
                        if j + 1 < n and s[j + 1] == quote:
                            buf.append(s[j + 1])
                            j += 2
                            continue
                        j += 1
                        break
                    j += 1
                emit("".join(buf), "string")
                i = j
                continue
            # Period: significant in COBOL (sentence/scope terminator). A standalone
            # '.' is a real period; a decimal point is handled inside number scanning.
            if ch == ".":
                emit(".", "period")
                i += 1
                continue
            # Two-char operators.
            if s[i:i + 2] in _TWO_CHAR:
                emit(s[i:i + 2], "punct")
                i += 2
                continue
            # Identifier or number. COBOL data-names may START with a digit and
            # contain hyphens (e.g. 0000-MAIN, 1000-INIT), so scan the whole
            # word-run first and classify afterwards: a run of pure digits (with an
            # optional decimal point) is a number, anything containing a letter,
            # hyphen, or underscore is a word.
            if _is_word_char(ch):
                j = i
                while j < n and _is_word_char(s[j]):
                    j += 1
                run = s[i:j]
                if run.isdigit():
                    # Extend a numeric literal across a single decimal point.
                    if j < n and s[j] == "." and j + 1 < n and s[j + 1].isdigit():
                        j += 1
                        while j < n and s[j].isdigit():
                            j += 1
                        emit(s[i:j], "number")
                    else:
                        emit(run, "number")
                else:
                    emit(run, "word")
                i = j
                continue
            # Single punctuation/operator.
            if ch in _SINGLE_PUNCT:
                emit(ch, "punct")
                i += 1
                continue
            # Anything else: emit as punctuation so nothing is silently dropped.
            emit(ch, "punct")
            i += 1
    return tokens
