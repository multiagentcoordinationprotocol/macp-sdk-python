"""Unit tests for the SDK logger and ``configure_logging`` helper."""

from __future__ import annotations

import logging

import pytest

from macp_sdk._logging import configure_logging, logger


@pytest.fixture(autouse=True)
def _restore_logger():
    """Snapshot and restore the shared logger so tests don't leak handlers."""
    handlers = list(logger.handlers)
    level = logger.level
    yield
    logger.handlers = handlers
    logger.setLevel(level)


class TestLogger:
    def test_logger_name(self):
        assert logger.name == "macp_sdk"

    def test_is_the_module_singleton(self):
        assert logging.getLogger("macp_sdk") is logger


class TestConfigureLogging:
    def test_adds_stream_handler_at_info(self):
        before = len(logger.handlers)
        configure_logging()
        added = logger.handlers[before:]
        assert len(added) == 1
        assert isinstance(added[0], logging.StreamHandler)
        assert logger.level == logging.INFO

    def test_honors_custom_level(self):
        configure_logging(level=logging.DEBUG)
        assert logger.level == logging.DEBUG

    def test_honors_custom_format(self):
        configure_logging(fmt="%(levelname)s :: %(message)s")
        handler = logger.handlers[-1]
        assert handler.formatter is not None
        record = logging.LogRecord(
            name="macp_sdk",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        assert handler.formatter.format(record) == "INFO :: hello"

    def test_default_format_includes_name_and_level(self):
        configure_logging()
        handler = logger.handlers[-1]
        assert handler.formatter is not None
        record = logging.LogRecord(
            name="macp_sdk",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="careful",
            args=(),
            exc_info=None,
        )
        out = handler.formatter.format(record)
        assert "macp_sdk" in out
        assert "WARNING" in out
        assert "careful" in out
