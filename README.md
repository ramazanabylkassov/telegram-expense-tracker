# Telegram expense tracker → BigQuery

Send the bot what you spent, as text or a voice note, any time of day:

> coffee 1500 · Magnum 12 400 and taxi 2k · вчера аптека 3500 · 🎙 *"spent twenty bucks on Netflix"*

An AI model (Gemini, Claude or ChatGPT, your choice) splits the message into expenses. The shared spend dictionary in BigQuery then decides or checks each category. The bot replies once per expense:

```
1 500 ₸ · Coffee (Starbucks)
Category: Eating out? (📖 known)          ← from the dictionary
[✅ Save] [✏️ Change] [🗑]

25 000 ₸ · Stationery (Meloman)
Category: Shopping? (🤖 guess)            ← model's guess, nothing known yet
```

**✅** saves the expense. **✏️** opens the category list, and picking one saves it straight away. **🗑** discards it. Every save also teaches the dictionary, so the next time *Meloman* comes up, the category is 📖 known.

**Report view** — the 📊 / 🧾 button under Today · Week · Month switches *your* reports between totals by category and a detailed list of every expense (grouped by day), and stays that way until you tap it again.

`/start` shows a button menu (📅 Today · 📆 Week · 🗓 Month · ↩️ Undo last · 🏷 Categories · 🔄 Reload, plus 🏠 Household for the owner and 👨‍👩‍👧 Family when a household is on). Tapping a button runs the command, and the menu stays in place.

Commands: `/today`, `/week`, `/month`, `/undo` (removes the last saved expense and what it taught the dictionary), `/categories`, `/reload`. In household mode there's also `/household` (owner only) and `/family`.

## Languages: English and Russian

Each person picks their language with **🌐 Language** in the `/start` menu, or with `/language`. The first time someone writes to the bot, their language is guessed from their Telegram app: Russian for ru, kk, uk, be, ky and uz, English otherwise.

The choice covers every message, button, report title and date (*Сегодня (сб, 26 сен)*, *Сентябрь 2026*), the household flows, and Telegram's own "/" command menu, which the bot sets for both languages on startup.

**Category names** live in `dim_categories`: `name` (English) and `name_ru`. Tables created before languages existed get `name_ru` added and filled automatically on the next start. Only empty values are filled, so your own edits are kept. To rename a category in Russian:
```sql
UPDATE `YOUR_PROJECT.finance.dim_categories` SET name_ru = 'Еда вне дома' WHERE name = 'Eating out';
```
Then run `/reload`.

**Data stays language-neutral.** Fact rows store `category_id`, the English category name and an English item `description`, which is the dictionary key. So "кофе" and "coffee" teach the same dictionary entry, and reports group by `category_id`, showing each category in the reader's language. For Russian users the AI also returns a Russian item label, used only for display.

To add another language, add its code to `LANGUAGES` in `i18n.py`, add a translation to every entry in `STRINGS`, and add a `name_<code>` column if you want translated category names.

## Delete my data

Everyone has **🗑 Delete my data** in the menu (or `/delete_my_data`). It's two steps: a warning that says how many expenses will go, then typing `DELETE` (or `УДАЛИТЬ`) within 5 minutes. Any other message cancels.

**What's deleted:** the person's `fct_expenses_<id>` table, their `dim_users` row, their rows in `log_interactions`, their join request, and everything on the bot's machine (unconfirmed proposals, undo history, language and report settings, unsent log rows).

**What stays:** shared categories and `dim_spend_variants`. The dictionary only holds anonymous counts ("starbucks → Eating out"), nothing tied to a person. Household membership also stays; the owner removes members with `/household`.

**The interaction log:** BigQuery can't delete rows streamed in the last ~30–90 minutes. Older rows go straight away, and the rest are retried every 30 minutes (queue in `pending.sqlite3`, table `log_purges`) until a clean pass. That usually takes under 2 hours. The confirmation message itself is never logged.

**Recovery:** none from the bot. BigQuery time travel can restore a dropped table for 7 days, by hand.

## Household mode (off by default)

Out of the box the bot is **private to you** (`OWNER_USER_ID`). Nothing household-related exists in BigQuery.

**The prompt to start one appears when it's needed:**
- **Someone else messages the bot.** They're told it's private, and you get a notification: *"Aigerim wants to use the bot. Start a household to let them in?"* with **🏠 Start household & add** and **Ignore** buttons. You're notified at most once a day per person.
- **You send `/household`.** You get the same prompt, with **🏠 Start household** and **Not now**.

**Once the household is started:**
- Each member logs into their own `fct_expenses_<id>` table, and the category dictionary is shared.
- `/family [today|week|month]` shows totals per person and per category, for every member.
- `/household` lists members with **Remove** buttons and an **End household** button, which asks for confirmation. You can also add someone directly with `/household add <telegram_id> <name>`.
- Ending the household removes members' access. Everyone's expenses stay in BigQuery.

Household state is stored in BigQuery (`dim_household_members`), so it survives moving the bot to another machine.

## Data model (BigQuery, dataset `finance`)

| Table | Scope | What's in it |
|---|---|---|
| `fct_expenses_<telegram_user_id>` | one per user | One row per confirmed expense: amount, currency, date, category, merchant, the raw input, what was suggested and whether it was corrected. Partitioned by `expense_date`. |
| `dim_categories` | shared | The category list: `category_id`, `name`, `description` (guidance for the model), `sort_order`, `is_active`. |
| `dim_spend_variants` | shared | The dictionary: every merchant (`starbucks`, `yandex go`) and item (`coffee`, `taxi`) ever confirmed, with its category and `confirmations` / `corrections` counts. |
| `dim_users` | shared | One row per Telegram user who ever wrote to the bot: name, @username, Telegram app language, language chosen in the bot, role (`owner` / `member` / `none`), first and last seen. Updated when something changes (at most hourly for `last_seen_at`); filled from the interaction log on first start. |
| `log_interactions` | shared | One row per update the bot receives: every text, voice note, command and button tap, including denied and unsupported ones. Records who sent it, what they sent, the transcript, the outcome (`proposed`, `saved`, `saved_corrected`, `discarded`, `denied`, `error`, …), the linked `expense_id`, the AI provider and model, latency, and the error with its traceback. Partitioned by day. |
| `dim_household_members` | household only | Who may use the bot: `user_id`, `display_name`, `role` (owner/member), `is_active`. Created when a household is started. |
| `v_expenses_all` | household only | View over all members' fact tables (`fct_expenses_*`). |
| `v_spend_variants` | view | The dictionary with category names. |

**Interaction log:** every handler is wrapped, so each update produces exactly one row, even when the handler crashes. Rows are buffered and streamed to BigQuery every 5 seconds in the background, so logging never slows down a reply. If BigQuery is unreachable, rows are retried for about 5 minutes, then appended to `interaction_log_failed.jsonl` so nothing is lost. `pending_ids` links a proposal message to the button taps on it. `schema.sql` has funnel, error and trace queries.

**How the dictionary is used on every mapping:**
1. The model sees the active categories with their descriptions, plus the ~300 most-confirmed variants as examples.
2. After the model answers, the bot looks up the merchant, then the item, in the dictionary. A hit overrides the model's guess, and when several categories match, the one with the most confirmations wins.
3. On save, the bot `MERGE`s the merchant and item into `dim_spend_variants` with the category the user actually chose.

**Changing categories:** do it directly in BigQuery. The bot re-reads the list every 10 minutes, or immediately on `/reload`.

```sql
INSERT `YOUR_PROJECT.finance.dim_categories` (category_id, name, description, sort_order, is_active)
VALUES (14, 'Kids', 'school, toys, kindergarten', 14, TRUE);

UPDATE `YOUR_PROJECT.finance.dim_categories` SET is_active = FALSE WHERE name = 'Travel';
```

Categories are referenced by `category_id`, so renaming one doesn't break old rows. Each fact row also keeps the name as it was when the expense was saved. `schema.sql` has the full DDL and useful queries.

On first start the bot creates and seeds everything: 13 categories and about 70 common Kazakhstan merchants and items. The seed data lives in `config.py` and is used only when the tables are empty.

## Setup on your Mac (~15 min)

1. **Telegram bot.** Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
2. **Service account.** In your GCP project (billing must be enabled, see *Cost* below):
   - Create a service account and grant it **BigQuery Data Editor** and **BigQuery Job User** on the project. Add **Vertex AI User** only if you use Gemini through Vertex.
   - Create a JSON key and save it in the project folder as `service-account.json`. `.gitignore` already excludes it. Keep the key on your Mac; it never needs to go anywhere else.
3. **AI provider.** Set `LLM_PROVIDER` in `.env`:
   - `gemini` (default, $0): a key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey). Free-tier prompts may be used by Google to improve its products. Alternatively, leave the key empty to use Vertex AI through the service account. That needs billing, but prompts aren't used for training.
   - `claude`: `ANTHROPIC_API_KEY` from [platform.claude.com](https://platform.claude.com). Haiku 4.5 costs about $1–2 a month. Voice is transcribed locally with Whisper, which is free and keeps the audio on your Mac. The first start downloads the Whisper model, about 500 MB for `small`.
   - `openai`: `OPENAI_API_KEY`. Voice goes to `gpt-transcribe`, text to `gpt-6-luna`.
4. **Configure.** Run `cp .env.example .env` and fill in `TELEGRAM_BOT_TOKEN`, `GCP_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS=./service-account.json` and your AI key. Leave `OWNER_USER_ID` empty for now.
5. **Run it once in the terminal.**
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python bot.py
   ```
   Message the bot. It replies with your Telegram user ID. Put it in `OWNER_USER_ID` and restart. The bot is now yours alone. To share it later, see *Household mode*.
6. **Run it in the background.** It then starts at login and restarts if it crashes:
   ```bash
   bash mac/install.sh     # logs: tail -f ~/Library/Logs/expense-bot.log
   bash mac/uninstall.sh   # stop
   ```

## Cost

- **BigQuery:** $0 in practice. A few KB of data a day and tiny queries stay far inside the always-free 10 GB of storage and 1 TB of queries a month. The interaction log uses streaming inserts, which are billed at $0.01 per 200 MB. At about 1 KB per interaction, that's fractions of a cent a year. Billing must still be *enabled*, because the dictionary updates (`MERGE`) and `/undo` (`DELETE`) are DML, which the no-billing sandbox refuses. A $1 budget alert is a good safety net.
- **AI:** $0 with Gemini's free tier, or about $1–2 a month with Claude Haiku or ChatGPT.
- **Hosting:** your Mac. Telegram holds messages for up to 24 hours while the Mac sleeps, and each expense is still dated by when you sent it.

## Files

| File | What it does |
|---|---|
| `bot.py` | Telegram handlers, buttons, commands |
| `catalog.py` | Shared categories + dictionary: cache, lookup, learning |
| `users.py` | `dim_users`: who's who, kept current from every interaction |
| `i18n.py` | All user-facing text in English and Russian, date formats, Telegram command menus |
| `household.py` | Optional household mode: members, join requests, `/family` totals |
| `interactions.py` | Interaction log: per-update record, background streaming to BigQuery |
| `extractor.py` | Prompt, JSON schema and validation for Gemini / Claude / OpenAI |
| `transcribe.py` | Voice → text for Claude/ChatGPT (local Whisper or OpenAI) |
| `storage.py` | BigQuery tables (per-user facts, shared dims), local SQLite for pending taps |
| `config.py` | Settings and first-run seed data |
| `schema.sql` | DDL and example queries |
| `mac/` | launchd install/uninstall |
| `Dockerfile` | If you later move it to a server |

Notes: facts go in through BigQuery load jobs rather than streaming inserts. Load jobs are free, and a row can be deleted immediately. Amounts in different currencies are stored as given and are never converted.
