"""Turns a text or voice message into structured expenses with a guessed category."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import config

log = logging.getLogger(__name__)


@dataclass
class ParsedExpense:
    amount: Decimal
    currency: str
    description: str
    category: str
    expense_date: date
    merchant: str | None = None
    label: str | None = None  # the item in the user's language, for display (description stays English)


@dataclass
class ParseResult:
    expenses: list[ParsedExpense] = field(default_factory=list)
    transcript: str | None = None  # what the model heard, for voice messages


@dataclass
class MappingContext:
    """What the shared dictionary contributes to one mapping."""

    categories: list[str]  # active category names (dim_categories)
    guide: str  # "- Name: description" lines
    examples: str = ""  # "variant (type) -> Category" lines from dim_spend_variants
    language: str = "en"  # the user's language; "ru" adds a Russian display label per item


_LANGUAGE_NAMES = {"ru": "Russian"}


def _schema(ctx: MappingContext) -> dict:
    props = {
        "amount": {"type": "number", "description": "Positive amount spent"},
        "currency": {"type": "string", "description": "ISO 4217 code, e.g. KZT, USD"},
        "description": {
            "type": "string",
            "description": "What was bought, as a short generic English noun (1-3 words), e.g. 'Coffee', 'Taxi', 'Groceries'",
        },
        "merchant": {"type": "string", "description": "Shop/brand/service name as the user said it, else empty"},
        "category": {"type": "string", "enum": ctx.categories},
        "date": {"type": "string", "description": "YYYY-MM-DD"},
    }
    required = ["amount", "currency", "description", "merchant", "category", "date"]
    if ctx.language in _LANGUAGE_NAMES:
        lang = _LANGUAGE_NAMES[ctx.language]
        props["label"] = {
            "type": "string",
            "description": f"The same item as `description`, in {lang}, 1-3 words (e.g. for 'Coffee': 'Кофе')",
        }
        required.append("label")
    return {
        "type": "object",
        "properties": {
            "transcript": {
                "type": "string",
                "description": "Verbatim transcript of the audio. Empty string for text input.",
            },
            "expenses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        },
        "required": ["transcript", "expenses"],
        "additionalProperties": False,
    }


def _system_prompt(today: date, ctx: MappingContext) -> str:
    known = (
        f"""
- Known spend variants, confirmed by users. When a merchant or item matches one, use its category:
{ctx.examples}"""
        if ctx.examples
        else ""
    )
    label_rule = (
        f"`label` is the same item in {_LANGUAGE_NAMES[ctx.language]}, for display."
        if ctx.language in _LANGUAGE_NAMES
        else "there is no separate label."
    )
    return f"""You extract personal spending records from short messages.
The user writes or speaks casually, in English, Russian or Kazakh, possibly mixing languages.
Today is {today.isoformat()} ({today.strftime('%A')}) in the user's time zone.

Rules:
- One message may contain several expenses; return each separately.
- Amounts: understand "1.5k", "2к", "полторы тысячи", "5 штук" etc. Return a plain number.
- If no currency is mentioned, use {config.DEFAULT_CURRENCY}. "тг", "тенге", "₸" = KZT; "$", "bucks" = USD; "руб" = RUB.
- Dates: default to today. Resolve relative dates ("yesterday", "вчера", "on Monday") against today. Never return a future date.
- Pick the single best category from this list:
{ctx.guide}{known}
- If the message contains no spending at all (a greeting, a question), return an empty expenses list.
- Income, refunds and transfers between own accounts are NOT expenses.
- `description` is always in English (it is a dictionary key); {label_rule}"""


def _to_expenses(data: dict, today: date, categories: list[str]) -> list[ParsedExpense]:
    out: list[ParsedExpense] = []
    for item in data.get("expenses", []):
        try:
            amount = Decimal(str(item["amount"])).quantize(Decimal("0.01"))
        except (InvalidOperation, KeyError, TypeError):
            log.warning("Skipping item with bad amount: %s", item)
            continue
        if amount <= 0:
            continue
        try:
            d = date.fromisoformat(item.get("date", ""))
        except ValueError:
            d = today
        if d > today or d < today - timedelta(days=366):
            d = today
        category = item.get("category")
        if category not in categories:
            category = "Other" if "Other" in categories else categories[-1]
        out.append(
            ParsedExpense(
                amount=amount,
                currency=(item.get("currency") or config.DEFAULT_CURRENCY).upper()[:3],
                description=(item.get("description") or "").strip()[:200] or "Expense",
                merchant=(item.get("merchant") or "").strip()[:100] or None,
                category=category,
                expense_date=d,
                label=(item.get("label") or "").strip()[:100] or None,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Providers                                                                   #
# --------------------------------------------------------------------------- #

_clients: dict = {}


def _gemini_client():
    if "gemini" not in _clients:
        from google import genai

        if config.GEMINI_API_KEY:
            _clients["gemini"] = genai.Client(api_key=config.GEMINI_API_KEY)
        else:
            _clients["gemini"] = genai.Client(
                vertexai=True, project=config.GCP_PROJECT, location=config.VERTEX_LOCATION
            )
    return _clients["gemini"]


def _openai_client():
    if "openai" not in _clients:
        from openai import AsyncOpenAI

        _clients["openai"] = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _clients["openai"]


async def _gemini(today: date, ctx: MappingContext, text: str | None, audio: bytes | None, audio_mime: str) -> dict:
    """Gemini hears audio natively, so voice is one call: transcribe + extract."""
    from google.genai import types

    if audio is not None:
        contents = [
            types.Part.from_bytes(data=audio, mime_type=audio_mime),
            "Transcribe this voice note verbatim, in the language it is spoken in (do not translate), "
            "and extract the expenses from it.",
        ]
    else:
        contents = [text or ""]
    response = await _gemini_client().aio.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=_system_prompt(today, ctx),
            response_mime_type="application/json",
            response_json_schema=_schema(ctx),
            temperature=0,
        ),
    )
    return json.loads(response.text)


async def _openai(today: date, ctx: MappingContext, text: str) -> dict:
    """ChatGPT, with strict JSON-schema output."""
    response = await _openai_client().chat.completions.create(
        model=config.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": _system_prompt(today, ctx)},
            {"role": "user", "content": text},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "expenses", "strict": True, "schema": _schema(ctx)},
        },
    )
    return json.loads(response.choices[0].message.content)


def _claude_client():
    if "claude" not in _clients:
        from anthropic import AsyncAnthropic

        _clients["claude"] = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _clients["claude"]


async def _claude(today: date, ctx: MappingContext, text: str) -> dict:
    """Claude, forced to answer through a tool whose input schema is the expense schema."""
    response = await _claude_client().messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1024,
        system=_system_prompt(today, ctx),
        tools=[
            {
                "name": "record_expenses",
                "description": "Record the expenses found in the user's message (may be an empty list).",
                "input_schema": _schema(ctx),
            }
        ],
        tool_choice={"type": "tool", "name": "record_expenses"},
        messages=[{"role": "user", "content": text}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return dict(block.input)
    return {"transcript": "", "expenses": []}


async def parse(
    text: str | None = None,
    audio: bytes | None = None,
    audio_mime: str = "audio/ogg",
    sent_at: datetime | None = None,
    ctx: MappingContext | None = None,
) -> ParseResult:
    """Parse a text message or a voice note. Exactly one of text/audio should be given.

    `sent_at` is when the user sent the message. When the bot runs on a laptop
    that was asleep, Telegram delivers queued messages later, so "today" must
    be the day the message was sent, not the day it's processed.
    """
    if ctx is None:  # no dictionary available: fall back to the seed list
        ctx = MappingContext(
            categories=config.CATEGORIES,
            guide="\n".join(f"- {c}: {config.CATEGORY_HINTS.get(c, '')}" for c in config.CATEGORIES),
        )
    today = (sent_at.astimezone(config.TIMEZONE) if sent_at else datetime.now(config.TIMEZONE)).date()

    if config.LLM_PROVIDER == "gemini":
        # Gemini hears audio itself: one call does transcription + extraction.
        data = await _gemini(today, ctx, text, audio, audio_mime)
    else:
        transcript = None
        if audio is not None:
            import transcribe

            transcript = await transcribe.transcribe(audio, audio_mime, ctx.language)
            text = transcript
        if not (text or "").strip():
            data = {"expenses": []}
        else:
            call = _claude if config.LLM_PROVIDER == "claude" else _openai
            data = await call(today, ctx, text)
        data["transcript"] = transcript or ""  # the real transcript, not a model echo

    return ParseResult(
        expenses=_to_expenses(data, today, ctx.categories),
        transcript=(data.get("transcript") or "").strip() or None,
    )
