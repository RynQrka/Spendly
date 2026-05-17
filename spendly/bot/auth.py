"""Authorization guard.

Spendly is single-user. Every handler checks this before doing anything.
"""

from __future__ import annotations

from telegram import Update

from spendly.core.config import settings
from spendly.core.logger import get_logger

log = get_logger(__name__)


def is_authorized(update: Update) -> bool:
    """Return True only if the sender is the configured owner."""
    user = update.effective_user
    if user is None:
        return False
    authorized = user.id == settings.user_id
    if not authorized:
        log.warning(
            "Unauthorized access attempt",
            extra={"telegram_id": user.id, "username": user.username},
        )
    return authorized
