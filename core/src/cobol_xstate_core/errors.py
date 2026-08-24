"""The one error base, at the lowest layer that both front-ends depend on.

Only the BASE lives here. Every leaf error follows whatever raises it:
``ReactiveLoweringError`` is raised by the COBOL reactive lowering and lives in
``cobol_xstate.errors``; ``ServiceUnavailable`` is raised by the estate boundary and
lives in :mod:`cobol_xstate_core.artifact_service`.

The rule that makes this work is that the front-ends **re-export this class, never
redefine it**. Two definitions of ``CobolXstateError`` would look harmless and be
anything but: each CLI's top-level ``except CobolXstateError`` would stop catching the
other package's failures, so an expected, explainable error would surface as an
"internal error" traceback instead of its own message. ``tests/test_logging.py``
asserts every domain error derives from the base as imported through the front-end,
which is the guard for exactly that.

This module imports nothing, so it is safe to import from anywhere.
"""
from __future__ import annotations


class CobolXstateError(Exception):
    """Base for every error these packages raise deliberately.

    Code at the boundary (either CLI, or any embedding application) can print
    ``str(exc)`` as the whole user-facing message: these carry human-readable
    explanations, not developer diagnostics. Unexpected exceptions - the ones that are
    NOT a ``CobolXstateError`` - signal a bug in the tool and warrant a traceback.
    """
