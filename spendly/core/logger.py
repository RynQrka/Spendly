"""Structured logging for Spendly.

Single-line format:  LEVEL | module | message  [key=val ...]
"""

from __future__ import annotations

import logging
import sys
from typing import Any, ClassVar


class _StructuredFormatter(logging.Formatter):
    _LEVELS: ClassVar[dict[int, str]] = {
        logging.DEBUG: "DEBUG",
        logging.INFO: " INFO",
        logging.WARNING: " WARN",
        logging.ERROR: "ERROR",
        logging.CRITICAL: " CRIT",
    }

    # Standard LogRecord attributes — excluded from the kv tail
    _STD: ClassVar[frozenset[str]] = frozenset(
        {
            "name",
            "msg",
            "args",
            "created",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "message",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
            "exc_info",
            "exc_text",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        level = self._LEVELS.get(record.levelno, record.levelname)
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        base = f"{ts} {level} | {record.name} | {record.getMessage()}"

        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)

        kv: dict[str, Any] = {
            k: v for k, v in record.__dict__.items() if k not in self._STD and not k.startswith("_")
        }
        if kv:
            base += "  [" + " ".join(f"{k}={v!r}" for k, v in kv.items()) + "]"

        return base


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger. Call once at startup."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))

    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(_StructuredFormatter())
        root.addHandler(h)

    # Silence chatty third-party loggers
    for noisy in ("httpx", "httpcore", "telegram", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
