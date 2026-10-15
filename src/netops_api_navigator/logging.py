"""Structured JSON logging via structlog."""

from __future__ import annotations

import logging
import sys

import structlog


class _DisconnectSafeStderr:
    """A closed MCP client's stderr pipe must not abort graph cleanup."""

    def __init__(self, stream):
        self._stream = stream
        self._disconnected = False

    def write(self, message):
        if not self._disconnected:
            try:
                return self._stream.write(message)
            except BrokenPipeError:
                self._disconnected = True
        return len(message)

    def flush(self):
        if not self._disconnected:
            try:
                self._stream.flush()
            except BrokenPipeError:
                self._disconnected = True


def setup_logging(level: str = "INFO") -> structlog.stdlib.BoundLogger:
    """Configure structlog for JSON output to stderr."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=_DisconnectSafeStderr(sys.stderr)),
        cache_logger_on_first_use=True,
    )

    # Silence noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return structlog.get_logger("netops-api-navigator")
