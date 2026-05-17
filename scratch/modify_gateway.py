import re
from pathlib import Path

file_path = Path("spendly/ai/gateway.py")
content = file_path.read_text(encoding="utf-8")

# Let's locate _log_to_db and replace it
target_pattern = """    async def _log_to_db(
        self,
        conn: Any,
        request: GatewayRequest,
        response: GatewayResponse,
    ) -> None:
        \"\"\"Write AI call record to ai_logs table.\"\"\"
        import json as _json
        from datetime import UTC, datetime

        try:
            await conn.execute(
                \"\"\"
                INSERT INTO ai_logs (
                    model, prompt_version, input_tokens, output_tokens,
                    latency_ms, intent, raw_input, raw_output, parsed_output,
                    is_valid, retry_count, error,
                    hallucination_flag, disagreement_flag, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                \"\"\",
                (
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
            )"""

replacement = """    async def _log_to_db(
        self,
        conn: Any,
        request: GatewayRequest,
        response: GatewayResponse,
    ) -> None:
        \"\"\"Write AI call record to ai_logs table.\"\"\"
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
                \"\"\"
                INSERT INTO ai_logs (
                    user_id, model, prompt_version, input_tokens, output_tokens,
                    latency_ms, intent, raw_input, raw_output, parsed_output,
                    is_valid, retry_count, error,
                    hallucination_flag, disagreement_flag, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                \"\"\",
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
            )"""

# Normalize line endings for replacement to work flawlessly on both LF and CRLF
norm_content = content.replace("\r\n", "\n")
norm_target = target_pattern.replace("\r\n", "\n")
norm_replacement = replacement.replace("\r\n", "\n")

if norm_target in norm_content:
    new_content = norm_content.replace(norm_target, norm_replacement)
    # Restore original line endings (CRLF if original content had it)
    if "\r\n" in content:
        new_content = new_content.replace("\n", "\r\n")
    file_path.write_text(new_content, encoding="utf-8")
    print("Success: AIGateway updated successfully.")
else:
    print("Error: Target pattern not found in gateway.py.")
