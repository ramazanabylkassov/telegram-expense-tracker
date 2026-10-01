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
# Your Telegram user ID: the bot's owner (no daily limits, 👥 Users, /block).
# Anyone else can use the bot too, within the daily limits below.
# Leave empty on first run: the bot will reply with your ID.
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

# AI prices in USD per million tokens, for the cost shown on the 🖥 App status page.
# Defaults are Claude Haiku 4.5's list prices; set them for another model (0 = show tokens only).
_haiku = LLM_PROVIDER == "claude" and "haiku" in CLAUDE_MODEL
LLM_PRICE_INPUT = float(os.getenv("LLM_PRICE_INPUT", "1" if _haiku else "0"))
LLM_PRICE_OUTPUT = float(os.getenv("LLM_PRICE_OUTPUT", "5" if _haiku else "0"))

# --- Behaviour ----------------------------------------------------------------
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty"))
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "KZT")
PENDING_DB_PATH = os.getenv("PENDING_DB_PATH", "pending.sqlite3")

# Telegram's "/" command list and the ☰ button next to the typing field.
# Off by default: the ▶️ Start button covers everything, and typed commands still work.
COMMAND_MENU = os.getenv("COMMAND_MENU", "off").strip().lower() in ("on", "true", "1", "yes")

# Daily limits per person (the owner has none), reset at midnight in TIMEZONE. 0 = no limit.
# Each message or voice note that goes to the AI counts once, however many expenses it holds.
DAILY_TEXT_LIMIT = int(os.getenv("DAILY_TEXT_LIMIT", "50"))
DAILY_VOICE_LIMIT = int(os.getenv("DAILY_VOICE_LIMIT", "30"))
MAX_VOICE_SECONDS = int(os.getenv("MAX_VOICE_SECONDS", "120"))  # longer voice notes are refused (0 = any)

# "Delete my data": the data is hidden at once and kept this many days (restorable from the bot),
# then erased for good. 0 = erase right away, no restore.
DELETE_RETENTION_DAYS = int(os.getenv("DELETE_RETENTION_DAYS", "30"))

# A proposal nobody answers is saved with its suggested category after this many minutes
# (counted from the proposal or the last tap on it). 0 = never auto-save.
AUTO_SAVE_MINUTES = float(os.getenv("AUTO_SAVE_MINUTES", "10"))

# Upload endpoint for the iPhone Action Button / Shortcuts (see ingest.py and README).
# Off unless INGEST_SECRET is set. Generate one with:
#   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
INGEST_SECRET = os.getenv("INGEST_SECRET", "").strip() or None
INGEST_HOST = os.getenv("INGEST_HOST", "127.0.0.1")  # a tunnel (e.g. Tailscale Funnel) forwards to this
INGEST_PORT = int(os.getenv("INGEST_PORT", "8787"))
# 📊 Monitoring dashboard (owner only; sign in from the bot). It has its own server that only
# listens on this Mac (127.0.0.1), on a port the Tailscale tunnel doesn't forward.
DASHBOARD = os.getenv("DASHBOARD", "on").strip().lower() in ("on", "true", "1", "yes")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8788"))
if DASHBOARD and DASHBOARD_PORT == INGEST_PORT:
    raise RuntimeError("DASHBOARD_PORT must differ from INGEST_PORT (the dashboard must not share the tunnelled port)")
# Public address of the endpoint (e.g. https://your-mac.tail1234.ts.net). Needed for the bot's
# 📲 Action Button menu, which gives every user their own key and the setup steps.
INGEST_PUBLIC_URL = os.getenv("INGEST_PUBLIC_URL", "").strip().rstrip("/") or None
# iCloud link to a shareable copy of the Shortcut (it asks for the key on install). Optional:
# without it, the bot explains how to build the Shortcut by hand.
SHORTCUT_URL = os.getenv("SHORTCUT_URL", "").strip() or None
SHORTCUT_NAME = os.getenv("SHORTCUT_NAME", "Log expense").strip()

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
    "Taxes",
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
    "Taxes": "income, property, vehicle and land tax, social payments, pension contributions, state fees and fines",
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
    "Taxes": "Налоги",
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
    "Taxes": {"merchants": ["eGov", "Salyk"], "items": ["Tax", "Income tax", "Property tax", "Vehicle tax", "Pension contribution", "Fine"]},
}
