"""Telegram expense tracker: text/voice in -> guessed category -> confirm -> BigQuery."""
from __future__ import annotations

import asyncio
import html
import logging
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
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
import extractor
import i18n
from catalog import Catalog
from household import Households
from i18n import fmt_day, fmt_month, t
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


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs before every handler: keeps dim_users current, in the background (never delays a reply)."""
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


def proposal_text(item: dict, lang: str) -> str:
    tag = t(lang, "tag_known") if item.get("suggestion_source") == "dictionary" else t(lang, "tag_guess")
    cat = category_label(item.get("category_id"), item["category"], lang)
    return f"{fmt_item(item, lang)}\n" + t(lang, "category_question", cat=html.escape(cat), tag=tag)


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
    limit = over_limit(update.effective_user.id, "text")
    if limit:
        note(outcome="daily_limit", limit=limit)
        await update.effective_message.reply_text(t(lang, "limit_text", n=limit))
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    text = update.effective_message.text
    try:
        result = await extractor.parse(
            text=text, sent_at=update.effective_message.date, ctx=await mapping_context(lang)
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
            ctx=await mapping_context(lang),
        )
    except Exception as e:
        log.exception("Voice parsing failed")
        note(outcome="parse_error", error=f"{type(e).__name__}: {e}"[:1000])
        await update.effective_message.reply_text(t(lang, "voice_failed"))
        return
    await propose(update, result, source="voice", raw_input=result.transcript, lang=lang)


async def mapping_context(lang: str) -> extractor.MappingContext:
    """The shared dictionary, as the model sees it on every mapping."""
    await catalog.refresh_if_stale()
    return extractor.MappingContext(
        categories=catalog.names, guide=catalog.category_guide(), examples=catalog.examples(), language=lang
    )


async def propose(update: Update, result: extractor.ParseResult, source: str, raw_input: str | None, lang: str):
    await propose_to(update.effective_message.reply_text, update.effective_user.id, result, source, raw_input, lang)


async def propose_to(send, user_id: int, result: extractor.ParseResult, source: str,
                     raw_input: str | None, lang: str) -> int:
    """Send one ✅/✏️/🗑 proposal per expense via `send(text, **kwargs)`. Shared by chat messages and
    Action Button uploads. Returns how many expenses were proposed."""
    note(transcript=result.transcript, expenses_found=len(result.expenses))
    spoken = source in ("voice", "upload_audio")
    if spoken and not (result.transcript or "").strip():
        note(outcome="empty_audio")
        await send(t(lang, "voice_empty"))
        return 0
    if result.transcript and spoken:
        await send(f"🎙 <i>{html.escape(result.transcript)}</i>", parse_mode=ParseMode.HTML)
    if not result.expenses:
        note(outcome="no_expense")
        await send(t(lang, "no_expense"))
        return 0
    pids, proposals = [], []
    for exp in result.expenses:
        # A variant users already confirmed beats the model's fresh guess.
        known = catalog.match(exp.merchant, exp.description)
        category = known or catalog.by_name(exp.category) or catalog.fallback()
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
        }
        pid = pending.add(user_id, payload)
        pids.append(pid)
        proposals.append(
            {k: payload[k] for k in ("amount", "currency", "description", "label", "merchant", "expense_date",
                                     "category", "ai_category", "suggestion_source")}
        )
        shown = await send(proposal_text(payload, lang), parse_mode=ParseMode.HTML, reply_markup=confirm_keyboard(pid, lang))
        if isinstance(getattr(shown, "message_id", None), int) and isinstance(getattr(shown, "chat_id", None), int):
            pending.set_message(pid, shown.chat_id, shown.message_id)  # lets auto-save update it later
    note(outcome="proposed", pending_ids=pids, proposals=proposals)
    return len(pids)


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
                ctx=await mapping_context(lang),
            )
        except Exception as e:
            note(outcome="parse_error", error=f"{type(e).__name__}: {e}"[:1000])
            await send(t(lang, "voice_failed" if audio else "parse_failed"))
            raise
        n = await propose_to(send, uid, result, source, raw_input=text or result.transcript, lang=lang)
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
        await query.answer(t(lang, "not_allowed"), show_alert=True)
        return
    action, pid, *rest = query.data.split(":")
    item = pending.get(pid)
    note(pending_ids=[pid])
    if item is None:
        note(outcome="already_handled")
        await query.answer(t(lang, "already_handled"))
        await query.edit_message_reply_markup(None)
        return
    if item["user_id"] != query.from_user.id:  # e.g. someone else's expense in a group chat
        note(outcome="not_owner", owner_user_id=item["user_id"])
        await query.answer(t(lang, "not_your_expense"), show_alert=True)
        return

    if action in ("ed", "bk"):
        pending.touch(pid)  # they're deciding: restart the auto-save clock
    if action == "ed":
        note(outcome="category_menu")
        await query.answer()
        await query.edit_message_text(
            f"{fmt_item(item, lang)}\n{t(lang, 'pick_category')}",
            parse_mode=ParseMode.HTML,
            reply_markup=category_keyboard(pid, lang),
        )
    elif action == "bk":
        note(outcome="back")
        await query.answer()
        await query.edit_message_text(
            proposal_text(item, lang), parse_mode=ParseMode.HTML, reply_markup=confirm_keyboard(pid, lang)
        )
    elif action == "no":
        note(outcome="discarded", category=item["category"], suggestion_source=item.get("suggestion_source"))
        pending.pop(pid)
        await query.answer(t(lang, "discarded_toast"))
        await query.edit_message_text(
            f"<s>{fmt_item(item, lang)}</s>\n{t(lang, 'discarded_line')}", parse_mode=ParseMode.HTML
        )
    elif action == "ok":
        await save(query, pid, item, item["category_id"], item["category"], lang)
    elif action == "set":
        category = catalog.by_id(int(rest[0]))
        if category is None:  # removed from dim_categories since the buttons were drawn
            note(outcome="category_gone")
            await query.answer(t(lang, "category_gone"))
            await query.edit_message_reply_markup(category_keyboard(pid, lang))
            return
        await save(query, pid, item, category.id, category.name, lang)


async def save(query, pid: str, item: dict, category_id: int, category_name: str, lang: str):
    try:
        done = await commit_expense(pid, category_id, category_name, lang,
                                    query.message.chat_id, query.message.message_id, "user")
    except Exception as e:
        log.exception("BigQuery insert failed")
        note(outcome="save_error", error=f"{type(e).__name__}: {e}"[:1000])
        await query.answer(t(lang, "save_failed"), show_alert=True)
        return
    if done is None:  # auto-save got there first
        note(outcome="already_handled")
        await query.answer(t(lang, "already_handled"))
        return
    item, row, shown = done
    await query.answer(t(lang, "saved_toast"))
    await query.edit_message_text(f"{fmt_item(item, lang)}\n✅ <b>{html.escape(shown)}</b>", parse_mode=ParseMode.HTML)


_save_lock = asyncio.Lock()


async def commit_expense(pid: str, category_id: int, category_name: str, lang: str,
                         chat_id: int, message_id: int, confirmed_by: str):
    """Save a pending proposal once, whoever gets there first (a tap or the auto-save timer).
    Returns (item, row, shown category) or None if it was already handled. Raises if BigQuery fails."""
    async with _save_lock:
        item = pending.get(pid)
        if item is None:
            return None
        row = build_row(item, category_id, category_name, confirmed_by)
        await warehouse.insert(row)
        pending.pop(pid)
    if household.enabled and not context_flags.get("view_ready"):
        await asyncio.to_thread(household.ensure_view)
        context_flags["view_ready"] = True
    note(
        outcome="saved_corrected" if row["was_corrected"] else "saved",
        expense_id=row["expense_id"],
        category=category_name,
        suggestion_source=item.get("suggestion_source"),
        suggested_category=item["category"],
    )

    # Teach the shared dictionary — only from real taps: an auto-saved guess was never checked by
    # anyone, and learning from it would make a wrong guess look "known". A failure here must not
    # lose the saved expense.
    keys = catalog.keys_for(item.get("merchant"), item.get("description"))
    learned = None
    category = catalog.by_id(category_id)
    if category and keys and confirmed_by == "user":
        try:
            await catalog.learn(keys, category, row["was_corrected"])
            learned = {"keys": keys, "category_id": category_id, "corrected": row["was_corrected"]}
        except Exception as e:
            log.exception("Dictionary update failed")
            note(dictionary_error=f"{type(e).__name__}: {e}"[:500])

    shown = category_label(category_id, category_name, lang)
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
    }
    if action == "view":
        await toggle_report_view(update)
        return
    cmd = commands.get(action)
    await query.answer()
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
        await query.answer(t(ulang(update), "not_allowed"), show_alert=True)
        return
    lang = ulang(update)
    mode = "summary" if pending.get_report_mode(query.from_user.id) == "detailed" else "detailed"
    pending.set_report_mode(query.from_user.id, mode)
    note(outcome="report_view_set", report_mode=mode)
    await query.answer(t(lang, "view_now_detailed" if mode == "detailed" else "view_now_summary"), show_alert=True)
    try:
        await query.edit_message_reply_markup(main_menu(query.from_user.id, lang))
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
        await query.answer(t(lang, "not_allowed"), show_alert=True)
        return
    key = pending.new_upload_key(query.from_user.id)
    note(outcome="shortcut_key_rotated")
    await query.answer(t(lang, "sc_new_key_done"))
    await query.edit_message_text(
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
    await update.effective_message.reply_text(
        t(lang, "del_warning", n=n),
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
        await query.answer(t(lang, "not_allowed"), show_alert=True)
        return
    await query.answer()
    if query.data == "del:go":
        context.user_data["delete_confirm_until"] = time.monotonic() + DELETE_WINDOW_SECONDS
        note(outcome="delete_armed")
        await query.edit_message_reply_markup(None)
        await update.effective_message.reply_text(t(lang, "del_type_to_confirm"), parse_mode=ParseMode.HTML)
    else:
        context.user_data.pop("delete_confirm_until", None)
        note(outcome="delete_cancelled")
        await query.edit_message_text(t(lang, "del_cancelled"))


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


async def delete_my_data(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str):
    """Step 4: erase the person's own data. Shared categories/dictionary are anonymous and stay."""
    uid = update.effective_user.id
    users.pause(uid)  # in-flight updates mustn't re-create their dim_users row
    cutoff = (datetime.now(timezone.utc) + LOG_PURGE_GRACE).isoformat()
    try:
        n = await asyncio.to_thread(warehouse.delete_user_data, uid)
    except Exception as e:
        log.exception("Deleting user data failed")
        note(outcome="delete_error", error=f"{type(e).__name__}: {e}"[:1000])
        await update.effective_message.reply_text(t(lang, "del_failed"))
        return
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
    interaction_log.drop_user(uid)
    pending.add_log_purge(uid, cutoff)  # finished in the background once BigQuery allows it
    asyncio.create_task(run_log_purges())
    note(outcome="deleted", expenses_deleted=n, _skip_log=True)  # don't write a new row about them
    await update.effective_message.reply_text(t(lang, "del_done", n=n))


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
    while True:
        await asyncio.sleep(1800)
        try:
            await run_log_purges()
        except Exception:
            log.exception("Log purge pass failed")


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
    text = t(lang, "help_text", currency=currency)
    if config.AUTO_SAVE_MINUTES > 0:
        text += t(lang, "help_auto", minutes=f"{config.AUTO_SAVE_MINUTES:g}")
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
    text += t(lang, "help_privacy")
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
        await query.answer(t(ulang(update), "not_allowed"), show_alert=True)
        return
    code = query.data.split(":", 1)[1]
    if code not in i18n.LANGUAGES:
        note(outcome="unknown_language")
        await query.answer()
        return
    pending.set_language(query.from_user.id, code)
    note(outcome="language_set", language=code)
    await sync_chat_commands(context.bot, update.effective_chat.id, code, force=True)
    asyncio.create_task(users.observe(query.from_user, role_of(query.from_user.id), code, force=True))
    await query.answer(t(code, "lang_set"))
    await query.edit_message_text(t(code, "lang_set"))
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
    try:
        if mode == "detailed":
            items = await warehouse.expenses(update.effective_user.id, start, today)
        else:
            rows = await warehouse.totals(update.effective_user.id, start, today)
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
        await send_long(update.effective_message, detailed_report(title, items, lang, multi_day=start != today))
        return
    note(outcome="report", report_mode=mode, period=period, period_start=str(start), period_end=str(today), rows=len(rows))
    if not rows:
        await update.effective_message.reply_text(t(lang, "nothing_yet", title=title))
        return
    by_currency: dict[str, Decimal] = {}
    lines = []
    for category_id, category, currency, total in rows:
        by_currency[currency] = by_currency.get(currency, Decimal(0)) + total
        lines.append(f"{html.escape(category_label(category_id, category, lang))}: {fmt_money(total, currency)}")
    totals = " + ".join(fmt_money(v, k) for k, v in by_currency.items())
    await update.effective_message.reply_text(
        f"<b>{title}</b> — {totals}\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML
    )


def sum_by_currency(pairs) -> str:
    totals: dict[str, Decimal] = {}
    for amount, currency in pairs:
        totals[currency] = totals.get(currency, Decimal(0)) + Decimal(str(amount))
    return " + ".join(fmt_money(v, k) for k, v in totals.items())


def detailed_report(title: str, items: list[dict], lang: str, multi_day: bool) -> str:
    """Every expense, newest first; grouped under day headings when the period spans several days."""
    lines = [f"<b>{title}</b> — {sum_by_currency((i['amount'], i['currency']) for i in items)}"]
    by_day: dict[date, list[dict]] = {}
    for i in items:
        by_day.setdefault(i["expense_date"], []).append(i)
    for day, day_items in by_day.items():
        lines.append("")
        if multi_day:
            lines.append(f"<b>{fmt_day(day, lang)}</b> — {sum_by_currency((i['amount'], i['currency']) for i in day_items)}")
        for i in day_items:
            name = (i.get("item_label") if lang != i18n.DEFAULT_LANGUAGE else None) or i.get("description") or "—"
            merchant = f" ({html.escape(i['merchant'])})" if i.get("merchant") else ""
            cat = category_label(i.get("category_id"), i.get("category") or "", lang)
            lines.append(
                f"• {fmt_money(i['amount'], i['currency'])} · {html.escape(name)}{merchant} — <i>{html.escape(cat)}</i>"
            )
    return "\n".join(lines)


async def send_long(msg, text: str, limit: int = 4000):
    """Telegram caps a message at 4096 characters: split on line breaks, never inside a line's tags."""
    chunk = ""
    for line in text.split("\n"):
        if chunk and len(chunk) + len(line) + 1 > limit:
            await msg.reply_text(chunk, parse_mode=ParseMode.HTML)
            chunk = ""
        chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        await msg.reply_text(chunk, parse_mode=ParseMode.HTML)


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
        await query.answer(toast)
        text, kb = household_view(uid, lang, bot_username)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)

    creator_only = {"link", "rm", "end", "endok"}
    async with household.lock:  # one change at a time, checked against the current state
        home = household.home_of(uid)
        if action in creator_only and not household.is_creator(uid):
            note(outcome="not_creator")
            await query.answer(t(lang, "hh_creator_only"), show_alert=True)
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
            await query.answer()
            await query.edit_message_text(
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
            await query.answer(t(lang, "hh_ended_toast"))
            await query.edit_message_text(t(lang, "hh_ended"), reply_markup=create_keyboard(lang))

        elif action == "leave":
            if home is None or household.is_creator(uid):
                await show()
                return
            note(outcome="household_leave_prompt")
            await query.answer()
            await query.edit_message_text(
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
            await query.answer()
            await query.edit_message_text(t(lang, "hh_left", name=home.name), reply_markup=create_keyboard(lang))

        elif action == "join":
            target = household.by_code(arg or "")
            if target is None:
                note(outcome="invite_invalid")
                await query.answer()
                await query.edit_message_text(t(lang, "hh_invite_invalid"))
            elif home is not None:
                note(outcome="in_other_household" if home.household_id != target.household_id else "already_member")
                await show()
            else:
                await household.join(uid, me, target)
                await users.set_role([uid], "member")
                note(outcome="household_joined", household_id=target.household_id)
                await tell(context, target.created_by, "hh_member_joined", who=me, name=target.name)
                await query.answer(t(lang, "hh_joined_toast"))
                await query.edit_message_text(
                    t(lang, "hh_joined", name=html.escape(target.name, quote=False)), parse_mode=ParseMode.HTML
                )

        elif action == "nojoin":
            note(outcome="join_declined")
            await query.answer()
            await query.edit_message_text(t(lang, "hh_join_cancelled"))

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


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Last line of defence: log the crash (the interaction row already has the traceback) and tell the user."""
    log.error("Unhandled error", exc_info=context.error)
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
    if config.INGEST_SECRET:
        import ingest

        try:
            app.bot_data["ingest_runner"] = await ingest.start(handle_upload, upload_user)
        except OSError:
            log.exception("Upload endpoint could not start (port %s busy?)", config.INGEST_PORT)
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
    runner = app.bot_data.get("ingest_runner")
    if runner:
        await runner.cleanup()
    await interaction_log.close()


def main():
    if config.OWNER_USER_ID is None:
        log.warning("OWNER_USER_ID is empty — the bot will only reply with the sender's ID.")
    warehouse.ensure_shared()
    interaction_log.ensure_table()
    catalog.load()
    household.setup(config.OWNER_USER_ID)
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
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))
    app.add_handler(CommandHandler("start", cmd_start, filters=new))
    app.add_handler(CommandHandler("help", cmd_help, filters=new))
    app.add_handler(CommandHandler("categories", cmd_categories, filters=new))
    app.add_handler(CommandHandler("today", cmd_today, filters=new))
    app.add_handler(CommandHandler("week", cmd_week, filters=new))
    app.add_handler(CommandHandler("month", cmd_month, filters=new))
    app.add_handler(CommandHandler("undo", cmd_undo, filters=new))
    app.add_handler(CommandHandler("reload", cmd_reload, filters=new))
    app.add_handler(CommandHandler("language", cmd_language, filters=new))
    app.add_handler(CommandHandler("household", cmd_household, filters=new))
    app.add_handler(CommandHandler("users", cmd_users, filters=new))
    app.add_handler(CommandHandler("delete_my_data", cmd_delete, filters=new))
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
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(new & filters.COMMAND, cmd_unknown))
    app.add_handler(MessageHandler(new & ~filters.StatusUpdate.ALL, handle_other))
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
