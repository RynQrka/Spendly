"""AI Gateway — single entry point for every Gemini API call.

ALL AI calls in Spendly go through here. Nothing calls Gemini directly.

Responsibilities:
- Build the prompt from template + context
- Choose the right model (Flash vs Flash Lite)
- Enforce structured JSON output
- Retry on invalid output or transient errors
- Log every call to ai_logs table
- Detect hallucinations and disagreements
- Rate limiting
- Return a typed GatewayResponse
"""

from __future__ import annotations

import time
from typing import Any

from google import genai
from google.genai import types as genai_types

from spendly.ai.models import (
    EXPENSE_PARSE_SCHEMA,
    HEALTH_CHECK_SCHEMA,
    INCOME_PARSE_SCHEMA,
    INSIGHT_SCHEMA,
    INTENT_SCHEMA,
    QUERY_SCHEMA,
    RECURRING_MANAGE_SCHEMA,
    REPLY_SCHEMA,
    GatewayRequest,
    GatewayResponse,
)
from spendly.ai.prompts import PROMPT_MAP
from spendly.ai.validator import check_hallucination, extract_json, validate_schema
from spendly.core.config import settings
from spendly.core.constants import GEMINI_FLASH
from spendly.core.logger import get_logger

log = get_logger(__name__)

# Schema lookup by prompt name
_SCHEMA_MAP: dict[str, dict[str, Any]] = {
    "intent_classify": INTENT_SCHEMA,
    "expense_parse": EXPENSE_PARSE_SCHEMA,
    "income_parse": INCOME_PARSE_SCHEMA,
    "query_build": QUERY_SCHEMA,
    "reply_format": REPLY_SCHEMA,
    "insight_generate": INSIGHT_SCHEMA,
    "anomaly_check": INSIGHT_SCHEMA,  # same shape — alerts array
    "correction_parse": INTENT_SCHEMA,  # validated loosely
    "monthly_reflection": REPLY_SCHEMA,  # validated loosely
    "recurring_manage": RECURRING_MANAGE_SCHEMA,
    "health_check": HEALTH_CHECK_SCHEMA,
}


class AIGateway:
    """Central Gemini interface. One instance lives for the lifetime of the bot."""

    def __init__(self) -> None:
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._flash = GEMINI_FLASH
        log.info("AI gateway initialised", extra={"flash": self._flash})

    # ── Public API ─────────────────────────────────────────────────────────────

    async def call(
        self,
        request: GatewayRequest,
        db_conn: Any | None = None,  # aiosqlite.Connection for logging
        db_total: float | None = None,  # for hallucination check
    ) -> GatewayResponse:
        """Make a single AI call with retry, validation, and logging.

        Returns a GatewayResponse. ok=False means all retries failed.
        """
        model_id = self._flash
        schema = request.schema or _SCHEMA_MAP.get(request.prompt_name, {})
        template = PROMPT_MAP.get(request.prompt_name)
        prompt_version = template.version if template else "unknown"

        built_prompt = self._build_prompt(request)
        retry_count = 0
        last_error = ""

        for attempt in range(settings.ai_max_retries + 1):
            retry_count = attempt
            t0 = time.monotonic()

            try:
                raw_output, in_tok, out_tok = await self._call_gemini(model_id, built_prompt)
            except Exception as exc:
                last_error = f"Gemini API error: {exc}"
                log.warning(
                    "Gemini call failed",
                    extra={"attempt": attempt + 1, "error": str(exc)[:200]},
                )
                if attempt < settings.ai_max_retries:
                    continue
                break

            latency_ms = int((time.monotonic() - t0) * 1000)

            # Extract JSON
            data = extract_json(raw_output)
            if data is None:
                last_error = "Response is not valid JSON"
                log.warning(
                    "JSON extraction failed",
                    extra={"attempt": attempt + 1, "preview": raw_output[:120]},
                )
                if attempt < settings.ai_max_retries:
                    continue
                break

            # Schema validation
            if schema:
                valid, err = validate_schema(data, schema)
                if not valid:
                    last_error = f"Schema validation failed: {err}"
                    log.warning(
                        "Schema validation failed",
                        extra={"attempt": attempt + 1, "error": err},
                    )
                    if attempt < settings.ai_max_retries:
                        continue
                    break

            # Hallucination check
            hallucination = check_hallucination(data, db_total)
            if hallucination:
                log.error(
                    "Hallucination detected — response flagged",
                    extra={"task": request.task, "db_total": db_total},
                )

            # Success
            response = GatewayResponse(
                ok=True,
                task=request.task,
                model=model_id,
                data=data,
                raw_output=raw_output,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=latency_ms,
                retry_count=retry_count,
                prompt_version=prompt_version,
                hallucination_flag=hallucination,
            )

            # Log to DB
            if db_conn is not None:
                await self._log_to_db(db_conn, request, response)

            log.info(
                "AI call success",
                extra={
                    "task": request.task,
                    "model": model_id,
                    "latency_ms": latency_ms,
                    "retries": retry_count,
                    "tokens_in": in_tok,
                    "tokens_out": out_tok,
                },
            )
            return response

        # All retries exhausted
        response = GatewayResponse(
            ok=False,
            task=request.task,
            model=model_id,
            data={},
            raw_output="",
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            retry_count=retry_count,
            prompt_version=prompt_version,
            error=last_error,
        )

        if db_conn is not None:
            await self._log_to_db(db_conn, request, response)

        log.error(
            "AI call failed after all retries",
            extra={"task": request.task, "error": last_error, "retries": retry_count},
        )
        return response

    # ── Health check ───────────────────────────────────────────────────────────

    async def health_check(self) -> dict[str, object]:
        """Ping Gemini and validate it returns correct structured output.

        Returns dict with: ok, model, latency_ms, error
        """
        from spendly.ai.models import GatewayRequest

        req = GatewayRequest(
            task="health_check",
            prompt_name="health_check",
            user_message="",
            use_lite=True,
            schema=HEALTH_CHECK_SCHEMA,
        )

        t0 = time.monotonic()
        resp = await self.call(req)
        latency_ms = int((time.monotonic() - t0) * 1000)

        ok = resp.ok and resp.data.get("status") == "ok"

        result: dict[str, object] = {
            "ok": ok,
            "model": self._flash,
            "latency_ms": latency_ms,
            "error": resp.error if not ok else None,
        }

        if ok:
            log.info("AI health check passed", extra={"latency_ms": latency_ms})
        else:
            log.error("AI health check failed", extra=result)

        return result

    # ── Prompt builder ─────────────────────────────────────────────────────────

    def _build_prompt(self, request: GatewayRequest) -> str:
        """Fill template placeholders with context values."""
        template = PROMPT_MAP.get(request.prompt_name)
        if template is None:
            # No template — use raw user message
            return request.user_message

        prompt = template.content

        # Always available substitutions
        base_context: dict[str, str] = {
            "user_message": request.user_message,
            **{k: str(v) for k, v in request.context.items()},
        }

        for key, val in base_context.items():
            prompt = prompt.replace(f"{{{key}}}", val)

        return prompt

    # ── Gemini API call ────────────────────────────────────────────────────────

    async def _call_gemini(self, model_id: str, prompt: str) -> tuple[str, int, int]:
        """Make the actual Gemini API call. Returns (raw_text, input_tokens, output_tokens)."""
        response = await self._client.aio.models.generate_content(
            model=model_id,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.3,  # low temperature for consistent structured output
                max_output_tokens=2048,
                response_mime_type="application/json",
            ),
        )

        raw_text = response.text or ""
        in_tok = response.usage_metadata.prompt_token_count or 0
        out_tok = response.usage_metadata.candidates_token_count or 0

        return raw_text, in_tok, out_tok

    # ── DB logging ─────────────────────────────────────────────────────────────

    async def _log_to_db(
        self,
        conn: Any,
        request: GatewayRequest,
        response: GatewayResponse,
    ) -> None:
        """Write AI call record to ai_logs table."""
        import json as _json
        from datetime import UTC, datetime

        user_id = None
        if hasattr(request, "user_id") and request.user_id is not None:
            user_id = request.user_id
        elif isinstance(request.context, dict) and "user_id" in request.context:
            try:
                user_id = int(request.context["user_id"])
            except (ValueError, TypeError):
                pass

        try:
            await conn.execute(
                """
                INSERT INTO ai_logs (
                    user_id, model, prompt_version, input_tokens, output_tokens,
                    latency_ms, intent, raw_input, raw_output, parsed_output,
                    is_valid, retry_count, error,
                    hallucination_flag, disagreement_flag, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    response.model,
                    response.prompt_version,
                    response.input_tokens,
                    response.output_tokens,
                    response.latency_ms,
                    request.task,
                    request.user_message[:2000],  # cap prompt length in log
                    response.raw_output[:2000],
                    _json.dumps(response.data) if response.data else None,
                    1 if response.ok else 0,
                    response.retry_count,
                    response.error,
                    1 if response.hallucination_flag else 0,
                    1 if response.disagreement_flag else 0,
                    datetime.now(UTC).isoformat(),
                ),
            )
            await conn.commit()
        except Exception:
            log.error("Failed to write to ai_logs", exc_info=True)


# ── Module-level singleton ─────────────────────────────────────────────────────
# Instantiated once. All handlers import and use this instance.

gateway = AIGateway()
