"""Spendly base exception hierarchy."""

from __future__ import annotations


class SpendlyError(Exception):
    """Root exception for all Spendly errors."""


class ConfigError(SpendlyError):
    """Raised when configuration is missing or invalid."""


class DatabaseError(SpendlyError):
    """Raised when a database operation fails."""


class LoggingBlockedError(SpendlyError):
    """Raised when expense logging is temporarily blocked (e.g. year-end rotation)."""


class AuthError(SpendlyError):
    """Raised when an unauthorized user sends a message."""
