"""The test-isolation contract around ``configure_logging``.

``configure_logging`` turns propagation off on the package logger and installs a stderr
handler, for the rest of the process. The repo-root ``conftest.py`` restores the logger
after every test so that a later ``caplog`` assertion still sees the package's records.
These two tests are deliberately ORDER-DEPENDENT within this module: the first performs
the mutation, the second asserts it was undone. Without the fixture the second fails -
which is exactly the information it should carry.
"""

import logging

from mainframe_artifacts.logging_setup import PACKAGE_LOGGER, configure_logging


def test_configure_logging_mutates_the_package_logger_for_the_process():
    """The mutation this file is about, stated as a fact rather than assumed."""
    (logger,) = configure_logging()
    assert logger.name == PACKAGE_LOGGER
    assert logger.propagate is False
    assert logger.handlers, "configure_logging installs its own stderr handler"


def test_the_next_test_sees_a_pristine_package_logger_and_caplog_works(caplog):
    """Runs AFTER the mutation above. Passes only if the autouse fixture in the repo-root
    conftest.py restored the logger; under a per-test fix alone this fails."""
    logger = logging.getLogger(PACKAGE_LOGGER)
    assert logger.propagate is True, (
        "configure_logging's propagate=False leaked into the next test - caplog is "
        "blind to this package for the rest of the process")
    assert not [h for h in logger.handlers
                if getattr(h, "_cobol_xstate_cli_handler", False)], (
        "configure_logging's stderr handler leaked into the next test")
    child = logging.getLogger(PACKAGE_LOGGER + ".isolation_probe")
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        child.warning("probe %d", 42)
    assert any(r.getMessage() == "probe 42" for r in caplog.records), (
        "a package-logger record did not reach caplog's root handler")
