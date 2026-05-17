"""Entry point — python -m spendly or uv run spendly."""

from __future__ import annotations

import asyncio

from spendly.core.config import settings
from spendly.core.logger import get_logger, setup_logging


def main() -> None:
    setup_logging(settings.log_level)
    log = get_logger(__name__)
    log.info("Starting Spendly", extra={"db": str(settings.db_path), "tz": str(settings.timezone)})

    from telegram import Bot

    from spendly.bot.app import build_app

    try:
        bot = Bot(token=settings.telegram_token)
        asyncio.run(bot.delete_webhook(drop_pending_updates=True))
        log.info("Deleted any existing Telegram webhook before polling")
    except Exception as exc:
        log.warning("Could not delete Telegram webhook before polling", extra={"error": str(exc)})

    app = build_app()
    app.run_polling(drop_pending_updates=True, timeout=30, bootstrap_retries=3)


if __name__ == "__main__":
    main()
