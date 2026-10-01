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
    kind: str = "expense"  # expense | income | saving (money put aside) | withdrawal (taken out of savings)
    goal: str | None = None  # name of the user's savings goal it goes to / comes from, if they said so


KINDS = ("expense", "income", "saving", "withdrawal")


@dataclass
class ParsedGoal:
    name: str
    target: Decimal | None
    currency: str
    deadline: date | None = None


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
    goals: list[str] = field(default_factory=list)  # the user's open savings goals, by name


_LANGUAGE_NAMES = {"ru": "Russian"}


def _schema(ctx: MappingContext) -> dict:
    props = {
        "kind": {"type": "string", "enum": list(KINDS)},
        "amount": {"type": "number", "description": "Positive amount"},
        "currency": {"type": "string", "description": "ISO 4217 code, e.g. KZT, USD"},
        "description": {
            "type": "string",
            "description": "What was bought, as a short generic English noun (1-3 words), e.g. 'Coffee', 'Taxi', 'Groceries'",
        },
        "merchant": {"type": "string", "description": "Shop/brand/service name as the user said it, else empty"},
        "category": {"type": "string", "enum": ctx.categories},
        "date": {"type": "string", "description": "YYYY-MM-DD"},
        "goal": (
            {"type": "string", "enum": [*ctx.goals, ""], "description": "Savings goal it belongs to, else empty"}
            if ctx.goals
            else {"type": "string", "description": "Always empty: the user has no savings goals"}
        ),
    }
    required = ["kind", "amount", "currency", "description", "merchant", "category", "date", "goal"]
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
    goals = (
        "The user's savings goals: " + "; ".join(f'"{g}"' for g in ctx.goals) + ". "
        "Set `goal` to the exact goal name when a saving or withdrawal is for one of them, else empty."
        if ctx.goals
        else "The user has no savings goals: `goal` is always empty."
    )
    return f"""You extract personal money records from short messages: spending, income and savings.
The user writes or speaks casually, in English, Russian or Kazakh, possibly mixing languages.
Today is {today.isoformat()} ({today.strftime('%A')}) in the user's time zone.

Rules:
- One message may contain several records; return each separately.
- `kind` of each record:
  - expense: money spent on goods, services, bills, gifts given, fees (the usual case);
  - income: money received — salary, bonus, freelance or business payment, gift received, interest, cashback
    ("зарплата 600000", "got paid 400k", "аванс пришёл");
  - saving: money put aside into savings, a deposit, a piggy bank or towards a goal
    ("отложил 50000", "put 100k into savings", "на депозит 200к", "в копилку на машину 30к");
  - withdrawal: money taken back out of savings or a deposit ("снял 20000 с депозита", "took 50k from savings").
  A refund, a transfer between the user's own everyday accounts, or a loan given/repaid is NOT a record.
- {goals}
- Amounts: understand "1.5k", "2к", "полторы тысячи", "5 штук" etc. Return a plain number.
- If no currency is mentioned, use {config.DEFAULT_CURRENCY}. "тг", "тенге", "₸" = KZT; "$", "bucks" = USD; "руб" = RUB.
- Dates: default to today. Resolve relative dates ("yesterday", "вчера", "on Monday") against today. Never return a future date.
- For expenses, pick the single best category from this list (for income/saving/withdrawal use "Other"):
{ctx.guide}{known}
- If the message contains no money records at all (a greeting, a question), return an empty expenses list.
- `description` for non-expenses: a short English noun such as 'Salary', 'Bonus', 'Savings', 'Deposit'.
- `description` is always in English (it is a dictionary key); {label_rule}"""


def _to_expenses(data: dict, today: date, categories: list[str], goals: list[str] | None = None) -> list[ParsedExpense]:
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
        kind = item.get("kind") if item.get("kind") in KINDS else "expense"
        goal = item.get("goal") if kind in ("saving", "withdrawal") and item.get("goal") in (goals or []) else None
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
                kind=kind,
                goal=goal,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Providers                                                                   #
# --------------------------------------------------------------------------- #

_clients: dict = {}


def _record_usage(input_tokens, output_tokens):
    """Put the call's token counts on the interaction being logged (log_interactions.llm_*_tokens)."""
    from interactions import note

    def n(v):
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    note(llm_input_tokens=n(input_tokens), llm_output_tokens=n(output_tokens))


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
    meta = getattr(response, "usage_metadata", None)
    _record_usage(getattr(meta, "prompt_token_count", None), getattr(meta, "candidates_token_count", None))
    return json.loads(response.text)


async def _openai(today: date, ctx: MappingContext, text: str) -> dict:
    """ChatGPT, with strict JSON-schema output."""
    return await _structured(_system_prompt(today, ctx), text, _schema(ctx), "expenses", "", provider="openai")


def _claude_client():
    if "claude" not in _clients:
        from anthropic import AsyncAnthropic

        _clients["claude"] = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _clients["claude"]


async def _claude(today: date, ctx: MappingContext, text: str) -> dict:
    """Claude, forced to answer through a tool whose input schema is the expense schema."""
    data = await _claude_tool(
        _system_prompt(today, ctx), text, _schema(ctx), "record_expenses",
        "Record the money records found in the user's message (may be an empty list).",
    )
    return data if data is not None else {"transcript": "", "expenses": []}


async def _claude_tool(system: str, text: str, schema: dict, name: str, description: str) -> dict | None:
    response = await _claude_client().messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1024,
        system=system,
        tools=[{"name": name, "description": description, "input_schema": schema}],
        tool_choice={"type": "tool", "name": name},
        messages=[{"role": "user", "content": text}],
    )
    usage = getattr(response, "usage", None)
    _record_usage(getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))
    for block in response.content:
        if block.type == "tool_use":
            return dict(block.input)
    return None


async def _structured(system: str, text: str, schema: dict, name: str, description: str,
                      provider: str | None = None) -> dict | None:
    """One text-only call to the model (the configured one by default), answered as JSON matching `schema`."""
    provider = provider or config.LLM_PROVIDER
    if provider == "claude":
        return await _claude_tool(system, text, schema, name, description)
    if provider == "gemini":
        from google.genai import types

        response = await _gemini_client().aio.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[text],
            config=types.GenerateContentConfig(
                system_instruction=system, response_mime_type="application/json",
                response_json_schema=schema, temperature=0,
            ),
        )
        meta = getattr(response, "usage_metadata", None)
        _record_usage(getattr(meta, "prompt_token_count", None), getattr(meta, "candidates_token_count", None))
        return json.loads(response.text)
    response = await _openai_client().chat.completions.create(
        model=config.OPENAI_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
        response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
    )
    usage = getattr(response, "usage", None)
    _record_usage(getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None))
    return json.loads(response.choices[0].message.content)


_GOAL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Short goal name in the user's own words and language, 1-4 words"},
        "target": {"type": "number", "description": "Target amount; 0 if none was given"},
        "currency": {"type": "string", "description": "ISO 4217 code"},
        "deadline": {"type": "string", "description": "YYYY-MM-DD, or empty if none was given"},
    },
    "required": ["name", "target", "currency", "deadline"],
    "additionalProperties": False,
}


async def parse_goal(text: str, sent_at: datetime | None = None) -> ParsedGoal | None:
    """'Trip to Japan 2 million by June' -> a savings goal. None if the text names no goal."""
    today = (sent_at.astimezone(config.TIMEZONE) if sent_at else datetime.now(config.TIMEZONE)).date()
    system = f"""The user is creating a savings goal. Extract its name, target amount, currency and deadline.
They write casually in English, Russian or Kazakh. Today is {today.isoformat()}.
- Amounts: understand "2 млн", "1.5m", "500к". No currency mentioned = {config.DEFAULT_CURRENCY}; "тг"/"тенге"/"₸" = KZT, "$" = USD.
- Deadline: resolve "by June", "к лету", "через год" against today to a date (end of that month/season); empty if none.
- If the text has no goal in it at all, return an empty name."""
    data = await _structured(system, text, _GOAL_SCHEMA, "savings_goal", "Record the savings goal.")
    if not data or not (data.get("name") or "").strip():
        return None
    try:
        target = Decimal(str(data.get("target") or 0)).quantize(Decimal("0.01"))
    except InvalidOperation:
        target = Decimal(0)
    try:
        deadline = date.fromisoformat(data.get("deadline") or "")
    except ValueError:
        deadline = None
    if deadline is not None and (deadline <= today or deadline > today + timedelta(days=365 * 30)):
        deadline = None
    return ParsedGoal(
        name=" ".join(data["name"].split())[:40],
        target=target if target > 0 else None,
        currency=(data.get("currency") or config.DEFAULT_CURRENCY).upper()[:3],
        deadline=deadline,
    )


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
        expenses=_to_expenses(data, today, ctx.categories, ctx.goals),
        transcript=(data.get("transcript") or "").strip() or None,
    )
