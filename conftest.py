"""Repo-root pytest configuration: covers both ``testpaths`` (``mainframe-artifacts/tests``
and ``cobol-parser/tests``)."""

import logging

import pytest

from mainframe_artifacts.logging_setup import PACKAGE_LOGGER


@pytest.fixture(autouse=True)
def restore_package_logger():
    """Undo ``configure_logging``'s process-wide mutation after every test.

    ``configure_logging`` sets ``propagate = False`` on the package logger and installs
    its own stderr handler, and never undoes either - right for a CLI that owns stderr,
    wrong to leave behind in a test process. pytest's ``caplog`` captures on the ROOT
    logger, so once any test has invoked a CLI ``run()`` (``test_parse_bundle.py`` does),
    every later ``caplog`` assertion over this package's loggers sees nothing. The suite
    was green only because ``testpaths`` happened to collect ``test_synonyms.py`` first;
    reversing the two paths, or running under a random-order plugin, turned it red.
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    saved = (logger.propagate, logger.level, list(logger.handlers))
    yield
    logger.propagate, logger.level = saved[0], saved[1]
    logger.handlers[:] = saved[2]
