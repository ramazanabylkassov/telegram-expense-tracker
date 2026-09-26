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
from household import Household
from i18n import fmt_day, fmt_month, t
from interactions import InteractionLogger, logged, note, set_logger
from storage import PendingStore, Warehouse, build_row
from users import UserDirectory

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("expense-bot")

pending = PendingStore()
warehouse = Warehouse()
catalog = Catalog(warehouse)
household = Household(warehouse)
users = UserDirectory(warehouse.client, warehouse.ds)
context_flags: dict = {}


def role_of(user_id: int | None) -> str:
    if household.is_owner(user_id):
        return "owner"
    return "member" if household.enabled and user_id in household.members else "none"


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
        commands = i18n.COMMANDS[lang] + (i18n.OWNER_COMMANDS[lang] if household.is_owner(chat_id) else [])
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
    user = update.effective_user
    if user and household.is_allowed(user.id):
        return True
    note(outcome="denied")
    msg = update.effective_message
    lang = lang_for(user)
    if household.owner_id is None:  # first run: tell whoever writes their ID
        if msg:
            await msg.reply_text(t(lang, "first_run_id", uid=user.id if user else "?"))
        return False
    if user and msg:
        await msg.reply_text(t(lang, "private_bot"))
        await notify_owner_of_request(context, user)
    return False


def display_name(user) -> str:
    return " ".join(p for p in (user.first_name, user.last_name) if p) or (user.username or str(user.id))


async def notify_owner_of_request(context: ContextTypes.DEFAULT_TYPE, user):
    """The household prompt appears when it's needed: someone else wants to use the bot."""
    name = display_name(user)
    if not household.record_request(user.id, name, user.username):
        return  # already told the owner in the last 24 h
    lang = stored_lang(household.owner_id)
    who = html.escape(name) + (html.escape(f" (@{user.username})") if user.username else "")
    if household.enabled:
        text, add_label = t(lang, "join_request_on", name=who), t(lang, "btn_add_to_household")
    else:
        text, add_label = t(lang, "join_request_off", name=who), t(lang, "btn_start_and_add")
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(add_label, callback_data=f"hh:add:{user.id}"),
          InlineKeyboardButton(t(lang, "btn_ignore"), callback_data=f"hh:ign:{user.id}")]]
    )
    try:
        await context.bot.send_message(household.owner_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        note(owner_notified=True)
    except Exception:
        log.exception("Could not notify the owner about a join request")


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
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    voice = update.effective_message.voice or update.effective_message.audio
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
    msg = update.effective_message
    note(transcript=result.transcript, expenses_found=len(result.expenses))
    if result.transcript and source == "voice":
        await msg.reply_text(f"🎙 <i>{html.escape(result.transcript)}</i>", parse_mode=ParseMode.HTML)
    if not result.expenses:
        note(outcome="no_expense")
        await msg.reply_text(t(lang, "no_expense"))
        return
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
        pid = pending.add(update.effective_user.id, payload)
        pids.append(pid)
        proposals.append(
            {k: payload[k] for k in ("amount", "currency", "description", "label", "merchant", "expense_date",
                                     "category", "ai_category", "suggestion_source")}
        )
        await msg.reply_text(
            proposal_text(payload, lang), parse_mode=ParseMode.HTML, reply_markup=confirm_keyboard(pid, lang)
        )
    note(outcome="proposed", pending_ids=pids, proposals=proposals)


# --------------------------------------------------------------------------- #
# Button taps                                                                 #
# --------------------------------------------------------------------------- #


@logged("button")
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    lang = ulang(update)
    if not household.is_allowed(query.from_user.id):
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
    row = build_row(item, category_id, category_name)
    try:
        await warehouse.insert(row)
    except Exception as e:
        log.exception("BigQuery insert failed")
        note(outcome="save_error", error=f"{type(e).__name__}: {e}"[:1000])
        await query.answer(t(lang, "save_failed"), show_alert=True)
        return
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

    # Teach the shared dictionary. A failure here must not lose the saved expense.
    keys = catalog.keys_for(item.get("merchant"), item.get("description"))
    learned = None
    category = catalog.by_id(category_id)
    if category and keys:
        try:
            await catalog.learn(keys, category, row["was_corrected"])
            learned = {"keys": keys, "category_id": category_id, "corrected": row["was_corrected"]}
        except Exception as e:
            log.exception("Dictionary update failed")
            note(dictionary_error=f"{type(e).__name__}: {e}"[:500])

    shown = category_label(category_id, category_name, lang)
    summary = f"{fmt_money(item['amount'], item['currency'])} · {item_name(item)} → {shown}"
    pending.remember_saved(
        row["expense_id"], item["user_id"], query.message.chat_id, query.message.message_id, summary, learned
    )
    await query.answer(t(lang, "saved_toast"))
    await query.edit_message_text(f"{fmt_item(item, lang)}\n✅ <b>{html.escape(shown)}</b>", parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Commands                                                                    #
# --------------------------------------------------------------------------- #


@logged("command")
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    lang = ulang(update)
    note(outcome="help")
    await sync_chat_commands(context.bot, update.effective_chat.id, lang)
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
    if household.enabled:
        rows.append([b("m_family", "family")])
    if household.is_owner(user_id):
        rows.append([b("m_users", "users"), b("m_household", "household")])
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
        "delete": cmd_delete,
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
    if not household.is_allowed(query.from_user.id):
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
    if not household.is_allowed(query.from_user.id):
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
    household.forget_request(uid)
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
    if household.is_owner(update.effective_user.id):
        text += t(lang, "help_owner")
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


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
    if not household.is_allowed(query.from_user.id):
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
    if not household.is_owner(update.effective_user.id):
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
    for i, r in enumerate(rows[:USERS_SHOWN], 1):
        uid = r["user_id"]
        if household.is_owner(uid):
            icon = "👑"
        elif household.enabled and uid in household.members:
            icon = "👤"
        else:
            icon = "🚫"
        member = household.members.get(uid)
        name = r.get("name") or (member.display_name if member else None)
        handle = f"@{r['username']}" if r.get("username") else None
        if name and handle:
            name = f"{name} ({handle})"
        name = name or handle or str(uid)
        last = r["last_seen"].astimezone(config.TIMEZONE)
        lines.append(t(
            lang, "users_line", i=i, icon=icon, name=html.escape(name),
            actions=r["actions"], recent=r["actions_30d"], saved=r["saved"],
            last=f"{fmt_day(last.date(), lang)} {last:%H:%M}",
        ))
    if len(rows) > USERS_SHOWN:
        lines.append(t(lang, "users_more", n=len(rows) - USERS_SHOWN))
    lines += ["", f"<i>{t(lang, 'users_legend')}</i>"]
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Household mode (optional)                                                   #
# --------------------------------------------------------------------------- #


def start_prompt_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(lang, "btn_start_household"), callback_data="hh:start"),
          InlineKeyboardButton(t(lang, "btn_not_now"), callback_data="hh:no")]]
    )


def status_view(lang: str) -> tuple[str, InlineKeyboardMarkup]:
    lines = [t(lang, "hh_title")]
    rows = []
    for m in sorted(household.members.values(), key=lambda m: (m.role != "owner", m.display_name)):
        tag = t(lang, "hh_owner_tag") if m.role == "owner" else ""
        lines.append(f"• {html.escape(m.display_name)}{tag} · <code>{m.user_id}</code>")
        if m.role != "owner":
            rows.append([InlineKeyboardButton(
                t(lang, "btn_remove_member", name=m.display_name), callback_data=f"hh:rm:{m.user_id}"
            )])
    lines.append(t(lang, "hh_howto"))
    rows.append([InlineKeyboardButton(t(lang, "btn_end_household"), callback_data="hh:end")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


@logged("command")
async def cmd_household(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    lang = ulang(update)
    msg = update.effective_message
    if not household.is_owner(update.effective_user.id):
        note(outcome="not_owner")
        await msg.reply_text(t(lang, "hh_owner_only_cmd"))
        return
    args = context.args or []
    if args[:1] == ["add"]:
        if len(args) < 2 or not args[1].lstrip("-").isdigit():
            note(outcome="bad_args")
            await msg.reply_text(t(lang, "hh_usage"))
            return
        if not household.enabled:
            note(outcome="household_prompt")
            await msg.reply_text(t(lang, "hh_start_prompt"), parse_mode=ParseMode.HTML,
                                 reply_markup=start_prompt_keyboard(lang))
            return
        uid, name = int(args[1]), " ".join(args[2:]) or args[1]
        await household.add(uid, name)
        await users.set_role([uid], "member")
        note(outcome="member_added", member_user_id=uid)
        await msg.reply_text(t(lang, "hh_added", name=name))
        await welcome_member(context, uid)
        return
    if args[:1] == ["remove"] and len(args) > 1 and args[1].isdigit():
        await household.remove(int(args[1]))
        await users.set_role([int(args[1])], "none")
        note(outcome="member_removed", member_user_id=int(args[1]))
        await msg.reply_text(t(lang, "hh_removed"))
        return
    if not household.enabled:
        note(outcome="household_prompt")
        await msg.reply_text(t(lang, "hh_start_prompt"), parse_mode=ParseMode.HTML,
                             reply_markup=start_prompt_keyboard(lang))
        return
    note(outcome="household_status", members=len(household.members))
    text, kb = status_view(lang)
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def welcome_member(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    try:
        await context.bot.send_message(user_id, t(stored_lang(user_id), "hh_welcome"))
    except Exception:  # they haven't opened the bot yet
        log.info("Couldn't message new member %s yet", user_id)


@logged("button")
async def handle_household_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    lang = ulang(update)
    if not household.is_owner(query.from_user.id):
        note(outcome="not_owner")
        await query.answer(t(lang, "hh_owner_only_btn"), show_alert=True)
        return
    parts = query.data.split(":")
    action, arg = parts[1], (int(parts[2]) if len(parts) > 2 else None)
    owner_name = display_name(query.from_user)

    if action == "start":
        if not household.enabled:
            await household.start(owner_name)
        note(outcome="household_started")
        await query.answer(t(lang, "hh_started_toast"))
        text, kb = status_view(lang)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif action == "no":
        note(outcome="household_declined")
        await query.answer()
        await query.edit_message_text(t(lang, "hh_declined"))
    elif action == "add":
        started = False
        if not household.enabled:
            await household.start(owner_name)
            started = True
        req = household.request(arg)
        name = req[0] if req else str(arg)
        await household.add(arg, name)
        await users.set_role([arg], "member")
        note(outcome="member_added", member_user_id=arg, household_started=started)
        await query.answer(t(lang, "hh_added_toast"))
        await query.edit_message_text(
            (t(lang, "hh_started_prefix") if started else "") + t(lang, "hh_can_use_now", name=html.escape(name)),
            parse_mode=ParseMode.HTML,
        )
        await welcome_member(context, arg)
    elif action == "ign":
        note(outcome="join_ignored", member_user_id=arg)
        await query.answer()
        await query.edit_message_text(t(lang, "hh_ignored"))
    elif action == "rm":
        await household.remove(arg)
        await users.set_role([arg], "none")
        note(outcome="member_removed", member_user_id=arg)
        await query.answer(t(lang, "hh_removed_toast"))
        text, kb = status_view(lang)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
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
        former = [uid for uid, m in household.members.items() if m.role != "owner"]
        await household.end()
        await users.set_role(former, "none")
        note(outcome="household_ended")
        await query.answer(t(lang, "hh_ended_toast"))
        await query.edit_message_text(t(lang, "hh_ended"))
    elif action == "cancel":
        note(outcome="back")
        await query.answer()
        if household.enabled:
            text, kb = status_view(lang)
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@logged("command")
async def cmd_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update, context):
        return
    lang = ulang(update)
    msg = update.effective_message
    if not household.enabled:
        note(outcome="household_off")
        if household.is_owner(update.effective_user.id):
            await msg.reply_text(t(lang, "hh_start_prompt"), parse_mode=ParseMode.HTML,
                                 reply_markup=start_prompt_keyboard(lang))
        else:
            await msg.reply_text(t(lang, "hh_off_member"))
        return
    today = datetime.now(config.TIMEZONE).date()
    period = (context.args or ["month"])[0].lower()
    if period == "today":
        start, title = today, t(lang, "family_title_today", d=fmt_day(today, lang))
    elif period == "week":
        start = today - timedelta(days=6)
        title = t(lang, "family_title_week", a=fmt_day(start, lang), b=fmt_day(today, lang))
    else:
        start, title = today.replace(day=1), t(lang, "family_title_month", m=fmt_month(today, lang))
    try:
        rows = await household.family_totals(start, today)
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
        f"👤 {html.escape(m)}: " + " + ".join(fmt_money(v, c) for c, v in cur.items())
        for m, cur in per_member.items()
    ]
    lines.append("")
    lines += [
        f"{html.escape(cat)}: {fmt_money(v, cur)}"
        for (cat, cur), v in sorted(per_cat.items(), key=lambda kv: kv[1], reverse=True)
    ]
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Everything else                                                             #
# --------------------------------------------------------------------------- #


@logged("edit")
async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edited messages are not re-processed: that would create a duplicate proposal."""
    if not household.is_allowed(update.effective_user.id if update.effective_user else None):
        note(outcome="denied")
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
    app.bot_data["log_task"] = asyncio.create_task(interaction_log.run())
    app.bot_data["purge_task"] = asyncio.create_task(log_purge_loop())
    saved = dict(pending.all_languages())
    if household.owner_id is not None:  # the owner always gets their owner-only commands
        saved.setdefault(household.owner_id, stored_lang(household.owner_id))
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
    for name in ("log_task", "purge_task"):
        task = app.bot_data.get(name)
        if task:
            task.cancel()
    await interaction_log.close()


def main():
    if config.OWNER_USER_ID is None:
        log.warning("OWNER_USER_ID is empty — the bot will only reply with the sender's ID.")
    warehouse.ensure_shared()
    interaction_log.ensure_table()
    catalog.load()
    household.load()
    users.ensure_table(household.owner_id, [u for u in household.members if not household.is_owner(u)])
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
    app.add_handler(MessageHandler(new & filters.Text(MENU_BUTTON_TEXTS), cmd_menu))
    app.add_handler(MessageHandler(new & (filters.VOICE | filters.AUDIO), handle_voice))
    app.add_handler(MessageHandler(new & filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    app.add_handler(CallbackQueryHandler(handle_household_button, pattern=r"^hh:"))
    app.add_handler(CallbackQueryHandler(handle_menu_button, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(handle_language_button, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(handle_delete_button, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(new & filters.COMMAND, cmd_unknown))
    app.add_handler(MessageHandler(new & ~filters.StatusUpdate.ALL, handle_other))
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
