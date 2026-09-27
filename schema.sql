-- The bot creates and seeds all of this on first start. Kept here as reference.
-- Replace YOUR_PROJECT with your project id; dataset defaults to `finance`.

------------------------------------------------------------------------------
-- Shared: category list. Source of truth for every mapping.
-- Edit freely; the bot re-reads it every 10 min (or on /reload).
--   add:        INSERT (new category_id, name, description, sort_order, TRUE)
--   rename:     UPDATE name  (history keeps the old name in fct tables; ids stay stable)
--   retire:     UPDATE is_active = FALSE
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.finance.dim_categories` (
  category_id  INT64  NOT NULL,
  name         STRING NOT NULL,   -- English; used by the model, the dictionary and fact rows
  name_ru      STRING,            -- shown to Russian-speaking users (added automatically to older tables)
  description  STRING,            -- guidance shown to the model
  sort_order   INT64,
  is_active    BOOL,
  updated_at   TIMESTAMP
);

------------------------------------------------------------------------------
-- Shared: spend dictionary. Every variant ever confirmed, by any user.
-- One row per (variant, variant_type, category_id); the bot MERGEs into it on
-- every save and uses the highest-confirmed category on every mapping.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.finance.dim_spend_variants` (
  variant        STRING NOT NULL,   -- normalised: 'starbucks', 'yandex go', 'coffee'
  variant_type   STRING NOT NULL,   -- merchant | item
  category_id    INT64  NOT NULL,
  confirmations  INT64,             -- times users saved it in this category
  corrections    INT64,             -- ...of which the user overrode the suggestion
  source         STRING,            -- seed | learned
  first_seen_at  TIMESTAMP,
  last_seen_at   TIMESTAMP
)
CLUSTER BY variant, variant_type;

------------------------------------------------------------------------------
-- Per user: one fact table per Telegram user id, e.g. fct_expenses_123456789.
-- One row per confirmed expense.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.finance.fct_expenses_123456789` (
  expense_id         STRING    NOT NULL,
  user_id            INT64     NOT NULL,
  expense_date       DATE      NOT NULL,  -- local date (Asia/Almaty), day the message was sent
  amount             NUMERIC   NOT NULL,
  currency           STRING    NOT NULL,  -- ISO 4217
  category_id        INT64     NOT NULL,  -- -> dim_categories
  category           STRING    NOT NULL,  -- name at time of saving
  description        STRING,              -- 'Coffee', 'Taxi'
  merchant           STRING,              -- 'Starbucks'
  suggested_category STRING,              -- what the bot proposed
  suggestion_source  STRING,              -- dictionary | ai
  was_corrected      BOOL,                -- user picked something else
  source             STRING,              -- text | voice
  raw_input          STRING,              -- original text / voice transcript
  item_label         STRING,              -- item as shown to the user, in their language (added to older tables automatically)
  confirmed_by       STRING,              -- user (tapped) | auto (saved after AUTO_SAVE_MINUTES with no answer); added automatically
  created_at         TIMESTAMP NOT NULL
)
PARTITION BY expense_date
CLUSTER BY category;

------------------------------------------------------------------------------
-- Shared: interaction log. One row per update the bot receives (every text,
-- voice note, command and button tap, including denied/unsupported ones).
-- Streamed in the background every few seconds.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.finance.log_interactions` (
  event_id          STRING    NOT NULL,
  event_ts          TIMESTAMP NOT NULL,  -- when the user acted
  logged_at         TIMESTAMP NOT NULL,  -- when the bot finished handling it
  update_id         INT64,
  user_id           INT64,
  username          STRING,
  chat_id           INT64,
  chat_type         STRING,              -- private | group | supergroup
  message_id        INT64,
  event_type        STRING,              -- text | voice | command | button | edit | other
  command           STRING,              -- /today, /undo, ...
  button_action     STRING,              -- ok | ed | set | no | bk
  input_text        STRING,              -- text / command / button data
  voice_duration_s  INT64,
  transcript        STRING,
  outcome           STRING,              -- see below
  expenses_found    INT64,
  pending_ids       ARRAY<STRING>,       -- links a proposal to the button taps on it
  expense_id        STRING,              -- -> fct_expenses_<user_id>.expense_id
  category          STRING,
  suggestion_source STRING,              -- dictionary | ai
  llm_provider      STRING,
  llm_model         STRING,
  latency_ms        INT64,
  error             STRING,
  details           JSON                 -- proposals, traceback, report period, ...
)
PARTITION BY DATE(event_ts)
CLUSTER BY user_id, event_type, outcome;
-- outcome values:
--   text/voice : proposed | no_expense | parse_error | daily_limit | voice_too_long | blocked
--   button     : saved | saved_corrected | discarded | category_menu | back
--                | already_handled | not_owner | category_gone | save_error | blocked
--   command    : help | listed | report | report_error | reloaded | reload_error
--                | undone | nothing_to_undo | undo_error | unknown_command | blocked
--                | household_status | household_none | family_report | not_owner | bad_args
--                | user_blocked | user_unblocked | not_blocked
--   /start join_<code>: join_prompt | invite_invalid | already_member | in_other_household
--   household buttons: household_created | invite_link_reset | member_removed | household_end_prompt
--                | household_ended | household_leave_prompt | household_left | household_joined
--                | join_declined | not_creator
--   shortcut   : shortcut_key_created | shortcut_shown | shortcut_off | shortcut_key_rotated (button)
--   auto_save  : auto_saved (no answer within AUTO_SAVE_MINUTES) | error (retried next pass)
--   other      : unsupported | blocked
--   edit       : edit_ignored | blocked   (edited messages are never re-processed)
--   any        : error (handler crashed; see error + details.traceback)

------------------------------------------------------------------------------
-- Shared: users. One row per Telegram user who ever wrote to the bot.
-- Written with MERGE when something about the person changes, or at most hourly
-- to move last_seen_at; backfilled from log_interactions on startup.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.finance.dim_users` (
  user_id            INT64 NOT NULL,   -- Telegram user id (= fct_expenses_<user_id>)
  first_name         STRING,
  last_name          STRING,
  username           STRING,           -- @handle without the @
  telegram_language  STRING,           -- Telegram app language code
  bot_language       STRING,           -- chosen in the bot: en | ru
  is_premium         BOOL,
  role               STRING,           -- owner | member | none
  first_seen_at      TIMESTAMP,
  last_seen_at       TIMESTAMP,        -- up to ~1 h behind; exact times are in log_interactions
  updated_at         TIMESTAMP
)
CLUSTER BY user_id;

-- Everyone with their spending this month:
-- SELECT u.first_name, u.username, u.role, SUM(e.amount) AS spent
-- FROM `YOUR_PROJECT.finance.dim_users` u
-- LEFT JOIN `YOUR_PROJECT.finance.v_expenses_all` e
--   ON e.user_id = u.user_id AND e.expense_date >= DATE_TRUNC(CURRENT_DATE('Asia/Almaty'), MONTH)
-- GROUP BY 1, 2, 3 ORDER BY spent DESC;

------------------------------------------------------------------------------
-- Households: anyone can create one (🏠 Household) and invite people with a link.
-- One household per person at a time. Leaving/ending never deletes expenses.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.finance.dim_households` (
  household_id  STRING NOT NULL,   -- h_<random>
  name          STRING,            -- "Ann's household"
  created_by    INT64,             -- Telegram user id of the creator
  invite_code   STRING,            -- t.me/<bot>?start=join_<invite_code>; replaced by "New link", NULL once ended
  is_active     BOOL,
  created_at    TIMESTAMP,
  updated_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.finance.dim_household_members` (
  user_id       INT64 NOT NULL,    -- one row per person: their current (or last) household
  display_name  STRING,
  role          STRING,            -- owner (created the household) | member
  is_active     BOOL,
  added_by      INT64,
  added_at      TIMESTAMP,         -- when they last joined
  updated_at    TIMESTAMP,
  household_id  STRING             -- -> dim_households (added automatically to older tables)
);

-- A household's totals this month, per person:
-- SELECT m.display_name, SUM(e.amount) AS spent
-- FROM `YOUR_PROJECT.finance.dim_household_members` m
-- JOIN `YOUR_PROJECT.finance.v_expenses_all` e ON e.user_id = m.user_id
-- WHERE m.household_id = 'h_...' AND m.is_active
--   AND e.expense_date >= DATE_TRUNC(CURRENT_DATE('Asia/Almaty'), MONTH)
-- GROUP BY 1 ORDER BY spent DESC;

-- v_expenses_all = SELECT * FROM `finance.fct_expenses_*`   (created with the first household; _TABLE_SUFFIX = user id)

-- Views the bot always creates:
--   v_spend_variants  = dictionary joined with category names

------------------------------------------------------------------------------
-- Useful queries
------------------------------------------------------------------------------
-- (Household mode) Everyone's spending this month by category, current category names:
-- SELECT c.name, e.currency, SUM(e.amount) AS total
-- FROM `YOUR_PROJECT.finance.v_expenses_all` e
-- JOIN `YOUR_PROJECT.finance.dim_categories` c USING (category_id)
-- WHERE e.expense_date >= DATE_TRUNC(CURRENT_DATE('Asia/Almaty'), MONTH)
-- GROUP BY 1, 2 ORDER BY total DESC;

-- How often is each suggestion source right?
-- SELECT suggestion_source, COUNTIF(was_corrected) AS fixed, COUNT(*) AS n
-- FROM `YOUR_PROJECT.finance.v_expenses_all` GROUP BY 1;

-- Variants users disagree on (same variant, several categories):
-- SELECT variant, variant_type, ARRAY_AGG(STRUCT(category, confirmations) ORDER BY confirmations DESC)
-- FROM `YOUR_PROJECT.finance.v_spend_variants`
-- GROUP BY 1, 2 HAVING COUNT(*) > 1;

-- Funnel: of the expenses proposed, how many were saved as-is, corrected, discarded?
-- SELECT
--   COUNTIF(outcome = 'saved')           AS saved_as_suggested,
--   COUNTIF(outcome = 'saved_corrected') AS corrected,
--   COUNTIF(outcome = 'discarded')       AS discarded
-- FROM `YOUR_PROJECT.finance.log_interactions`
-- WHERE event_type = 'button' AND DATE(event_ts) >= CURRENT_DATE() - 30;

-- Errors and slow responses in the last 7 days:
-- SELECT event_ts, user_id, event_type, outcome, latency_ms, error, input_text
-- FROM `YOUR_PROJECT.finance.log_interactions`
-- WHERE DATE(event_ts) >= CURRENT_DATE() - 7
--   AND (error IS NOT NULL OR latency_ms > 10000)
-- ORDER BY event_ts DESC;

-- Full story of one proposal (message -> taps):
-- SELECT event_ts, event_type, outcome, input_text, category
-- FROM `YOUR_PROJECT.finance.log_interactions`
-- WHERE 'PENDING_ID' IN UNNEST(pending_ids) ORDER BY event_ts;
