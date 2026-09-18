"""cics_parser must work with nothing else from the family importable - not even this repo.

That is the property the distribution exists to create: a second reader of the same CICS
members (an estate index, say) lexes a deck and a DFHCSDUP listing through the SAME code
cics-dependencies does, and carries no extractor to do it. A single stray import would
erase that while every other test still passed, so it is asserted in a fresh interpreter
with every sibling package blocked - including mainframe_artifacts, which cics-parser
deliberately does not depend on.

``sys.meta_path`` finders are consulted through ``find_spec``; ``find_module`` was REMOVED
in Python 3.12, so a blocker that only defines it is ignored and the test passes vacuously.
The first test guards against exactly that.
"""

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import cics_parser

# Wherever this run found cics_parser (an install, the root pyproject's pythonpath, a
# flattened copy), the child gets that tree and nothing else from the family.
_TREE = str(Path(cics_parser.__file__).resolve().parents[1])

_BLOCKED = ("cics_dependencies", "mainframe_artifacts", "cobol_parser", "cobol_xstate",
            "jcl_dependencies", "eztrieve_dependencies", "asm_dependencies", "mfdep")

_PREAMBLE = textwrap.dedent("""
    import sys
    sys.path.insert(0, %r)

    class Blocker:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in %r:
                raise ImportError("BLOCKED " + name)
            return None

    sys.meta_path.insert(0, Blocker())
""")

_MODULES = sorted(p.stem for p in Path(_TREE, "cics_parser").glob("*.py"))

#: What a cics_parser module may import: the standard library it uses, and itself.
_ALLOWED = ("__future__", "re", "dataclasses", "typing")


def _isolated(body):
    """A fresh interpreter: blocking a module already in sys.modules does nothing."""
    return subprocess.run([sys.executable, "-c",
                           _PREAMBLE % (_TREE, _BLOCKED) + textwrap.dedent(body)],
                          capture_output=True, text=True)


@pytest.mark.parametrize("package", _BLOCKED)
def test_the_blocker_actually_blocks(package):
    """Guard the guard. If this passes when it should not, everything below is vacuous."""
    proc = _isolated("import {0}".format(package))
    assert proc.returncode != 0
    assert "BLOCKED {0}".format(package) in proc.stderr


def test_a_deck_and_a_listing_lex_with_every_sibling_blocked():
    proc = _isolated("""
        from cics_parser.detect import looks_like_csd_report, source_kind
        from cics_parser.lexer import lex_csd, lex_csd_report, lex_macro

        deck = " DEFINE TRANSACTION(APMN) GROUP(G)\\n        PROGRAM(APPMENU)\\n"
        listing = (" TRANSACTION(APMN)       GROUP(G)\\n"
                   "                         PROGRAM(APPMENU)\\n"
                   " DFH5109 I END OF DFHCSDUP UTILITY JOB.  HIGHEST RETURN CODE WAS: 0\\n")
        assert not looks_like_csd_report(deck) and looks_like_csd_report(listing)
        for text, lex in ((deck, lex_csd), (listing, lex_csd_report)):
            stmts, _ = lex(text)
            assert [(s.verb, s.first("TRANSACTION"), s.first("PROGRAM"))
                    for s in stmts] == [("DEFINE", "APMN", "APPMENU")], stmts
        macro, _ = lex_macro("         DFHPCT TYPE=ENTRY,TRANSID=APMN,PROGRAM=APPMENU\\n")
        assert macro[0].first("PROGRAM") == "APPMENU"
        assert source_kind(deck) == "csd"
        print("OK")
    """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


@pytest.mark.parametrize("module", _MODULES)
def test_no_module_imports_beyond_the_standard_library(module):
    """Read the source too: an import in a rarely-taken branch would not show up above.
    Parsed rather than grepped - a docstring sentence can begin with "from a listing"."""
    tree = ast.parse(Path(_TREE, "cics_parser", module + ".py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names = [node.module]
        else:
            continue
        for name in names:
            assert name.split(".")[0] in _ALLOWED, (
                "cics_parser/%s.py line %d imports %s" % (module, node.lineno, name))
