"""Telegram expense tracker: text/voice in -> guessed category -> confirm -> BigQuery."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
import dashboard
import extractor
import i18n
import savings
from catalog import Catalog
from household import Households
from i18n import fmt_date, fmt_day, fmt_month, t
from interactions import InteractionLogger, logged, note, record, set_logger
from storage import PendingStore, Warehouse, build_row
from users import UserDirectory

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("expense-bot")

pending = PendingStore()
warehouse = Warehouse()
catalog = Catalog(warehouse)
household = Households(warehouse)
goals = savings.Goals(warehouse)
users = UserDirectory(warehouse.client, warehouse.ds)
context_flags: dict = {}


def is_admin(user_id: int | None) -> bool:
    """The bot's owner (OWNER_USER_ID): no daily limits, 👥 Users, /block."""
    return user_id is not None and user_id == config.OWNER_USER_ID


def can_use(user_id: int | None) -> bool:
    """Anyone can use the bot unless the owner blocked them."""
    return user_id is not None and not pending.is_blocked(user_id)


def role_of(user_id: int | None) -> str:
    """For dim_users.role."""
    if is_admin(user_id):
        return "owner"
    if pending.is_blocked(user_id):
        return "blocked"
    m = household.member(user_id)
    if m:
        return "household_owner" if m.role == "owner" else "member"
    return "none"


STARTED_AT = datetime.now(timezone.utc)
_last_update: dict = {"at": None}


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs before every handler: keeps dim_users current, in the background (never delays a reply)."""
    _last_update["at"] = datetime.now(timezone.utc)
    network_back()
    user = update.effective_user
    if user is not None:
        asyncio.create_task(users.observe(user, role_of(user.id), pending.get_language(user.id)))

CURRENCY_SIGNS = {"KZT": "₸", "USD": "$", "EUR": "€", "RUB": "₽", "GBP": "£"}


# --------------------------------------------------------------------------- #
# Language                                                                    #
# --------------------------------------------------------------------------- #


def lang_for(user) -> str:
    """The user's chosen language; on first contact, guessed from their Telegram app language."""
    if user is None:
        return i18n.DEFAULT_LANGUAGE
    lang = pending.get_language(user.id)
    if lang is None:
        lang = i18n.detect(getattr(user, "language_code", None))
        pending.set_language(user.id, lang)
    return lang


def ulang(update: Update) -> str:
    lang = lang_for(update.effective_user)
    note(language=lang)
    return lang


_chat_commands: dict[int, str] = {}  # chat_id -> language its "/" list is currently in


async def sync_chat_commands(bot, chat_id: int, lang: str, force: bool = False):
    """Telegram shows the "/" list in the app's language by default. A per-chat list overrides
    that, so the list follows the language chosen in the bot instead."""
    if not config.COMMAND_MENU or (not force and _chat_commands.get(chat_id) == lang):
        return
    try:
        commands = i18n.COMMANDS[lang] + (i18n.OWNER_COMMANDS[lang] if is_admin(chat_id) else [])
        await bot.set_my_commands([BotCommand(c, d) for c, d in commands], scope=BotCommandScopeChat(chat_id))
        _chat_commands[chat_id] = lang
    except Exception:
        log.exception("Could not set the command list for chat %s", chat_id)


def stored_lang(user_id: int | None) -> str:
    """For messages to someone who isn't the sender (owner notifications, welcomes)."""
    return (pending.get_language(user_id) if user_id else None) or i18n.DEFAULT_LANGUAGE


def category_label(category_id: int | None, fallback: str, lang: str) -> str:
    c = catalog.by_id(category_id) if category_id is not None else None
    return c.label(lang) if c else fallback


# --------------------------------------------------------------------------- #
# Formatting helpers                                                          #
# --------------------------------------------------------------------------- #


def fmt_money(amount: Decimal | str, currency: str) -> str:
    amount = Decimal(str(amount))
    s = f"{amount:,.2f}".rstrip("0").rstrip(".").replace(",", " ")
    sign = CURRENCY_SIGNS.get(currency)
    return f"{s} {sign}" if sign else f"{s} {currency}"


def item_name(item: dict) -> str:
    return item.get("label") or item["description"]


def fmt_item(item: dict, lang: str) -> str:
    today = datetime.now(config.TIMEZONE).date().isoformat()
    line = f"<b>{fmt_money(item['amount'], item['currency'])}</b> · {html.escape(item_name(item))}"
    if item.get("merchant"):
        line += f" ({html.escape(item['merchant'])})"
    if item["expense_date"] != today:
        line += f"\n📅 {fmt_day(date.fromisoformat(item['expense_date']), lang)}"
    return line


def kind_of(item: dict) -> str:
    return item.get("kind") or "expense"


def kind_label(kind: str, lang: str) -> str:
    return f"{savings.ICONS[kind]} {t(lang, 'kind_' + kind)}"


def goal_suffix(goal_id: str | None, lang: str) -> str:
    g = goals.get(goal_id)
    return f" → 🎯 {g.name}" if g else ""


def shown_as(item: dict, category_id: int, category_name: str, lang: str) -> str:
    """What a saved record is filed under, for the user: its category, or its kind (and goal)."""
    kind = kind_of(item)
    if kind == "expense":
        return category_label(category_id, category_name, lang)
    return kind_label(kind, lang) + goal_suffix(item.get("goal_id"), lang)


def proposal_text(item: dict, lang: str) -> str:
    kind = kind_of(item)
    if kind != "expense":
        what = html.escape(kind_label(kind, lang) + goal_suffix(item.get("goal_id"), lang))
        return f"{fmt_item(item, lang)}\n" + t(lang, "kind_question", kind=what, tag=t(lang, "tag_guess"))
    tag = t(lang, "tag_known") if item.get("suggestion_source") == "dictionary" else t(lang, "tag_guess")
    cat = category_label(item.get("category_id"), item["category"], lang)
    return f"{fmt_item(item, lang)}\n" + t(lang, "category_question", cat=html.escape(cat), tag=tag)


def kind_buttons(pid: str, lang: str, current: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(kind_label(k, lang), callback_data=f"k:{pid}:{k}")
            for k in savings.KINDS if k != current]


def change_keyboard(pid: str, item: dict, lang: str) -> InlineKeyboardMarkup:
    """✏️ on a proposal. Spending: the categories, then "it's actually income / savings…".
    Anything else: the other kinds, and for savings/withdrawals which goal."""
    kind = kind_of(item)
    if kind == "expense":
        rows = list(category_keyboard(pid, lang).inline_keyboard[:-1])
    else:
        rows = []
        if kind in ("saving", "withdrawal"):
            rows += goal_rows(pid, item, lang)
    kb = kind_buttons(pid, lang, kind)
    rows += [kb[i : i + 2] for i in range(0, len(kb), 2)]
    rows.append([InlineKeyboardButton(t(lang, "btn_back"), callback_data=f"bk:{pid}")])
    return InlineKeyboardMarkup(rows)


def goal_rows(pid: str, item: dict, lang: str) -> list[list[InlineKeyboardButton]]:
    mine = goals.of(item["user_id"])
    if not mine:
        return []
    current = item.get("goal_id")
    buttons = [InlineKeyboardButton(("✓ " if g.goal_id == current else "") + f"🎯 {g.name}",
                                    callback_data=f"g:{pid}:{g.goal_id}") for g in mine]
    buttons.append(InlineKeyboardButton(("✓ " if not current else "") + t(lang, "btn_no_goal"),
                                        callback_data=f"g:{pid}:-"))
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def confirm_keyboard(pid: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t(lang, "btn_save"), callback_data=f"ok:{pid}"),
                InlineKeyboardButton(t(lang, "btn_change"), callback_data=f"ed:{pid}"),
                InlineKeyboardButton("🗑", callback_data=f"no:{pid}"),
            ]
        ]
    )


def category_keyboard(pid: str, lang: str) -> InlineKeyboardMarkup:
    # Stable category_id in the button, so edits to dim_categories can't shift what a tap means.
    buttons = [InlineKeyboardButton(c.label(lang), callback_data=f"set:{pid}:{c.id}") for c in catalog.categories]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(t(lang, "btn_back"), callback_data=f"bk:{pid}")])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# Access control                                                              #
# --------------------------------------------------------------------------- #


async def allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """The bot is open to everyone, except people the owner blocked."""
    user = update.effective_user
    if user is None:
        return False
    msg = update.effective_message
    if config.OWNER_USER_ID is None:  # first run: tell whoever writes their ID
        note(outcome="denied")
        if msg:
            await msg.reply_text(t(lang_for(user), "first_run_id", uid=user.id))
        return False
    if pending.is_blocked(user.id):
        note(outcome="blocked")
        if update.callback_query is not None:
            await update.callback_query.answer(t(lang_for(user), "blocked"), show_alert=True)
        elif msg:
            await msg.reply_text(t(lang_for(user), "blocked"))
        return False
    return True


# Telegram answers that aren't errors: a button tap answered too late (it waited while the Mac slept
# or the bot restarted), or an edit that would leave the message exactly as it is (a double tap).
_HARMLESS = ("message is not modified", "query is too old", "query id is invalid")


def _harmless(e: Exception) -> bool:
    return isinstance(e, BadRequest) and any(s in str(e).lower() for s in _HARMLESS)


async def answer(query, *args, **kwargs):
    """query.answer(), except a late answer is skipped instead of failing the whole tap."""
    try:
        return await query.answer(*args, **kwargs)
    except BadRequest as e:
        if not _harmless(e):
            raise
        log.info("Button answered too late, skipped: %s", e)


async def edit_text(query, *args, **kwargs):
    try:
        return await query.edit_message_text(*args, **kwargs)
    except BadRequest as e:
        if not _harmless(e):
            raise


async def edit_markup(query, *args, **kwargs):
    try:
        return await query.edit_message_reply_markup(*args, **kwargs)
    except BadRequest as e:
        if not _harmless(e):
            raise


def display_name(user) -> str:
    return " ".join(p for p in (user.first_name, user.last_name) if p) or (user.username or str(user.id))


# --------------------------------------------------------------------------- #
# Daily limits                                                                #
# --------------------------------------------------------------------------- #


def over_limit(user_id: int, kind: str) -> int | None:
    """Count one AI call of `kind` (text | voice) for today. Returns the limit if this one would go
    over it (and then doesn't count it), else None. The owner has no limits."""
    limit = config.DAILY_VOICE_LIMIT if kind == "voice" else config.DAILY_TEXT_LIMIT
    if is_admin(user_id) or limit <= 0:
        return None
    day = datetime.now(config.TIMEZONE).date().isoformat()
    if pending.count_use(user_id, kind, day) > limit:
        pending.uncount_use(user_id, kind, day)
        return limit
    return None


def voice_too_long(user_id: int, seconds: int | None = None, size: int | None = None) -> bool:
    """Long recordings cost the most (local Whisper runs on the owner's computer). Uploads have no
    duration, so their size stands in for it (~256 kbit/s, generous for voice)."""
    cap = config.MAX_VOICE_SECONDS
    if is_admin(user_id) or cap <= 0:
        return False
    if seconds is not None:
        return seconds > cap
    return (size or 0) > cap * 32_000


# --------------------------------------------------------------------------- #
# Incoming expenses                                                           #
# --------------------------------------------------------------------------- #


@logged("text")
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    lang = ulang(update)
    if await delete_confirmation(update, context, lang):
        return
    uid, msg = update.effective_user.id, update.effective_message
    reply_to = getattr(msg, "reply_to_message", None)
    vn = None
    if isinstance(getattr(reply_to, "message_id", None), int):
        rid = pending.report_for_message(uid, update.effective_chat.id, reply_to.message_id)
        if rid:  # line numbers for 🗑 Delete a line: no AI involved, so no daily limit
            await ask_delete_lines(update, rid, msg.text, lang)
            return
        # a reply to a 🎙 transcript or its fix prompt = a corrected transcript
        vn = pending.voice_note_for_reply(uid, update.effective_chat.id, reply_to.message_id)
    awaiting = context.user_data.pop("awaiting", None)
    if vn is None and isinstance(awaiting, dict) and awaiting.get("until", 0) > time.monotonic():
        if awaiting["kind"] == "lines" and re.search(r"\d", msg.text or ""):
            if not await ask_delete_lines(update, awaiting["id"], msg.text, lang):
                context.user_data["awaiting"] = awaiting  # let them try again
            return
        if awaiting["kind"] == "fix":
            found = pending.voice_note(awaiting["id"])
            if found and found["user_id"] == uid:
                vn = found
        if awaiting["kind"] == "goal":
            limit = over_limit(uid, "text")  # the goal is read by the AI too
            if limit:
                note(outcome="daily_limit", limit=limit)
                await msg.reply_text(t(lang, "limit_text", n=limit))
                return
            await create_goal(update, context, msg.text, lang)
            return
        # anything else (e.g. words instead of line numbers): an ordinary message
    limit = over_limit(uid, "text")
    if limit:
        note(outcome="daily_limit", limit=limit)
        await msg.reply_text(t(lang, "limit_text", n=limit))
        return
    if vn is not None:
        await correct_transcript(update, context, vn, msg.text, lang)
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    text = update.effective_message.text
    try:
        result = await extractor.parse(
            text=text, sent_at=update.effective_message.date, ctx=await mapping_context(lang, uid)
        )
    except Exception as e:
        log.exception("Parsing failed")
        note(outcome="parse_error", error=f"{type(e).__name__}: {e}"[:1000])
        await update.effective_message.reply_text(t(lang, "parse_failed"))
        return
    await propose(update, result, source="text", raw_input=text, lang=lang)


@logged("voice")
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    lang = ulang(update)
    if await delete_confirmation(update, context, lang):
        return
    voice = update.effective_message.voice or update.effective_message.audio
    uid = update.effective_user.id
    if voice_too_long(uid, seconds=voice.duration or 0):
        note(outcome="voice_too_long")
        await update.effective_message.reply_text(t(lang, "voice_too_long", s=config.MAX_VOICE_SECONDS))
        return
    limit = over_limit(uid, "voice")
    if limit:
        note(outcome="daily_limit", limit=limit)
        await update.effective_message.reply_text(t(lang, "limit_voice", n=limit))
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    tg_file = await voice.get_file()
    audio = bytes(await tg_file.download_as_bytearray())
    try:
        result = await extractor.parse(
            audio=audio,
            audio_mime=voice.mime_type or "audio/ogg",
            sent_at=update.effective_message.date,
            ctx=await mapping_context(lang, uid),
        )
    except Exception as e:
        log.exception("Voice parsing failed")
        note(outcome="parse_error", error=f"{type(e).__name__}: {e}"[:1000])
        await update.effective_message.reply_text(t(lang, "voice_failed"))
        return
    await propose(update, result, source="voice", raw_input=result.transcript, lang=lang,
                  sent_at=update.effective_message.date)


async def mapping_context(lang: str, user_id: int | None = None) -> extractor.MappingContext:
    """The shared dictionary (plus the person's own savings goals), as the model sees it on every mapping."""
    await catalog.refresh_if_stale()
    return extractor.MappingContext(
        categories=catalog.names, guide=catalog.category_guide(), examples=catalog.examples(), language=lang,
        goals=[g.name for g in goals.of(user_id)] if user_id is not None else [],
    )


async def propose(update: Update, result: extractor.ParseResult, source: str, raw_input: str | None, lang: str,
                  sent_at: datetime | None = None):
    await propose_to(update.effective_message.reply_text, update.effective_user.id, result, source, raw_input, lang,
                     sent_at=sent_at)


def fix_keyboard(gid: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_fix_text"), callback_data=f"fx:{gid}")]])


def is_message_id(m) -> bool:
    return isinstance(getattr(m, "message_id", None), int) and isinstance(getattr(m, "chat_id", None), int)


async def propose_to(send, user_id: int, result: extractor.ParseResult, source: str,
                     raw_input: str | None, lang: str, sent_at: datetime | None = None,
                     group: str | None = None) -> int:
    """Send one ✅/✏️/🗑 proposal per expense via `send(text, **kwargs)`. Shared by chat messages and
    Action Button uploads. Returns how many expenses were proposed.
    Spoken input first shows the transcript with ✏️ Fix text; `group` = re-proposing a corrected transcript."""
    note(transcript=result.transcript, expenses_found=len(result.expenses))
    spoken = source in ("voice", "upload_audio")
    if spoken and group is None and not (result.transcript or "").strip():
        note(outcome="empty_audio")
        await send(t(lang, "voice_empty"))
        return 0
    gid = group
    if spoken and group is None:
        gid = secrets.token_hex(5)
        pending.add_voice_note(gid, user_id, result.transcript, source,
                               (sent_at or datetime.now(timezone.utc)).isoformat())
        shown = await send(f"🎙 <i>{html.escape(result.transcript)}</i>", parse_mode=ParseMode.HTML,
                           reply_markup=fix_keyboard(gid, lang))
        if is_message_id(shown):
            pending.set_voice_note_message(gid, shown.chat_id, shown.message_id)
        note(voice_note=gid)
    if not result.expenses:
        note(outcome="no_expense")
        await send(t(lang, "no_expense"))
        return 0
    pids, proposals = [], []
    for exp in result.expenses:
        # A variant users already confirmed beats the model's fresh guess (spending only: income and
        # savings never go through the dictionary).
        known = catalog.match(exp.merchant, exp.description) if exp.kind == "expense" else None
        category = known or catalog.by_name(exp.category) or catalog.fallback()
        goal = goals.by_name(user_id, exp.goal)
        payload = {
            "amount": str(exp.amount),
            "currency": exp.currency,
            "description": exp.description,  # English, the dictionary key
            "label": exp.label if lang != i18n.DEFAULT_LANGUAGE else None,  # the user's language, for display
            "merchant": exp.merchant,
            "expense_date": exp.expense_date.isoformat(),
            "category": category.name,
            "category_id": category.id,
            "ai_category": exp.category,
            "suggestion_source": "dictionary" if known else "ai",
            "source": source,
            "raw_input": raw_input,
            "kind": exp.kind,
            "ai_kind": exp.kind,
            "goal_id": goal.goal_id if goal else None,
        }
        if gid:
            payload["group"] = gid  # the transcript it came from, so a correction can withdraw it
        pid = pending.add(user_id, payload)
        pids.append(pid)
        proposals.append(
            {k: payload[k] for k in ("amount", "currency", "description", "label", "merchant", "expense_date",
                                     "category", "ai_category", "suggestion_source", "kind", "goal_id")}
        )
        shown = await send(proposal_text(payload, lang), parse_mode=ParseMode.HTML, reply_markup=confirm_keyboard(pid, lang))
        if is_message_id(shown):
            pending.set_message(pid, shown.chat_id, shown.message_id)  # lets auto-save update it later
    if gid:
        pending.voice_note_counts(gid, proposed=len(pids))
    note(outcome="proposed", pending_ids=pids, proposals=proposals)
    return len(pids)


# --------------------------------------------------------------------------- #
# ✏️ Fix text: correcting a misheard transcript                                #
# --------------------------------------------------------------------------- #


@logged("button")
async def handle_fix_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """✏️ Fix text under a 🎙 transcript: ask for the corrected text as a reply."""
    query = update.callback_query
    if not await allowed(update, context):
        return
    lang = ulang(update)
    uid = query.from_user.id
    gid = query.data.split(":", 1)[1]
    if gid == "cancel":
        context.user_data.pop("awaiting", None)
        note(outcome="fix_cancelled")
        await answer(query)
        await edit_text(query, t(lang, "fix_cancelled"))
        return
    vn = pending.voice_note(gid)
    note(voice_note=gid)
    if vn is None or vn["user_id"] != uid:
        note(outcome="fix_gone")
        await answer(query, t(lang, "fix_gone"), show_alert=True)
        return
    for pid, _, _ in pending.pending_in_group(uid, gid):
        pending.touch(pid)  # they're fixing it: don't auto-save the misheard version meanwhile
    await answer(query)
    # Wait for their next message. (Not ForceReply: that replaces the pinned ▶️ Start keyboard.)
    context.user_data["awaiting"] = {"kind": "fix", "id": gid, "until": time.monotonic() + AWAIT_SECONDS}
    prompt = await update.effective_message.reply_text(
        t(lang, "fix_prompt", text=html.escape(vn["transcript"])),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="fx:cancel")]]),
    )
    if isinstance(getattr(prompt, "message_id", None), int):
        pending.set_voice_note_prompt(gid, prompt.message_id)
    note(outcome="fix_prompt")


async def correct_transcript(update: Update, context: ContextTypes.DEFAULT_TYPE, vn: dict, text: str, lang: str):
    """The corrected text arrived: withdraw what the misheard version proposed (unanswered ones),
    show the correction on the 🎙 message, and propose again from the corrected text."""
    uid, msg, gid = update.effective_user.id, update.effective_message, vn["gid"]
    text = (text or "").strip()
    note(voice_note=gid, transcript_fixed=True, original_transcript=vn["transcript"])
    withdrawn = []
    async with _save_lock:  # a ✅ tap on one of them can't slip in between
        for pid, chat_id, message_id in pending.pending_in_group(uid, gid):
            item = pending.pop(pid)
            if item is not None:
                withdrawn.append((item, chat_id, message_id))
    for item, chat_id, message_id in withdrawn:
        if chat_id and message_id:
            try:
                await context.bot.edit_message_text(
                    f"<s>{fmt_item(item, lang)}</s>\n✖️ <i>{t(lang, 'fix_replaced')}</i>",
                    chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML,
                )
            except Exception:
                log.info("Couldn't mark a replaced proposal")
    if vn["chat_id"] and vn["transcript_message_id"]:
        try:
            await context.bot.edit_message_text(
                f"🎙 <s>{html.escape(vn['transcript'])}</s>\n✏️ <i>{html.escape(text)}</i>",
                chat_id=vn["chat_id"], message_id=vn["transcript_message_id"], parse_mode=ParseMode.HTML,
                reply_markup=fix_keyboard(gid, lang),
            )
        except Exception:
            log.info("Couldn't update the transcript message")
    pending.voice_note_fixed(gid, text)
    if vn["saved"]:
        await msg.reply_text(t(lang, "fix_already_saved", n=vn["saved"]))
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        result = await extractor.parse(
            text=text, sent_at=datetime.fromisoformat(vn["sent_at"]), ctx=await mapping_context(lang, vn["user_id"])
        )
    except Exception as e:
        log.exception("Parsing the corrected transcript failed")
        note(outcome="parse_error", error=f"{type(e).__name__}: {e}"[:1000])
        await msg.reply_text(t(lang, "parse_failed"))
        return
    note(withdrawn=len(withdrawn))
    await propose_to(msg.reply_text, uid, result, vn["source"], raw_input=text, lang=lang, group=gid)


# --------------------------------------------------------------------------- #
# Action Button / Shortcut uploads (see ingest.py)                            #
# --------------------------------------------------------------------------- #

_app: Application | None = None  # set in _post_init; uploads use its bot to message you


def upload_user(key: str) -> int | None:
    """Whose upload is this? INGEST_SECRET = the owner; otherwise a personal key from 📲 Action Button.
    A key stops working when its person is blocked or deletes their data."""
    import ingest

    if ingest.owner_key_matches(key):
        return config.OWNER_USER_ID
    uid = pending.user_for_upload_key(key)
    return uid if can_use(uid) else None


async def handle_upload(uid: int, audio: bytes | None, mime: str, text: str | None) -> dict:
    """A recording or text from the Shortcut -> the same proposals as a chat message, in that person's chat."""
    if _app is None:
        raise RuntimeError("the bot is not running yet")
    lang = stored_lang(uid)
    source = "upload_audio" if audio else "upload_text"

    first = [True]

    async def send(message: str, **kwargs):
        # "📲 From your Shortcut:" goes on top of the first message (the transcript), not on its own.
        if first[0]:
            first[0] = False
            prefix = t(lang, "upload_prefix")
            message = f"{html.escape(prefix) if kwargs.get('parse_mode') == ParseMode.HTML else prefix}\n{message}"
        return await _app.bot.send_message(uid, message, **kwargs)

    async with record(source, uid, uid, input_text=text, details={"audio_bytes": len(audio or b""), "mime": mime}):
        note(language=lang)
        if audio and voice_too_long(uid, size=len(audio)):
            note(outcome="voice_too_long")
            await send(t(lang, "voice_too_long", s=config.MAX_VOICE_SECONDS))
            return {"expenses": 0, "error": "recording too long"}
        limit = over_limit(uid, "voice" if audio else "text")
        if limit:
            note(outcome="daily_limit", limit=limit)
            await send(t(lang, "limit_voice" if audio else "limit_text", n=limit))
            return {"expenses": 0, "error": "daily limit reached"}
        try:
            result = await extractor.parse(
                audio=audio if audio else None,
                audio_mime=mime,
                text=None if audio else text,
                sent_at=datetime.now(timezone.utc),
                ctx=await mapping_context(lang, uid),
            )
        except Exception as e:
            note(outcome="parse_error", error=f"{type(e).__name__}: {e}"[:1000])
            await send(t(lang, "voice_failed" if audio else "parse_failed"))
            raise
        n = await propose_to(send, uid, result, source, raw_input=text or result.transcript, lang=lang,
                             sent_at=datetime.now(timezone.utc))
        return {"expenses": n, "transcript": result.transcript}


# --------------------------------------------------------------------------- #
# Button taps                                                                 #
# --------------------------------------------------------------------------- #


@logged("button")
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    lang = ulang(update)
    if not can_use(query.from_user.id):
        note(outcome="denied")
        await answer(query, t(lang, "not_allowed"), show_alert=True)
        return
    action, pid, *rest = query.data.split(":")
    item = pending.get(pid)
    note(pending_ids=[pid])
    if item is None:
        note(outcome="already_handled")
        await answer(query, t(lang, "already_handled"))
        await edit_markup(query, None)
        return
    if item["user_id"] != query.from_user.id:  # e.g. someone else's expense in a group chat
        note(outcome="not_owner", owner_user_id=item["user_id"])
        await answer(query, t(lang, "not_your_expense"), show_alert=True)
        return

    if action in ("ed", "bk", "k"):
        pending.touch(pid)  # they're deciding: restart the auto-save clock
    if action == "ed":
        note(outcome="category_menu")
        await answer(query)
        await edit_text(query,
            f"{fmt_item(item, lang)}\n{t(lang, 'pick_category' if kind_of(item) == 'expense' else 'pick_kind')}",
            parse_mode=ParseMode.HTML,
            reply_markup=change_keyboard(pid, item, lang),
        )
    elif action == "k":
        kind = rest[0] if rest and rest[0] in savings.KINDS else None
        if kind is None:
            await answer(query)
            return
        item = pending.update(pid, kind=kind, goal_id=item.get("goal_id") if kind in ("saving", "withdrawal") else None)
        note(outcome="kind_changed", kind=kind)
        if kind == "expense":  # which category, then save
            await answer(query)
            await edit_text(query, f"{fmt_item(item, lang)}\n{t(lang, 'pick_category')}",
                            parse_mode=ParseMode.HTML, reply_markup=change_keyboard(pid, item, lang))
        elif kind in ("saving", "withdrawal") and goals.of(item["user_id"]):  # which goal, then save
            await answer(query)
            await edit_text(query, f"{fmt_item(item, lang)}\n{kind_label(kind, lang)} · {t(lang, 'pick_goal')}",
                            parse_mode=ParseMode.HTML, reply_markup=change_keyboard(pid, item, lang))
        else:
            await save(query, pid, item, 0, savings.ROW_CATEGORY[kind], lang)
    elif action == "g":
        gid = rest[0] if rest else "-"
        goal = goals.get(gid, item["user_id"]) if gid != "-" else None
        if gid != "-" and (goal is None or not goal.is_open):
            note(outcome="goal_gone")
            await answer(query, t(lang, "goal_gone"))
            await edit_markup(query, change_keyboard(pid, item, lang))
            return
        item = pending.update(pid, goal_id=goal.goal_id if goal else None)
        kind = kind_of(item)
        await save(query, pid, item, 0, savings.ROW_CATEGORY.get(kind, "Savings"), lang)
    elif action == "bk":
        note(outcome="back")
        await answer(query)
        await edit_text(query, 
            proposal_text(item, lang), parse_mode=ParseMode.HTML, reply_markup=confirm_keyboard(pid, lang)
        )
    elif action == "no":
        note(outcome="discarded", category=item["category"], suggestion_source=item.get("suggestion_source"))
        pending.pop(pid)
        await answer(query, t(lang, "discarded_toast"))
        await edit_text(query, 
            f"<s>{fmt_item(item, lang)}</s>\n{t(lang, 'discarded_line')}", parse_mode=ParseMode.HTML
        )
    elif action == "ok":
        kind = kind_of(item)
        if kind == "expense":
            await save(query, pid, item, item["category_id"], item["category"], lang)
        else:
            await save(query, pid, item, 0, savings.ROW_CATEGORY[kind], lang)
    elif action == "set":
        category = catalog.by_id(int(rest[0]))
        if category is None:  # removed from dim_categories since the buttons were drawn
            note(outcome="category_gone")
            await answer(query, t(lang, "category_gone"))
            await edit_markup(query, category_keyboard(pid, lang))
            return
        if kind_of(item) != "expense":  # a category button = it's spending after all
            item = pending.update(pid, kind="expense", goal_id=None)
        await save(query, pid, item, category.id, category.name, lang)


async def save(query, pid: str, item: dict, category_id: int, category_name: str, lang: str):
    try:
        done = await commit_expense(pid, category_id, category_name, lang,
                                    query.message.chat_id, query.message.message_id, "user")
    except Exception as e:
        log.exception("BigQuery insert failed")
        note(outcome="save_error", error=f"{type(e).__name__}: {e}"[:1000])
        await answer(query, t(lang, "save_failed"), show_alert=True)
        return
    if done is None:  # auto-save got there first
        note(outcome="already_handled")
        await answer(query, t(lang, "already_handled"))
        return
    item, row, shown = done
    await answer(query, t(lang, "saved_toast"))
    await edit_text(query, f"{fmt_item(item, lang)}\n✅ <b>{html.escape(shown)}</b>", parse_mode=ParseMode.HTML)


_save_lock = asyncio.Lock()


async def commit_expense(pid: str, category_id: int, category_name: str, lang: str,
                         chat_id: int, message_id: int, confirmed_by: str):
    """Save a pending proposal once, whoever gets there first (a tap or the auto-save timer).
    Returns (item, row, shown category) or None if it was already handled. Raises if BigQuery fails."""
    async with _save_lock:
        item = pending.get(pid)
        if item is None:
            return None
        kind = kind_of(item)
        if kind != "expense":  # income / savings: no spending category
            category_id, category_name = 0, savings.ROW_CATEGORY[kind]
            if item.get("goal_id") and not goals.get(item["goal_id"], item["user_id"]):
                item["goal_id"] = None  # the goal was closed or deleted meanwhile
        row = build_row(item, category_id, category_name, confirmed_by)
        await warehouse.insert(row)
        pending.pop(pid)
        if item.get("group"):
            pending.voice_note_counts(item["group"], saved=1)
    if household.enabled and not context_flags.get("view_ready"):
        await asyncio.to_thread(household.ensure_view)
        context_flags["view_ready"] = True
    note(
        outcome="saved_corrected" if row["was_corrected"] else "saved",
        expense_id=row["expense_id"],
        category=category_name,
        suggestion_source=item.get("suggestion_source"),
        suggested_category=item["category"],
        kind=kind,
        goal_id=row.get("goal_id"),
    )

    # Teach the shared dictionary — only from real taps: an auto-saved guess was never checked by
    # anyone, and learning from it would make a wrong guess look "known". A failure here must not
    # lose the saved expense.
    keys = catalog.keys_for(item.get("merchant"), item.get("description"))
    learned = None
    category = catalog.by_id(category_id)
    if category and keys and confirmed_by == "user" and kind == "expense":
        try:
            await catalog.learn(keys, category, row["was_corrected"])
            learned = {"keys": keys, "category_id": category_id, "corrected": row["was_corrected"]}
        except Exception as e:
            log.exception("Dictionary update failed")
            note(dictionary_error=f"{type(e).__name__}: {e}"[:500])

    shown = shown_as(item, category_id, category_name, lang)
    summary = f"{fmt_money(item['amount'], item['currency'])} · {item_name(item)} → {shown}"
    pending.remember_saved(row["expense_id"], item["user_id"], chat_id, message_id, summary, learned)
    return item, row, shown


# --------------------------------------------------------------------------- #
# Auto-save: no answer within AUTO_SAVE_MINUTES -> suggested category         #
# --------------------------------------------------------------------------- #


async def auto_save_pass():
    if config.AUTO_SAVE_MINUTES <= 0 or _app is None:
        return
    for pid, chat_id, message_id in pending.overdue(config.AUTO_SAVE_MINUTES):
        item = pending.get(pid)
        if item is None:
            continue
        uid = item["user_id"]
        lang = stored_lang(uid)
        async with record("auto_save", uid, chat_id, pending_ids=[pid]):
            note(language=lang)
            try:
                done = await commit_expense(pid, item["category_id"], item["category"], lang,
                                            chat_id, message_id, "auto")
            except Exception as e:  # BigQuery hiccup: try again on the next pass
                log.exception("Auto-save failed for %s", pid)
                note(outcome="save_error", error=f"{type(e).__name__}: {e}"[:1000])
                continue
            if done is None:
                note(outcome="already_handled")
                continue
            item, row, shown = done
            note(outcome="auto_saved")
            try:
                await _app.bot.edit_message_text(
                    f"{fmt_item(item, lang)}\n✅ <b>{html.escape(shown)}</b> · <i>{t(lang, 'auto_saved')}</i>",
                    chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML,
                )
            except Exception:
                log.info("Auto-saved %s but could not update its message", pid)


async def auto_save_loop():
    while True:
        await asyncio.sleep(30)
        try:
            await auto_save_pass()
        except Exception:
            log.exception("Auto-save pass failed")


# --------------------------------------------------------------------------- #
# Commands                                                                    #
# --------------------------------------------------------------------------- #


@logged("command")
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    first_time = pending.get_language(update.effective_user.id) is None
    lang = ulang(update)
    await sync_chat_commands(context.bot, update.effective_chat.id, lang)
    args = context.args if isinstance(context.args, list) else []
    arg = args[0] if args else ""
    if arg.startswith("join_"):  # opened an invite link: t.me/<bot>?start=join_<code>
        if first_time:
            await update.effective_message.reply_text(t(lang, "start_text"), reply_markup=reply_keyboard(lang))
        await show_join_prompt(update.effective_message, update.effective_user, arg[5:], lang)
        return
    note(outcome="help")
    await send_start(update.effective_message, update.effective_user.id, lang)


async def send_start(msg, user_id: int, lang: str):
    """Intro (which also installs the ▶️ Start button above the typing field), then the action menu."""
    await msg.reply_text(t(lang, "start_text"), reply_markup=reply_keyboard(lang))
    await msg.reply_text(t(lang, "pick_action"), reply_markup=main_menu(user_id, lang))


def reply_keyboard(lang: str) -> ReplyKeyboardMarkup:
    """A button pinned above the typing field. A message can carry only one keyboard, so it rides on the intro."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(t(lang, "kb_menu"))]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=t(lang, "input_placeholder"),
    )


# Texts the ▶️ Start button sends (every language, plus old labels), so they're never parsed as expenses.
MENU_BUTTON_TEXTS = [i18n.STRINGS["kb_menu"][code] for code in i18n.LANGUAGES] + i18n.LEGACY_KB_MENU


@logged("command")
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The ▶️ Start button: the action menu."""
    if not await allowed(update, context):
        return
    lang = ulang(update)
    note(outcome="menu")
    if update.effective_message.text in i18n.LEGACY_KB_MENU:
        # Old 📋 Menu keyboard: send the intro once more, which swaps in the ▶️ Start button.
        await send_start(update.effective_message, update.effective_user.id, lang)
        return
    await update.effective_message.reply_text(
        t(lang, "pick_action"), reply_markup=main_menu(update.effective_user.id, lang)
    )


# --------------------------------------------------------------------------- #
# /start menu buttons                                                         #
# --------------------------------------------------------------------------- #


def main_menu(user_id: int, lang: str) -> InlineKeyboardMarkup:
    def b(key: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(t(lang, key), callback_data=f"m:{action}")

    rows = [
        [b("m_today", "today"), b("m_week", "week"), b("m_month", "month")],
        [b("m_savings", "savings")],
        [b("m_undo", "undo"), b("m_categories", "categories")],
    ]
    if household.home_of(user_id):
        rows.append([b("m_family", "family"), b("m_household", "household")])
    else:
        rows.append([b("m_household", "household")])
    if is_admin(user_id):
        rows.append([b("m_users", "users")])
    if shortcut_available():
        rows.append([b("m_shortcut", "shortcut")])
    view = "m_view_detailed" if pending.get_report_mode(user_id) == "detailed" else "m_view_summary"
    rows.insert(1, [b(view, "view")])  # right under Today / Week / Month, which it affects
    rows.append([b("m_reload", "reload"), b("m_language", "language")])
    if pending.active_deletion(user_id):
        rows.append([b("m_restore", "restore")])
    rows.append([b("m_help", "help"), b("m_delete", "delete")])
    return InlineKeyboardMarkup(rows)


@logged("button")
async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A menu tap runs the same code as the typed command; the answer arrives as a new message."""
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    commands = {
        "today": cmd_today, "week": cmd_week, "month": cmd_month, "undo": cmd_undo,
        "categories": cmd_categories, "reload": cmd_reload, "family": cmd_family,
        "household": cmd_household, "language": cmd_language, "users": cmd_users, "help": cmd_help,
        "delete": cmd_delete, "shortcut": cmd_shortcut,
        "restore": cmd_restore, "savings": cmd_savings,
    }
    if action == "view":
        await toggle_report_view(update)
        return
    cmd = commands.get(action)
    await answer(query)
    if cmd is None:
        note(outcome="unknown_menu_item")
        return
    note(menu_item=action)
    # __wrapped__ = the command without its own @logged wrapper, so one tap = one log row
    await cmd.__wrapped__(update, context)


async def toggle_report_view(update: Update):
    """The 📊/🧾 menu button: switches this person's report view and relabels the button in place."""
    query = update.callback_query
    if not can_use(query.from_user.id):
        note(outcome="denied")
        await answer(query, t(ulang(update), "not_allowed"), show_alert=True)
        return
    lang = ulang(update)
    mode = "summary" if pending.get_report_mode(query.from_user.id) == "detailed" else "detailed"
    pending.set_report_mode(query.from_user.id, mode)
    note(outcome="report_view_set", report_mode=mode)
    await answer(query, t(lang, "view_now_detailed" if mode == "detailed" else "view_now_summary"), show_alert=True)
    try:
        await edit_markup(query, main_menu(query.from_user.id, lang))
    except Exception:  # an old menu that can't be edited any more
        log.info("Could not relabel the menu")


# --------------------------------------------------------------------------- #
# 📲 Action Button: personal upload keys                                      #
# --------------------------------------------------------------------------- #


def shortcut_available() -> bool:
    """The upload endpoint is on and the bot knows its public address."""
    return bool(config.INGEST_SECRET and config.INGEST_PUBLIC_URL)


def shortcut_text(lang: str, key: str | None) -> str:
    """Setup steps. `key` is shown only right after it's created (only its hash is stored)."""
    key_line = t(lang, "sc_key", secret=html.escape(key)) if key else t(lang, "sc_key_hidden")
    return t(lang, "sc_title") + "\n\n" + key_line + "\n\n" + shortcut_steps(lang) + "\n\n" + t(lang, "sc_footer")


def shortcut_steps(lang: str) -> str:
    """Install steps: the iCloud link if there is one, otherwise how to build the Shortcut by hand."""
    name = html.escape(config.SHORTCUT_NAME)
    if config.SHORTCUT_URL:
        return t(lang, "sc_steps_link", link=html.escape(config.SHORTCUT_URL), name=name)
    return t(lang, "sc_steps_manual", url=html.escape(f"{config.INGEST_PUBLIC_URL}/ingest"), name=name)


def shortcut_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_sc_new_key"), callback_data="sc:new")]])


@logged("command")
async def cmd_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Setup for the iPhone Action Button. The first time, it also creates the person's key."""
    if not await allowed(update, context):
        return
    lang = ulang(update)
    if not shortcut_available():
        note(outcome="shortcut_off")
        await update.effective_message.reply_text(t(lang, "sc_off"))
        return
    uid = update.effective_user.id
    key = None
    if pending.upload_key_created(uid) is None:
        key = pending.new_upload_key(uid)
        note(outcome="shortcut_key_created")
    else:
        note(outcome="shortcut_shown")
    await update.effective_message.reply_text(
        shortcut_text(lang, key), parse_mode=ParseMode.HTML, reply_markup=shortcut_keyboard(lang),
        disable_web_page_preview=True,
    )


@logged("button")
async def handle_shortcut_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔄 New key: replaces the person's key (the old one stops working) and shows the new one."""
    query = update.callback_query
    lang = ulang(update)
    if not can_use(query.from_user.id) or not shortcut_available():
        note(outcome="denied")
        await answer(query, t(lang, "not_allowed"), show_alert=True)
        return
    key = pending.new_upload_key(query.from_user.id)
    note(outcome="shortcut_key_rotated")
    await answer(query, t(lang, "sc_new_key_done"))
    await edit_text(query, 
        shortcut_text(lang, key), parse_mode=ParseMode.HTML, reply_markup=shortcut_keyboard(lang),
        disable_web_page_preview=True,
    )


# --------------------------------------------------------------------------- #
# Delete my data                                                              #
# --------------------------------------------------------------------------- #

DELETE_WORDS = {"DELETE", "УДАЛИТЬ"}  # accepted in either language
DELETE_WINDOW_SECONDS = 300
LOG_PURGE_GRACE = timedelta(minutes=5)  # also covers the log rows of the delete conversation itself


@logged("command")
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: explain what will be deleted."""
    if not await allowed(update, context):
        return
    lang = ulang(update)
    try:
        n = sum(1 for _ in await warehouse.expenses(update.effective_user.id, date(2000, 1, 1), date(2100, 1, 1)))
    except Exception:
        n = "?"
    note(outcome="delete_prompt", expenses=n)
    days = config.DELETE_RETENTION_DAYS
    when = t(lang, "del_when_later", days=days) if days > 0 else t(lang, "del_when_now")
    await update.effective_message.reply_text(
        t(lang, "del_warning", n=n, when=when),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(t(lang, "btn_del_continue"), callback_data="del:go"),
              InlineKeyboardButton(t(lang, "btn_del_cancel"), callback_data="del:no")]]
        ),
    )


@logged("button")
async def handle_delete_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: Continue arms a 5-minute window for typing the confirmation word."""
    query = update.callback_query
    lang = ulang(update)
    if not can_use(query.from_user.id):
        note(outcome="denied")
        await answer(query, t(lang, "not_allowed"), show_alert=True)
        return
    if query.data == "del:restore":
        await restore_my_data(update, lang)
        return
    await answer(query)
    if query.data == "del:go":
        context.user_data["delete_confirm_until"] = time.monotonic() + DELETE_WINDOW_SECONDS
        note(outcome="delete_armed")
        await edit_markup(query, None)
        await update.effective_message.reply_text(t(lang, "del_type_to_confirm"), parse_mode=ParseMode.HTML)
    else:
        context.user_data.pop("delete_confirm_until", None)
        note(outcome="delete_cancelled")
        await edit_text(query, t(lang, "del_cancelled"))


async def delete_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> bool:
    """Step 3: the next message after Continue. Returns True if it was consumed here."""
    until = context.user_data.pop("delete_confirm_until", None)
    if not isinstance(until, (int, float)):  # no deletion waiting for confirmation
        return False
    msg = update.effective_message
    word = (msg.text or "").strip().strip(".!").upper()
    if word in DELETE_WORDS and time.monotonic() <= until:
        await delete_my_data(update, context, lang)
    elif word in DELETE_WORDS:
        note(outcome="delete_expired")
        await msg.reply_text(t(lang, "del_expired"))
    else:
        note(outcome="delete_cancelled")
        await msg.reply_text(t(lang, "del_cancelled"))
    return True


deletion_lock = asyncio.Lock()  # delete / restore / erase, one at a time


async def delete_my_data(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str):
    """Step 4: soft delete. The person's data disappears from the bot at once, is kept for
    DELETE_RETENTION_DAYS (restorable from the bot), then erase_due_deletions() removes it for good.
    Shared categories/dictionary are anonymous and stay."""
    uid = update.effective_user.id
    users.pause(uid)  # in-flight updates mustn't un-delete their dim_users row
    now = datetime.now(timezone.utc)
    days = config.DELETE_RETENTION_DAYS
    async with deletion_lock:
        try:
            n, archive = await asyncio.to_thread(warehouse.soft_delete_user_data, uid, max(days, 0), now)
        except Exception as e:
            log.exception("Deleting user data failed")
            note(outcome="delete_error", error=f"{type(e).__name__}: {e}"[:1000])
            await update.effective_message.reply_text(t(lang, "del_failed"))
            return
        try:  # their savings goals go with their data (and come back with it)
            await asyncio.to_thread(goals.soft_delete_user, uid, now.isoformat())
        except Exception:
            log.exception("Couldn't hide the savings goals of %s", uid)
        snapshot = {"language": pending.get_language(uid), "report_mode": pending.get_report_mode(uid)}
        pending.add_deletion(uid, now.isoformat(), (now + timedelta(days=max(days, 0))).isoformat(),
                             (now + LOG_PURGE_GRACE).isoformat(), archive, snapshot)
        pending.forget_user(uid)
    try:  # leave their household (or end it, if they created it); expenses of others stay
        async with household.lock:
            m = household.member(uid)
            if m and m.role == "owner":
                former = await household.end(m.household_id)
                await users.set_role([x for x in former if x != uid], "none")
            elif m:
                await household.leave(uid)
    except Exception:
        log.exception("Couldn't take %s out of their household", uid)
    note(outcome="deleted", expenses_deleted=n, retention_days=days, _skip_log=days <= 0)
    if days <= 0:  # no retention: erase now; log rows still in the streaming buffer are retried by run_log_purges
        pending.add_log_purge(uid, (now + LOG_PURGE_GRACE).isoformat())
        asyncio.create_task(erase_due_deletions())
        asyncio.create_task(run_log_purges())
        await update.effective_message.reply_text(t(lang, "del_done_now", n=n))
        return
    until = fmt_day((now + timedelta(days=days)).astimezone(config.TIMEZONE).date(), lang)
    await update.effective_message.reply_text(
        t(lang, "del_done", n=n, days=days, date=until),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(t(lang, "btn_restore"), callback_data="del:restore")]]
        ),
    )


async def restore_my_data(update: Update, lang: str):
    """↩️ Restore my data: within the retention period, put everything back."""
    uid = update.effective_user.id
    q = update.callback_query  # only the button under "Deleted" is ours to answer (menu taps are answered already)
    query = q if q is not None and q.data == "del:restore" else None
    reply = update.effective_message.reply_text
    async with deletion_lock:
        d = pending.active_deletion(uid)
        if d is None:
            note(outcome="nothing_to_restore")
            if query:
                await answer(query, t(lang, "restore_nothing"), show_alert=True)
            else:
                await reply(t(lang, "restore_nothing"))
            return
        if query:
            await answer(query)
        try:
            n = await asyncio.to_thread(warehouse.restore_user_data, uid, d["archive_table"])
            await asyncio.to_thread(goals.restore_user, uid, d["deleted_at"])
        except Exception as e:
            log.exception("Restoring user data failed")
            note(outcome="restore_error", error=f"{type(e).__name__}: {e}"[:1000])
            await reply(t(lang, "restore_failed"))
            return
        snap = d["snapshot"]
        if snap.get("language"):
            pending.set_language(uid, snap["language"])
        if snap.get("report_mode"):
            pending.set_report_mode(uid, snap["report_mode"])
        pending.finish_deletion(uid, d["deleted_at"], "restored")
    users.forget_cache(uid)
    lang = stored_lang(uid)
    note(outcome="restored", expenses_restored=n)
    if query:
        try:
            await edit_markup(query, None)
        except Exception:
            pass
    await reply(t(lang, "restored", n=n))


@logged("command")
async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    await restore_my_data(update, ulang(update))


async def erase_due_deletions():
    """Hard delete everything whose retention period is over. Safe to repeat: each step is idempotent,
    and a deletion is only marked done when every step succeeded (else it's retried next pass).
    The archive table also expires in BigQuery by itself, even if this Mac is off."""
    async with deletion_lock:
        for d in pending.due_deletions(datetime.now(timezone.utc).isoformat()):
            uid = d["user_id"]
            try:
                removed = await asyncio.to_thread(
                    warehouse.hard_delete_user_data, uid, d["archive_table"], d["log_cutoff"]
                )
                await asyncio.to_thread(goals.hard_delete_user, uid)
            except Exception as e:  # e.g. rows still in the streaming buffer (only with retention 0)
                log.info("Erasing data of %s not possible yet (%s); will retry", uid, str(e)[:120])
                continue
            interaction_log.drop_user(uid, d["log_cutoff"])
            pending.finish_deletion(uid, d["deleted_at"], "purged")
            log.info("Erased the deleted data of %s (%d log rows)", uid, removed)


async def run_log_purges():
    """Delete interaction-log rows for people who deleted their data. Recently streamed rows can't be
    deleted yet (BigQuery streaming buffer), so this is retried until a pass finds nothing left."""
    for uid, cutoff in pending.open_log_purges():
        try:
            removed = await asyncio.to_thread(warehouse.purge_user_log, uid, cutoff)
        except Exception as e:
            log.info("Log purge for %s not possible yet (%s); will retry", uid, str(e)[:120])
            continue
        if removed == 0 and datetime.now(timezone.utc) > datetime.fromisoformat(cutoff) + timedelta(hours=2):
            pending.finish_log_purge(uid, cutoff)
            log.info("Log purge for %s complete", uid)


async def log_purge_loop():
    """Every 30 minutes: erase deletions whose retention period is over (and finish older log purges)."""
    while True:
        try:
            await erase_due_deletions()
            await run_log_purges()
        except Exception:
            log.exception("Deletion pass failed")
        await asyncio.sleep(1800)


CURRENCY_NAMES = {
    "en": {"KZT": "tenge (₸)", "USD": "US dollars ($)", "EUR": "euros (€)", "RUB": "roubles (₽)"},
    "ru": {"KZT": "тенге (₸)", "USD": "доллары ($)", "EUR": "евро (€)", "RUB": "рубли (₽)"},
}


@logged("command")
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Short instructions: what the bot is and how to use it."""
    if not await allowed(update, context):
        return
    lang = ulang(update)
    note(outcome="help_shown")
    currency = CURRENCY_NAMES.get(lang, {}).get(config.DEFAULT_CURRENCY, config.DEFAULT_CURRENCY)
    days = config.DELETE_RETENTION_DAYS
    text = t(lang, "help_text", currency=currency, days=days)
    if config.AUTO_SAVE_MINUTES > 0:
        text += t(lang, "help_auto", minutes=f"{config.AUTO_SAVE_MINUTES:g}")
    text += t(lang, "help_fix")
    text += t(lang, "help_delete_line")
    text += t(lang, "help_savings")
    text += t(lang, "help_household")
    markup = None
    if shortcut_available():
        text += t(lang, "help_shortcut", steps=shortcut_steps(lang))
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_sc_get_key"), callback_data="m:shortcut")]])
    if not is_admin(update.effective_user.id) and (config.DAILY_TEXT_LIMIT > 0 or config.DAILY_VOICE_LIMIT > 0):
        unlimited = "∞"
        text += t(lang, "help_limits",
                  text=config.DAILY_TEXT_LIMIT or unlimited, voice=config.DAILY_VOICE_LIMIT or unlimited,
                  s=config.MAX_VOICE_SECONDS or unlimited)
    text += t(lang, "help_privacy", days=days)
    if is_admin(update.effective_user.id):
        text += t(lang, "help_owner")
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
    )


# --------------------------------------------------------------------------- #
# Language                                                                    #
# --------------------------------------------------------------------------- #


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"lang:{code}") for code, label in i18n.LANGUAGES.items()]]
    )


@logged("command")
async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    lang = ulang(update)
    note(outcome="language_menu")
    await update.effective_message.reply_text(t(lang, "lang_pick"), reply_markup=language_keyboard())


@logged("button")
async def handle_language_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not can_use(query.from_user.id):
        note(outcome="denied")
        await answer(query, t(ulang(update), "not_allowed"), show_alert=True)
        return
    code = query.data.split(":", 1)[1]
    if code not in i18n.LANGUAGES:
        note(outcome="unknown_language")
        await answer(query)
        return
    pending.set_language(query.from_user.id, code)
    note(outcome="language_set", language=code)
    await sync_chat_commands(context.bot, update.effective_chat.id, code, force=True)
    asyncio.create_task(users.observe(query.from_user, role_of(query.from_user.id), code, force=True))
    await answer(query, t(code, "lang_set"))
    await edit_text(query, t(code, "lang_set"))
    # Fresh menus in the new language, including the ▶️ Start button label
    # (older menus keep their old labels but still work).
    await send_start(update.effective_message, query.from_user.id, code)


# --------------------------------------------------------------------------- #
# Catalog & reports                                                           #
# --------------------------------------------------------------------------- #


@logged("command")
async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    lang = ulang(update)
    await catalog.refresh_if_stale()
    note(outcome="listed", categories=len(catalog.categories))
    await update.effective_message.reply_text("\n".join(f"• {c.label(lang)}" for c in catalog.categories))


@logged("command")
async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    lang = ulang(update)
    try:
        await catalog.refresh_if_stale(force=True)
    except Exception as e:
        log.exception("Reload failed")
        note(outcome="reload_error", error=f"{type(e).__name__}: {e}"[:1000])
        await update.effective_message.reply_text(t(lang, "reload_failed"))
        return
    note(outcome="reloaded", categories=len(catalog.categories), variants=len(catalog.variants))
    await update.effective_message.reply_text(
        t(lang, "reloaded", c=len(catalog.categories), v=len(catalog.variants))
    )


async def _report(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str):
    if not await allowed(update, context):
        return
    lang = ulang(update)
    today = datetime.now(config.TIMEZONE).date()
    if period == "today":
        start, title = today, t(lang, "title_today", d=fmt_day(today, lang))
    elif period == "week":
        start = today - timedelta(days=6)
        title = t(lang, "title_week", a=fmt_day(start, lang), b=fmt_day(today, lang))
    else:
        start, title = today.replace(day=1), fmt_month(today, lang)
    mode = pending.get_report_mode(update.effective_user.id)
    flow = None
    try:
        if mode == "detailed":
            items, flow = await asyncio.gather(warehouse.expenses(update.effective_user.id, start, today),
                                               warehouse.money_flow(update.effective_user.id, start, today))
        else:
            rows, flow = await asyncio.gather(warehouse.totals(update.effective_user.id, start, today),
                                              warehouse.money_flow(update.effective_user.id, start, today))
    except Exception as e:
        log.exception("Report query failed")
        note(outcome="report_error", error=f"{type(e).__name__}: {e}"[:1000])
        await update.effective_message.reply_text(t(lang, "report_failed"))
        return
    if mode == "detailed":
        note(outcome="report", report_mode=mode, period=period, period_start=str(start),
             period_end=str(today), rows=len(items))
        if not items:
            await update.effective_message.reply_text(t(lang, "nothing_yet", title=title))
            return
        uid = update.effective_user.id
        index: list[tuple[int, str, str]] = []
        text = detailed_report(title, items, lang, multi_day=start != today, index=index)
        text += money_lines(savings.overview(flow or []), lang)
        rid = secrets.token_hex(5)
        sent = await send_long(update.effective_message, text, reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(t(lang, "btn_delete_line"), callback_data=f"rl:{rid}")]]
        ))
        pending.save_report(rid, uid, index)
        for m in sent:
            if is_message_id(m):
                pending.add_report_message(rid, uid, m.chat_id, m.message_id)
        note(report_id=rid)
        return
    note(outcome="report", report_mode=mode, period=period, period_start=str(start), period_end=str(today), rows=len(rows))
    extra = money_lines(savings.overview(flow or []), lang)
    if not rows and not extra:
        await update.effective_message.reply_text(t(lang, "nothing_yet", title=title))
        return
    by_currency: dict[str, Decimal] = {}
    lines = []
    for category_id, category, currency, total in rows:
        by_currency[currency] = by_currency.get(currency, Decimal(0)) + total
        lines.append(f"{html.escape(category_label(category_id, category, lang))}: {fmt_money(total, currency)}")
    totals = " + ".join(fmt_money(v, k) for k, v in by_currency.items())
    head = f"<b>{title}</b> — {totals}" if totals else f"<b>{title}</b>"
    body = "\n\n" + "\n".join(lines) if lines else ""
    await update.effective_message.reply_text(head + body + extra, parse_mode=ParseMode.HTML)


def money_text(d: dict[str, Decimal]) -> str:
    return " + ".join(fmt_money(v, k) for k, v in d.items() if v) or "0"


def money_lines(o: savings.Overview, lang: str) -> str:
    """The income / put aside / left block under a report. Empty for someone who only logs spending."""
    if not (o.income or o.put_aside or o.taken_out):
        return ""
    lines = [""]
    if o.income:
        lines.append(f"💵 {t(lang, 'r_income')}: {money_text(o.income)}")
    if o.put_aside:
        lines.append(f"💰 {t(lang, 'r_put_aside')}: {money_text(o.put_aside)}")
    if o.taken_out:
        lines.append(f"🏦 {t(lang, 'r_taken_out')}: {money_text(o.taken_out)}")
    if o.income:
        lines.append(f"🟰 <b>{t(lang, 'r_left')}: {money_text(o.left)}</b>")
    return "\n" + "\n".join(lines)


def sum_by_currency(pairs) -> str:
    totals: dict[str, Decimal] = {}
    for amount, currency in pairs:
        totals[currency] = totals.get(currency, Decimal(0)) + Decimal(str(amount))
    return " + ".join(fmt_money(v, k) for k, v in totals.items())


def detailed_report(title: str, items: list[dict], lang: str, multi_day: bool,
                    index: list[tuple[int, str, str]] | None = None) -> str:
    """Every expense, newest first, numbered 1, 2, 3… across the whole report (the number is what
    🗑 Delete a line asks for); grouped under day headings when the period spans several days.
    `index` collects (number, expense_id, plain-text line) for each line."""
    n = 0

    def spent(rows):  # headings add up spending only; income and savings lines carry their own icon
        total = sum_by_currency((i["amount"], i["currency"]) for i in rows if (i.get("kind") or "expense") == "expense")
        return f" — {total}" if total else ""

    lines = [f"<b>{title}</b>{spent(items)}"]
    by_day: dict[date, list[dict]] = {}
    for i in items:
        by_day.setdefault(i["expense_date"], []).append(i)
    for day, day_items in by_day.items():
        lines.append("")
        if multi_day:
            lines.append(f"<b>{fmt_day(day, lang)}</b>{spent(day_items)}")
        for i in day_items:
            name = (i.get("item_label") if lang != i18n.DEFAULT_LANGUAGE else None) or i.get("description") or "—"
            merchant = f" ({html.escape(i['merchant'])})" if i.get("merchant") else ""
            kind = i.get("kind") or "expense"
            if kind == "expense":
                cat, icon = category_label(i.get("category_id"), i.get("category") or "", lang), ""
            else:
                cat, icon = kind_label(kind, lang) + goal_suffix(i.get("goal_id"), lang), savings.ICONS[kind] + " "
            n += 1
            lines.append(
                f"<b>{n}.</b> {icon}{fmt_money(i['amount'], i['currency'])} · {html.escape(name)}{merchant} — <i>{html.escape(cat)}</i>"
            )
            if index is not None and i.get("expense_id"):
                plain_merchant = f" ({i['merchant']})" if i.get("merchant") else ""
                index.append((n, i["expense_id"],
                              f"{fmt_day(day, lang)} · {fmt_money(i['amount'], i['currency'])} · {name}{plain_merchant} — {cat}"))
    return "\n".join(lines)


async def send_long(msg, text: str, limit: int = 4000, reply_markup=None) -> list:
    """Telegram caps a message at 4096 characters: split on line breaks, never inside a line's tags.
    `reply_markup` goes on the last part. Returns the messages sent."""
    sent, chunk = [], ""
    for line in text.split("\n"):
        if chunk and len(chunk) + len(line) + 1 > limit:
            sent.append(await msg.reply_text(chunk, parse_mode=ParseMode.HTML))
            chunk = ""
        chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        sent.append(await msg.reply_text(chunk, parse_mode=ParseMode.HTML, reply_markup=reply_markup))
    return sent


# ---- 🗑 Delete a line (detailed reports) ---------------------------------- #

MAX_LINES_PER_DELETE = 10
AWAIT_SECONDS = 600  # after 🗑 Delete a line / ✏️ Fix text, the next message is the answer for this long  # keeps the confirm button's data under Telegram's 64 bytes


@logged("button")
async def handle_report_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """rl:<report>  -> ask which line;   rx:<report>:<n,n>  -> delete them;   rx:no -> cancel."""
    query = update.callback_query
    if not await allowed(update, context):
        return
    lang = ulang(update)
    uid = query.from_user.id
    parts = query.data.split(":")
    if query.data == "rx:no":
        context.user_data.pop("awaiting", None)
        note(outcome="delete_lines_cancelled")
        await answer(query)
        await edit_text(query, t(lang, "dl_cancelled"))
        return
    rid = parts[1]
    rows = pending.report_rows(rid, uid)
    note(report_id=rid)
    if not rows:
        note(outcome="report_gone")
        await answer(query, t(lang, "dl_gone"), show_alert=True)
        return
    if parts[0] == "rl":
        await answer(query)
        # Wait for their next message (not ForceReply: it would replace the pinned ▶️ Start keyboard).
        context.user_data["awaiting"] = {"kind": "lines", "id": rid, "until": time.monotonic() + AWAIT_SECONDS}
        prompt = await update.effective_message.reply_text(
            t(lang, "dl_prompt", n=max(rows)),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="rx:no")]]),
        )
        if isinstance(getattr(prompt, "message_id", None), int):
            pending.add_report_message(rid, uid, update.effective_chat.id, prompt.message_id)
        note(outcome="delete_lines_prompt")
        return
    # rx:<rid>:<numbers> - confirmed
    context.user_data.pop("awaiting", None)
    await answer(query)
    numbers = [int(x) for x in parts[2].split(",") if x.isdigit()] if len(parts) > 2 else []
    deleted, gone = [], []
    for n in numbers:
        row = rows.get(n)
        if row is None:
            continue
        if row["deleted"]:
            gone.append(n)
            continue
        try:
            removed = await warehouse.delete(row["expense_id"], uid)
        except Exception as e:
            log.exception("Deleting a line failed")
            note(outcome="delete_lines_error", error=f"{type(e).__name__}: {e}"[:1000])
            key = "undo_sandbox" if "DML" in str(e) or "billing" in str(e).lower() else "dl_failed"
            await update.effective_message.reply_text(t(lang, key))
            break
        pending.mark_expense_deleted(row["expense_id"])
        if not removed:  # already gone (e.g. ↩️ Undo)
            gone.append(n)
            continue
        deleted.append(row)
        await forget_save(context, row["expense_id"], lang)
    note(outcome="lines_deleted", lines=[r["idx"] for r in deleted], expense_ids=[r["expense_id"] for r in deleted],
         already_gone=gone)
    text = t(lang, "dl_done", lines="\n".join(f"{r['idx']}. {r['summary']}" for r in deleted)) if deleted else ""
    if gone:
        text += ("\n\n" if text else "") + t(lang, "dl_already", lines=", ".join(map(str, gone)))
    await edit_text(query, text or t(lang, "dl_cancelled"))


async def forget_save(context: ContextTypes.DEFAULT_TYPE, expense_id: str, lang: str):
    """After deleting a saved expense: undo what it taught the dictionary and strike out its proposal,
    exactly like ↩️ Undo (when its save history is still on this machine)."""
    saved = pending.saved_entry(expense_id)
    if saved is None:
        return
    chat_id, message_id, summary, learned = saved
    pending.forget_saved(expense_id)
    if learned:
        try:
            await catalog.unlearn(learned["keys"], learned["category_id"], learned["corrected"])
        except Exception:
            log.exception("Dictionary rollback failed")
    try:
        await context.bot.edit_message_text(
            f"<s>{html.escape(summary)}</s>\n{t(lang, 'undone_line')}",
            chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass  # too old to edit


async def ask_delete_lines(update: Update, rid: str, text: str, lang: str) -> bool:
    """A reply to a detailed report (or to its "which line?" prompt) with line numbers: confirm first."""
    uid, msg = update.effective_user.id, update.effective_message
    rows = pending.report_rows(rid, uid)
    note(report_id=rid)
    if not rows:
        note(outcome="report_gone")
        await msg.reply_text(t(lang, "dl_gone"))
        return True
    wanted = list(dict.fromkeys(int(x) for x in re.findall(r"\d+", text or "")))
    if not wanted or any(n not in rows for n in wanted) or len(wanted) > MAX_LINES_PER_DELETE:
        note(outcome="delete_lines_bad_input")
        await msg.reply_text(t(lang, "dl_bad", n=max(rows), max=MAX_LINES_PER_DELETE))
        return False
    live = [n for n in wanted if not rows[n]["deleted"]]
    if not live:
        note(outcome="already_deleted")
        await msg.reply_text(t(lang, "dl_already", lines=", ".join(map(str, wanted))))
        return True
    note(outcome="delete_lines_confirm", lines=live)
    await msg.reply_text(
        t(lang, "dl_confirm", lines="\n".join(f"{n}. {rows[n]['summary']}" for n in live)),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "btn_dl_yes", n=len(live)), callback_data=f"rx:{rid}:{','.join(map(str, live))}"),
            InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="rx:no"),
        ]]),
    )
    return True


@logged("command")
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _report(update, context, "today")


@logged("command")
async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _report(update, context, "week")


@logged("command")
async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _report(update, context, "month")


@logged("command")
async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    lang = ulang(update)
    last = pending.last_saved(update.effective_user.id)
    if not last:
        note(outcome="nothing_to_undo")
        await update.effective_message.reply_text(t(lang, "nothing_to_undo"))
        return
    expense_id, chat_id, message_id, summary, learned = last
    try:
        await warehouse.delete(expense_id, update.effective_user.id)
    except Exception as e:
        log.exception("Undo failed")
        note(outcome="undo_error", expense_id=expense_id, error=f"{type(e).__name__}: {e}"[:1000])
        if "DML" in str(e) or "billing" in str(e).lower():
            await update.effective_message.reply_text(t(lang, "undo_sandbox"))
        else:
            await update.effective_message.reply_text(t(lang, "undo_failed"))
        return
    pending.forget_saved(expense_id)
    note(outcome="undone", expense_id=expense_id, summary=summary)
    if learned:
        try:
            await catalog.unlearn(learned["keys"], learned["category_id"], learned["corrected"])
        except Exception:
            log.exception("Dictionary rollback failed")
    try:
        await context.bot.edit_message_text(
            f"<s>{html.escape(summary)}</s>\n{t(lang, 'undone_line')}",
            chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass  # original message may be too old to edit
    await update.effective_message.reply_text(t(lang, "removed_summary", s=summary))


# --------------------------------------------------------------------------- #
# 💰 Savings: income, money put aside, goals                                  #
# --------------------------------------------------------------------------- #


def savings_keyboard(user_id: int, lang: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(t(lang, "btn_new_goal"), callback_data="sv:new")]]
    if goals.of(user_id):
        rows[0].append(InlineKeyboardButton(t(lang, "btn_close_goal"), callback_data="sv:cl"))
    return InlineKeyboardMarkup(rows)


def savings_text(user_id: int, o: savings.Overview, lang: str, today: date) -> str:
    lines = [f"💰 <b>{t(lang, 'sv_title')}</b>"]
    balance = {c: v for c, v in o.balance.items() if v}
    lines.append(f"{t(lang, 'sv_total')}: <b>{money_text(balance)}</b>")
    lines += ["", f"<b>{fmt_month(today, lang)}</b>"]
    lines.append(f"💵 {t(lang, 'r_income')}: {money_text(o.income)}")
    lines.append(f"🧾 {t(lang, 'r_spent')}: {money_text(o.spent)}")
    lines.append(f"💰 {t(lang, 'r_put_aside')}: {money_text(o.put_aside)}")
    if o.taken_out:
        lines.append(f"🏦 {t(lang, 'r_taken_out')}: {money_text(o.taken_out)}")
    if o.income:
        lines.append(f"🟰 {t(lang, 'r_left')}: <b>{money_text(o.left)}</b>")
        rates = [(c, o.savings_rate(c)) for c in o.income]
        shown = [f"{r}%" + (f" ({c})" if len(rates) > 1 else "") for c, r in rates if r is not None]
        if shown:
            lines.append(f"📈 {t(lang, 'sv_rate')}: {', '.join(shown)}")
    mine = goals.of(user_id)
    lines += ["", f"🎯 <b>{t(lang, 'sv_goals')}</b>"]
    if not mine:
        lines.append(t(lang, "sv_no_goals"))
    for g in mine:
        p = savings.progress(g, o.by_goal.get(g.goal_id, {}), today)
        head = f"<b>{html.escape(g.name)}</b> — {fmt_money(p.saved, g.currency)}"
        if g.target:
            head += f" / {fmt_money(g.target, g.currency)}"
        lines.append(head)
        if p.others:
            lines.append("   + " + money_text(p.others))
        if g.target:
            detail = f"{savings.bar(p.percent)} {p.percent}%"
            if p.reached:
                detail += " · " + t(lang, "sv_reached")
            elif p.overdue:
                detail += " · " + t(lang, "sv_overdue", date=fmt_date(g.deadline, lang))
            elif p.per_month is not None:
                detail += " · " + t(lang, "sv_per_month", amount=fmt_money(p.per_month, g.currency),
                                    date=fmt_date(g.deadline, lang))
            lines.append("   " + detail)
        elif g.deadline:
            lines.append("   " + t(lang, "sv_by", date=fmt_date(g.deadline, lang)))
    lines += ["", t(lang, "sv_how")]
    return "\n".join(lines)


@logged("command")
async def cmd_savings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    await show_savings(update, ulang(update))


async def show_savings(update: Update, lang: str):
    uid = update.effective_user.id
    today = datetime.now(config.TIMEZONE).date()
    try:
        flow = await warehouse.money_flow(uid, today.replace(day=1), today)
    except Exception as e:
        log.exception("Savings query failed")
        note(outcome="report_error", error=f"{type(e).__name__}: {e}"[:1000])
        await update.effective_message.reply_text(t(lang, "report_failed"))
        return
    note(outcome="savings_shown", goals=len(goals.of(uid)))
    await update.effective_message.reply_text(
        savings_text(uid, savings.overview(flow), lang, today), parse_mode=ParseMode.HTML,
        reply_markup=savings_keyboard(uid, lang),
    )


@logged("button")
async def handle_savings_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sv:new -> ask for a goal · sv:x -> cancel · sv:cl -> which goal to close · sv:c:<id> -> confirm ·
    sv:cy:<id> -> close it · sv:show -> the savings screen."""
    query = update.callback_query
    if not await allowed(update, context):
        return
    lang = ulang(update)
    uid = query.from_user.id
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "new":
        if len(goals.of(uid)) >= savings.MAX_OPEN_GOALS:
            note(outcome="too_many_goals")
            await answer(query, t(lang, "goal_too_many", n=savings.MAX_OPEN_GOALS), show_alert=True)
            return
        await answer(query)
        context.user_data["awaiting"] = {"kind": "goal", "id": "", "until": time.monotonic() + AWAIT_SECONDS}
        note(outcome="goal_prompt")
        await update.effective_message.reply_text(
            t(lang, "goal_prompt", currency=config.DEFAULT_CURRENCY), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="sv:x")]]),
        )
    elif action == "x":
        context.user_data.pop("awaiting", None)
        note(outcome="goal_cancelled")
        await answer(query)
        await edit_text(query, t(lang, "goal_cancelled"))
    elif action == "show":
        await answer(query)
        await show_savings(update, lang)
    elif action == "cl":
        mine = goals.of(uid)
        await answer(query)
        if not mine:
            await update.effective_message.reply_text(t(lang, "sv_no_goals"))
            return
        note(outcome="close_goal_menu")
        await update.effective_message.reply_text(
            t(lang, "goal_which_close"),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"🎯 {g.name}", callback_data=f"sv:c:{g.goal_id}")] for g in mine]
                + [[InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="sv:x")]]
            ),
        )
    elif action in ("c", "cy"):
        g = goals.get(parts[2] if len(parts) > 2 else None, uid)
        if g is None or not g.is_open:
            note(outcome="goal_gone")
            await answer(query, t(lang, "goal_gone"), show_alert=True)
            return
        await answer(query)
        if action == "c":
            await edit_text(query, t(lang, "goal_close_confirm", name=html.escape(g.name)), parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton(t(lang, "btn_close_goal_yes"), callback_data=f"sv:cy:{g.goal_id}"),
                                InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="sv:x"),
                            ]]))
            return
        try:
            async with goals.lock:
                await goals.close(g.goal_id)
        except Exception as e:
            log.exception("Closing a goal failed")
            note(outcome="goal_error", error=f"{type(e).__name__}: {e}"[:1000])
            await update.effective_message.reply_text(t(lang, "goal_failed"))
            return
        note(outcome="goal_closed", goal_id=g.goal_id)
        await edit_text(query, t(lang, "goal_closed", name=html.escape(g.name)), parse_mode=ParseMode.HTML)
    else:
        await answer(query)


async def create_goal(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, lang: str):
    """The answer to 🎯 New goal: "Trip to Japan 2 000 000 by June" -> a goal."""
    uid, msg = update.effective_user.id, update.effective_message
    try:
        parsed = await extractor.parse_goal(text, sent_at=msg.date)
    except Exception as e:
        log.exception("Goal parsing failed")
        note(outcome="parse_error", error=f"{type(e).__name__}: {e}"[:1000])
        await msg.reply_text(t(lang, "parse_failed"))
        return
    if parsed is None:
        note(outcome="goal_not_understood")
        context.user_data["awaiting"] = {"kind": "goal", "id": "", "until": time.monotonic() + AWAIT_SECONDS}
        await msg.reply_text(t(lang, "goal_not_understood"), parse_mode=ParseMode.HTML)
        return
    async with goals.lock:
        if goals.by_name(uid, parsed.name):
            note(outcome="goal_exists")
            await msg.reply_text(t(lang, "goal_exists", name=html.escape(parsed.name)), parse_mode=ParseMode.HTML)
            return
        if len(goals.of(uid)) >= savings.MAX_OPEN_GOALS:
            note(outcome="too_many_goals")
            await msg.reply_text(t(lang, "goal_too_many", n=savings.MAX_OPEN_GOALS))
            return
        try:
            g = await goals.create(uid, parsed.name, parsed.target, parsed.currency, parsed.deadline)
        except Exception as e:
            log.exception("Creating a goal failed")
            note(outcome="goal_error", error=f"{type(e).__name__}: {e}"[:1000])
            await msg.reply_text(t(lang, "goal_failed"))
            return
    note(outcome="goal_created", goal_id=g.goal_id, target=str(g.target) if g.target else None,
         currency=g.currency, deadline=str(g.deadline) if g.deadline else None)
    details = []
    if g.target:
        details.append(t(lang, "goal_target", amount=fmt_money(g.target, g.currency)))
    if g.deadline:
        details.append(t(lang, "sv_by", date=fmt_date(g.deadline, lang)))
        if g.target:
            months = savings.months_until(datetime.now(config.TIMEZONE).date(), g.deadline)
            per = (g.target / months).quantize(Decimal("1"), rounding="ROUND_CEILING")
            details.append(t(lang, "goal_needs", amount=fmt_money(per, g.currency)))
    await msg.reply_text(
        t(lang, "goal_created", name=html.escape(g.name), details=" · ".join(details),
          example=html.escape(g.name)),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "m_savings"), callback_data="sv:show")]]),
    )


# --------------------------------------------------------------------------- #
# Owner tools                                                                 #
# --------------------------------------------------------------------------- #

USERS_SHOWN = 20


@logged("command")
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Everyone who has used (or tried to use) the bot, most active first. Owner only."""
    if not await allowed(update, context):
        return
    lang = ulang(update)
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        note(outcome="not_owner")
        await msg.reply_text(t(lang, "owner_only"))
        return
    await interaction_log.flush()  # include the last few seconds of activity
    try:
        rows = await warehouse.user_activity()
    except Exception as e:
        log.exception("Users report failed")
        note(outcome="report_error", error=f"{type(e).__name__}: {e}"[:1000])
        await msg.reply_text(t(lang, "users_failed"))
        return
    note(outcome="users_report", users=len(rows))
    if not rows:
        await msg.reply_text(t(lang, "users_none"))
        return
    active = sum(1 for r in rows if r["actions_30d"])
    lines = [t(lang, "users_title", n=len(rows), a=active), ""]
    blocked = pending.blocked_ids()
    for i, r in enumerate(rows[:USERS_SHOWN], 1):
        uid = r["user_id"]
        member = household.member(uid)
        if is_admin(uid):
            icon = "👑"
        elif uid in blocked:
            icon = "🚫"
        elif member and member.role == "owner":
            icon = "🏠"
        elif member:
            icon = "👤"
        else:
            icon = "🙂"
        name = r.get("name") or (member.display_name if member else None)
        handle = f"@{r['username']}" if r.get("username") else None
        if name and handle:
            name = f"{name} ({handle})"
        name = name or handle or str(uid)
        last = r["last_seen"].astimezone(config.TIMEZONE)
        lines.append(t(
            lang, "users_line", i=i, icon=icon, name=html.escape(name), uid=uid,
            actions=r["actions"], recent=r["actions_30d"], saved=r["saved"],
            last=f"{fmt_day(last.date(), lang)} {last:%H:%M}",
        ))
    if len(rows) > USERS_SHOWN:
        lines.append(t(lang, "users_more", n=len(rows) - USERS_SHOWN))
    lines += ["", f"<i>{t(lang, 'users_legend')}</i>"]
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# 🖥 App status page (dashboard.py): local to the Mac, not part of Telegram     #
# --------------------------------------------------------------------------- #


def local_metrics(days: int) -> dict:
    """What only the running bot knows, for the 🖥 App status page."""
    now = datetime.now(timezone.utc)
    today = datetime.now(config.TIMEZONE).date().isoformat()
    db = pending.db
    oldest = db.execute("SELECT MIN(created_at) FROM pending").fetchone()[0]
    usage = {k: n for k, n in db.execute(
        "SELECT kind, SUM(n) FROM usage WHERE day = ? GROUP BY kind", (today,)).fetchall()}
    at_limit = 0
    for kind, limit in (("text", config.DAILY_TEXT_LIMIT), ("voice", config.DAILY_VOICE_LIMIT)):
        if limit > 0:
            at_limit += db.execute(
                "SELECT COUNT(*) FROM usage WHERE day = ? AND kind = ? AND n >= ? AND user_id IS NOT ?",
                (today, kind, limit, config.OWNER_USER_ID)).fetchone()[0]
    per_user: dict[str, dict[str, int]] = {}
    for uid, kind, n in db.execute("SELECT user_id, kind, n FROM usage WHERE day = ?", (today,)).fetchall():
        per_user.setdefault(str(uid), {})[kind] = n
    fallback = interaction_log.fallback
    since = (now - timedelta(days=days)).isoformat()
    downtime, outages = 0.0, pending.outages_since(since)
    for started, ended in outages:
        a = max(datetime.fromisoformat(started), now - timedelta(days=days))
        b = datetime.fromisoformat(ended) if ended else now
        downtime += max(0.0, (b - a).total_seconds())
    return {
        "started_at": STARTED_AT.isoformat(),
        "deleted_in_period": db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM deletions WHERE deleted_at >= ?", (since,)).fetchone()[0],
        "restored_in_period": db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM deletions WHERE deleted_at >= ? AND status = 'restored'",
            (since,)).fetchone()[0],
        "outages": len(outages),
        "downtime_seconds": round(downtime),
        "down_now": _network["down_since"] is not None,
        "last_outage": ({"started_at": outages[-1][0], "ended_at": outages[-1][1]} if outages else None),
        "last_update_at": _last_update["at"].isoformat() if _last_update["at"] else None,
        "now": now.isoformat(),
        "pending": db.execute("SELECT COUNT(*) FROM pending").fetchone()[0],
        "pending_oldest": oldest,
        "log_buffer": len(interaction_log.buffer),
        "log_fallback_rows": sum(1 for _ in fallback.open(encoding="utf-8")) if fallback.exists() else 0,
        "log_purges_open": len(pending.open_log_purges()),
        "deletions_waiting": len(soft := pending.soft_deletions()),
        "next_erase_at": soft[0]["purge_after"] if soft else None,
        "blocked": len(pending.blocked_ids()),
        "households": len(household.homes),
        "household_members": len(household.members),
        "usage_today": {"text": usage.get("text", 0), "voice": usage.get("voice", 0)},
        "at_limit_today": at_limit,
        "upload_keys": db.execute("SELECT COUNT(*) FROM upload_keys").fetchone()[0],
        "config": {
            "llm_provider": config.LLM_PROVIDER,
            "llm_model": {"gemini": config.GEMINI_MODEL, "claude": config.CLAUDE_MODEL,
                          "openai": config.OPENAI_MODEL}[config.LLM_PROVIDER],
            "transcriber": "gemini" if config.LLM_PROVIDER == "gemini" else config.TRANSCRIBER,
            "whisper_model": config.WHISPER_MODEL,
            "daily_text_limit": config.DAILY_TEXT_LIMIT,
            "daily_voice_limit": config.DAILY_VOICE_LIMIT,
            "max_voice_seconds": config.MAX_VOICE_SECONDS,
            "auto_save_minutes": config.AUTO_SAVE_MINUTES,
            "upload_endpoint": bool(config.INGEST_SECRET),
            "public_url": bool(config.INGEST_PUBLIC_URL),
            "timezone": str(config.TIMEZONE),
            "price_input": config.LLM_PRICE_INPUT,
            "price_output": config.LLM_PRICE_OUTPUT,
            "currency": config.DEFAULT_CURRENCY,
            "categories": len(catalog.categories),
        },
        "user_state": {  # for the users table
            "blocked": sorted(pending.blocked_ids()),
            "households": {str(uid): m.role for uid, m in household.members.items()},
            "owner": config.OWNER_USER_ID,
            "usage_today": per_user,
        },
    }


metrics = dashboard.Metrics(warehouse.client, warehouse.ds, local_metrics)


# --------------------------------------------------------------------------- #
# Owner: block / unblock                                                     #
# --------------------------------------------------------------------------- #


@logged("command")
async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_blocked(update, context, True)


@logged("command")
async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_blocked(update, context, False)


async def set_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE, block: bool):
    """Owner only: /block <user_id> stops someone using the bot (their data stays); /unblock undoes it."""
    if not await allowed(update, context):
        return
    lang = ulang(update)
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        note(outcome="not_owner")
        await msg.reply_text(t(lang, "owner_only"))
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        note(outcome="bad_args")
        await msg.reply_text(t(lang, "block_usage"), parse_mode=ParseMode.HTML)
        return
    target = int(args[0])
    note(target_user_id=target)
    if is_admin(target):
        note(outcome="bad_args")
        await msg.reply_text(t(lang, "block_self"))
        return
    if block:
        pending.block(target)
        note(outcome="user_blocked")
        await msg.reply_text(t(lang, "blocked_done", uid=target), parse_mode=ParseMode.HTML)
    elif pending.unblock(target):
        note(outcome="user_unblocked")
        await msg.reply_text(t(lang, "unblocked_done", uid=target), parse_mode=ParseMode.HTML)
    else:
        note(outcome="not_blocked")
        await msg.reply_text(t(lang, "not_blocked", uid=target), parse_mode=ParseMode.HTML)
    await users.set_role([target], role_of(target))


# --------------------------------------------------------------------------- #
# Households                                                                  #
# --------------------------------------------------------------------------- #


def invite_link(bot_username: str | None, code: str) -> str:
    return f"https://t.me/{bot_username or 'your_bot'}?start=join_{code}"


def create_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_create_household"), callback_data="hh:create")]])


def household_view(user_id: int, lang: str, bot_username: str | None) -> tuple[str, InlineKeyboardMarkup]:
    """🏠 Household: members, and what this person may do (the creator invites/removes/ends; others leave)."""
    home = household.home_of(user_id)
    if home is None:
        return t(lang, "hh_none"), create_keyboard(lang)
    lines = [t(lang, "hh_title", name=html.escape(home.name, quote=False)), ""]
    members = household.members_of(home.household_id)
    for m in members:
        tags = (t(lang, "hh_owner_tag") if m.role == "owner" else "") + (t(lang, "hh_you_tag") if m.user_id == user_id else "")
        lines.append(f"• {html.escape(m.display_name, quote=False)}{tags}")
    rows = []
    if household.is_creator(user_id):
        lines += ["", t(lang, "hh_invite", link=html.escape(invite_link(bot_username, home.invite_code), quote=False))]
        rows.append([InlineKeyboardButton(t(lang, "btn_new_link"), callback_data="hh:link")])
        for m in members:
            if m.role != "owner":
                rows.append([InlineKeyboardButton(
                    t(lang, "btn_remove_member", name=m.display_name), callback_data=f"hh:rm:{m.user_id}"
                )])
        rows.append([InlineKeyboardButton(t(lang, "btn_end_household"), callback_data="hh:end")])
    else:
        lines += ["", t(lang, "hh_member_note")]
        rows.append([InlineKeyboardButton(t(lang, "btn_leave_household"), callback_data="hh:leave")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


@logged("command")
async def cmd_household(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    lang = ulang(update)
    uid = update.effective_user.id
    home = household.home_of(uid)
    note(outcome="household_status" if home else "household_none", household_id=home.household_id if home else None)
    text, kb = household_view(uid, lang, context.bot.username)
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True
    )


async def show_join_prompt(msg, user, code: str, lang: str):
    """Someone opened an invite link (t.me/<bot>?start=join_<code>)."""
    home = household.by_code(code)
    mine = household.home_of(user.id)
    note(invite_code_valid=home is not None)
    if home is None:
        note(outcome="invite_invalid")
        await msg.reply_text(t(lang, "hh_invite_invalid"))
    elif mine and mine.household_id == home.household_id:
        note(outcome="already_member")
        await msg.reply_text(t(lang, "hh_already_in", name=home.name))
    elif mine:
        note(outcome="in_other_household")
        await msg.reply_text(t(lang, "hh_in_other", mine=mine.name, other=home.name))
    else:
        note(outcome="join_prompt", household_id=home.household_id)
        creator = household.member(home.created_by)
        await msg.reply_text(
            t(lang, "hh_join_prompt", name=html.escape(home.name, quote=False),
              creator=html.escape(creator.display_name if creator else "?", quote=False)),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(t(lang, "btn_join"), callback_data=f"hh:join:{code}"),
                  InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="hh:nojoin")]]
            ),
        )


async def tell(context: ContextTypes.DEFAULT_TYPE, user_id: int, key: str, **params):
    """A note to someone other than the person tapping (e.g. "Aigerim joined your household")."""
    try:
        await context.bot.send_message(user_id, t(stored_lang(user_id), key, **params))
    except Exception:  # they blocked the bot, or never opened it
        log.info("Couldn't message %s", user_id)


@logged("button")
async def handle_household_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    lang = ulang(update)
    uid = query.from_user.id
    if not await allowed(update, context):
        return
    parts = query.data.split(":")
    action, arg = parts[1], (parts[2] if len(parts) > 2 else None)
    me = display_name(query.from_user)
    bot_username = context.bot.username

    async def show(toast: str | None = None):
        await answer(query, toast)
        text, kb = household_view(uid, lang, bot_username)
        await edit_text(query, text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)

    creator_only = {"link", "rm", "end", "endok"}
    async with household.lock:  # one change at a time, checked against the current state
        home = household.home_of(uid)
        if action in creator_only and not household.is_creator(uid):
            note(outcome="not_creator")
            await answer(query, t(lang, "hh_creator_only"), show_alert=True)
            return

        if action == "create":
            if home is None:
                name = t(lang, "hh_default_name", name=query.from_user.first_name or me)
                home = await household.create(uid, me, name)
                await users.set_role([uid], "household_owner")
                note(outcome="household_created", household_id=home.household_id)
                await show(t(lang, "hh_created_toast"))
            else:  # an old "Create" button, tapped while already in a household
                note(outcome="household_status")
                await show()

        elif action == "link":
            await household.new_link(home.household_id)
            note(outcome="invite_link_reset", household_id=home.household_id)
            await show(t(lang, "hh_new_link_toast"))

        elif action == "rm":
            target = household.member(int(arg)) if arg and arg.lstrip("-").isdigit() else None
            if target is None or target.household_id != home.household_id or target.role == "owner":
                note(outcome="already_handled")
                await show()
                return
            await household.leave(target.user_id)
            await users.set_role([target.user_id], "none")
            note(outcome="member_removed", member_user_id=target.user_id, household_id=home.household_id)
            await tell(context, target.user_id, "hh_you_were_removed", name=home.name)
            await show(t(lang, "hh_removed_toast"))

        elif action == "end":
            note(outcome="household_end_prompt")
            await answer(query)
            await edit_text(query, 
                t(lang, "hh_end_confirm"),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(t(lang, "btn_yes_end"), callback_data="hh:endok"),
                      InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="hh:cancel")]]
                ),
            )

        elif action == "endok":
            former = await household.end(home.household_id)
            others = [x for x in former if x != uid]
            await users.set_role(former, "none")
            note(outcome="household_ended", household_id=home.household_id, members=len(former))
            for x in others:
                await tell(context, x, "hh_ended_by_creator", name=home.name)
            await answer(query, t(lang, "hh_ended_toast"))
            await edit_text(query, t(lang, "hh_ended"), reply_markup=create_keyboard(lang))

        elif action == "leave":
            if home is None or household.is_creator(uid):
                await show()
                return
            note(outcome="household_leave_prompt")
            await answer(query)
            await edit_text(query, 
                t(lang, "hh_leave_confirm", name=home.name),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(t(lang, "btn_yes_leave"), callback_data="hh:leaveok"),
                      InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="hh:cancel")]]
                ),
            )

        elif action == "leaveok":
            if home is None or household.is_creator(uid):
                await show()
                return
            await household.leave(uid)
            await users.set_role([uid], "none")
            note(outcome="household_left", household_id=home.household_id)
            await tell(context, home.created_by, "hh_member_left", who=me, name=home.name)
            await answer(query)
            await edit_text(query, t(lang, "hh_left", name=home.name), reply_markup=create_keyboard(lang))

        elif action == "join":
            target = household.by_code(arg or "")
            if target is None:
                note(outcome="invite_invalid")
                await answer(query)
                await edit_text(query, t(lang, "hh_invite_invalid"))
            elif home is not None:
                note(outcome="in_other_household" if home.household_id != target.household_id else "already_member")
                await show()
            else:
                await household.join(uid, me, target)
                await users.set_role([uid], "member")
                note(outcome="household_joined", household_id=target.household_id)
                await tell(context, target.created_by, "hh_member_joined", who=me, name=target.name)
                await answer(query, t(lang, "hh_joined_toast"))
                await edit_text(query, 
                    t(lang, "hh_joined", name=html.escape(target.name, quote=False)), parse_mode=ParseMode.HTML
                )

        elif action == "nojoin":
            note(outcome="join_declined")
            await answer(query)
            await edit_text(query, t(lang, "hh_join_cancelled"))

        else:  # cancel / back
            note(outcome="back")
            await show()


@logged("command")
async def cmd_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    lang = ulang(update)
    msg = update.effective_message
    home = household.home_of(update.effective_user.id)
    if home is None:
        note(outcome="household_none")
        await msg.reply_text(t(lang, "hh_none"), parse_mode=ParseMode.HTML, reply_markup=create_keyboard(lang))
        return
    today = datetime.now(config.TIMEZONE).date()
    period = (context.args or ["month"])[0].lower()
    h = html.escape(home.name, quote=False)
    if period == "today":
        start, title = today, t(lang, "family_title_today", h=h, d=fmt_day(today, lang))
    elif period == "week":
        start = today - timedelta(days=6)
        title = t(lang, "family_title_week", h=h, a=fmt_day(start, lang), b=fmt_day(today, lang))
    else:
        start, title = today.replace(day=1), t(lang, "family_title_month", h=h, m=fmt_month(today, lang))
    try:
        rows = await household.family_totals(home.household_id, start, today)
    except Exception as e:
        log.exception("Family report failed")
        note(outcome="report_error", error=f"{type(e).__name__}: {e}"[:1000])
        await msg.reply_text(t(lang, "family_failed"))
        return
    note(outcome="family_report", period=period, rows=len(rows))
    if not rows:
        await msg.reply_text(t(lang, "nothing_yet", title=title))
        return
    per_member: dict[str, dict[str, Decimal]] = {}
    per_cat: dict[tuple[str, str], Decimal] = {}
    for member, category_id, category, currency, total in rows:
        per_member.setdefault(member, {}).setdefault(currency, Decimal(0))
        per_member[member][currency] += total
        key = (category_label(category_id, category, lang), currency)
        per_cat[key] = per_cat.get(key, Decimal(0)) + total
    lines = [f"<b>{title}</b>", ""]
    lines += [
        f"👤 {html.escape(m, quote=False)}: " + " + ".join(fmt_money(v, c) for c, v in cur.items())
        for m, cur in per_member.items()
    ]
    lines.append("")
    lines += [
        f"{html.escape(cat, quote=False)}: {fmt_money(v, cur)}"
        for (cat, cur), v in sorted(per_cat.items(), key=lambda kv: kv[1], reverse=True)
    ]
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Everything else                                                             #
# --------------------------------------------------------------------------- #


@logged("membership")
async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram tells the bot when someone blocks it (status "kicked") or unblocks it again.
    Logged so 🖥 App status can count people who left."""
    change = update.my_chat_member
    if change is None or change.chat.type != "private":
        note(outcome="ignored")
        return
    old, new = change.old_chat_member.status, change.new_chat_member.status
    note(old_status=old, new_status=new)
    if new == "kicked":
        note(outcome="bot_blocked")
    elif old == "kicked":
        note(outcome="bot_unblocked")
    else:
        note(outcome="membership_changed")


@logged("edit")
async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edited messages are not re-processed: that would create a duplicate proposal."""
    if not can_use(update.effective_user.id if update.effective_user else None):
        note(outcome="blocked")
        return
    note(outcome="edit_ignored")
    await update.effective_message.reply_text(t(ulang(update), "edit_ignored"))


@logged("command")
async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    note(outcome="unknown_command")
    await update.effective_message.reply_text(t(ulang(update), "unknown_command"))


@logged("other")
async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photos, stickers, files… — logged, and the user gets a hint."""
    if not await allowed(update, context):
        return
    note(outcome="unsupported")
    await update.effective_message.reply_text(t(ulang(update), "unsupported"))


_network = {"down_since": None, "warned_at": 0.0}
NETWORK_WARN_EVERY = 600  # seconds between "still can't reach Telegram" lines


def network_back():
    """Called on the first update after an outage: one line saying how long it lasted."""
    since = _network["down_since"]
    if since is not None:
        _network["down_since"] = None
        pending.outage_ended(datetime.now(timezone.utc).isoformat())
        secs = int((datetime.now(timezone.utc) - since).total_seconds())
        log.info("Telegram reachable again after %s", f"{secs // 60} min {secs % 60} s" if secs >= 60 else f"{secs} s")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Last line of defence: log the crash (the interaction row already has the traceback) and tell the user."""
    err = context.error
    if update is None and isinstance(err, (NetworkError, TimedOut)) and not isinstance(err, BadRequest):
        # Fetching updates failed: no internet, Mac asleep, Telegram hiccup. The library keeps retrying
        # and Telegram holds messages for 24 h, so this is a warning, not a crash.
        now = time.monotonic()
        if _network["down_since"] is None:
            _network["down_since"] = datetime.now(timezone.utc)
            _network["warned_at"] = now
            pending.outage_started(_network["down_since"].isoformat())
            log.warning("Can't reach Telegram (%s). Retrying automatically; messages wait on Telegram's side.",
                        f"{type(err).__name__}: {err}".strip(": "))
        elif now - _network["warned_at"] >= NETWORK_WARN_EVERY:
            _network["warned_at"] = now
            log.warning("Still can't reach Telegram (since %s UTC). Check the Mac's internet connection.",
                        f"{_network['down_since']:%H:%M}")
        return
    log.error("Unhandled error", exc_info=err)
    try:
        if isinstance(update, Update):
            try:
                lang = lang_for(update.effective_user)
            except Exception:  # the last-resort handler must not depend on anything that can fail
                lang = i18n.DEFAULT_LANGUAGE
            if update.effective_message:
                await update.effective_message.reply_text(t(lang, "error_msg"))
            elif update.callback_query:
                await update.callback_query.answer(t(lang, "error_toast"), show_alert=True)
    except Exception:
        pass


interaction_log = InteractionLogger(f"{warehouse.ds}.log_interactions", warehouse.client)
set_logger(interaction_log)


async def _post_init(app: Application):
    global _app
    _app = app
    app.bot_data["log_task"] = asyncio.create_task(interaction_log.run())
    if config.INGEST_SECRET:  # Action Button uploads (this port is the one the tunnel forwards)
        import ingest

        try:
            app.bot_data["ingest_runner"] = await ingest.start(handle_upload, upload_user)
        except OSError:
            log.exception("Upload endpoint could not start (port %s busy?)", config.INGEST_PORT)
    if config.DASHBOARD:  # separate server, 127.0.0.1 only: the dashboard never goes through the tunnel
        try:
            app.bot_data["dashboard_runner"] = await dashboard.start(metrics)
        except OSError:
            log.exception("Dashboard could not start (port %s busy?)", config.DASHBOARD_PORT)
    app.bot_data["purge_task"] = asyncio.create_task(log_purge_loop())
    app.bot_data["auto_save_task"] = asyncio.create_task(auto_save_loop())
    saved = dict(pending.all_languages())
    if config.OWNER_USER_ID is not None:  # the owner always gets their owner-only commands
        saved.setdefault(config.OWNER_USER_ID, stored_lang(config.OWNER_USER_ID))
    if not config.COMMAND_MENU:
        await clear_command_menus(app.bot, list(saved))
        return
    # The "/" command list in Telegram, per app language (replaces BotFather's /setcommands).
    for code, commands in i18n.COMMANDS.items():
        try:
            await app.bot.set_my_commands(
                [BotCommand(c, d) for c, d in commands],
                language_code=None if code == i18n.DEFAULT_LANGUAGE else code,
            )
        except Exception:
            log.exception("Could not set the %s command menu", code)
    for user_id, lang in saved.items():
        if lang in i18n.COMMANDS:
            await sync_chat_commands(app.bot, user_id, lang, force=True)


async def clear_command_menus(bot, chat_ids: list[int]):
    """No "/" list anywhere, so Telegram hides the ☰ button. Typed commands keep working."""
    targets = [{}] + [{"language_code": code} for code in i18n.LANGUAGES if code != i18n.DEFAULT_LANGUAGE]
    targets += [{"scope": BotCommandScopeChat(cid)} for cid in chat_ids]
    for kwargs in targets:
        try:
            await bot.delete_my_commands(**kwargs)
        except Exception:
            log.exception("Could not clear the command list %s", kwargs)
    log.info("Command menu off: cleared %d command lists", len(targets))


async def _post_shutdown(app: Application):
    for name in ("log_task", "purge_task", "auto_save_task"):
        task = app.bot_data.get(name)
        if task:
            task.cancel()
    for name in ("ingest_runner", "dashboard_runner"):
        runner = app.bot_data.get(name)
        if runner:
            await runner.cleanup()
    await interaction_log.close()


def main():
    if config.OWNER_USER_ID is None:
        log.warning("OWNER_USER_ID is empty — the bot will only reply with the sender's ID.")
    warehouse.ensure_shared()
    changed = warehouse.migrate_fact_tables()  # kind / goal_id on tables made before savings existed
    if changed:
        log.info("Added the savings columns to %d expense tables", changed)
    interaction_log.ensure_table()
    pending.outage_ended(datetime.now(timezone.utc).isoformat())  # an outage still open from before a restart
    catalog.load()
    household.setup(config.OWNER_USER_ID)
    goals.setup()
    users.ensure_table(config.OWNER_USER_ID, [u for u in household.members if not is_admin(u)])
    import transcribe

    transcribe.warm_up()
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    new = ~filters.UpdateType.EDITED
    app.add_handler(TypeHandler(Update, track_user), group=-1)  # before everything else, for every update
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))
    app.add_handler(CommandHandler("start", cmd_start, filters=new))
    app.add_handler(CommandHandler("help", cmd_help, filters=new))
    app.add_handler(CommandHandler("categories", cmd_categories, filters=new))
    app.add_handler(CommandHandler("today", cmd_today, filters=new))
    app.add_handler(CommandHandler("week", cmd_week, filters=new))
    app.add_handler(CommandHandler("month", cmd_month, filters=new))
    app.add_handler(CommandHandler("savings", cmd_savings, filters=new))
    app.add_handler(CommandHandler("undo", cmd_undo, filters=new))
    app.add_handler(CommandHandler("reload", cmd_reload, filters=new))
    app.add_handler(CommandHandler("language", cmd_language, filters=new))
    app.add_handler(CommandHandler("household", cmd_household, filters=new))
    app.add_handler(CommandHandler("users", cmd_users, filters=new))
    app.add_handler(CommandHandler("delete_my_data", cmd_delete, filters=new))
    app.add_handler(CommandHandler("restore_my_data", cmd_restore, filters=new))
    app.add_handler(CommandHandler("family", cmd_family, filters=new))
    app.add_handler(CommandHandler("shortcut", cmd_shortcut, filters=new))
    app.add_handler(CommandHandler("block", cmd_block, filters=new))
    app.add_handler(CommandHandler("unblock", cmd_unblock, filters=new))
    app.add_handler(MessageHandler(new & filters.Text(MENU_BUTTON_TEXTS), cmd_menu))
    app.add_handler(MessageHandler(new & (filters.VOICE | filters.AUDIO), handle_voice))
    app.add_handler(MessageHandler(new & filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    app.add_handler(CallbackQueryHandler(handle_household_button, pattern=r"^hh:"))
    app.add_handler(CallbackQueryHandler(handle_menu_button, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(handle_language_button, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(handle_delete_button, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(handle_shortcut_button, pattern=r"^sc:"))
    app.add_handler(CallbackQueryHandler(handle_fix_button, pattern=r"^fx:"))
    app.add_handler(CallbackQueryHandler(handle_report_button, pattern=r"^r[lx]:"))
    app.add_handler(CallbackQueryHandler(handle_savings_button, pattern=r"^sv:"))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(new & filters.COMMAND, cmd_unknown))
    app.add_handler(MessageHandler(new & ~filters.StatusUpdate.ALL, handle_other))
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
