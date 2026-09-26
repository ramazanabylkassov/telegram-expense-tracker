"""User-facing text in English and Russian.

t(lang, key, **params) returns the string for `lang` ("en" or "ru"), falling back to English.
Add a language by adding its code to LANGUAGES and a value to every entry in STRINGS.
"""
from __future__ import annotations

from datetime import date

LANGUAGES = {"en": "🇬🇧 English", "ru": "🇷🇺 Русский"}
DEFAULT_LANGUAGE = "en"

# Telegram app language codes that should start in Russian.
_RUSSIAN_SPEAKING = {"ru", "kk", "uk", "be", "ky", "uz"}


def detect(language_code: str | None) -> str:
    """Pick a starting language from the user's Telegram app language."""
    code = (language_code or "").split("-")[0].lower()
    return "ru" if code in _RUSSIAN_SPEAKING else DEFAULT_LANGUAGE


def t(lang: str, key: str, **params) -> str:
    entry = STRINGS[key]
    text = entry.get(lang) or entry[DEFAULT_LANGUAGE]
    return text.format(**params) if params else text


# ---- dates ------------------------------------------------------------------

_DAYS = {"en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
         "ru": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]}
_MONTHS_SHORT = {"en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
                 "ru": ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]}
_MONTHS_FULL = {"en": ["January", "February", "March", "April", "May", "June", "July", "August",
                       "September", "October", "November", "December"],
                "ru": ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
                       "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]}


def fmt_day(d: date, lang: str) -> str:
    """en: 'Sat, Sep 26'   ru: 'сб, 26 сен'  (never depends on the machine's locale)."""
    lang = lang if lang in _DAYS else DEFAULT_LANGUAGE
    day, month = _DAYS[lang][d.weekday()], _MONTHS_SHORT[lang][d.month - 1]
    return f"{day}, {d.day} {month}" if lang == "ru" else f"{day}, {month} {d.day}"


def fmt_month(d: date, lang: str) -> str:
    """en: 'September 2026'   ru: 'Сентябрь 2026'."""
    lang = lang if lang in _MONTHS_FULL else DEFAULT_LANGUAGE
    return f"{_MONTHS_FULL[lang][d.month - 1]} {d.year}"


# ---- Telegram command menu (the "/" list in the chat) -------------------------

COMMANDS = {
    "en": [
        ("start", "Menu"),
        ("today", "Today's totals"),
        ("week", "Last 7 days"),
        ("month", "This month"),
        ("undo", "Remove the last saved expense"),
        ("categories", "Category list"),
        ("language", "Change language"),
        ("reload", "Reload categories from BigQuery"),
        ("help", "How to use the bot"),
    ],
    "ru": [
        ("start", "Меню"),
        ("today", "Итоги за сегодня"),
        ("week", "Последние 7 дней"),
        ("month", "Этот месяц"),
        ("undo", "Отменить последний расход"),
        ("categories", "Список категорий"),
        ("language", "Сменить язык"),
        ("reload", "Обновить категории из BigQuery"),
        ("help", "Как пользоваться ботом"),
    ],
}

# Labels the pinned button had before; people who still have them get the new one on their next tap.
LEGACY_KB_MENU = ["📋 Menu", "📋 Меню"]

# Added to the owner's own "/" list only.
OWNER_COMMANDS = {
    "en": [("users", "Users by activity"), ("household", "Share the bot with your household")],
    "ru": [("users", "Пользователи по активности"), ("household", "Семейный режим")],
}

# ---- strings ----------------------------------------------------------------

STRINGS: dict[str, dict[str, str]] = {
    # access
    "first_run_id": {
        "en": "Your Telegram user ID is {uid}. If this is your bot, set OWNER_USER_ID to it in .env and restart.",
        "ru": "Ваш Telegram ID: {uid}. Если это ваш бот, укажите его в OWNER_USER_ID в файле .env и перезапустите бота.",
    },
    "private_bot": {
        "en": "This is a private bot. I've let its owner know you'd like to use it.",
        "ru": "Это приватный бот. Я сообщил владельцу, что вы хотите им пользоваться.",
    },
    "join_request_on": {
        "en": "👋 {name} wants to use the bot.\nAdd them to your household?",
        "ru": "👋 {name} хочет пользоваться ботом.\nДобавить в семью?",
    },
    "join_request_off": {
        "en": "👋 {name} wants to use the bot.\n\nHousehold mode is off, so the bot is only yours. "
              "Start a household to let them in? Each person keeps their own expense table, "
              "and you get combined /family reports.",
        "ru": "👋 {name} хочет пользоваться ботом.\n\nСемейный режим выключен — бот только ваш. "
              "Включить его, чтобы добавить этого человека? У каждого будет своя таблица расходов, "
              "а вам станут доступны общие отчёты /family.",
    },
    "btn_add_to_household": {"en": "➕ Add to household", "ru": "➕ Добавить в семью"},
    "btn_start_and_add": {"en": "🏠 Start household & add", "ru": "🏠 Включить и добавить"},
    "btn_ignore": {"en": "Ignore", "ru": "Игнорировать"},
    "not_allowed": {"en": "Not allowed", "ru": "Нет доступа"},

    # expenses
    "parse_failed": {
        "en": "⚠️ Couldn't process that right now — please try again.",
        "ru": "⚠️ Не получилось обработать сообщение — попробуйте ещё раз.",
    },
    "voice_failed": {
        "en": "⚠️ Couldn't process that voice note — please try again.",
        "ru": "⚠️ Не получилось обработать голосовое — попробуйте ещё раз.",
    },
    "no_expense": {
        "en": "I didn't find an expense in that. Try something like “coffee 1500” or “taxi 2.3k yesterday”.",
        "ru": "Не нашёл в сообщении расходов. Попробуйте, например: «кофе 1500» или «такси 2300 вчера».",
    },
    "category_question": {"en": "Category: <b>{cat}</b>? <i>({tag})</i>", "ru": "Категория: <b>{cat}</b>? <i>({tag})</i>"},
    "tag_known": {"en": "📖 known", "ru": "📖 знаю"},
    "tag_guess": {"en": "🤖 guess", "ru": "🤖 догадка"},
    "btn_save": {"en": "✅ Save", "ru": "✅ Сохранить"},
    "btn_change": {"en": "✏️ Change", "ru": "✏️ Изменить"},
    "btn_back": {"en": "« Back", "ru": "« Назад"},
    "pick_category": {"en": "Pick a category:", "ru": "Выберите категорию:"},
    "already_handled": {"en": "Already handled", "ru": "Уже обработано"},
    "not_your_expense": {"en": "That's not your expense", "ru": "Это не ваш расход"},
    "discarded_toast": {"en": "Discarded", "ru": "Удалено"},
    "discarded_line": {"en": "🗑 Discarded", "ru": "🗑 Удалено"},
    "category_gone": {"en": "That category no longer exists", "ru": "Этой категории больше нет"},
    "save_failed": {"en": "Saving failed — tap again to retry", "ru": "Не удалось сохранить — нажмите ещё раз"},
    "saved_toast": {"en": "Saved", "ru": "Сохранено"},

    # /start menu
    "start_text": {
        "en": "Send me what you spent, as text or a voice note — e.g. “coffee 1500”, "
              "“Magnum 12 400 and taxi 2k”, “вчера аптека 3500”.\n"
              "I'll guess the category; tap ✅ to save or ✏️ to change it.\n\n"
              "The ▶️ Start button below the chat opens the actions any time.",
        "ru": "Напишите или наговорите, на что потратили, например: «кофе 1500», "
              "«Magnum 12 400 и такси 2к», «вчера аптека 3500».\n"
              "Я предложу категорию — нажмите ✅, чтобы сохранить, или ✏️, чтобы изменить.\n\n"
              "Кнопка ▶️ Старт под чатом открывает действия в любой момент.",
    },
    "pick_action": {"en": "Pick an action:", "ru": "Выберите действие:"},
    "kb_menu": {"en": "▶️ Start", "ru": "▶️ Старт"},
    "input_placeholder": {"en": "e.g. coffee 1500", "ru": "например, кофе 1500"},
    "m_today": {"en": "📅 Today", "ru": "📅 Сегодня"},
    "m_week": {"en": "📆 Week", "ru": "📆 Неделя"},
    "m_month": {"en": "🗓 Month", "ru": "🗓 Месяц"},
    "m_undo": {"en": "↩️ Undo last", "ru": "↩️ Отменить"},
    "m_categories": {"en": "🏷 Categories", "ru": "🏷 Категории"},
    "m_family": {"en": "👨‍👩‍👧 Family", "ru": "👨‍👩‍👧 Семья"},
    "m_household": {"en": "🏠 Household", "ru": "🏠 Семейный режим"},
    "m_reload": {"en": "🔄 Reload", "ru": "🔄 Обновить"},
    "m_users": {"en": "👥 Users", "ru": "👥 Пользователи"},
    "m_help": {"en": "❓ How it works", "ru": "❓ Как пользоваться"},
    "m_view_summary": {"en": "📊 Reports: totals", "ru": "📊 Отчёты: итоги"},
    "m_view_detailed": {"en": "🧾 Reports: detailed", "ru": "🧾 Отчёты: подробно"},
    "view_now_detailed": {"en": "🧾 Reports now list every expense", "ru": "🧾 Теперь отчёты показывают каждый расход"},
    "view_now_summary": {"en": "📊 Reports now show totals by category", "ru": "📊 Теперь отчёты показывают итоги по категориям"},
    "m_language": {"en": "🌐 Language", "ru": "🌐 Язык"},

    # language
    "lang_pick": {"en": "🌐 Choose a language:", "ru": "🌐 Выберите язык:"},
    "lang_set": {"en": "✅ Language: English", "ru": "✅ Язык: русский"},

    # catalog
    "reload_failed": {"en": "⚠️ Couldn't reload from BigQuery.", "ru": "⚠️ Не удалось обновить данные из BigQuery."},
    "reloaded": {
        "en": "Reloaded: {c} categories, {v} known spend variants.",
        "ru": "Обновлено: категорий — {c}, известных вариантов трат — {v}.",
    },

    # reports
    "report_failed": {"en": "⚠️ Couldn't load totals right now.", "ru": "⚠️ Не удалось загрузить итоги."},
    "nothing_yet": {"en": "{title}: nothing recorded yet.", "ru": "{title}: пока ничего нет."},
    "title_today": {"en": "Today ({d})", "ru": "Сегодня ({d})"},
    "title_week": {"en": "Last 7 days ({a} – {b})", "ru": "Последние 7 дней ({a} – {b})"},

    # undo
    "nothing_to_undo": {"en": "Nothing to undo.", "ru": "Нечего отменять."},
    "undo_sandbox": {
        "en": "⚠️ Undo needs DELETE, which the BigQuery sandbox (no billing account) doesn't allow. "
              "Enable billing on the project (usage stays inside the free tier) or delete the row in the console.",
        "ru": "⚠️ Для отмены нужен DELETE, а песочница BigQuery (без платёжного аккаунта) его не разрешает. "
              "Включите биллинг в проекте (расход останется в бесплатном лимите) или удалите строку в консоли.",
    },
    "undo_failed": {"en": "⚠️ Couldn't undo right now.", "ru": "⚠️ Не удалось отменить."},
    "undone_line": {"en": "↩️ Undone", "ru": "↩️ Отменено"},
    "removed_summary": {"en": "↩️ Removed: {s}", "ru": "↩️ Удалено: {s}"},

    # household
    "hh_start_prompt": {
        "en": "🏠 <b>Household mode is off</b> — the bot is only yours.\n\n"
              "Start a household to share it: each person logs their own expenses into their own table, "
              "the category dictionary is shared, and you get combined /family reports. "
              "You can end it any time; everyone's data stays.",
        "ru": "🏠 <b>Семейный режим выключен</b> — бот только ваш.\n\n"
              "Включите его, чтобы делиться ботом: каждый ведёт свои расходы в своей таблице, "
              "словарь категорий общий, а вам доступны общие отчёты /family. "
              "Выключить можно в любой момент — данные сохранятся.",
    },
    "btn_start_household": {"en": "🏠 Start household", "ru": "🏠 Включить"},
    "btn_not_now": {"en": "Not now", "ru": "Не сейчас"},
    "hh_title": {"en": "🏠 <b>Household</b>", "ru": "🏠 <b>Семья</b>"},
    "hh_owner_tag": {"en": " (owner)", "ru": " (владелец)"},
    "btn_remove_member": {"en": "Remove {name}", "ru": "Удалить: {name}"},
    "hh_howto": {
        "en": "\nTo add someone: they message the bot and you get an Add button, "
              "or send <code>/household add &lt;telegram_id&gt; &lt;name&gt;</code>.",
        "ru": "\nЧтобы добавить человека: пусть напишет боту — вам придёт кнопка «Добавить». "
              "Или отправьте <code>/household add &lt;telegram_id&gt; &lt;имя&gt;</code>.",
    },
    "btn_end_household": {"en": "End household", "ru": "Выключить семейный режим"},
    "hh_owner_only_cmd": {
        "en": "Only the bot's owner can manage the household.",
        "ru": "Управлять семьёй может только владелец бота.",
    },
    "hh_usage": {"en": "Usage: /household add <telegram_id> <name>", "ru": "Формат: /household add <telegram_id> <имя>"},
    "hh_added": {"en": "➕ Added {name}.", "ru": "➕ Добавлено: {name}."},
    "hh_removed": {"en": "Removed.", "ru": "Удалено."},
    "hh_welcome": {
        "en": "🏠 You've been added to the household. Send me what you spend — e.g. “coffee 1500” — "
              "as text or a voice note. /start shows everything I can do.",
        "ru": "🏠 Вас добавили в семью. Пишите или наговаривайте, на что потратили, например «кофе 1500». "
              "/start — все возможности.",
    },
    "hh_owner_only_btn": {"en": "Only the owner can do that", "ru": "Это может сделать только владелец"},
    "hh_started_toast": {"en": "Household started", "ru": "Семейный режим включён"},
    "hh_declined": {
        "en": "OK — household mode stays off. /household brings this back.",
        "ru": "Хорошо, семейный режим остаётся выключенным. /household — вернуться к этому.",
    },
    "hh_added_toast": {"en": "Added", "ru": "Добавлено"},
    "hh_started_prefix": {"en": "🏠 Household started. ", "ru": "🏠 Семейный режим включён. "},
    "hh_can_use_now": {"en": "➕ {name} can now use the bot.", "ru": "➕ {name} теперь может пользоваться ботом."},
    "hh_ignored": {"en": "Ignored. They can't use the bot.", "ru": "Проигнорировано. Этот человек не сможет пользоваться ботом."},
    "hh_removed_toast": {"en": "Removed", "ru": "Удалено"},
    "hh_end_confirm": {
        "en": "End the household? Members lose access to the bot. Everyone's expenses stay in BigQuery.",
        "ru": "Выключить семейный режим? Участники потеряют доступ к боту. Все расходы останутся в BigQuery.",
    },
    "btn_yes_end": {"en": "Yes, end it", "ru": "Да, выключить"},
    "btn_cancel": {"en": "Cancel", "ru": "Отмена"},
    "hh_ended_toast": {"en": "Household ended", "ru": "Семейный режим выключен"},
    "hh_ended": {
        "en": "Household ended. The bot is only yours again. /household to start a new one.",
        "ru": "Семейный режим выключен. Бот снова только ваш. /household — включить заново.",
    },
    "hh_off_member": {"en": "Household mode is off.", "ru": "Семейный режим выключен."},
    "family_title_today": {"en": "Household · today ({d})", "ru": "Семья · сегодня ({d})"},
    "family_title_week": {"en": "Household · last 7 days ({a} – {b})", "ru": "Семья · последние 7 дней ({a} – {b})"},
    "family_title_month": {"en": "Household · {m}", "ru": "Семья · {m}"},
    "family_failed": {
        "en": "⚠️ Couldn't load household totals right now.",
        "ru": "⚠️ Не удалось загрузить семейные итоги.",
    },

    # how it works
    "help_text": {
        "en": "❓ <b>How it works</b>\n\n"
              "A private expense tracker. Tell it what you spent — it picks a category and saves it "
              "to your own table.\n\n"
              "<b>1. Log</b> — type or send a voice note:\n"
              "“coffee 1500”, “Magnum 12 400 and taxi 2k”, “вчера аптека 3500”.\n"
              "Several expenses in one message are fine. No currency means {currency}; "
              "“yesterday” or a weekday sets the date.\n\n"
              "<b>2. Confirm</b>\n"
              "✅ Save · ✏️ Change category · 🗑 Discard\n"
              "📖 = a category it already knows, 🤖 = its guess. Every choice teaches it.\n\n"
              "<b>3. Check</b> — 📅 Today · 📆 Week · 🗓 Month\n\n"
              "<b>Mistakes</b> — ↩️ Undo removes the last saved expense. Edited messages aren't picked up: "
              "send a new one.\n\n"
              "🏷 Categories · 🌐 Language · the ▶️ Start button below the chat opens everything.\n"
              "🗑 Delete my data erases everything the bot keeps about you.",
        "ru": "❓ <b>Как пользоваться</b>\n\n"
              "Личный учёт расходов. Сообщите, на что потратили, — бот подберёт категорию и сохранит "
              "в вашу таблицу.\n\n"
              "<b>1. Запишите</b> — текстом или голосовым:\n"
              "«кофе 1500», «Magnum 12 400 и такси 2к», «вчера аптека 3500».\n"
              "Можно несколько трат в одном сообщении. Без валюты — {currency}; "
              "«вчера» или день недели задают дату.\n\n"
              "<b>2. Подтвердите</b>\n"
              "✅ Сохранить · ✏️ Изменить категорию · 🗑 Удалить\n"
              "📖 — категория уже известна, 🤖 — догадка бота. Каждый ваш выбор его обучает.\n\n"
              "<b>3. Смотрите итоги</b> — 📅 Сегодня · 📆 Неделя · 🗓 Месяц\n\n"
              "<b>Ошибки</b> — ↩️ Отменить удаляет последний сохранённый расход. Правки сообщений "
              "не учитываются: отправьте новое.\n\n"
              "🏷 Категории · 🌐 Язык · кнопка ▶️ Старт под чатом открывает всё.\n"
              "🗑 Удалить мои данные — стирает всё, что бот хранит о вас.",
    },
    "help_owner": {
        "en": "\n\n👑 <b>Owner</b> — 👥 Users: who uses the bot · 🏠 Household: share it with family.",
        "ru": "\n\n👑 <b>Владелец</b> — 👥 Пользователи: кто пользуется ботом · 🏠 Семейный режим: поделиться с семьёй.",
    },

    # delete my data
    "m_delete": {"en": "🗑 Delete my data", "ru": "🗑 Удалить мои данные"},
    "del_warning": {
        "en": "⚠️ <b>Delete all your data?</b>\n\n"
              "This permanently removes:\n"
              "• all your saved expenses ({n})\n"
              "• your activity history and profile in the bot\n"
              "• your settings (language, report view) and anything not yet saved\n\n"
              "Shared categories stay. This can't be undone.",
        "ru": "⚠️ <b>Удалить все ваши данные?</b>\n\n"
              "Будут безвозвратно удалены:\n"
              "• все ваши сохранённые расходы ({n})\n"
              "• история действий и профиль в боте\n"
              "• ваши настройки (язык, вид отчётов) и всё несохранённое\n\n"
              "Общие категории останутся. Отменить это нельзя.",
    },
    "btn_del_continue": {"en": "Continue", "ru": "Продолжить"},
    "btn_del_cancel": {"en": "Cancel", "ru": "Отмена"},
    "del_type_to_confirm": {
        "en": "To confirm, send the word <code>DELETE</code> within 5 minutes.\nAny other message cancels.",
        "ru": "Для подтверждения отправьте слово <code>УДАЛИТЬ</code> в течение 5 минут.\n"
              "Любое другое сообщение отменит удаление.",
    },
    "del_cancelled": {"en": "Deletion cancelled. Nothing was deleted.", "ru": "Удаление отменено. Ничего не удалено."},
    "del_expired": {
        "en": "That confirmation expired. Nothing was deleted — start again from the menu if you still want to.",
        "ru": "Время подтверждения истекло. Ничего не удалено — начните заново из меню, если нужно.",
    },
    "del_done": {
        "en": "✅ Done. Deleted {n} expenses, your profile and your settings.\n"
              "The last few minutes of your activity history will be cleared within about 2 hours.",
        "ru": "✅ Готово. Удалено расходов: {n}, а также профиль и настройки.\n"
              "Последние минуты истории действий будут очищены примерно в течение 2 часов.",
    },
    "del_failed": {
        "en": "⚠️ Deletion didn't finish. Try again in a minute — repeating it is safe.",
        "ru": "⚠️ Удаление не завершилось. Попробуйте ещё раз через минуту — повторять безопасно.",
    },

    # owner: users by activity
    "owner_only": {"en": "Only the bot's owner can see this.", "ru": "Это доступно только владельцу бота."},
    "users_title": {
        "en": "👥 <b>Users: {n}</b> · active in the last 30 days: {a}",
        "ru": "👥 <b>Пользователей: {n}</b> · активны за 30 дней: {a}",
    },
    "users_line": {
        "en": "{i}. {icon} {name} — {actions} actions ({recent} in 30 days) · {saved} saved · last {last}",
        "ru": "{i}. {icon} {name} — действий: {actions} (за 30 дней: {recent}) · сохранено: {saved} · был(а) {last}",
    },
    "users_more": {"en": "…and {n} more", "ru": "…и ещё {n}"},
    "users_legend": {
        "en": "👑 owner · 👤 household member · 🚫 no access",
        "ru": "👑 владелец · 👤 участник семьи · 🚫 нет доступа",
    },
    "users_none": {"en": "No activity logged yet.", "ru": "Пока нет активности."},
    "users_failed": {"en": "⚠️ Couldn't load users right now.", "ru": "⚠️ Не удалось загрузить пользователей."},

    # misc
    "edit_ignored": {
        "en": "✏️ I don't pick up edits. Send the corrected expense as a new message "
              "(and tap 🗑 on the old one if it isn't saved yet, or /undo if it is).",
        "ru": "✏️ Я не отслеживаю правки. Отправьте исправленный расход новым сообщением "
              "(а старый удалите кнопкой 🗑, если он ещё не сохранён, или командой /undo, если сохранён).",
    },
    "unknown_command": {
        "en": "I don't know that command. /start shows what I can do.",
        "ru": "Не знаю такой команды. /start — список возможностей.",
    },
    "unsupported": {
        "en": "I understand text and voice notes, e.g. “coffee 1500”.",
        "ru": "Я понимаю текст и голосовые, например «кофе 1500».",
    },
    "error_msg": {
        "en": "⚠️ Something went wrong on my side. It's been logged.",
        "ru": "⚠️ Что-то пошло не так на моей стороне. Ошибка записана в лог.",
    },
    "error_toast": {"en": "Something went wrong — it's been logged.", "ru": "Что-то пошло не так — ошибка записана."},
}
