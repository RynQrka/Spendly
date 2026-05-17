"""Typed models for AI gateway inputs and outputs.

Flash always returns structured JSON. These dataclasses define
exactly what shape that JSON must take for each task type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── Input to the gateway ───────────────────────────────────────────────────────


@dataclass
class GatewayRequest:
    """Everything the gateway needs to make one Gemini call."""

    task: str  # e.g. "expense_parse", "intent_classify"
    prompt_name: str  # key into prompt_versions table
    user_message: str  # raw user input
    context: dict[str, Any] = field(default_factory=dict)  # date, history, patterns, etc.
    use_lite: bool = False  # True = Flash Lite, False = Flash
    schema: dict[str, Any] | None = None  # expected JSON schema for output validation


# ── Output from the gateway ───────────────────────────────────────────────────


@dataclass
class GatewayResponse:
    """Everything the gateway returns after a Gemini call."""

    ok: bool
    task: str
    model: str
    data: dict[str, Any]  # parsed JSON from Gemini
    raw_output: str  # raw text Gemini returned
    input_tokens: int
    output_tokens: int
    latency_ms: int
    retry_count: int
    prompt_version: str
    error: str | None = None
    hallucination_flag: bool = False
    disagreement_flag: bool = False


# ── Structured output schemas ─────────────────────────────────────────────────
# These define what Flash must return for each task.
# The gateway validates every response against its schema before returning.


INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["intent"],
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "EXPENSE_LOG",
                "INCOME_LOG",
                "QUERY",
                "CORRECTION",
                "INSIGHT",
                "EXPORT",
                "SUMMARY",
                "ACKNOWLEDGEMENT",
                "CLARIFICATION",
                "RECURRING_MANAGE",
                "WHAT_IF",
                "UNKNOWN",
            ],
        },
        "clarification_question": {"type": "string"},
        "confidence": {"type": "number"},
    },
}

RECURRING_MANAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["ADD", "UPDATE", "DELETE", "LIST"],
        },
        "merchant": {"type": "string"},
        "amount": {"type": "number"},
        "frequency": {
            "type": "string",
            "enum": ["daily", "weekly", "monthly", "yearly"],
        },
        "billing_day": {"type": "number"},
        "billing_month": {"type": "number"},
        "transaction_type": {"type": "string", "enum": ["expense", "income"]},
        "target_id": {"type": "number"},
        "confidence": {"type": "number"},
        "clarification_question": {"type": "string"},
    },
}

EXPENSE_PARSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["amount", "category"],
                "properties": {
                    "amount": {"type": "number"},
                    "category": {"type": "string"},
                    "merchant": {"type": "string"},
                    "expense_date": {"type": "string"},
                    "payment_method": {"type": "string"},
                    "note": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": "string"},
    },
}

INCOME_PARSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["amount", "source"],
                "properties": {
                    "amount": {"type": "number"},
                    "source": {"type": "string"},
                    "income_date": {"type": "string"},
                    "payment_method": {"type": "string"},
                    "note": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": "string"},
    },
}

QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["intent", "filters"],
    "properties": {
        "intent": {"type": "string"},
        "filters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "category": {"type": "string"},
                "merchant": {"type": "string"},
                "min_amount": {"type": "number"},
                "max_amount": {"type": "number"},
                "payment_method": {"type": "string"},
                "compare_date_from": {"type": "string"},
                "compare_date_to": {"type": "string"},
                "transaction_type": {
                    "type": "string",
                    "enum": ["expense", "income", "both"],
                },
            },
        },
        "output_format": {"type": "string"},
    },
}

REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["message"],
    "properties": {
        "message": {"type": "string"},
        "tone_used": {"type": "string"},
    },
}

INSIGHT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["insights"],
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["insight_type", "title", "body"],
                "properties": {
                    "insight_type": {"type": "string"},
                    "category": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "data": {"type": "object"},
                },
            },
        }
    },
}

HEALTH_CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status"],
    "properties": {
        "status": {"type": "string", "enum": ["ok"]},
        "message": {"type": "string"},
    },
}
