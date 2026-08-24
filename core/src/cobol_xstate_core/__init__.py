"""cobol_xstate_core - what the COBOL and JCL front-ends both need.

The two front-ends (``cobol_xstate``, ``jcl_dependencies``) answer different questions
about different languages, but they reach the SAME estate the SAME way: one artifact
service, one two-stage retrieval (close over the text, then fetch what the manifest
names), one report vocabulary. That shared half lives here so neither front-end depends
on the other - they are peers, and a JCL run has no business carrying a COBOL modeling
engine it will never execute.

What belongs here is decided by one question: *would BOTH front-ends need it?* Retrieval,
the estate-client boundary, the manifest vocabulary they both produce rows for, and the
CLI plumbing they both repeat. What does NOT belong here is anything that knows COBOL or
knows JCL - the parsers, the models, the views. Core is deliberately ignorant of its
consumers: it never imports either front-end, and it never names them.

Library logging contract, same as the front-ends: attach a no-op handler to this
package's logger so importing core never writes to stderr on its own. The application
decides - see :func:`cobol_xstate_core.logging_setup.configure_logging`, which each CLI
calls with the logger roots it wants configured.
"""

import logging as _logging

from .errors import CobolXstateError

_logging.getLogger(__name__).addHandler(_logging.NullHandler())

__all__ = ["CobolXstateError"]

__version__ = "0.1.0"
