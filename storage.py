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
import json
import logging
import secrets
import sqlite3
from datetime import date, datetime, timezone
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
        self.db.commit()

    def add(self, user_id: int, payload: dict) -> str:
        pid = secrets.token_hex(4)
        payload = {**payload, "user_id": user_id}
        self.db.execute(
            "INSERT INTO pending VALUES (?, ?, ?, ?)",
            (pid, user_id, json.dumps(payload, ensure_ascii=False), _now()),
        )
        self.db.commit()
        return pid

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
        for table in ("pending", "saved", "report_prefs", "user_settings"):
            self.db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        self.db.commit()

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

    def delete_user_data(self, user_id: int) -> int:
        """Drop the person's expense table and their dim_users row. Returns how many expenses were deleted.
        Their log rows are removed separately (purge_user_log), because recently streamed rows
        can't be deleted until BigQuery moves them out of its streaming buffer."""
        table = self.fact_table(user_id)
        try:
            n = next(iter(self.client.query(f"SELECT COUNT(*) AS n FROM `{table}`").result())).n
        except NotFound:
            n = 0
        self.client.delete_table(table, not_found_ok=True)
        self._fact_tables_ready.discard(user_id)
        try:
            self.client.query(
                f"DELETE FROM `{self.ds}.dim_users` WHERE user_id = @uid",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("uid", "INT64", user_id)]
                ),
            ).result()
        except NotFound:
            pass
        return n

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
        """(category_id, category name as saved, currency, total) — the bot shows the name in the user's language."""
        try:
            rows = self.client.query(
                f"""SELECT category_id, ANY_VALUE(category) AS category, currency, SUM(amount) AS total
                    FROM `{self.fact_table(user_id)}`
                    WHERE expense_date BETWEEN @start AND @end
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
            f"""SELECT expense_date, amount, currency, category_id, category, description, item_label, merchant
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


def build_row(item: dict, category_id: int, category: str) -> dict:
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
        "was_corrected": category_id != item["category_id"],
        "source": item.get("source"),
        "raw_input": item.get("raw_input"),
        "item_label": item.get("label"),
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
