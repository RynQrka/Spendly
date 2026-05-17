"""Project-wide system constants.

These are internal system values — not user-configurable.
All user-facing settings (budget, thresholds, API keys, paths) live in .env
and are loaded via core/config.py.
"""

from __future__ import annotations

# ── Moods ─────────────────────────────────────────────────────────────────────

MOODS: dict[str, str] = {
    "great": "⭐ (Great Value)",
    "neutral": "😐 (Neutral)",
    "regret": "💸 (Regret)",
}

# ── Expense categories ─────────────────────────────────────────────────────────

CATEGORIES: tuple[str, ...] = (
    "Food",
    "Transport",
    "Shopping",
    "Bills",
    "Subscription",
    "Entertainment",
    "Health",
    "Travel",
    "Education",
    "Other",
)

# ── Intent types ───────────────────────────────────────────────────────────────


class Intent:
    EXPENSE_LOG = "EXPENSE_LOG"
    INCOME_LOG = "INCOME_LOG"
    QUERY = "QUERY"
    CORRECTION = "CORRECTION"
    INSIGHT = "INSIGHT"
    EXPORT = "EXPORT"
    SUMMARY = "SUMMARY"
    CLARIFICATION = "CLARIFICATION"
    ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
    SMS_LOG = "SMS_LOG"
    RECURRING_MANAGE = "RECURRING_MANAGE"
    WHAT_IF = "WHAT_IF"
    UNKNOWN = "UNKNOWN"


# ── Gemini tone pool ───────────────────────────────────────────────────────────

# ── Gemini tone pool ───────────────────────────────────────────────────────────

DEFAULT_TONE = "witty_sarcastic"
REMOVED_TONES: frozenset[str] = frozenset({"humble_encouraging", "elder_brother", "zen_monk"})

TONES: tuple[str, ...] = (
    "witty_sarcastic",
    "financial_advisor",
    "savage_roaster",
    "strict_accountant",
    "desi_parent",
    "gym_trainer",
    "detective",
    "therapist",
    "minimalist",
)

TONE_PROMPTS: dict[str, str] = {
    "savage_roaster": (
        "You are brutally honest and roast the user for bad financial decisions. "
        "Be witty, sarcastic, and slightly mean — but keep it fun and never abusive. "
        "Use memes and humor to point out wasteful spending."
    ),
    "financial_advisor": (
        "You are a professional, high-end financial advisor. Be structured, precise, analytical, "
        "and insightful. Focus on long-term wealth and ROI. Use technical terms correctly."
    ),
    "strict_accountant": (
        "Focus strictly on numbers, discipline, and financial correctness. You hate waste. "
        "Be formal, objective, and blunt about budget leaks. No emotional talk."
    ),
    "desi_parent": (
        "Sound like a typical strict Indian parent. Be dramatic, worry about "
        "'what people will think' of these gastos, and focus heavily on "
        "saving for the future. Use a bit of 'scolding' love."
    ),
    "gym_trainer": (
        "Treat finance like a workout. Talk about financial 'gains', "
        "'cutting the fat' from the budget, and staying consistent with "
        "the routine. Be high-energy and aggressive about discipline."
    ),
    "detective": (
        "Analyze spending like a detective solving a complex case. Look for 'suspicious patterns', "
        "'clues' as to where the money went, and 'interrogate' the necessity of large expenses."
    ),
    "therapist": (
        "Focus on the emotional reasons behind spending. Be calm, deep, understanding, "
        "and reflective. Help the user understand their feelings about money and value."
    ),
    "minimalist": (
        "Extremely brief and objective. No greetings, no emojis, only facts and the confirmation "
        "of the action taken. Perfect for power users."
    ),
    "witty_sarcastic": (
        "A blend of humor and intelligence. "
        "You celebrate good deals with wit and mock poor choices with "
        "sharp (but funny) sarcasm."
    ),
}


def normalize_tone(tone: str | None) -> str:
    if not tone or tone in REMOVED_TONES or tone not in TONE_PROMPTS:
        return DEFAULT_TONE
    return tone


# ── Gemini model identifiers ───────────────────────────────────────────────────

GEMINI_FLASH = "gemini-3-flash-preview"

# ── System internals — not user-configurable ──────────────────────────────────

LOW_CONFIDENCE_CUTOFF = 0.70  # below this, Gemini asks a clarifying question
CONVERSATION_HISTORY_N = 10  # turns kept in context window per Gemini call
MERCHANT_MEMORY_MIN_HITS = 2  # min occurrences before merchant mapping is trusted

# ── DB & year-end rotation ────────────────────────────────────────────────────

DB_FILENAME = "expense.db"
ARCHIVE_SUFFIX = "expense{year}.db"  # e.g. expense2025.db
YEAREND_TRIGGER_HOUR = 23
YEAREND_TRIGGER_MIN = 58

# ── Validation limits ─────────────────────────────────────────────────────────

MAX_EXPENSE_AMOUNT = 10_000_000  # absolute sanity cap on any single expense (INR)
MIN_EXPENSE_AMOUNT = 0.01
