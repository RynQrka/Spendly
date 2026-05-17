"""Voice query handler - Phase 15.

Allows the user to send voice messages to the bot to log expenses or query their database
as if they were texting.
"""

from __future__ import annotations

from typing import Any

import aiosqlite
from google import genai
from google.genai import types

from spendly.ai.router import route
from spendly.core.config import settings
from spendly.core.logger import get_logger

log = get_logger(__name__)


async def process_voice_expense(
    conn: aiosqlite.Connection,
    user_id: int,
    audio_path: str,
    bot_data: dict[str, Any],
    telegram_context: Any | None = None,
) -> tuple[str, Any]:
    """Read voice and route as natural text."""
    client = genai.Client(api_key=settings.gemini_api_key)

    # 1. Transcribe audio using Gemini Flash directly (fast audio handling)
    try:
        log.info("Uploading audio for transcription", extra={"file": str(audio_path)})
        audio_file = client.files.upload(file=str(audio_path))

        log.info("Sending audio to Gemini")
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                types.Part.from_text(
                    "Transcribe this voice message accurately into text. "
                    "It likely contains financial tracking details or queries about expenses "
                    "and budgets. Do not respond to the audio, just transcribe it literally. "
                    "Return only the transcription."
                ),
                audio_file,
            ],
        )

        # We can delete immediately
        try:
            client.files.delete(name=audio_file.name)
        except Exception:
            log.warning("Failed to delete remote audio file", exc_info=True)

        transcribed_text = response.text.strip()
        log.info("Voice transcribed", extra={"text": transcribed_text})

        if not transcribed_text:
            return "I couldn't hear what you said. Try again?", None

        # 2. Route the text as if the user typed it
        return await route(conn, user_id, transcribed_text, bot_data, telegram_context)

    except Exception:
        log.error("Voice handler failed", exc_info=True)
        return "I'm having trouble processing that voice note right now.", None
