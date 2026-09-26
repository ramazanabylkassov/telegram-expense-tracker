"""Settings, read from environment variables (or a .env file)."""
import os
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _req(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# --- Telegram -----------------------------------------------------------------
TELEGRAM_BOT_TOKEN = _req("TELEGRAM_BOT_TOKEN")
# Your Telegram user ID. The bot is private to you unless you start household mode
# with /household. Leave empty on first run: the bot will reply with your ID.
_owner = os.getenv("OWNER_USER_ID") or os.getenv("ALLOWED_USER_IDS", "").split(",")[0]  # old name still works
OWNER_USER_ID = int(_owner.strip()) if _owner.strip() else None

# --- Google Cloud / BigQuery --------------------------------------------------
GCP_PROJECT = _req("GCP_PROJECT")
BQ_DATASET = os.getenv("BQ_DATASET", "finance")
BQ_LOCATION = os.getenv("BQ_LOCATION", "US")

# --- LLM ---------------------------------------------------------------------
# "gemini", "claude" or "openai" (ChatGPT)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
if LLM_PROVIDER not in ("gemini", "claude", "openai"):
    raise RuntimeError("LLM_PROVIDER must be 'gemini', 'claude' or 'openai'")

# Voice notes for Claude/ChatGPT (Gemini hears audio itself):
#   local  -> Whisper on this machine (free)      openai -> OpenAI transcription API (paid)
TRANSCRIBER = os.getenv("TRANSCRIBER", "openai" if LLM_PROVIDER == "openai" else "local").strip().lower()
if TRANSCRIBER not in ("local", "openai"):
    raise RuntimeError("TRANSCRIBER must be 'local' or 'openai'")
# tiny / base / small / medium / large-v3 — bigger is more accurate but slower
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")

# Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") if LLM_PROVIDER != "claude" else _req("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")

# OpenAI: text model parses expenses; voice is transcribed first with the
# transcription model.
_needs_openai = LLM_PROVIDER == "openai" or (LLM_PROVIDER != "gemini" and TRANSCRIBER == "openai")
OPENAI_API_KEY = _req("OPENAI_API_KEY") if _needs_openai else os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-transcribe")

# Gemini:
# If GEMINI_API_KEY is set, the Gemini Developer API is used.
# Otherwise Vertex AI is used with the same service account as BigQuery.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# --- Behaviour ----------------------------------------------------------------
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty"))
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "KZT")
PENDING_DB_PATH = os.getenv("PENDING_DB_PATH", "pending.sqlite3")

# Telegram's "/" command list and the ☰ button next to the typing field.
# Off by default: the ▶️ Start button covers everything, and typed commands still work.
COMMAND_MENU = os.getenv("COMMAND_MENU", "off").strip().lower() in ("on", "true", "1", "yes")

# BigQuery table names: one fact table per Telegram user, e.g. fct_expenses_123456789
FACT_TABLE_PREFIX = os.getenv("FACT_TABLE_PREFIX", "fct_expenses_")
# How often (seconds) the bot re-reads the shared category list and dictionary from BigQuery
CATALOG_REFRESH_SECONDS = int(os.getenv("CATALOG_REFRESH_SECONDS", "600"))
# Interaction log (BigQuery table log_interactions): rows are buffered and streamed
# every N seconds; rows that keep failing are appended to a local JSONL file.
INTERACTION_LOG_FLUSH_SECONDS = float(os.getenv("INTERACTION_LOG_FLUSH_SECONDS", "5"))
INTERACTION_LOG_BATCH = int(os.getenv("INTERACTION_LOG_BATCH", "50"))
INTERACTION_LOG_FALLBACK = os.getenv("INTERACTION_LOG_FALLBACK", "interaction_log_failed.jsonl")
# How many known spend variants to show the model on each mapping
PROMPT_VARIANTS_LIMIT = int(os.getenv("PROMPT_VARIANTS_LIMIT", "300"))

# ---------------------------------------------------------------------------
# SEED DATA — used only once, to fill dim_categories / dim_spend_variants when
# they are empty. After that, BigQuery is the source of truth: edit categories
# there (rename, add, set is_active = FALSE) and the bot picks it up.
# ---------------------------------------------------------------------------
CATEGORIES = [
    "Groceries",
    "Eating out",
    "Transport",
    "Housing & Utilities",
    "Health",
    "Shopping",
    "Entertainment",
    "Subscriptions & Services",
    "Education",
    "Travel",
    "Gifts & Donations",
    "Personal care",
    "Other",
]

# Written to dim_categories.description; shown to the model as guidance.
CATEGORY_HINTS = {
    "Groceries": "supermarkets, markets, household food",
    "Eating out": "restaurants, cafes, coffee, fast food, food delivery",
    "Transport": "taxi, fuel, parking, bus, metro, car service",
    "Housing & Utilities": "rent, electricity, water, gas, internet, mobile plan",
    "Health": "pharmacy, doctor, dentist, lab tests, gym membership",
    "Shopping": "clothes, electronics, marketplaces, home goods",
    "Entertainment": "cinema, concerts, games, bars, hobbies",
    "Subscriptions & Services": "streaming, software, cloud storage, bank fees",
    "Education": "courses, books, tuition",
    "Travel": "flights, hotels, trips",
    "Gifts & Donations": "presents, charity",
    "Personal care": "haircut, cosmetics, beauty salon",
    "Other": "anything that does not fit elsewhere",
}

# Russian names, written to dim_categories.name_ru. Also used once to fill name_ru on a
# table created before languages existed (only where name_ru is still empty).
CATEGORY_NAMES_RU = {
    "Groceries": "Продукты",
    "Eating out": "Кафе и рестораны",
    "Transport": "Транспорт",
    "Housing & Utilities": "Жильё и коммуналка",
    "Health": "Здоровье",
    "Shopping": "Покупки",
    "Entertainment": "Развлечения",
    "Subscriptions & Services": "Подписки и сервисы",
    "Education": "Образование",
    "Travel": "Путешествия",
    "Gifts & Donations": "Подарки и благотворительность",
    "Personal care": "Уход за собой",
    "Other": "Другое",
}

# Initial spend variants for dim_spend_variants. The table then grows by itself.
SEED_VARIANTS = {
    "Groceries": {"merchants": ["Magnum", "Small", "Galmart", "Anvar", "Arbuz.kz"], "items": ["Groceries", "Supermarket"]},
    "Eating out": {"merchants": ["Starbucks", "Wolt", "Glovo", "KFC", "Burger King"], "items": ["Coffee", "Lunch", "Dinner", "Restaurant"]},
    "Transport": {"merchants": ["Yandex Go", "inDrive", "Onay"], "items": ["Taxi", "Fuel", "Parking", "Bus"]},
    "Housing & Utilities": {"merchants": ["Kazakhtelecom", "Beeline", "Kcell", "Tele2"], "items": ["Rent", "Electricity", "Internet", "Mobile plan"]},
    "Health": {"merchants": ["Europharma", "Invivo"], "items": ["Pharmacy", "Doctor", "Dentist", "Gym"]},
    "Shopping": {"merchants": ["Kaspi Magazin", "Wildberries", "Ozon", "Sulpak", "Technodom"], "items": ["Clothes", "Electronics"]},
    "Entertainment": {"merchants": ["Kinopark", "Chaplin"], "items": ["Cinema", "Concert", "Bar"]},
    "Subscriptions & Services": {"merchants": ["Netflix", "Spotify", "iCloud", "YouTube Premium", "ChatGPT", "Claude"], "items": ["Subscription"]},
    "Education": {"merchants": ["Coursera", "Udemy"], "items": ["Course", "Books"]},
    "Travel": {"merchants": ["Air Astana", "FlyArystan", "Aviata", "Booking.com"], "items": ["Flight", "Hotel"]},
    "Gifts & Donations": {"items": ["Gift", "Donation"]},
    "Personal care": {"items": ["Haircut", "Barber", "Cosmetics"]},
}
