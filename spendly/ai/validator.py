"""Output validation for Gemini responses.

Three layers:
1. JSON parseable — raw string must be valid JSON
2. Schema valid — structure matches expected shape
3. Hallucination check — numbers in AI response match DB truth
"""

from __future__ import annotations

import json
import re
from typing import Any

from spendly.core.logger import get_logger

log = get_logger(__name__)

# ── JSON extraction ────────────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def extract_json(raw: str) -> dict[str, Any] | None:
    """Extract and parse JSON from a Gemini response string.

    Handles:
    - Plain JSON
    - JSON wrapped in ```json ... ``` fences
    - Leading/trailing whitespace
    Returns None if parsing fails.
    """
    text = raw.strip()

    # Try direct parse first
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Try extracting from code fence
    match = _JSON_FENCE_RE.search(text)
    if match:
        try:
            result = json.loads(match.group(1).strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    # Try finding first { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            result = json.loads(text[start : end + 1])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    log.warning("JSON extraction failed", extra={"raw_length": len(raw), "preview": raw[:100]})
    return None


# ── Schema validation ──────────────────────────────────────────────────────────


def validate_schema(data: dict[str, Any], schema: dict[str, Any]) -> tuple[bool, str | None]:
    """Lightweight structural schema validation — no heavy dependencies.

    Checks:
    - Required fields present
    - Field types match declared types
    Returns (is_valid, error_message).
    """
    required = schema.get("required", [])
    props = schema.get("properties", {})

    for field_name in required:
        if field_name not in data:
            return False, f"Missing required field: '{field_name}'"

    for field_name, field_schema in props.items():
        if field_name not in data:
            continue  # optional field, skip

        value = data[field_name]
        expected_type = field_schema.get("type")

        if expected_type == "string" and not isinstance(value, str) and value is not None:
            return False, f"Field '{field_name}' must be a string, got {type(value).__name__}"

        if expected_type == "number" and not isinstance(value, (int, float)) and value is not None:
            return False, f"Field '{field_name}' must be a number, got {type(value).__name__}"

        if expected_type == "boolean" and not isinstance(value, bool) and value is not None:
            return False, f"Field '{field_name}' must be a boolean, got {type(value).__name__}"

        if expected_type == "array" and not isinstance(value, list) and value is not None:
            return False, f"Field '{field_name}' must be an array, got {type(value).__name__}"

        if expected_type == "object" and not isinstance(value, dict) and value is not None:
            return False, f"Field '{field_name}' must be an object, got {type(value).__name__}"

        # Enum check
        if "enum" in field_schema and value is not None and value not in field_schema["enum"]:
            allowed = field_schema["enum"]
            return False, f"Field '{field_name}' must be one of {allowed}, got {value!r}"

    return True, None


# ── Hallucination detection ────────────────────────────────────────────────────


def check_hallucination(
    ai_data: dict[str, Any],
    db_total: float | None,
    tolerance_pct: float = 0.02,  # 2% tolerance for rounding
) -> bool:
    """Return True if AI response contains a total that disagrees with DB truth.

    Only checks if ai_data contains a recognisable total field and db_total is provided.
    """
    if db_total is None:
        return False

    # Look for total-like fields in the response
    candidates = ["total", "total_spend", "amount", "weekly_total", "monthly_total"]
    for key in candidates:
        ai_value = ai_data.get(key)
        if isinstance(ai_value, (int, float)) and ai_value > 0:
            diff_pct = abs(ai_value - db_total) / max(db_total, 1)
            if diff_pct > tolerance_pct:
                log.warning(
                    "Hallucination detected",
                    extra={
                        "field": key,
                        "ai_value": ai_value,
                        "db_value": db_total,
                        "diff_pct": round(diff_pct * 100, 2),
                    },
                )
                return True

    return False


# ── Disagreement detection ─────────────────────────────────────────────────────


def check_disagreement(
    ai_summary: dict[str, Any],
    db_records: list[dict[str, Any]],
) -> bool:
    """Return True if AI summary contradicts the underlying DB records.

    Computes the actual total from db_records and compares against any
    total field in ai_summary.
    """
    if not db_records:
        return False

    db_total = sum(float(r.get("amount", 0)) for r in db_records)
    return check_hallucination(ai_summary, db_total)
