from cobol_parse.normalizer import normalize, SourceFormat
from cobol_parse.lexer import tokenize


def _toks(code: str):
    return tokenize(normalize("       " + code + "\n", SourceFormat.FIXED))


def test_digit_led_hyphenated_name_is_one_word():
    toks = _toks("PERFORM 1000-INIT")
    words = [t.text for t in toks if t.kind == "word"]
    assert "1000-INIT" in words
    assert "PERFORM" in words


def test_paragraph_name_with_leading_digits():
    toks = _toks("GO TO 0000-MAIN")
    words = [t.up for t in toks if t.kind == "word"]
    assert "0000-MAIN" in words


def test_numeric_literal_with_decimal():
    toks = _toks("MOVE 12.50 TO WS-AMT")
    nums = [t.text for t in toks if t.kind == "number"]
    assert "12.50" in nums


def test_string_literal_and_relational_operator():
    toks = _toks("IF WS-EOF = 'Y'")
    kinds = {(t.kind, t.up) for t in toks}
    assert ("string", "'Y'") in kinds
    assert ("punct", "=") in kinds


def test_period_is_its_own_token():
    toks = _toks("CONTINUE.")
    assert toks[-1].kind == "period"
