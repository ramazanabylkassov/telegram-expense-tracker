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


def fmt_date(d: date, lang: str) -> str:
    """A deadline: en 'Jun 30, 2027'   ru '30 июн 2027'."""
    lang = lang if lang in _MONTHS_SHORT else DEFAULT_LANGUAGE
    month = _MONTHS_SHORT[lang][d.month - 1]
    return f"{d.day} {month} {d.year}" if lang == "ru" else f"{month} {d.day}, {d.year}"


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
        ("savings", "Income, savings and goals"),
        ("undo", "Remove the last saved expense"),
        ("categories", "Category list"),
        ("language", "Change language"),
        ("household", "Household: shared totals"),
        ("family", "Household totals"),
        ("reload", "Reload categories from BigQuery"),
        ("help", "How to use the bot"),
    ],
    "ru": [
        ("start", "Меню"),
        ("today", "Итоги за сегодня"),
        ("week", "Последние 7 дней"),
        ("month", "Этот месяц"),
        ("savings", "Доходы, накопления и цели"),
        ("undo", "Отменить последний расход"),
        ("categories", "Список категорий"),
        ("language", "Сменить язык"),
        ("household", "Семья: общие итоги"),
        ("family", "Итоги семьи"),
        ("reload", "Обновить категории из BigQuery"),
        ("help", "Как пользоваться ботом"),
    ],
}

# Labels the pinned button had before; people who still have them get the new one on their next tap.
LEGACY_KB_MENU = ["📋 Menu", "📋 Меню"]

# Added to the owner's own "/" list only.
OWNER_COMMANDS = {
    "en": [("users", "Users by activity"), ("block", "Block a user: /block <id>"), ("unblock", "Unblock: /unblock <id>")],
    "ru": [("users", "Пользователи по активности"), ("block", "Заблокировать: /block <id>"), ("unblock", "Разблокировать: /unblock <id>")],
}

# ---- strings ----------------------------------------------------------------

STRINGS: dict[str, dict[str, str]] = {
    # access
    "first_run_id": {
        "en": "Your Telegram user ID is {uid}. If this is your bot, set OWNER_USER_ID to it in .env and restart.",
        "ru": "Ваш Telegram ID: {uid}. Если это ваш бот, укажите его в OWNER_USER_ID в файле .env и перезапустите бота.",
    },
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
    "voice_empty": {
        "en": "🎙 I couldn't hear anything in that recording. Try again and speak for a second or two.",
        "ru": "🎙 В этой записи ничего не слышно. Попробуйте ещё раз и говорите хотя бы секунду-две.",
    },
    "upload_prefix": {"en": "📲 From your Shortcut:", "ru": "📲 Из вашей быстрой команды:"},
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
    "m_family": {
        "en": '👨\u200d👩\u200d👧 Family totals',
        "ru": '👨\u200d👩\u200d👧 Итоги семьи',
    },
    "m_household": {
        "en": '🏠 Household',
        "ru": '🏠 Семья',
    },
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
    "hh_title": {
        "en": '🏠 <b>{name}</b>',
        "ru": '🏠 <b>{name}</b>',
    },
    "hh_owner_tag": {
        "en": ' (created it)',
        "ru": ' (создатель)',
    },
    "btn_remove_member": {
        "en": 'Remove {name}',
        "ru": 'Удалить: {name}',
    },
    "btn_end_household": {
        "en": 'End household',
        "ru": 'Распустить семью',
    },
    "hh_removed_toast": {
        "en": 'Removed',
        "ru": 'Удалено',
    },
    "hh_end_confirm": {
        "en": "End the household? Everyone leaves it and the invite link stops working. Nobody's expenses are deleted.",
        "ru": 'Распустить семью? Все участники выйдут, ссылка-приглашение перестанет работать. Ничьи расходы не удаляются.',
    },
    "btn_yes_end": {
        "en": 'Yes, end it',
        "ru": 'Да, распустить',
    },
    "btn_cancel": {"en": "Cancel", "ru": "Отмена"},
    "hh_ended_toast": {
        "en": 'Household ended',
        "ru": 'Семья распущена',
    },
    "hh_ended": {
        "en": "Household ended. Everyone's expenses stay with them. You can create a new one any time.",
        "ru": 'Семья распущена. Расходы остались у каждого. Новую семью можно создать в любой момент.',
    },
    "family_title_today": {
        "en": '{h} · today ({d})',
        "ru": '{h} · сегодня ({d})',
    },
    "family_title_week": {
        "en": '{h} · last 7 days ({a} – {b})',
        "ru": '{h} · последние 7 дней ({a} – {b})',
    },
    "family_title_month": {
        "en": '{h} · {m}',
        "ru": '{h} · {m}',
    },
    "family_failed": {
        "en": "⚠️ Couldn't load household totals right now.",
        "ru": "⚠️ Не удалось загрузить семейные итоги.",
    },

    # how it works
    "help_text": {
        "en": "❓ <b>How it works</b>\n\n"
              "A personal expense tracker. Tell it what you spent — it picks a category and saves it "
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
              "🗑 Delete my data hides everything the bot keeps about you and erases it after {days} days (↩️ Restore brings it back until then).",
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
              "🗑 Удалить мои данные — скрывает всё, что бот хранит о вас, и стирает через {days} дней (до этого ↩️ Восстановить вернёт всё обратно).",
    },
    "help_auto": {
        "en": "\n⏱ No answer within {minutes} min? It's saved with the suggested category.",
        "ru": "\n⏱ Нет ответа {minutes} мин? Расход сохранится с предложенной категорией.",
    },
    "auto_saved": {"en": "auto-saved", "ru": "сохранено автоматически"},
    "btn_fix_text": {"en": "✏️ Fix text", "ru": "✏️ Исправить текст"},
    "btn_delete_line": {"en": "🗑 Delete a line", "ru": "🗑 Удалить строку"},
    "dl_prompt": {
        "en": "🗑 Which line should I delete? Send its number (1–{n}). Several at once: 3, 5",
        "ru": "🗑 Какую строку удалить? Отправьте её номер (1–{n}). Несколько сразу: 3, 5",
    },
    "dl_placeholder": {"en": "Line number", "ru": "Номер строки"},
    "dl_bad": {
        "en": "Send line numbers from the report, 1–{n} (up to {max} at once), e.g. 3 or 3, 5",
        "ru": "Отправьте номера строк из отчёта, 1–{n} (не больше {max} за раз), например 3 или 3, 5",
    },
    "dl_confirm": {"en": "Delete for good?\n\n{lines}", "ru": "Удалить навсегда?\n\n{lines}"},
    "btn_dl_yes": {"en": "🗑 Delete ({n})", "ru": "🗑 Удалить ({n})"},
    "dl_done": {
        "en": "🗑 Deleted:\n{lines}\n\nOpen the report again to see the new totals.",
        "ru": "🗑 Удалено:\n{lines}\n\nОткройте отчёт заново, чтобы увидеть новые итоги.",
    },
    "dl_already": {"en": "Already deleted: line {lines}.", "ru": "Уже удалено: строка {lines}."},
    "dl_cancelled": {"en": "OK, nothing deleted.", "ru": "Хорошо, ничего не удалено."},
    "dl_gone": {
        "en": "This report is too old to delete from. Open it again and pick the line there.",
        "ru": "Из этого отчёта уже нельзя удалять. Откройте его заново и выберите строку там.",
    },
    "dl_failed": {
        "en": "⚠️ Couldn't delete right now. Try again in a minute.",
        "ru": "⚠️ Не удалось удалить. Попробуйте через минуту.",
    },
    "help_delete_line": {
        "en": "\n🗑 Wrong entry anywhere? In the 🧾 detailed report every line has a number: tap 🗑 Delete a line and send the number.",
        "ru": "\n🗑 Ошибка в любой записи? В 🧾 подробном отчёте у каждой строки есть номер: нажмите 🗑 Удалить строку и отправьте номер.",
    },
    "fix_prompt": {
        "en": "✏️ Send the corrected text as your next message. What I heard (tap to copy):\n<code>{text}</code>",
        "ru": "✏️ Отправьте исправленный текст следующим сообщением. Что я услышал (нажмите, чтобы скопировать):\n<code>{text}</code>",
    },
    "fix_cancelled": {"en": "OK, the transcript stays as it was.", "ru": "Хорошо, расшифровка остаётся как есть."},
    "fix_placeholder": {"en": "Corrected text", "ru": "Исправленный текст"},
    "fix_replaced": {"en": "replaced by the corrected text", "ru": "заменено исправленным текстом"},
    "fix_already_saved": {
        "en": "Note: {n} expense(s) from the original recording were already saved and stay saved. If they're wrong, ↩️ Undo removes the last one.",
        "ru": "Обратите внимание: {n} расход(ов) из исходной записи уже сохранены и останутся. Если они неверны, ↩️ Отменить удалит последний.",
    },
    "fix_gone": {
        "en": "This recording is too old to fix. Just send the expense again.",
        "ru": "Эту запись уже нельзя исправить. Просто отправьте расход заново.",
    },
    "help_fix": {
        "en": "\n✏️ Voice misheard? Tap ✏️ Fix text under the transcript (or reply to it) with the right words.",
        "ru": "\n✏️ Голос распознан неверно? Нажмите ✏️ Исправить текст под расшифровкой (или ответьте на неё) и напишите правильно.",
    },
    "m_shortcut": {"en": "📲 Action Button", "ru": "📲 Кнопка действия"},
    "sc_title": {
        "en": "📲 <b>Log expenses with the iPhone Action Button</b>\nPress it, say what you spent, tap to stop. The proposal arrives here as usual.",
        "ru": "📲 <b>Расходы с кнопки действия iPhone</b>\nНажмите, скажите, на что потратили, коснитесь, чтобы остановить. Предложение придёт сюда, как обычно.",
    },
    "sc_key": {
        "en": "🔑 Your personal key (tap to copy):\n<code>{secret}</code>\nIt's shown only this once, so keep it until the Shortcut is set up.",
        "ru": "🔑 Ваш личный ключ (нажмите, чтобы скопировать):\n<code>{secret}</code>\nОн показывается только один раз — сохраните его до настройки команды.",
    },
    "sc_key_hidden": {
        "en": "🔑 You already have a key. For safety it isn't shown again. Lost it, or setting up a new phone? Tap 🔄 New key.",
        "ru": "🔑 У вас уже есть ключ. Из соображений безопасности он не показывается повторно. Потеряли его или настраиваете новый телефон? Нажмите 🔄 Новый ключ.",
    },
    "sc_steps_link": {
        "en": "<b>Setup (1 minute, on your iPhone):</b>\n1. Open {link} and tap <b>Add Shortcut</b>.\n2. When it asks for your key, paste the key.\n3. Settings → Action Button → <b>Shortcut</b> → pick «{name}».",
        "ru": "<b>Настройка (1 минута, на iPhone):</b>\n1. Откройте {link} и нажмите <b>Добавить быструю команду</b>.\n2. Когда спросит ключ, вставьте его.\n3. Настройки → Кнопка действия → <b>Быстрая команда</b> → выберите «{name}».",
    },
    "sc_steps_manual": {
        "en": "<b>Setup (Shortcuts app → +):</b>\n1. <b>Record Audio</b>: start Immediately, finish On Tap.\n2. <b>Get Contents of URL</b>: <code>{url}</code>\n   Method <b>POST</b> · header <code>Authorization</code> = <code>Bearer </code> + your key\n   Request Body <b>Form</b> · key <code>file</code>, type File, value <i>Recorded Audio</i>.\n3. Name it «{name}», then Settings → Action Button → Shortcut → pick it.",
        "ru": "<b>Настройка (приложение «Быстрые команды» → +):</b>\n1. <b>Записать аудио</b>: начало — сразу, окончание — по касанию.\n2. <b>Получить содержимое URL</b>: <code>{url}</code>\n   Метод <b>POST</b> · заголовок <code>Authorization</code> = <code>Bearer </code> + ваш ключ\n   Тело запроса <b>Форма</b> · ключ <code>file</code>, тип «Файл», значение <i>Записанное аудио</i>.\n3. Назовите её «{name}», затем Настройки → Кнопка действия → Быстрая команда → выберите её.",
    },
    "sc_footer": {
        "en": "🔒 Anyone with your key can add expenses to your account. If it leaks, tap 🔄 New key and the old one stops working at once.",
        "ru": "🔒 Любой, у кого есть ваш ключ, может добавлять расходы в ваш аккаунт. Если ключ утёк, нажмите 🔄 Новый ключ — старый сразу перестанет работать.",
    },
    "btn_sc_new_key": {"en": "🔄 New key", "ru": "🔄 Новый ключ"},
    "btn_sc_get_key": {"en": "📲 Get my Action Button key", "ru": "📲 Получить ключ для кнопки действия"},
    "help_shortcut": {
        "en": "\n\n📲 <b>iPhone Action Button</b>\nPress the Action Button, say what you spent, tap to stop. The proposal arrives here like any other message.\n\nFirst get your personal key with the button below (or ▶️ Start → 📲 Action Button), then:\n{steps}\n\nNo Action Button? Run the Shortcut from the home screen, Siri or Back Tap instead.",
        "ru": "\n\n📲 <b>Кнопка действия iPhone</b>\nНажмите кнопку действия, скажите, на что потратили, коснитесь, чтобы остановить. Предложение придёт сюда, как обычное сообщение.\n\nСначала получите личный ключ кнопкой ниже (или ▶️ Старт → 📲 Кнопка действия), затем:\n{steps}\n\nНет кнопки действия? Запускайте команду с экрана «Домой», через Siri или «Касание задней панели».",
    },
    "sc_new_key_done": {"en": "New key created. The old one no longer works.", "ru": "Новый ключ создан. Старый больше не работает."},
    "sc_off": {
        "en": "Action Button uploads aren't set up on this bot yet. Ask the bot's owner.",
        "ru": "Загрузка с кнопки действия в этом боте пока не настроена. Спросите владельца бота.",
    },
    "help_owner": {
        "en": '\n\n👑 <b>Owner</b> — no daily limits · 👥 Users: who uses the bot (with IDs) · /block &lt;id&gt; and /unblock &lt;id&gt;.',
        "ru": '\n\n👑 <b>Владелец</b> — без дневных лимитов · 👥 Пользователи: кто пользуется ботом (с ID) · /block &lt;id&gt; и /unblock &lt;id&gt;.',
    },

    # delete my data
    "m_delete": {"en": "🗑 Delete my data", "ru": "🗑 Удалить мои данные"},
    "del_warning": {
        "en": '⚠️ <b>Delete all your data?</b>\n\nThis removes from the bot:\n• all your saved expenses ({n})\n• your activity history and profile\n• your settings (language, report view), anything not yet saved, your Action Button key\n• your household membership (a household you created is ended)\n\n{when}\n\nShared categories stay.',
        "ru": '⚠️ <b>Удалить все ваши данные?</b>\n\nИз бота будут удалены:\n• все ваши сохранённые расходы ({n})\n• история действий и профиль\n• ваши настройки (язык, вид отчётов), всё несохранённое, ключ для кнопки действия\n• участие в семье (созданная вами семья будет распущена)\n\n{when}\n\nОбщие категории останутся.',
    },
    "del_when_later": {
        "en": "Your data disappears from the bot right away and is kept for {days} days, so you can restore it if this was a mistake. After that it's erased for good.",
        "ru": 'Данные сразу исчезнут из бота и будут храниться {days} дней — на случай, если вы передумаете. После этого они будут стёрты навсегда.',
    },
    "del_when_now": {
        "en": "This is permanent and can't be undone.",
        "ru": 'Это навсегда, отменить будет нельзя.',
    },
    "del_done_now": {
        "en": '✅ Done. Deleted {n} expenses, your profile and your settings.\nThe last few minutes of your activity history will be cleared within about 2 hours.',
        "ru": '✅ Готово. Удалено расходов: {n}, а также профиль и настройки.\nПоследние минуты истории действий будут очищены примерно в течение 2 часов.',
    },
    "btn_restore": {
        "en": '↩️ Restore my data',
        "ru": '↩️ Восстановить мои данные',
    },
    "m_restore": {
        "en": '↩️ Restore my data',
        "ru": '↩️ Восстановить мои данные',
    },
    "restored": {
        "en": "↩️ Restored: {n} expenses and your settings are back. Household membership isn't restored; ask for an invite link to rejoin.",
        "ru": '↩️ Восстановлено: расходов — {n}, настройки тоже вернулись. Участие в семье не восстанавливается — попросите ссылку-приглашение, чтобы вступить снова.',
    },
    "restore_nothing": {
        "en": "There's nothing to restore: no deleted data is waiting, or it has already been erased.",
        "ru": 'Восстанавливать нечего: удалённых данных нет или они уже стёрты.',
    },
    "restore_failed": {
        "en": "⚠️ Restoring didn't finish. Try again in a minute; repeating it is safe.",
        "ru": '⚠️ Восстановление не завершилось. Попробуйте ещё раз через минуту — повторять безопасно.',
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
        "en": "✅ Done. {n} expenses, your profile and your settings are gone from the bot.\n\nThey're kept for {days} days and erased for good on {date}. Changed your mind? Tap ↩️ Restore my data before then (it's also in the menu).",
        "ru": '✅ Готово. Расходов: {n}, а также профиль и настройки удалены из бота.\n\nОни хранятся {days} дней и будут стёрты навсегда {date}. Передумали? Нажмите ↩️ Восстановить мои данные до этого срока (кнопка есть и в меню).',
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
        "en": '{i}. {icon} {name} · <code>{uid}</code> — {actions} actions ({recent} in 30 days) · {saved} saved · last {last}',
        "ru": '{i}. {icon} {name} · <code>{uid}</code> — действий: {actions} (за 30 дней: {recent}) · сохранено: {saved} · был(а) {last}',
    },
    "users_more": {"en": "…and {n} more", "ru": "…и ещё {n}"},
    "users_legend": {
        "en": '👑 you · 🏠 created a household · 👤 household member · 🙂 on their own · 🚫 blocked\nBlock someone: /block &lt;id&gt; · undo: /unblock &lt;id&gt;',
        "ru": '👑 вы · 🏠 создал(а) семью · 👤 участник семьи · 🙂 сам(а) по себе · 🚫 заблокирован(а)\nЗаблокировать: /block &lt;id&gt; · отменить: /unblock &lt;id&gt;',
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
    "blocked": {
        "en": "This bot isn't available to you.",
        "ru": 'Этот бот вам недоступен.',
    },
    "limit_text": {
        "en": "You've reached today's limit of {n} messages. It resets at midnight.",
        "ru": 'Вы достигли дневного лимита — {n} сообщений. Он обнулится в полночь.',
    },
    "limit_voice": {
        "en": "You've reached today's limit of {n} voice notes. It resets at midnight; text messages still work.",
        "ru": 'Вы достигли дневного лимита — {n} голосовых. Он обнулится в полночь; текстом писать можно.',
    },
    "voice_too_long": {
        "en": 'That recording is too long. Keep voice notes under {s} seconds.',
        "ru": 'Слишком длинная запись. Голосовые — не длиннее {s} секунд.',
    },
    "block_usage": {
        "en": 'Usage: <code>/block &lt;user_id&gt;</code> · <code>/unblock &lt;user_id&gt;</code>. IDs are in 👥 Users.',
        "ru": 'Формат: <code>/block &lt;user_id&gt;</code> · <code>/unblock &lt;user_id&gt;</code>. ID есть в 👥 Пользователи.',
    },
    "block_self": {
        "en": "You can't block yourself.",
        "ru": 'Нельзя заблокировать себя.',
    },
    "blocked_done": {
        "en": "🚫 Blocked <code>{uid}</code>. They can't use the bot any more; their data stays. /unblock {uid} undoes it.",
        "ru": '🚫 <code>{uid}</code> заблокирован. Пользоваться ботом больше нельзя, данные сохранены. /unblock {uid} — отменить.',
    },
    "unblocked_done": {
        "en": '✅ Unblocked <code>{uid}</code>.',
        "ru": '✅ <code>{uid}</code> разблокирован.',
    },
    "not_blocked": {
        "en": "<code>{uid}</code> wasn't blocked.",
        "ru": '<code>{uid}</code> не был заблокирован.',
    },
    "hh_none": {
        "en": "🏠 <b>You're not in a household</b>\n\nA household lets family or flatmates see combined totals: everyone logs their own expenses as usual, and 👨\u200d👩\u200d👧 Family totals adds them up (per person and per category). Individual expenses stay private.\n\nCreate one and share its invite link, or open a link someone sent you.",
        "ru": '🏠 <b>Вы не состоите в семье</b>\n\nСемья — это общие итоги для близких или соседей: каждый записывает свои расходы как обычно, а 👨\u200d👩\u200d👧 Итоги семьи складывают их (по людям и категориям). Отдельные траты остаются личными.\n\nСоздайте семью и отправьте ссылку-приглашение или откройте ссылку, которую прислали вам.',
    },
    "btn_create_household": {
        "en": '🏠 Create household',
        "ru": '🏠 Создать семью',
    },
    "hh_default_name": {
        "en": "{name}'s household",
        "ru": 'Семья {name}',
    },
    "hh_you_tag": {
        "en": ' — you',
        "ru": ' — вы',
    },
    "hh_invite": {
        "en": '🔗 Invite link — anyone who opens it can join:\n{link}\n🔄 New link turns the old one off.',
        "ru": '🔗 Ссылка-приглашение — любой, кто её откроет, сможет вступить:\n{link}\n🔄 Новая ссылка отключает старую.',
    },
    "btn_new_link": {
        "en": '🔄 New link',
        "ru": '🔄 Новая ссылка',
    },
    "hh_member_note": {
        "en": "You see everyone's totals in 👨\u200d👩\u200d👧 Family totals. Only the creator can invite people.",
        "ru": 'Итоги всех — в 👨\u200d👩\u200d👧 Итоги семьи. Приглашать может только создатель.',
    },
    "btn_leave_household": {
        "en": '🚪 Leave household',
        "ru": '🚪 Выйти из семьи',
    },
    "hh_creator_only": {
        "en": "Only the household's creator can do that",
        "ru": 'Это может сделать только создатель семьи',
    },
    "hh_created_toast": {
        "en": 'Household ready',
        "ru": 'Семья создана',
    },
    "hh_new_link_toast": {
        "en": 'New link ready. The old one no longer works.',
        "ru": 'Новая ссылка готова. Старая больше не работает.',
    },
    "hh_you_were_removed": {
        "en": 'You were removed from “{name}”. Your expenses are still yours.',
        "ru": 'Вас удалили из «{name}». Ваши расходы остались у вас.',
    },
    "hh_ended_by_creator": {
        "en": '“{name}” was ended by its creator. Your expenses are still yours.',
        "ru": 'Создатель распустил «{name}». Ваши расходы остались у вас.',
    },
    "hh_leave_confirm": {
        "en": 'Leave “{name}”? Your expenses stay yours; the others just stop seeing your totals.',
        "ru": 'Выйти из «{name}»? Ваши расходы останутся у вас — другие просто перестанут видеть ваши итоги.',
    },
    "btn_yes_leave": {
        "en": 'Yes, leave',
        "ru": 'Да, выйти',
    },
    "hh_left": {
        "en": 'You left “{name}”.',
        "ru": 'Вы вышли из «{name}».',
    },
    "hh_member_left": {
        "en": '🚪 {who} left “{name}”.',
        "ru": '🚪 {who} вышел(а) из «{name}».',
    },
    "hh_invite_invalid": {
        "en": "This invite link doesn't work any more. Ask for a new one.",
        "ru": 'Эта ссылка-приглашение больше не работает. Попросите новую.',
    },
    "hh_already_in": {
        "en": "You're already in “{name}”.",
        "ru": 'Вы уже в «{name}».',
    },
    "hh_in_other": {
        "en": "You're in “{mine}”. To join “{other}”, first leave or end yours in 🏠 Household, then open the link again.",
        "ru": 'Вы состоите в «{mine}». Чтобы вступить в «{other}», сначала выйдите из своей семьи (или распустите её) в 🏠 Семья и откройте ссылку снова.',
    },
    "hh_join_prompt": {
        "en": "🏠 <b>Join “{name}”?</b>\n\nCreated by {creator}. Members see each other's totals per person and per category in 👨\u200d👩\u200d👧 Family totals. Individual expenses stay private. You can leave any time.",
        "ru": '🏠 <b>Вступить в «{name}»?</b>\n\nСоздатель: {creator}. Участники видят итоги друг друга по людям и категориям в 👨\u200d👩\u200d👧 Итоги семьи. Отдельные траты остаются личными. Выйти можно в любой момент.',
    },
    "btn_join": {
        "en": '✅ Join',
        "ru": '✅ Вступить',
    },
    "hh_joined_toast": {
        "en": 'Joined',
        "ru": 'Готово',
    },
    "hh_joined": {
        "en": "🏠 You're in <b>{name}</b>. Log expenses as usual; 👨\u200d👩\u200d👧 Family totals shows everyone together.",
        "ru": '🏠 Вы в <b>{name}</b>. Записывайте расходы как обычно — 👨\u200d👩\u200d👧 Итоги семьи покажут всех вместе.',
    },
    "hh_member_joined": {
        "en": '👋 {who} joined “{name}”.',
        "ru": '👋 {who} вступил(а) в «{name}».',
    },
    "hh_join_cancelled": {
        "en": "OK, you didn't join.",
        "ru": 'Хорошо, вы не вступили.',
    },
    "help_household": {
        "en": "\n\n🏠 <b>Household</b> — create one and share its invite link to see combined totals with family or flatmates. Everyone's individual expenses stay private.",
        "ru": '\n\n🏠 <b>Семья</b> — создайте её и отправьте ссылку-приглашение, чтобы видеть общие итоги с близкими или соседями. Отдельные траты каждого остаются личными.',
    },
    "help_limits": {
        "en": '\n\n⚖️ Daily limits: {text} messages and {voice} voice notes (up to {s} s each). They reset at midnight.',
        "ru": '\n\n⚖️ Дневные лимиты: {text} сообщений и {voice} голосовых (до {s} с каждое). Обнуляются в полночь.',
    },
    "help_privacy": {
        "en": "\n\n🔒 Your expenses are stored in the bot owner's Google Cloud (BigQuery). 🗑 Delete my data removes them from the bot at once and erases them for good after {days} days.",
        "ru": '\n\n🔒 Ваши расходы хранятся в Google Cloud (BigQuery) владельца бота. 🗑 Удалить мои данные — сразу убирает их из бота и навсегда стирает через {days} дней.',
    },

    # 💰 savings
    "kind_expense": {"en": "Expense", "ru": "Расход"},
    "kind_income": {"en": "Income", "ru": "Доход"},
    "kind_saving": {"en": "Put aside", "ru": "Отложено"},
    "kind_withdrawal": {"en": "From savings", "ru": "Из накоплений"},
    "kind_question": {"en": "Type: <b>{kind}</b>? <i>({tag})</i>", "ru": "Тип: <b>{kind}</b>? <i>({tag})</i>"},
    "pick_kind": {"en": "What is it?", "ru": "Что это?"},
    "pick_goal": {"en": "which goal?", "ru": "на какую цель?"},
    "btn_no_goal": {"en": "No goal", "ru": "Без цели"},
    "goal_gone": {"en": "That goal is closed now.", "ru": "Эта цель уже закрыта."},
    "m_savings": {"en": "💰 Savings", "ru": "💰 Накопления"},
    "r_income": {"en": "Income", "ru": "Доход"},
    "r_spent": {"en": "Spent", "ru": "Потрачено"},
    "r_put_aside": {"en": "Put aside", "ru": "Отложено"},
    "r_taken_out": {"en": "Taken from savings", "ru": "Взято из накоплений"},
    "r_left": {"en": "Left", "ru": "Осталось"},
    "sv_title": {"en": "Savings", "ru": "Накопления"},
    "sv_total": {"en": "Saved in total", "ru": "Всего накоплено"},
    "sv_rate": {"en": "Saving rate", "ru": "Доля сбережений"},
    "sv_goals": {"en": "Goals", "ru": "Цели"},
    "sv_no_goals": {"en": "No goals yet — tap 🎯 New goal.", "ru": "Целей пока нет — нажмите 🎯 Новая цель."},
    "sv_reached": {"en": "🎉 reached!", "ru": "🎉 достигнута!"},
    "sv_overdue": {"en": "the date ({date}) has passed", "ru": "срок ({date}) прошёл"},
    "sv_per_month": {"en": "{amount}/month to make it by {date}", "ru": "{amount} в месяц, чтобы успеть к {date}"},
    "sv_by": {"en": "by {date}", "ru": "к {date}"},
    "sv_how": {
        "en": "<i>Log like spending: “salary 600 000”, “put aside 50 000 for Japan”, “took 20 000 from savings”.</i>",
        "ru": "<i>Записывайте как расходы: «зарплата 600 000», «отложил 50 000 на Японию», «снял 20 000 с накоплений».</i>",
    },
    "btn_new_goal": {"en": "🎯 New goal", "ru": "🎯 Новая цель"},
    "btn_close_goal": {"en": "✅ Close a goal", "ru": "✅ Закрыть цель"},
    "btn_close_goal_yes": {"en": "✅ Close it", "ru": "✅ Закрыть"},
    "goal_prompt": {
        "en": "🎯 Send the goal as your next message: a name, a target and, if you like, a date.\n"
              "For example: <i>Trip to Japan 2 000 000 by June</i> or <i>Emergency fund 1 500 000</i>. "
              "No currency means {currency}.",
        "ru": "🎯 Отправьте цель следующим сообщением: название, сумму и, если хотите, срок.\n"
              "Например: <i>Поездка в Японию 2 000 000 к июню</i> или <i>Подушка безопасности 1 500 000</i>. "
              "Без валюты — {currency}.",
    },
    "goal_not_understood": {
        "en": "I couldn't find a goal in that. Try e.g. <i>New car 5 000 000 by 2027</i> — or tap Cancel above.",
        "ru": "Не нашёл в сообщении цели. Например: <i>Машина 5 000 000 к 2027</i> — или нажмите «Отмена» выше.",
    },
    "goal_cancelled": {"en": "OK, no new goal.", "ru": "Хорошо, без новой цели."},
    "goal_exists": {"en": "You already have a goal called <b>{name}</b>.", "ru": "У вас уже есть цель <b>{name}</b>."},
    "goal_too_many": {
        "en": "You can have up to {n} open goals. Close one first.",
        "ru": "Можно держать до {n} открытых целей. Сначала закройте одну.",
    },
    "goal_failed": {"en": "⚠️ Couldn't save that right now. Try again in a minute.", "ru": "⚠️ Не удалось сохранить. Попробуйте через минуту."},
    "goal_target": {"en": "target {amount}", "ru": "цель {amount}"},
    "goal_needs": {"en": "about {amount}/month", "ru": "примерно {amount} в месяц"},
    "goal_created": {
        "en": "🎯 New goal: <b>{name}</b>\n{details}\n\nTo add to it, say e.g. “put aside 50 000 for {example}”.",
        "ru": "🎯 Новая цель: <b>{name}</b>\n{details}\n\nЧтобы пополнить, напишите, например: «отложил 50 000 на {example}».",
    },
    "goal_which_close": {"en": "Which goal should I close?", "ru": "Какую цель закрыть?"},
    "goal_close_confirm": {
        "en": "Close <b>{name}</b>? Money already put aside for it stays in your savings total.",
        "ru": "Закрыть цель <b>{name}</b>? Уже отложенные на неё деньги останутся в общих накоплениях.",
    },
    "goal_closed": {"en": "✅ Closed <b>{name}</b>.", "ru": "✅ Цель <b>{name}</b> закрыта."},
    "help_savings": {
        "en": "\n\n💰 <b>Income & savings</b> — log them like spending: “salary 600 000”, “put aside 50 000”, "
              "“took 20 000 from the deposit”. Reports then show income, savings and what's left; "
              "💰 Savings shows your total, the month and your 🎯 goals with what's still needed per month. "
              "✏️ switches an entry between expense, income and savings.",
        "ru": "\n\n💰 <b>Доходы и накопления</b> — записывайте как расходы: «зарплата 600 000», «отложил 50 000», "
              "«снял 20 000 с депозита». В отчётах появятся доход, отложенное и остаток; "
              "💰 Накопления покажут общую сумму, месяц и 🎯 цели — сколько ещё нужно в месяц. "
              "✏️ переключает запись между расходом, доходом и накоплением.",
    },
}
