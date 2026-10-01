"""Storage.

Local SQLite   – expenses waiting for the user's tap, and what /undo can remove.
BigQuery       – everything durable:
    fct_expenses_<telegram_user_id>   one fact table per user, one row per confirmed expense
    dim_categories                    shared category list (edit it in BigQuery; the bot picks it up)
    dim_spend_variants                shared dictionary: spend variant (merchant / item) -> category,
                                      with how often users confirmed it. Grows with every save.
    log_interactions                  every update the bot receives: who, what, outcome, latency, errors
                                      (written by interactions.py)
    v_spend_variants                  dictionary joined with category names, for browsing
  Household mode only (see household.py):
    dim_household_members, v_expenses_all
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

import config

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Pending (awaiting the user's tap)                                           #
# --------------------------------------------------------------------------- #


class PendingStore:
    def __init__(self, path: str = config.PENDING_DB_PATH):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS pending (
                   id TEXT PRIMARY KEY,
                   user_id INTEGER NOT NULL,
                   payload TEXT NOT NULL,
                   created_at TEXT NOT NULL)"""
        )
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(pending)")}
        for col, decl in (("chat_id", "INTEGER"), ("message_id", "INTEGER"), ("touched_at", "TEXT")):
            if col not in cols:  # pending.sqlite3 from an older version
                self.db.execute(f"ALTER TABLE pending ADD COLUMN {col} {decl}")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS log_purges (
                   user_id INTEGER NOT NULL,
                   cutoff TEXT NOT NULL,          -- delete this user's log rows up to here
                   requested_at TEXT NOT NULL,
                   done INTEGER NOT NULL DEFAULT 0,
                   PRIMARY KEY (user_id, cutoff))"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS report_prefs (
                   user_id INTEGER PRIMARY KEY,
                   report_mode TEXT NOT NULL,
                   updated_at TEXT NOT NULL)"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS user_settings (
                   user_id INTEGER PRIMARY KEY,
                   language TEXT NOT NULL,
                   updated_at TEXT NOT NULL)"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS saved (
                   expense_id TEXT PRIMARY KEY,
                   user_id INTEGER NOT NULL,
                   chat_id INTEGER NOT NULL,
                   message_id INTEGER NOT NULL,
                   summary TEXT NOT NULL,
                   saved_at TEXT NOT NULL,
                   learned TEXT)"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS upload_keys (
                   user_id INTEGER PRIMARY KEY,
                   key_hash TEXT NOT NULL UNIQUE,  -- sha256 of the key; the key itself is only shown once
                   created_at TEXT NOT NULL)"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS usage (
                   user_id INTEGER NOT NULL,
                   day TEXT NOT NULL,              -- local date (TIMEZONE)
                   kind TEXT NOT NULL,             -- text | voice
                   n INTEGER NOT NULL,
                   PRIMARY KEY (user_id, day, kind))"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS voice_notes (
                   gid TEXT PRIMARY KEY,           -- links the transcript to its proposals (payload "group")
                   user_id INTEGER NOT NULL,
                   chat_id INTEGER,
                   transcript_message_id INTEGER,  -- the 🎙 message (replying to it = a correction)
                   prompt_message_id INTEGER,      -- the "send the corrected text" message
                   transcript TEXT NOT NULL,       -- current text (the last correction, if any)
                   source TEXT NOT NULL,           -- voice | upload_audio
                   sent_at TEXT NOT NULL,          -- when the recording was sent: dates stay relative to it
                   proposed INTEGER NOT NULL DEFAULT 0,
                   saved INTEGER NOT NULL DEFAULT 0,
                   fixes INTEGER NOT NULL DEFAULT 0,
                   created_at TEXT NOT NULL)"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS report_rows (
                   report_id TEXT NOT NULL,        -- one detailed report as sent
                   user_id INTEGER NOT NULL,
                   idx INTEGER NOT NULL,           -- the line number shown in the report
                   expense_id TEXT NOT NULL,
                   summary TEXT NOT NULL,          -- plain text of the line, for the confirmation
                   deleted INTEGER NOT NULL DEFAULT 0,
                   created_at TEXT NOT NULL,
                   PRIMARY KEY (report_id, idx))"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS report_messages (
                   report_id TEXT NOT NULL,
                   user_id INTEGER NOT NULL,
                   chat_id INTEGER NOT NULL,
                   message_id INTEGER NOT NULL,    -- the report itself or its "which line?" prompt
                   created_at TEXT NOT NULL,
                   PRIMARY KEY (chat_id, message_id))"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS outages (
                   started_at TEXT NOT NULL,       -- first failed attempt to reach Telegram (UTC)
                   ended_at TEXT)                  -- first update received afterwards; NULL = still down"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS deletions (
                   user_id INTEGER NOT NULL,
                   deleted_at TEXT NOT NULL,       -- when they confirmed "delete my data" (UTC)
                   purge_after TEXT NOT NULL,      -- hard delete from this moment on
                   log_cutoff TEXT NOT NULL,       -- log rows up to here belong to the deleted data
                   archive_table TEXT,             -- deleted_fct_expenses_<id>_<ts>, NULL if they had none
                   snapshot TEXT,                  -- settings to give back on restore (JSON)
                   status TEXT NOT NULL DEFAULT 'soft',   -- soft | restored | purged
                   PRIMARY KEY (user_id, deleted_at))"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS blocked_users (
                   user_id INTEGER PRIMARY KEY,
                   blocked_at TEXT NOT NULL)"""
        )
        self.db.commit()

    # ---- voice transcripts that can be corrected ----------------------------- #

    VOICE_NOTE_DAYS = 7  # a transcript can be corrected for a week

    def add_voice_note(self, gid: str, user_id: int, transcript: str, source: str, sent_at: str):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.VOICE_NOTE_DAYS)).isoformat()
        self.db.execute("DELETE FROM voice_notes WHERE created_at < ?", (cutoff,))
        self.db.execute(
            "INSERT INTO voice_notes (gid, user_id, transcript, source, sent_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (gid, user_id, transcript, source, sent_at, _now()),
        )
        self.db.commit()

    def set_voice_note_message(self, gid: str, chat_id: int, message_id: int):
        self.db.execute("UPDATE voice_notes SET chat_id = ?, transcript_message_id = ? WHERE gid = ?",
                        (chat_id, message_id, gid))
        self.db.commit()

    def set_voice_note_prompt(self, gid: str, message_id: int):
        self.db.execute("UPDATE voice_notes SET prompt_message_id = ? WHERE gid = ?", (message_id, gid))
        self.db.commit()

    _VN_COLS = ("gid", "user_id", "chat_id", "transcript_message_id", "prompt_message_id", "transcript",
                "source", "sent_at", "proposed", "saved", "fixes", "created_at")

    def voice_note(self, gid: str) -> dict | None:
        row = self.db.execute(f"SELECT {', '.join(self._VN_COLS)} FROM voice_notes WHERE gid = ?", (gid,)).fetchone()
        return dict(zip(self._VN_COLS, row)) if row else None

    def voice_note_for_reply(self, user_id: int, chat_id: int, message_id: int) -> dict | None:
        """The transcript a reply is correcting: a reply to the 🎙 message or to the fix prompt."""
        row = self.db.execute(
            f"SELECT {', '.join(self._VN_COLS)} FROM voice_notes WHERE user_id = ? AND chat_id = ? "
            f"AND (transcript_message_id = ? OR prompt_message_id = ?)",
            (user_id, chat_id, message_id, message_id),
        ).fetchone()
        return dict(zip(self._VN_COLS, row)) if row else None

    def voice_note_counts(self, gid: str, proposed: int = 0, saved: int = 0):
        self.db.execute("UPDATE voice_notes SET proposed = proposed + ?, saved = saved + ? WHERE gid = ?",
                        (proposed, saved, gid))
        self.db.commit()

    def voice_note_fixed(self, gid: str, transcript: str):
        self.db.execute("UPDATE voice_notes SET transcript = ?, fixes = fixes + 1 WHERE gid = ?", (transcript, gid))
        self.db.commit()

    def pending_in_group(self, user_id: int, gid: str) -> list[tuple[str, int | None, int | None]]:
        """Unanswered proposals that came from this transcript: (pid, chat_id, message_id)."""
        rows = self.db.execute(
            "SELECT id, chat_id, message_id, payload FROM pending WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [(pid, c, m) for pid, c, m, p in rows if json.loads(p).get("group") == gid]

    # ---- detailed reports: line number -> expense ------------------------------ #

    REPORT_DAYS = 7  # lines of a report can be deleted by number for a week

    def save_report(self, report_id: str, user_id: int, rows: list[tuple[int, str, str]]):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.REPORT_DAYS)).isoformat()
        self.db.execute("DELETE FROM report_rows WHERE created_at < ?", (cutoff,))
        self.db.execute("DELETE FROM report_messages WHERE created_at < ?", (cutoff,))
        now = _now()
        self.db.executemany(
            "INSERT INTO report_rows (report_id, user_id, idx, expense_id, summary, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(report_id, user_id, idx, eid, summary, now) for idx, eid, summary in rows],
        )
        self.db.commit()

    def add_report_message(self, report_id: str, user_id: int, chat_id: int, message_id: int):
        self.db.execute("INSERT OR REPLACE INTO report_messages VALUES (?, ?, ?, ?, ?)",
                        (report_id, user_id, chat_id, message_id, _now()))
        self.db.commit()

    def report_for_message(self, user_id: int, chat_id: int, message_id: int) -> str | None:
        row = self.db.execute(
            "SELECT report_id FROM report_messages WHERE user_id = ? AND chat_id = ? AND message_id = ?",
            (user_id, chat_id, message_id),
        ).fetchone()
        return row[0] if row else None

    def report_rows(self, report_id: str, user_id: int) -> dict[int, dict]:
        rows = self.db.execute(
            "SELECT idx, expense_id, summary, deleted FROM report_rows WHERE report_id = ? AND user_id = ?",
            (report_id, user_id),
        ).fetchall()
        return {i: {"idx": i, "expense_id": e, "summary": s_, "deleted": bool(d)} for i, e, s_, d in rows}

    def mark_expense_deleted(self, expense_id: str):
        """In every open report, so a line deleted from one report shows as deleted in the others."""
        self.db.execute("UPDATE report_rows SET deleted = 1 WHERE expense_id = ?", (expense_id,))
        self.db.commit()

    def saved_entry(self, expense_id: str) -> tuple[int, int, str, dict | None] | None:
        """(chat_id, message_id, summary, learned) of a save, if its undo history is still here."""
        row = self.db.execute(
            "SELECT chat_id, message_id, summary, learned FROM saved WHERE expense_id = ?", (expense_id,)
        ).fetchone()
        return (*row[:3], json.loads(row[3]) if row[3] else None) if row else None

    # ---- Telegram outages (for 🖥 App status) ----------------------------------- #

    def outage_started(self, at: str):
        self.db.execute("INSERT INTO outages VALUES (?, NULL)", (at,))
        self.db.commit()

    def outage_ended(self, at: str):
        self.db.execute("UPDATE outages SET ended_at = ? WHERE ended_at IS NULL", (at,))
        self.db.commit()

    def outages_since(self, since: str) -> list[tuple[str, str | None]]:
        return self.db.execute(
            "SELECT started_at, ended_at FROM outages WHERE started_at >= ? OR ended_at IS NULL OR ended_at >= ? "
            "ORDER BY started_at", (since, since)
        ).fetchall()

    # ---- daily limits and blocking -------------------------------------------- #

    def count_use(self, user_id: int, kind: str, day: str) -> int:
        """Add one use of `kind` today and return today's total, including this one."""
        self.db.execute(
            "INSERT INTO usage VALUES (?, ?, ?, 1) "
            "ON CONFLICT(user_id, day, kind) DO UPDATE SET n = n + 1",
            (user_id, day, kind),
        )
        self.db.execute("DELETE FROM usage WHERE day < date(?, '-7 days')", (day,))  # keep it small
        self.db.commit()
        return self.db.execute(
            "SELECT n FROM usage WHERE user_id = ? AND day = ? AND kind = ?", (user_id, day, kind)
        ).fetchone()[0]

    def uncount_use(self, user_id: int, kind: str, day: str):
        """Give a use back (the message never reached the AI)."""
        self.db.execute(
            "UPDATE usage SET n = MAX(n - 1, 0) WHERE user_id = ? AND day = ? AND kind = ?", (user_id, day, kind)
        )
        self.db.commit()

    def is_blocked(self, user_id: int | None) -> bool:
        return user_id is not None and self.db.execute(
            "SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)
        ).fetchone() is not None

    def block(self, user_id: int):
        self.db.execute("INSERT OR REPLACE INTO blocked_users VALUES (?, ?)", (user_id, _now()))
        self.db.commit()

    def unblock(self, user_id: int) -> bool:
        cur = self.db.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))
        self.db.commit()
        return cur.rowcount > 0

    def blocked_ids(self) -> set[int]:
        return {r[0] for r in self.db.execute("SELECT user_id FROM blocked_users")}

    # ---- personal upload keys (iPhone Action Button / Shortcut) -------------- #

    @staticmethod
    def _hash_key(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def new_upload_key(self, user_id: int) -> str:
        """Create (or replace) this person's upload key. The old one stops working at once."""
        key = "exp_" + secrets.token_urlsafe(24)
        self.db.execute(
            "INSERT OR REPLACE INTO upload_keys VALUES (?, ?, ?)", (user_id, self._hash_key(key), _now())
        )
        self.db.commit()
        return key

    def upload_key_created(self, user_id: int) -> str | None:
        row = self.db.execute("SELECT created_at FROM upload_keys WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None

    def user_for_upload_key(self, key: str) -> int | None:
        if not key:
            return None
        row = self.db.execute(
            "SELECT user_id FROM upload_keys WHERE key_hash = ?", (self._hash_key(key),)
        ).fetchone()
        return row[0] if row else None

    def add(self, user_id: int, payload: dict) -> str:
        pid = secrets.token_hex(4)
        payload = {**payload, "user_id": user_id}
        now = _now()
        self.db.execute(
            "INSERT INTO pending (id, user_id, payload, created_at, touched_at) VALUES (?, ?, ?, ?, ?)",
            (pid, user_id, json.dumps(payload, ensure_ascii=False), now, now),
        )
        self.db.commit()
        return pid

    def update(self, pid: str, **changes) -> dict | None:
        """Change fields of a waiting proposal (e.g. ✏️ switched it from expense to income)."""
        item = self.get(pid)
        if item is None:
            return None
        item.update(changes)
        self.db.execute("UPDATE pending SET payload = ? WHERE id = ?", (json.dumps(item, ensure_ascii=False), pid))
        self.db.commit()
        return item

    def set_message(self, pid: str, chat_id: int, message_id: int):
        """Where the proposal was shown, so it can be updated when it auto-saves."""
        self.db.execute("UPDATE pending SET chat_id = ?, message_id = ? WHERE id = ?", (chat_id, message_id, pid))
        self.db.commit()

    def touch(self, pid: str):
        """The person is looking at it (e.g. opened ✏️ Change): restart the auto-save clock."""
        self.db.execute("UPDATE pending SET touched_at = ? WHERE id = ?", (_now(), pid))
        self.db.commit()

    def overdue(self, minutes: float) -> list[tuple[str, int, int]]:
        """Proposals untouched for `minutes`. Only ones whose message we know (so older proposals,
        made before auto-save existed, are never saved by surprise)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        return self.db.execute(
            "SELECT id, chat_id, message_id FROM pending "
            "WHERE message_id IS NOT NULL AND touched_at IS NOT NULL AND touched_at <= ?",
            (cutoff,),
        ).fetchall()

    def get(self, pid: str) -> dict | None:
        row = self.db.execute("SELECT payload FROM pending WHERE id = ?", (pid,)).fetchone()
        return json.loads(row[0]) if row else None

    def pop(self, pid: str) -> dict | None:
        item = self.get(pid)
        self.db.execute("DELETE FROM pending WHERE id = ?", (pid,))
        self.db.commit()
        return item

    def remember_saved(
        self, expense_id: str, user_id: int, chat_id: int, message_id: int, summary: str, learned: dict | None = None
    ):
        self.db.execute(
            "INSERT INTO saved VALUES (?, ?, ?, ?, ?, ?, ?)",
            (expense_id, user_id, chat_id, message_id, summary, _now(), json.dumps(learned) if learned else None),
        )
        self.db.commit()

    def last_saved(self, user_id: int) -> tuple[str, int, int, str, dict | None] | None:
        row = self.db.execute(
            "SELECT expense_id, chat_id, message_id, summary, learned FROM saved "
            "WHERE user_id = ? ORDER BY saved_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return (*row[:4], json.loads(row[4]) if row[4] else None)

    def get_report_mode(self, user_id: int) -> str:
        """'summary' (totals by category, the default) or 'detailed' (every expense)."""
        row = self.db.execute("SELECT report_mode FROM report_prefs WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else "summary"

    def set_report_mode(self, user_id: int, mode: str):
        self.db.execute("INSERT OR REPLACE INTO report_prefs VALUES (?, ?, ?)", (user_id, mode, _now()))
        self.db.commit()

    def get_language(self, user_id: int) -> str | None:
        row = self.db.execute("SELECT language FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None

    def set_language(self, user_id: int, language: str):
        self.db.execute(
            "INSERT OR REPLACE INTO user_settings VALUES (?, ?, ?)", (user_id, language, _now())
        )
        self.db.commit()

    def all_languages(self) -> list[tuple[int, str]]:
        return self.db.execute("SELECT user_id, language FROM user_settings").fetchall()

    def forget_saved(self, expense_id: str):
        self.db.execute("DELETE FROM saved WHERE expense_id = ?", (expense_id,))
        self.db.commit()

    # ---- "delete my data" ---------------------------------------------------- #

    def forget_user(self, user_id: int):
        """Everything this machine keeps about the person: proposals, undo history, settings."""
        # Kept on purpose: a block and today's usage, so deleting your data isn't a way around them.
        for table in ("pending", "saved", "report_prefs", "user_settings", "upload_keys", "voice_notes",
                      "report_rows", "report_messages"):
            self.db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        self.db.commit()

    def add_deletion(self, user_id: int, deleted_at: str, purge_after: str, log_cutoff: str,
                     archive_table: str | None, snapshot: dict):
        self.db.execute(
            "INSERT INTO deletions (user_id, deleted_at, purge_after, log_cutoff, archive_table, snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, deleted_at, purge_after, log_cutoff, archive_table, json.dumps(snapshot)),
        )
        self.db.commit()

    def active_deletion(self, user_id: int) -> dict | None:
        """Their latest soft deletion that can still be restored."""
        row = self.db.execute(
            "SELECT user_id, deleted_at, purge_after, log_cutoff, archive_table, snapshot FROM deletions "
            "WHERE user_id = ? AND status = 'soft' ORDER BY deleted_at DESC LIMIT 1", (user_id,)
        ).fetchone()
        return self._deletion(row) if row else None

    def due_deletions(self, now: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT user_id, deleted_at, purge_after, log_cutoff, archive_table, snapshot FROM deletions "
            "WHERE status = 'soft' AND purge_after <= ? ORDER BY purge_after", (now,)
        ).fetchall()
        return [self._deletion(r) for r in rows]

    def soft_deletions(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT user_id, deleted_at, purge_after, log_cutoff, archive_table, snapshot FROM deletions "
            "WHERE status = 'soft' ORDER BY purge_after"
        ).fetchall()
        return [self._deletion(r) for r in rows]

    def finish_deletion(self, user_id: int, deleted_at: str, status: str):
        """restored, or purged (then the snapshot goes too: nothing of theirs is kept)."""
        self.db.execute(
            "UPDATE deletions SET status = ?, snapshot = CASE WHEN ? = 'purged' THEN NULL ELSE snapshot END "
            "WHERE user_id = ? AND deleted_at = ?", (status, status, user_id, deleted_at)
        )
        self.db.commit()

    @staticmethod
    def _deletion(row) -> dict:
        keys = ("user_id", "deleted_at", "purge_after", "log_cutoff", "archive_table", "snapshot")
        d = dict(zip(keys, row))
        d["snapshot"] = json.loads(d["snapshot"]) if d["snapshot"] else {}
        return d

    def add_log_purge(self, user_id: int, cutoff: str):
        self.db.execute(
            "INSERT OR REPLACE INTO log_purges (user_id, cutoff, requested_at, done) VALUES (?, ?, ?, 0)",
            (user_id, cutoff, _now()),
        )
        self.db.commit()

    def open_log_purges(self) -> list[tuple[int, str]]:
        return self.db.execute("SELECT user_id, cutoff FROM log_purges WHERE done = 0").fetchall()

    def finish_log_purge(self, user_id: int, cutoff: str):
        self.db.execute("UPDATE log_purges SET done = 1 WHERE user_id = ? AND cutoff = ?", (user_id, cutoff))
        self.db.commit()


# --------------------------------------------------------------------------- #
# BigQuery schemas                                                            #
# --------------------------------------------------------------------------- #

FACT_SCHEMA = [
    bigquery.SchemaField("expense_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("user_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("expense_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("amount", "NUMERIC", mode="REQUIRED"),
    bigquery.SchemaField("currency", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("category_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("category", "STRING", mode="REQUIRED"),  # name at time of saving
    bigquery.SchemaField("description", "STRING"),
    bigquery.SchemaField("merchant", "STRING"),
    bigquery.SchemaField("suggested_category", "STRING"),
    bigquery.SchemaField("suggestion_source", "STRING"),  # dictionary | ai
    bigquery.SchemaField("was_corrected", "BOOL"),
    bigquery.SchemaField("source", "STRING"),  # text | voice
    bigquery.SchemaField("raw_input", "STRING"),
    bigquery.SchemaField("item_label", "STRING"),  # item name in the user's language at saving time
    bigquery.SchemaField("confirmed_by", "STRING"),  # user (tapped) | auto (no answer in AUTO_SAVE_MINUTES)
    # expense | income | saving (put aside) | withdrawal (taken out of savings). NULL = expense (older rows).
    bigquery.SchemaField("kind", "STRING"),
    bigquery.SchemaField("goal_id", "STRING"),  # dim_savings_goals.goal_id for savings/withdrawals towards a goal
    bigquery.SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
]

CATEGORY_SCHEMA = [
    bigquery.SchemaField("category_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("name", "STRING", mode="REQUIRED"),  # English; what the model and fact rows use
    bigquery.SchemaField("name_ru", "STRING"),  # shown to Russian-speaking users
    bigquery.SchemaField("description", "STRING"),  # guidance for the model
    bigquery.SchemaField("sort_order", "INT64"),
    bigquery.SchemaField("is_active", "BOOL"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
]

VARIANT_SCHEMA = [
    bigquery.SchemaField("variant", "STRING", mode="REQUIRED"),  # normalised, e.g. "starbucks"
    bigquery.SchemaField("variant_type", "STRING", mode="REQUIRED"),  # merchant | item
    bigquery.SchemaField("category_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("confirmations", "INT64"),  # times users saved this variant in this category
    bigquery.SchemaField("corrections", "INT64"),  # ...of which the user had overridden the suggestion
    bigquery.SchemaField("source", "STRING"),  # seed | learned
    bigquery.SchemaField("first_seen_at", "TIMESTAMP"),
    bigquery.SchemaField("last_seen_at", "TIMESTAMP"),
]


class Warehouse:
    def __init__(self):
        self.client = bigquery.Client(project=config.GCP_PROJECT, location=config.BQ_LOCATION)
        self.ds = f"{config.GCP_PROJECT}.{config.BQ_DATASET}"
        self.categories_table = f"{self.ds}.dim_categories"
        self.variants_table = f"{self.ds}.dim_spend_variants"
        self._fact_tables_ready: set[int] = set()

    def fact_table(self, user_id: int) -> str:
        return f"{self.ds}.{config.FACT_TABLE_PREFIX}{int(user_id)}"

    # ---- setup ------------------------------------------------------------ #

    def ensure_shared(self):
        """Create dataset, shared tables and views if missing; seed them on first run."""
        # Check first, so a service account with access to this dataset only
        # (no project-wide bigquery.datasets.create) still starts fine.
        try:
            self.client.get_dataset(self.ds)
        except NotFound:
            ds = bigquery.Dataset(self.ds)
            ds.location = config.BQ_LOCATION
            self.client.create_dataset(ds, exists_ok=True)
        self.client.create_table(bigquery.Table(self.categories_table, schema=CATEGORY_SCHEMA), exists_ok=True)
        vt = bigquery.Table(self.variants_table, schema=VARIANT_SCHEMA)
        vt.clustering_fields = ["variant", "variant_type"]
        self.client.create_table(vt, exists_ok=True)

        if self._count(self.categories_table) == 0:
            self._seed()
        else:
            self._add_new_seed_categories()
        self._migrate_category_languages()

        views = {
            "v_spend_variants": f"""
                SELECT v.variant, v.variant_type, c.name AS category, v.category_id,
                       v.confirmations, v.corrections, v.source, v.first_seen_at, v.last_seen_at
                FROM `{self.variants_table}` v
                JOIN `{self.categories_table}` c USING (category_id)""",
        }
        for name, sql in views.items():
            view = bigquery.Table(f"{self.ds}.{name}")
            view.view_query = sql
            self.client.create_table(view, exists_ok=True)

    def _ensure_fact_table(self, user_id: int):
        if user_id in self._fact_tables_ready:
            return
        table = bigquery.Table(self.fact_table(user_id), schema=FACT_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(field="expense_date")
        table.clustering_fields = ["category"]
        self.client.create_table(table, exists_ok=True)
        self._add_missing_fact_columns(user_id)
        self._fact_tables_ready.add(user_id)

    def _add_missing_fact_columns(self, user_id: int):
        """Fact tables created by an older version get columns added since (e.g. item_label)."""
        table = self.client.get_table(self.fact_table(user_id))
        have = {f.name for f in table.schema}
        missing = [f for f in FACT_SCHEMA if f.name not in have]
        if missing:
            table.schema = [*table.schema, *[bigquery.SchemaField(f.name, f.field_type) for f in missing]]
            self.client.update_table(table, ["schema"])
            log.info("%s: added columns %s", table.table_id, [f.name for f in missing])
        return bool(missing)

    def migrate_fact_tables(self) -> int:
        """Give every existing fct_expenses_* table the columns added since it was created. Needed at
        startup: a wildcard query (family totals) uses the newest table's schema, and a query naming
        `kind` must find it in every table. Metadata only, so free. Returns how many tables changed."""
        changed = 0
        for t in self.client.list_tables(self.ds):
            name = t.table_id
            if not name.startswith(config.FACT_TABLE_PREFIX) or not name[len(config.FACT_TABLE_PREFIX):].isdigit():
                continue
            uid = int(name[len(config.FACT_TABLE_PREFIX):])
            if self._add_missing_fact_columns(uid):
                changed += 1
            self._fact_tables_ready.add(uid)
        return changed

    def _count(self, table: str) -> int:
        return next(iter(self.client.query(f"SELECT COUNT(*) AS n FROM `{table}`").result())).n

    def _load(self, rows: list[dict], table: str, schema):
        job = self.client.load_table_from_json(
            rows,
            table,
            job_config=bigquery.LoadJobConfig(
                schema=schema, write_disposition=bigquery.WriteDisposition.WRITE_APPEND
            ),
        )
        job.result()

    def _seed(self):
        now = _now()
        cats = [
            {
                "category_id": i + 1,
                "name": name,
                "name_ru": config.CATEGORY_NAMES_RU.get(name),
                "description": config.CATEGORY_HINTS.get(name, ""),
                "sort_order": i + 1,
                "is_active": True,
                "updated_at": now,
            }
            for i, name in enumerate(config.CATEGORIES)
        ]
        self._load(cats, self.categories_table, CATEGORY_SCHEMA)
        ids = {c["name"]: c["category_id"] for c in cats}
        variants = [
            {
                "variant": normalise(v),
                "variant_type": vtype,
                "category_id": ids[cat],
                "confirmations": 0,
                "corrections": 0,
                "source": "seed",
                "first_seen_at": now,
                "last_seen_at": now,
            }
            for cat, groups in config.SEED_VARIANTS.items()
            if cat in ids
            for vtype, vs in (("merchant", groups.get("merchants", [])), ("item", groups.get("items", [])))
            for v in vs
        ]
        if variants:
            self._load(variants, self.variants_table, VARIANT_SCHEMA)
        log.info("Seeded %d categories and %d spend variants", len(cats), len(variants))

    def _add_new_seed_categories(self):
        """Categories added to config.CATEGORIES after the table was seeded (e.g. Taxes) are added once,
        with their seed dictionary entries, just before "Other". A name already in the table, even one you
        switched off (is_active = FALSE), is left alone."""
        rows = list(self.client.query(
            f"SELECT category_id, name, sort_order FROM `{self.categories_table}`"
        ).result())
        have = {r.name for r in rows}
        new = [n for n in config.CATEGORIES if n not in have]
        if not new:
            return
        now = _now()
        next_id = max((r.category_id for r in rows), default=0) + 1
        next_sort = max((r.sort_order or 0 for r in rows), default=0) + 1
        cats = [
            {
                "category_id": next_id + i,
                "name": name,
                "name_ru": config.CATEGORY_NAMES_RU.get(name),
                "description": config.CATEGORY_HINTS.get(name, ""),
                "sort_order": next_sort + i,
                "is_active": True,
                "updated_at": now,
            }
            for i, name in enumerate(new)
        ]
        self._load(cats, self.categories_table, CATEGORY_SCHEMA)
        if "Other" in have:  # keep the catch-all last
            self.client.query(
                f"""UPDATE `{self.categories_table}` SET sort_order = @s, updated_at = CURRENT_TIMESTAMP()
                    WHERE name = 'Other'""",
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ScalarQueryParameter("s", "INT64", next_sort + len(new))
                ]),
            ).result()
        ids = {c["name"]: c["category_id"] for c in cats}
        variants = [
            {
                "variant": normalise(v), "variant_type": vtype, "category_id": ids[cat], "confirmations": 0,
                "corrections": 0, "source": "seed", "first_seen_at": now, "last_seen_at": now,
            }
            for cat, groups in config.SEED_VARIANTS.items()
            if cat in ids
            for vtype, vs in (("merchant", groups.get("merchants", [])), ("item", groups.get("items", [])))
            for v in vs
        ]
        if variants:  # a merchant/item people already mapped elsewhere keeps its mapping
            taken = {(r.variant, r.variant_type) for r in self.client.query(
                f"SELECT variant, variant_type FROM `{self.variants_table}` WHERE variant IN UNNEST(@v)",
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ArrayQueryParameter("v", "STRING", sorted({x["variant"] for x in variants}))
                ]),
            ).result()}
            variants = [x for x in variants if (x["variant"], x["variant_type"]) not in taken]
        if variants:
            self._load(variants, self.variants_table, VARIANT_SCHEMA)
        log.info("Added new categories %s with %d spend variants", new, len(variants))

    def _migrate_category_languages(self):
        """Tables created before languages existed: add name_ru and fill it for the seeded categories.
        Only empty name_ru values are filled, so names you've edited in BigQuery are kept."""
        table = self.client.get_table(self.categories_table)
        if "name_ru" not in {f.name for f in table.schema}:
            table.schema = [*table.schema, bigquery.SchemaField("name_ru", "STRING")]
            self.client.update_table(table, ["schema"])
            log.info("Added name_ru to dim_categories")
        missing = next(iter(self.client.query(
            f"SELECT COUNT(*) AS n FROM `{self.categories_table}` WHERE name_ru IS NULL"
        ).result())).n
        if not missing:
            return
        params = [
            bigquery.ArrayQueryParameter("en", "STRING", list(config.CATEGORY_NAMES_RU)),
            bigquery.ArrayQueryParameter("ru", "STRING", list(config.CATEGORY_NAMES_RU.values())),
        ]
        self.client.query(
            f"""UPDATE `{self.categories_table}` c
                SET name_ru = m.ru, updated_at = CURRENT_TIMESTAMP()
                FROM (SELECT en, @ru[OFFSET(i)] AS ru FROM UNNEST(@en) AS en WITH OFFSET i) m
                WHERE c.name = m.en AND c.name_ru IS NULL""",
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        ).result()
        log.info("Filled Russian category names")

    # ---- shared dictionary -------------------------------------------------- #

    def load_categories(self) -> list[dict]:
        rows = self.client.query(
            f"""SELECT category_id, name, IFNULL(name_ru, '') AS name_ru, IFNULL(description, '') AS description
                FROM `{self.categories_table}`
                WHERE IFNULL(is_active, TRUE)
                ORDER BY sort_order, category_id"""
        ).result()
        return [dict(r.items()) for r in rows]

    def load_variants(self) -> list[dict]:
        rows = self.client.query(
            f"""SELECT variant, variant_type, category_id,
                       IFNULL(confirmations, 0) AS confirmations, source, last_seen_at
                FROM `{self.variants_table}`"""
        ).result()
        return [dict(r.items()) for r in rows]

    def learn_variants(self, variants: list[tuple[str, str]], category_id: int, corrected: bool):
        """Upsert (variant, type) -> category, bumping the confirmation counters."""
        if not variants:
            return
        sql = f"""
        MERGE `{self.variants_table}` t
        USING (
          SELECT v.variant, v.variant_type FROM UNNEST(@variants) AS v
        ) s
        ON t.variant = s.variant AND t.variant_type = s.variant_type AND t.category_id = @category_id
        WHEN MATCHED THEN UPDATE SET
          confirmations = IFNULL(t.confirmations, 0) + 1,
          corrections   = IFNULL(t.corrections, 0) + @corrected,
          last_seen_at  = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
          (variant, variant_type, category_id, confirmations, corrections, source, first_seen_at, last_seen_at)
          VALUES (s.variant, s.variant_type, @category_id, 1, @corrected, 'learned',
                  CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
        """
        params = [
            bigquery.ArrayQueryParameter(
                "variants",
                "STRUCT",
                [
                    bigquery.StructQueryParameter(
                        None,
                        bigquery.ScalarQueryParameter("variant", "STRING", v),
                        bigquery.ScalarQueryParameter("variant_type", "STRING", t),
                    )
                    for v, t in variants
                ],
            ),
            bigquery.ScalarQueryParameter("category_id", "INT64", category_id),
            bigquery.ScalarQueryParameter("corrected", "INT64", 1 if corrected else 0),
        ]
        self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()

    def unlearn_variants(self, variants: list[tuple[str, str]], category_id: int, corrected: bool):
        """Reverse one learn_variants() call (used by /undo)."""
        if not variants:
            return
        sql = f"""
        UPDATE `{self.variants_table}` t
        SET confirmations = GREATEST(IFNULL(t.confirmations, 0) - 1, 0),
            corrections   = GREATEST(IFNULL(t.corrections, 0) - @corrected, 0)
        WHERE t.category_id = @category_id
          AND EXISTS (SELECT 1 FROM UNNEST(@variants) v
                      WHERE v.variant = t.variant AND v.variant_type = t.variant_type)
        """
        params = [
            bigquery.ArrayQueryParameter(
                "variants",
                "STRUCT",
                [
                    bigquery.StructQueryParameter(
                        None,
                        bigquery.ScalarQueryParameter("variant", "STRING", v),
                        bigquery.ScalarQueryParameter("variant_type", "STRING", t),
                    )
                    for v, t in variants
                ],
            ),
            bigquery.ScalarQueryParameter("category_id", "INT64", category_id),
            bigquery.ScalarQueryParameter("corrected", "INT64", 1 if corrected else 0),
        ]
        self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()

    # ---- "delete my data" ---------------------------------------------------- #

    # ---- "delete my data": soft delete now, hard delete after the retention period ---- #

    def archive_table_name(self, user_id: int, when: datetime) -> str:
        # Outside the fct_expenses_* wildcard, so reports, family totals and v_expenses_all never see it.
        return f"{self.ds}.deleted_{config.FACT_TABLE_PREFIX}{int(user_id)}_{when:%Y%m%d%H%M%S}"

    def soft_delete_user_data(self, user_id: int, retention_days: int, when: datetime) -> tuple[int, str | None]:
        """Move the person's expense table to an archive table that BigQuery itself deletes after
        `retention_days` (+1 day of slack, so the bot's own hard delete normally comes first), and mark
        their dim_users row deleted. Returns (expenses archived, archive table or None)."""
        src = self.fact_table(user_id)
        try:
            row = next(iter(self.client.query(f"SELECT COUNT(*) AS n FROM `{src}`").result()), None)
            n = row.n if row is not None else 0
        except NotFound:
            n, dst = 0, None
        else:
            dst = self.archive_table_name(user_id, when)
            self.client.copy_table(src, dst).result()  # copy jobs are free
            table = self.client.get_table(dst)
            table.expires = when + timedelta(days=retention_days + 1)
            table.description = (f"Soft-deleted expenses of user {user_id} ({when:%Y-%m-%d %H:%M} UTC). "
                                 f"Restorable from the bot until they are erased.")
            self.client.update_table(table, ["expires", "description"])
            self.client.delete_table(src, not_found_ok=True)
        self._fact_tables_ready.discard(user_id)
        self._dml(f"UPDATE `{self.ds}.dim_users` SET deleted_at = CURRENT_TIMESTAMP() WHERE user_id = @uid",
                  uid=user_id)
        return n, dst

    def restore_user_data(self, user_id: int, archive: str | None) -> int:
        """Put the archived expenses back (next to anything logged since) and unmark dim_users."""
        n = 0
        if archive:
            try:
                old = self.client.get_table(archive)
            except NotFound:
                old = None  # already expired
            if old is not None:
                self._ensure_fact_table(user_id)
                have = {f.name for f in self.client.get_table(self.fact_table(user_id)).schema}
                cols = ", ".join(f"`{f.name}`" for f in old.schema if f.name in have)
                job = self.client.query(
                    f"INSERT INTO `{self.fact_table(user_id)}` ({cols}) SELECT {cols} FROM `{archive}`"
                )
                job.result()
                n = job.num_dml_affected_rows or 0
                self.client.delete_table(archive, not_found_ok=True)
        self._dml(f"UPDATE `{self.ds}.dim_users` SET deleted_at = NULL WHERE user_id = @uid", uid=user_id)
        return n

    def hard_delete_user_data(self, user_id: int, archive: str | None, cutoff: str) -> int:
        """End of the retention period: erase the archive, the log rows up to the deletion, and the
        dim_users row unless they came back and used the bot since (then deleted_at was cleared)."""
        if archive:
            self.client.delete_table(archive, not_found_ok=True)
        removed = self.purge_user_log(user_id, cutoff)
        self._dml(f"DELETE FROM `{self.ds}.dim_users` WHERE user_id = @uid AND deleted_at IS NOT NULL",
                  uid=user_id)
        return removed

    def _dml(self, sql: str, **params):
        types = {int: "INT64", str: "STRING"}
        try:
            self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter(k, types[type(v)], v) for k, v in params.items()
            ])).result()
        except NotFound:
            pass

    def purge_user_log(self, user_id: int, cutoff: str) -> int:
        """Delete the person's interaction-log rows up to `cutoff`. Raises while some are still
        in the streaming buffer; the caller retries later."""
        job = self.client.query(
            f"""DELETE FROM `{self.ds}.log_interactions`
                WHERE user_id = @uid AND event_ts <= @cutoff""",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("uid", "INT64", user_id),
                    bigquery.ScalarQueryParameter("cutoff", "TIMESTAMP", cutoff),
                ]
            ),
        )
        job.result()
        return job.num_dml_affected_rows or 0

    def _user_activity(self) -> list[dict]:
        """Everyone who ever wrote to the bot, most active (last 30 days, then all time) first."""
        sql = f"""
        WITH activity AS (
          SELECT
            user_id,
            ARRAY_AGG(JSON_VALUE(details, '$.user_name') IGNORE NULLS ORDER BY event_ts DESC LIMIT 1)[SAFE_OFFSET(0)] AS log_name,
            ARRAY_AGG(username IGNORE NULLS ORDER BY event_ts DESC LIMIT 1)[SAFE_OFFSET(0)] AS log_username,
            COUNT(*) AS actions,
            COUNTIF(event_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)) AS actions_30d,
            COUNTIF(outcome IN ('saved', 'saved_corrected')) AS saved,
            MAX(event_ts) AS last_seen
          FROM `{self.ds}.log_interactions`
          WHERE user_id IS NOT NULL
          GROUP BY user_id
        )
        SELECT
          a.user_id,
          COALESCE(NULLIF(TRIM(CONCAT(IFNULL(u.first_name, ''), ' ', IFNULL(u.last_name, ''))), ''), a.log_name) AS name,
          COALESCE(u.username, a.log_username) AS username,
          a.actions, a.actions_30d, a.saved, a.last_seen
        FROM activity a
        LEFT JOIN `{self.ds}.dim_users` u USING (user_id)
        WHERE u.deleted_at IS NULL  -- people who deleted their data don't show while it waits to be erased
        ORDER BY a.actions_30d DESC, a.actions DESC, a.last_seen DESC
        """
        try:
            return [dict(r.items()) for r in self.client.query(sql).result()]
        except NotFound:
            return []

    async def user_activity(self) -> list[dict]:
        return await asyncio.to_thread(self._user_activity)

    # ---- per-user facts ----------------------------------------------------- #

    def _insert(self, row: dict):
        # Load job, not streaming insert: free, and the row can be deleted right away (/undo).
        self._ensure_fact_table(row["user_id"])
        self._load([row], self.fact_table(row["user_id"]), FACT_SCHEMA)

    def _delete(self, expense_id: str, user_id: int) -> int:
        job = self.client.query(
            f"DELETE FROM `{self.fact_table(user_id)}` WHERE expense_id = @id",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("id", "STRING", expense_id)]
            ),
        )
        job.result()
        return job.num_dml_affected_rows or 0

    def _totals(self, user_id: int, start: date, end: date) -> list[tuple[int, str, str, Decimal]]:
        """(category_id, category name as saved, currency, total) of expenses only (not income or savings)
        — the bot shows the name in the user's language."""
        if user_id not in self._fact_tables_ready:
            self._ready_if_exists(user_id)
        try:
            rows = self.client.query(
                f"""SELECT category_id, ANY_VALUE(category) AS category, currency, SUM(amount) AS total
                    FROM `{self.fact_table(user_id)}`
                    WHERE expense_date BETWEEN @start AND @end AND {EXPENSES_ONLY}
                    GROUP BY category_id, currency
                    ORDER BY total DESC""",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("start", "DATE", start),
                        bigquery.ScalarQueryParameter("end", "DATE", end),
                    ]
                ),
            ).result()
        except NotFound:  # user hasn't saved anything yet
            return []
        return [(r.category_id, r.category, r.currency, r.total) for r in rows]

    # ---- async wrappers so the bot's event loop never blocks ---------------- #

    async def insert(self, row: dict):
        await asyncio.to_thread(self._insert, row)

    async def delete(self, expense_id: str, user_id: int) -> int:
        return await asyncio.to_thread(self._delete, expense_id, user_id)

    async def totals(self, user_id: int, start: date, end: date):
        return await asyncio.to_thread(self._totals, user_id, start, end)

    def _expenses(self, user_id: int, start: date, end: date) -> list[dict]:
        """Every expense in the period, newest first."""
        try:
            self.client.get_table(self.fact_table(user_id))
        except NotFound:  # user hasn't saved anything yet
            return []
        if user_id not in self._fact_tables_ready:
            self._add_missing_fact_columns(user_id)
            self._fact_tables_ready.add(user_id)
        rows = self.client.query(
            f"""SELECT expense_id, expense_date, amount, currency, category_id, category, description, item_label, merchant,
                       IFNULL(kind, 'expense') AS kind, goal_id
                FROM `{self.fact_table(user_id)}`
                WHERE expense_date BETWEEN @start AND @end
                ORDER BY expense_date DESC, created_at DESC""",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("start", "DATE", start),
                    bigquery.ScalarQueryParameter("end", "DATE", end),
                ]
            ),
        ).result()
        return [dict(r.items()) for r in rows]

    async def expenses(self, user_id: int, start: date, end: date) -> list[dict]:
        return await asyncio.to_thread(self._expenses, user_id, start, end)

    def _money_flow(self, user_id: int, start: date, end: date) -> list[dict]:
        """Per kind, goal and currency: the total within [start, end] and the all-time total
        (savings balances need all time). One small query."""
        try:
            rows = self.client.query(
                f"""SELECT IFNULL(kind, 'expense') AS kind, goal_id, currency,
                           SUM(IF(expense_date BETWEEN @start AND @end, amount, 0)) AS period_total,
                           SUM(amount) AS all_time
                    FROM `{self.fact_table(user_id)}`
                    GROUP BY 1, 2, 3""",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("start", "DATE", start),
                        bigquery.ScalarQueryParameter("end", "DATE", end),
                    ]
                ),
            ).result()
        except NotFound:
            return []
        return [dict(r.items()) for r in rows]

    async def money_flow(self, user_id: int, start: date, end: date) -> list[dict]:
        if user_id not in self._fact_tables_ready:
            await asyncio.to_thread(self._ready_if_exists, user_id)
        return await asyncio.to_thread(self._money_flow, user_id, start, end)

    def _ready_if_exists(self, user_id: int):
        try:
            self._add_missing_fact_columns(user_id)
            self._fact_tables_ready.add(user_id)
        except NotFound:
            pass


# Rows written before savings existed have kind NULL: they are expenses.
EXPENSES_ONLY = "IFNULL(kind, 'expense') = 'expense'"


def build_row(item: dict, category_id: int, category: str, confirmed_by: str = "user") -> dict:
    kind = item.get("kind") or "expense"
    suggested_kind = item.get("ai_kind") or kind
    corrected = kind != suggested_kind or (kind == "expense" and category_id != item["category_id"])
    return {
        "expense_id": secrets.token_hex(8),
        "user_id": item["user_id"],
        "expense_date": item["expense_date"],
        "amount": item["amount"],
        "currency": item["currency"],
        "category_id": category_id,
        "category": category,
        "description": item.get("description"),
        "merchant": item.get("merchant"),
        "suggested_category": item["category"],
        "suggestion_source": item.get("suggestion_source"),
        "was_corrected": corrected,
        "source": item.get("source"),
        "raw_input": item.get("raw_input"),
        "item_label": item.get("label"),
        "confirmed_by": confirmed_by,
        "kind": kind,
        "goal_id": item.get("goal_id") if kind in ("saving", "withdrawal") else None,
        "created_at": _now(),
    }


def normalise(text: str | None) -> str:
    """Lower-case, strip punctuation and extra spaces: 'Starbucks!' -> 'starbucks'."""
    if not text:
        return ""
    cleaned = "".join(ch if ch.isalnum() or ch in " &" else " " for ch in text.lower())
    return " ".join(cleaned.split())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
