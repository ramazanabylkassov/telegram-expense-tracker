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

**No answer for 10 minutes?** The bot saves the expense with its suggested category and edits the message to `✅ Eating out · auto-saved`. Tapping ✏️ restarts the 10 minutes, so you won't lose it mid-pick. Auto-saved rows get `confirmed_by = 'auto'` in BigQuery, and they don't teach the dictionary (only your own taps do). `/undo` works on them as usual. Change the delay with `AUTO_SAVE_MINUTES` in `.env` (`0` turns it off).

**Voice misheard?** Every 🎙 transcript, from Telegram voice notes and Action Button uploads alike, has a **✏️ Fix text** button. Tapping it shows the transcript ready to copy; send the corrected text as your next message (within 10 minutes; Cancel is under the prompt). Replying directly to the 🎙 message works the same way. The bot then:
- withdraws the proposals from the misheard version that are still unanswered (marked "✖️ replaced by the corrected text");
- updates the 🎙 message to show old → new;
- reads the corrected text again, dated by when the recording was sent (so "yesterday" stays right).

Proposals already saved stay saved, and the bot says so, so you can ↩️ Undo them. Tapping Fix also restarts their auto-save clock. A transcript can be corrected for 7 days and more than once. A correction counts as one text message towards the daily limit. Transcripts are kept locally in `pending.sqlite3` (`voice_notes`) for that week and removed by 🗑 Delete my data. In `log_interactions` a correction is a text row with `details.transcript_fixed = true` and the `original_transcript`.

**Report view** — the 📊 / 🧾 button under Today · Week · Month switches *your* reports between totals by category and a detailed list of every expense (grouped by day), and stays that way until you tap it again.

**Delete any line:** in the 🧾 detailed view every expense is numbered (1, 2, 3… across the whole report). **🗑 Delete a line** under the report asks for the number. Send it as your next message (several at once: `3, 5`), or reply to the report with the numbers at any time. A message without numbers counts as an ordinary message. The bot shows those lines and asks to confirm. Deleting works like ↩️ Undo for any line:
- the row is removed from BigQuery;
- what that save taught the dictionary is rolled back (when the save's history is still on the Mac);
- the original proposal message is struck out.

No AI call is involved, so it doesn't count towards the daily limits. Numbers always refer to the report they came from, so a stale number can't hit the wrong expense, and a line deleted in one report shows as deleted in the others. The prompts never replace the pinned ▶️ Start keyboard; they use an inline Cancel button and wait 10 minutes for the answer. Reports can be used this way for 7 days (stored in `pending.sqlite3`: `report_rows`, `report_messages`).

## 💰 Income and savings

Log them the same way as spending, by text, voice or the Action Button. One message can mix them all: "зарплата 600 000, отложил 100 000 на Японию, кофе 1500".

| Kind | Examples | Shown as |
|---|---|---|
| Expense | "coffee 1500" | its category, as before |
| 💵 Income | "salary 600 000", "аванс 250к", "cashback 5000" | 💵 Income |
| 💰 Put aside | "put aside 50 000", "на депозит 200к", "отложил 30 000 на машину" | 💰 Put aside (→ 🎯 goal) |
| 🏦 From savings | "took 20 000 from savings", "снял 50 000 с депозита" | 🏦 From savings (→ 🎯 goal) |

Refunds, transfers between your own everyday accounts and loans are not recorded. The AI decides the kind; ✏️ on a proposal switches it:
- on an expense, ✏️ shows the categories plus 💵 / 💰 / 🏦;
- on anything else, it shows the other kinds and, for savings, your goals ("No goal" is fine).

A pick saves at once, like picking a category. Income and savings never teach the category dictionary, and auto-save, ↩️ Undo, 🗑 Delete a line and Delete my data work on them as on expenses.

**🎯 Goals.** 💰 Savings → 🎯 New goal, then send something like "Trip to Japan 2 000 000 by June" (the AI reads name, target, currency and date; target and date are optional). After that, "отложил 50 000 на Японию" counts towards that goal: the bot gives the model your goal names. ✅ Close a goal (with a confirmation) takes it off the list. Money already put aside stays in your total. You can have up to 8 open goals.

**💰 Savings** (menu or `/savings`) shows:
- the total saved (all time: put aside − taken out);
- this month: income, spent, put aside, taken out, and **left** = income − spent − (put aside − taken out);
- the saving rate (net put aside ÷ income);
- each goal: saved / target, a progress bar, %, and how much per month is still needed to make the date.

Currencies are never converted. Each one is shown separately, and "left" is only shown in currencies you earned in.

**Reports** keep their spending totals exactly as before. Under them come 💵 Income, 💰 Put aside, 🏦 Taken from savings and 🟰 Left, but only when the period has any. In the 🧾 detailed view income and savings lines carry their icon and aren't counted in the spending totals. Family totals stay spending only, so income and savings remain private.

`/start` shows a button menu (📅 Today · 📆 Week · 🗓 Month · 💰 Savings · ↩️ Undo last · 🏷 Categories · 🔄 Reload · 🏠 Household, plus 👨‍👩‍👧 Family totals when you're in a household and 👥 Users for the owner). Tapping a button runs the command, and the menu stays in place.

Commands: `/today`, `/week`, `/month`, `/savings`, `/undo` (removes the last saved expense and what it taught the dictionary), `/categories`, `/reload`, `/household`, `/family`. The owner also has `/users`, `/block <id>` and `/unblock <id>`.

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

## iPhone Action Button (or any Shortcut)

**Why not just send the recording to the chat?** A Shortcut that posts through the Telegram Bot API (`sendAudio` with the bot token) makes the *bot* the sender, and Telegram never delivers a bot's own messages back to it. So the bot has a small upload endpoint instead. The Shortcut sends the recording straight to the bot, and the usual ✅ / ✏️ / 🗑 proposal appears in your chat.

**1. Turn the endpoint on.** Generate a secret and add it to `.env`:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```
```
INGEST_SECRET=<the value printed above>
# INGEST_PORT=8787   (default)
```
Run `pip install -r requirements.txt` (this adds `aiohttp`) and restart. The log shows `Upload endpoint listening on http://127.0.0.1:8787/ingest`. It only listens on your Mac itself.

**2. Give your phone a way to reach your Mac.** Tailscale Funnel is free and gives a fixed HTTPS address:
- Install Tailscale on the Mac and sign in. Make its command-line tool available (the app's settings have an option to install it, or `brew install tailscale`).
- Run `tailscale funnel --bg 8787`. The first time, it gives you a link to enable HTTPS and Funnel for your account.
- You'll get an address like `https://macbook-pro-3.tail1234.ts.net`. Check it from your phone's browser: `https://…ts.net/health` should show `ok`.
- To stop it: `tailscale funnel off`.

**3. Build the Shortcut** (Shortcuts app → +):
1. **Record Audio.** Set *Start Recording* to Immediately and *Finish Recording* to On Tap.
2. **Get Contents of URL.** URL: `https://<your-address>.ts.net/ingest`. Method: **POST**. Headers: `Authorization` = `Bearer <INGEST_SECRET>`. Request Body: **Form**, with one field: key `file`, type **File**, value *Recorded Audio*.
3. Optional: **Get Dictionary Value** `expenses` → **Show Notification** "Sent ✓".

Then go to Settings → Action Button → Shortcut and pick it. For a text version, use **Dictate Text** and send a form field `text` instead of `file`.

**What the endpoint accepts:** `POST /ingest` takes multipart (`file` and/or `text`), raw `audio/*`, JSON `{"text": "…"}` or `text/plain`, up to 20 MB. The key goes in `Authorization: Bearer …` or `X-Api-Key`.

**Responses:** `200 {"ok": true, "expenses": n, "transcript": "…"}`; `401` wrong key; `413` too large; `415` nothing usable; `500` processing failed (you also get a message in the chat).

Each upload is logged in `log_interactions` as `upload_audio` or `upload_text`, and saved expenses have `source = upload_audio`. If the Mac is asleep, the Shortcut gets a connection error, so nothing is lost silently.

### Sharing the Action Button with other users

Every person gets their **own key**, and the key decides whose chat and table an upload goes to. `INGEST_SECRET` is the owner's key; never share it or a Shortcut that contains it.

**1. Tell the bot its public address** (in `.env`, then restart):
```
INGEST_PUBLIC_URL=https://<your-address>.ts.net
```
The menu now has a **📲 Action Button** item (also `/shortcut`) for everyone. The first tap creates that person's key and shows it once (only a hash is stored). **🔄 New key** replaces it, and the old key stops working right away. A key also stops working when its person is blocked or deletes their data. Uploads count towards the person's daily limits.

**2. Optional but much easier: a one-tap Shortcut link.** Make a shareable copy of your Shortcut that asks for the key on install:
1. In Shortcuts, duplicate your Action Button Shortcut and name the copy **Log expense**.
2. At the very top, add a **Text** action containing `PASTE-YOUR-KEY`.
3. In **Get Contents of URL**, change the `Authorization` header value to `Bearer ` followed by the **Text** variable (tap the value field, type `Bearer `, then pick *Text* from the variables bar).
4. Open the copy's settings (ⓘ) → **Setup** → **Add Import Question**. Pick the Text action and ask "Paste the key the bot gave you".
5. Run it once with your own key to check it, then set the Text back to `PASTE-YOUR-KEY`.
6. Share → **Copy iCloud Link**, and add it to `.env`:
```
SHORTCUT_URL=https://www.icloud.com/shortcuts/…
# SHORTCUT_NAME=Log expense   (default; the name the bot tells people to pick)
```
Now a member's **📲 Action Button** message reads: open the link → Add Shortcut → paste your key → Settings → Action Button → pick *Log expense*. Without `SHORTCUT_URL`, the bot shows the manual build steps with the URL filled in instead.

## 🖥 App status (on your Mac only)

A local web page showing how the **bot itself** is doing: users, traffic, AI tokens and cost, and problems. It never shows anyone's expenses: no amounts, no categories, and it doesn't read the per-user expense tables. It isn't part of the Telegram bot at all: no button, no command, nothing in the help.

**Open it:** while the bot runs, go to **http://127.0.0.1:8788** in any browser on the Mac (or run `open http://127.0.0.1:8788`). The bot's log prints the address at startup. Bookmark it; there's no sign-in.

**Why that's safe:** the page has its own tiny server that listens only on the Mac (`127.0.0.1:8788`). The Tailscale tunnel forwards a different port (8787, the Action Button upload), so the page can't be reached from your phone or the internet. It also refuses requests that:
- don't come directly from the Mac;
- use an address other than the Mac's own (this stops a website from reaching it through DNS tricks);
- came through a proxy or tunnel;
- were made by another website open in your browser.

Browsers also don't let other sites read its data. Change the port with `DASHBOARD_PORT` (it must differ from `INGEST_PORT`); turn the page off with `DASHBOARD=off`.

**What's on it** (last 24 hours, 7, 30 or 90 days, each compared with the period before):
- **Users:** total, active, new, and **left**: people who blocked the bot in Telegram (the bot now logs Telegram's notice) plus people who used 🗑 Delete my data. Charts of active and new users.
- **Traffic:** all requests, messages that went to the AI (text, voice, Action Button), minutes of voice Whisper transcribed, and daily-limit hits.
- **AI usage:** tokens sent and received, and the **cost** for the period and this month, with a projection for the month. Also tokens per message, and response time and tokens by input type. Token counts come from each AI response and are stored in `log_interactions` (`llm_input_tokens`, `llm_output_tokens`). Prices are set with `LLM_PRICE_INPUT` / `LLM_PRICE_OUTPUT` in USD per million tokens (defaults: Claude Haiku 4.5, $1 / $5).
- **Issues:** errors and error rate, **Telegram outages** (times this Mac couldn't reach Telegram and how long; stored in `pending.sqlite3`, table `outages`), response time, how often the AI's category was kept, and the 20 latest errors with their text.
- **People:** everyone active with their status (you, household, on their own, blocked by you, blocked the bot), actions, AI messages, tokens, errors, today's usage against the limits, and their ID for `/block`.
- **Bot health & settings:** uptime, last message received, Telegram connection, queues, restorable deleted accounts, households, blocks, AI model and prices, dictionary size, limits.

Every chart has a "Show as table" view. **Auto-refresh:** every 5 minutes by default, or choose every minute, 15 minutes, hour or off (remembered in that browser). A countdown shows the next update, and it only counts while the tab is visible. BigQuery numbers are cached for 5 minutes on the bot's side, so a faster setting only updates the bot's own numbers and never raises costs. **↻ Refresh** fetches fresh BigQuery numbers, at most once a minute. The page follows your light or dark setting.

**Checked at startup:** when the bot starts, BigQuery validates every query on the page with a free dry run. The log says `App status queries checked by BigQuery: all OK`, or names the query it rejected. Each section loads separately, so one failing query shows an error in its card and the rest still loads. The queries only read `log_interactions`, `dim_users` and the dictionary, about 90 MB per refresh however many users you have, so even a page left open all day stays inside BigQuery's free tier. Token and audio counts start from this version; older rows have none.

## Delete my data (soft delete, erased after 30 days)

Everyone has **🗑 Delete my data** in the menu (or `/delete_my_data`). It's two steps: a warning that says how many expenses will go, then typing `DELETE` (or `УДАЛИТЬ`) within 5 minutes. Any other message cancels.

**Right away (soft delete):** the data disappears from the bot.
- The person's `fct_expenses_<id>` table is copied to `deleted_fct_expenses_<id>_<timestamp>` and the original is dropped. The copy is outside the `fct_expenses_*` wildcard, so reports, family totals and `v_expenses_all` no longer see it. Copy jobs are free.
- Their `dim_users` row gets `deleted_at`, which hides them from 👥 Users and the dashboard.
- On your Mac: unconfirmed proposals, undo history and the Action Button key are deleted. Language and report view are kept aside for a restore.
- They leave their household, or it's ended if they created it. Nobody else's expenses are touched.
- Their `log_interactions` rows stay until the final erase.

**Within 30 days:** **↩️ Restore my data** puts everything back. The button is under the "done" message, in the menu while there's something to restore, and at `/restore_my_data`. Expenses logged since are kept alongside the restored ones. Household membership isn't restored; they rejoin with an invite link.

**After 30 days (hard delete):** a pass every 30 minutes erases the archive table, their log rows up to the deletion, their `dim_users` row and the kept settings. Anything they logged after deleting is left alone, because it's their current data. If they used the bot again in the meantime, their current profile row is kept too. As a backstop, the archive table has a BigQuery **expiration of 31 days**, so it disappears even if your Mac is off. The remaining steps run the next time the bot is up.

**Setting:** `DELETE_RETENTION_DAYS` (default 30). `0` means erase at once with no restore, as before; log rows still in BigQuery's streaming buffer are retried until gone.

**What stays:** shared categories and `dim_spend_variants`. The dictionary only holds anonymous counts ("starbucks → Eating out"), nothing tied to a person. A block by the owner and today's usage against the daily limits also stay, so deleting data isn't a way around them.

**On your Mac:** `pending.sqlite3`, table `deletions`, lists every deletion with its status (`soft`, `restored` or `purged`). A purged row keeps only the dates, nothing of the person's data. The dashboard's health card shows how many deleted accounts are waiting and when the next one is erased.

## Who can use the bot

**Anyone.** Whoever finds the bot can start logging straight away. Each person gets their own `fct_expenses_<id>` table, and everyone shares the category dictionary.

**The owner** (`OWNER_USER_ID`, you) has no limits, sees **👥 Users** (everyone, most active first, with their IDs) and can block people.

**Daily limits** protect your AI credits and your Mac. Only messages that go to the AI count, however many expenses they hold. The limits reset at midnight in `TIMEZONE`, and `0` turns a limit off:

| `.env` | Default | What it limits |
|---|---|---|
| `DAILY_TEXT_LIMIT` | 50 | Text messages (and text uploads) per person per day |
| `DAILY_VOICE_LIMIT` | 30 | Voice notes (and audio uploads) per person per day |
| `MAX_VOICE_SECONDS` | 120 | Length of one voice note. For uploads, the file size stands in for it (~32 KB per second) |

Someone over a limit gets a short message saying when it resets, and the AI isn't called. Counts are kept in `pending.sqlite3` (table `usage`, last 7 days).

**Blocking:** `/block <id>` stops someone using the bot, including buttons and their Action Button key. Their data stays. `/unblock <id>` undoes it. IDs are shown in 👥 Users. Blocks are stored in `pending.sqlite3` (`blocked_users`).

**Privacy:** ❓ How it works tells people their expenses are stored in your Google Cloud and that 🗑 Delete my data erases them. You can read everyone's tables in BigQuery, so run a public bot only if you're comfortable being responsible for that data.

## Households

A household is a group (a family, flatmates) that sees **combined totals**. Anyone can create one. A person can be in **one household at a time**.

- **Create:** 🏠 Household → **Create household**. It's named after its creator ("Ann's household" / "Семья Ann").
- **Invite:** the creator's 🏠 Household view shows an invite link like `https://t.me/<bot>?start=join_<code>`. Opening it shows *"Join “Ann's household”?"* with ✅ Join / Cancel. Someone opening the bot for the first time through the link also gets the intro and the ▶️ Start button. **🔄 New link** turns the old link off.
- **Together:** everyone keeps logging into their own table. **👨‍👩‍👧 Family totals** (`/family [today|week|month]`) shows the household's totals per person and per category. Individual expenses aren't shown to other members.
- **Manage (creator only):** **Remove** a member, **🔄 New link**, **End household**, which asks for confirmation. Members can **🚪 Leave**. The people affected get a short message each time.
- **Nothing is deleted:** leaving, removal and ending never touch anyone's expenses.
- **Already in one?** Opening another household's link says to leave or end the current one first.

State is stored in BigQuery (`dim_households`, `dim_household_members`) and cached in memory. On first start after upgrading, the old single household (you plus the members you added) becomes a regular household with you as its creator, and its members keep their access.

## Data model (BigQuery, dataset `finance`)

| Table | Scope | What's in it |
|---|---|---|
| `fct_expenses_<telegram_user_id>` | one per user | One row per confirmed entry: amount, currency, date, category, merchant, the raw input, what was suggested and whether it was corrected. `kind` is `expense` (NULL in older rows), `income`, `saving` or `withdrawal`; `goal_id` links savings to a goal. Non-expense rows have `category_id = 0`. Anything that adds up spending filters `IFNULL(kind, 'expense') = 'expense'`. Partitioned by `expense_date`. Older tables get the new columns at startup. |
| `dim_savings_goals` | shared | One row per goal: `goal_id`, `user_id`, `name`, `target_amount`, `currency`, `deadline`, `closed_at`, and `deleted_at` (set by Delete my data, cleared by Restore, erased with the rest). |
| `dim_categories` | shared | The category list: `category_id`, `name`, `description` (guidance for the model), `sort_order`, `is_active`. |
| `dim_spend_variants` | shared | The dictionary: every merchant (`starbucks`, `yandex go`) and item (`coffee`, `taxi`) ever confirmed, with its category and `confirmations` / `corrections` counts. |
| `dim_users` | shared | One row per Telegram user who ever wrote to the bot: name, @username, Telegram app language, language chosen in the bot, role (`owner` = you, `household_owner`, `member`, `none`, `blocked`), first and last seen. Updated when something changes (at most hourly for `last_seen_at`); filled from the interaction log on first start. |
| `log_interactions` | shared | One row per update the bot receives: every text, voice note, command and button tap, including blocked and unsupported ones. Records who sent it, what they sent, the transcript, the outcome (`proposed`, `saved`, `saved_corrected`, `discarded`, `denied`, `error`, …), the linked `expense_id`, the AI provider and model, latency, and the error with its traceback. Partitioned by day. |
| `dim_households` | shared | One row per household: `household_id`, `name`, `created_by`, the current `invite_code`, `is_active`. |
| `dim_household_members` | shared | One row per person who has been in a household: `user_id`, `display_name`, `household_id`, `role` (owner = created it / member), `is_active`. |
| `v_expenses_all` | view | Everyone's fact tables together (`fct_expenses_*`), all kinds; family totals add up only `kind = expense` for the members of one household. |
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
   Message the bot. It replies with your Telegram user ID. Put it in `OWNER_USER_ID` and restart. Anyone else can use the bot within the daily limits; see *Who can use the bot*.
6. **Run it in the background.** It then starts at login and restarts if it crashes:
   ```bash
   bash mac/install.sh     # logs: tail -f ~/Library/Logs/expense-bot.log
   bash mac/uninstall.sh   # stop
   ```

## Cost

- **BigQuery:** $0 in practice. A few KB of data a day and tiny queries stay far inside the always-free 10 GB of storage and 1 TB of queries a month. The interaction log uses streaming inserts, which are billed at $0.01 per 200 MB. At about 1 KB per interaction, that's fractions of a cent a year. Billing must still be *enabled*, because the dictionary updates (`MERGE`) and `/undo` (`DELETE`) are DML, which the no-billing sandbox refuses. A $1 budget alert is a good safety net.
- **AI:** $0 with Gemini's free tier, or about $1–2 a month with Claude Haiku or ChatGPT for one person. A public bot costs that per active user; the daily limits cap the worst case. On Haiku a message costs roughly half a cent (the prompt carries the category list and dictionary examples), so someone using all 50 messages costs about $0.25 that day. Lower `DAILY_TEXT_LIMIT` if strangers find the bot. Local Whisper voice runs on your Mac, so many voice users means a busy Mac.
- **Hosting:** your Mac. Telegram holds messages for up to 24 hours while the Mac sleeps, and each expense is still dated by when you sent it.

## Files

| File | What it does |
|---|---|
| `bot.py` | Telegram handlers, buttons, commands |
| `catalog.py` | Shared categories + dictionary: cache, lookup, learning |
| `users.py` | `dim_users`: who's who, kept current from every interaction |
| `i18n.py` | All user-facing text in English and Russian, date formats, Telegram command menus |
| `dashboard.py`, `dashboard.html` | 🖥 App status page (local, http://127.0.0.1:8788): metrics queries and the page |
| `savings.py` | Income / savings kinds, `dim_savings_goals`, totals, goal progress |
| `household.py` | Households: create, invite links, join/leave/remove/end, `/family` totals |
| `interactions.py` | Interaction log: per-update record, background streaming to BigQuery |
| `extractor.py` | Prompt, JSON schema and validation for Gemini / Claude / OpenAI |
| `transcribe.py` | Voice → text for Claude/ChatGPT (local Whisper or OpenAI) |
| `storage.py` | BigQuery tables (per-user facts, shared dims), local SQLite for pending taps |
| `config.py` | Settings and first-run seed data |
| `schema.sql` | DDL and example queries |
| `mac/` | launchd install/uninstall |
| `Dockerfile` | If you later move it to a server |

Notes: facts go in through BigQuery load jobs rather than streaming inserts. Load jobs are free, and a row can be deleted immediately. Amounts in different currencies are stored as given and are never converted.
