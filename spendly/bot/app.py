"""Telegram Application factory — Phase 7.

post_init order:
  1. Init DB
  2. DB health check
  3. Seed prompt versions
  4. AI health check
  5. Start scheduler (anomaly, check-ins, year-end)
  6. Send startup ping
"""

from __future__ import annotations

from datetime import UTC, datetime

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from spendly.bot.handlers import (
    cmd_health,
    cmd_set_budget,
    cmd_set_currency,
    cmd_set_tone,
    cmd_settings,
    cmd_start,
    error_handler,
    handle_callback,
    handle_message,
    handle_voice,
)
from spendly.core.config import settings
from spendly.core.logger import get_logger
from spendly.db.connection import get_connection, health_check, init_db

log = get_logger(__name__)


async def _post_init(application: Application) -> None:
    # 1. Init DB
    await init_db()

    # 2. DB health check + schema integrity
    db_result = await health_check()
    if not db_result["ok"]:
        raise RuntimeError(f"DB health check failed: {db_result['error']}")

    from spendly.db.connection import verify_schema

    schema_result = await verify_schema()
    if not schema_result["ok"]:
        missing = schema_result["missing_tables"]
        log.warning("Schema missing tables — re-running init_db", extra={"missing": missing})
        await init_db()  # attempt self-heal

    # 3. Seed prompts
    from spendly.ai.seeder import seed_prompts

    conn = await get_connection()
    try:
        await seed_prompts(conn)
    finally:
        await conn.close()

    # 4. AI health check (non-fatal)
    from spendly.ai.gateway import gateway

    ai_result = await gateway.health_check()
    if not ai_result["ok"]:
        log.error("AI health check failed at startup", extra={"error": ai_result["error"]})
        await application.bot.send_message(
            chat_id=settings.user_id,
            text=(f"Spendly started but Gemini is not responding.\nError: `{ai_result['error']}`"),
            parse_mode="Markdown",
        )

    # 5. Start scheduler
    from spendly.bot.scheduler import build_scheduler

    scheduler = build_scheduler(application)
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    application.bot_data["startup_time"] = datetime.now(UTC)

    # 6. Shared state init
    application.bot_data["owner_chat_id"] = settings.user_id
    application.bot_data["ai_healthy"] = ai_result["ok"]
    application.bot_data["logging_blocked"] = False

    log.info(
        "Spendly initialised",
        extra={
            "user_id": settings.user_id,
            "db_tables": db_result["tables"],
            "wal": db_result["wal"],
            "ai_ok": ai_result["ok"],
        },
    )

    if ai_result["ok"]:
        await application.bot.send_message(
            chat_id=settings.user_id,
            text="Spendly is online and ready.",
        )


async def _post_shutdown(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("Scheduler stopped")


def build_app() -> Application:
    # ── Error Monitoring ──────────────────────────────────────────────────────
    if settings.sentry_dsn:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=1.0)
        log.info("Sentry monitoring active (Bot)")

    app = (
        ApplicationBuilder()
        .token(settings.telegram_token)
        .get_updates_connect_timeout(10)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(15)
        .get_updates_pool_timeout(30)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("set_tone", cmd_set_tone))
    app.add_handler(CommandHandler("set_budget", cmd_set_budget))
    app.add_handler(CommandHandler("set_currency", cmd_set_currency))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)

    return app
