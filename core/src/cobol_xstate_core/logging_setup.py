"""Command-line-side logging configuration, shared by both front-end CLIs.

The LIBRARY never configures logging: each package's ``__init__`` attaches a
``NullHandler`` to its own logger and every module logs through
``logging.getLogger(__name__)``. Only a command-line entry point calls
:func:`configure_logging`, so importing any of these packages leaves a host
application's logging untouched - the documented contract for a well-behaved library
(https://docs.python.org/3/howto/logging.html#configuring-logging-for-a-library).

**Why ``loggers`` is a parameter.** The code now spans more than one top-level package,
and a logger is only configured if something configures its ROOT. A module in
``cobol_xstate_core`` logs to ``cobol_xstate_core.<module>``, which is not under
``cobol_xstate`` - so configuring one root and not the other does not merely lose
messages, it changes behaviour in both directions: records from the unconfigured tree
propagate to the root logger (double-printing under a host that configured one) or, with
no handler anywhere, hit logging's ``lastResort`` and print WARNING+ straight to stderr -
which is why ``-qq`` would stop being silent. Each CLI therefore passes every root its
run can emit from, and ``tests/test_logging.py``'s ``-qq`` test is the guard.

Core names no front-end here: the default configures only core's own root, and the
caller adds its own. A shared module that knew its consumers' names would be a
dependency pointing the wrong way.
"""
from __future__ import annotations

import logging
import sys
from typing import List, Sequence

#: This package's top-level logger name. Every module logger
#: (``cobol_xstate_core.artifact_service`` ...) is a child of it.
PACKAGE_LOGGER = "cobol_xstate_core"

_HANDLER_TAG = "_cobol_xstate_cli_handler"


def level_for(verbose: int = 0, quiet: int = 0) -> int:
    """Resolve the ``-v`` / ``-q`` counts to a logging level.

    The default is INFO - a normal CLI run stays exactly as chatty as it always was
    (progress + warnings + errors). ``quiet`` wins over ``verbose`` when both are given.
    """
    if quiet >= 2:
        return logging.ERROR      # -qq: only failures
    if quiet == 1:
        return logging.WARNING    # -q : failures + warnings (hides progress)
    if verbose >= 1:
        return logging.DEBUG      # -v : adds swallowed tracebacks + internal detail
    return logging.INFO           # default: progress + warnings + failures


def configure_logging(verbose: int = 0, quiet: int = 0,
                      loggers: Sequence[str] = (PACKAGE_LOGGER,)
                      ) -> List[logging.Logger]:
    """Install a single stderr handler on each named logger and set its level.

    Idempotent per logger: a second call replaces the handler this function added rather
    than stacking another, so repeated ``run()`` calls in one process (and the test
    suite) never double-print. The handler binds to the CURRENT ``sys.stderr`` each call,
    which also keeps pytest's per-test stderr capture working.

    Returns the configured loggers in the order given.
    """
    level = level_for(verbose, quiet)
    configured: List[logging.Logger] = []
    for name in loggers:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        # The CLI owns stderr, so don't also bubble records up to the root logger (which
        # a host might have configured) - that would double-print under the CLI.
        logger.propagate = False

        for handler in [h for h in logger.handlers if getattr(h, _HANDLER_TAG, False)]:
            logger.removeHandler(handler)

        handler = logging.StreamHandler(sys.stderr)
        # Messages already carry their own context (``[source] ...``); a bare format keeps
        # CLI output identical to the historical ``print(..., file=sys.stderr)`` lines.
        handler.setFormatter(logging.Formatter("%(message)s"))
        setattr(handler, _HANDLER_TAG, True)
        logger.addHandler(handler)
        configured.append(logger)
    return configured
