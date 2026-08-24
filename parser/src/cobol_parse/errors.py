"""Exception hierarchy for the parse front-end.

RE-EXPORTED base, never redefined. The base lives at the lowest layer every consumer
depends on (:mod:`cobol_xstate_core.errors`) because ``ServiceUnavailable`` - raised by
the estate boundary, which is core's - must derive from the SAME class the consumers'
CLIs catch. Defining a second CobolXstateError here would look harmless and would
silently stop ``except CobolXstateError`` at a CLI boundary from catching parse
failures, turning an expected, explainable error into an "internal error" traceback.
cobol/tests/test_logging.py::test_every_domain_error_derives_from_the_one_base is the
guard.

This module imports nothing from the package, so it is safe to import from anywhere.
"""
from __future__ import annotations

from cobol_xstate_core.errors import CobolXstateError


class SourceFormatError(CobolXstateError):
    """The source format (fixed / free) could not be determined, or is invalid."""


class ParseError(CobolXstateError):
    """The COBOL or JCL source could not be parsed into a model."""


class CopybookError(CobolXstateError):
    """A COPY member / copybook could not be resolved or expanded."""
