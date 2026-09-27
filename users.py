"""dim_users: one row per Telegram user who has ever written to the bot.

Kept up to date from every interaction, cheaply:
  - a row is MERGEd when something about the person changes (name, @username, language, role),
    or at most once an hour to move last_seen_at forward;
  - on startup, anyone who appears in log_interactions but has no row yet is added (backfill).
The exact per-event history stays in log_interactions; this table is the "who is who".
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from google.cloud import bigquery

log = logging.getLogger(__name__)

USERS_SCHEMA = [
    bigquery.SchemaField("user_id", "INT64", mode="REQUIRED"),  # Telegram user id
    bigquery.SchemaField("first_name", "STRING"),
    bigquery.SchemaField("last_name", "STRING"),
    bigquery.SchemaField("username", "STRING"),  # @handle, without the @
    bigquery.SchemaField("telegram_language", "STRING"),  # the Telegram app's language code
    bigquery.SchemaField("bot_language", "STRING"),  # language chosen in the bot (en | ru)
    bigquery.SchemaField("is_premium", "BOOL"),
    bigquery.SchemaField("role", "STRING"),  # owner (of the bot) | household_owner | member | none | blocked
    bigquery.SchemaField("first_seen_at", "TIMESTAMP"),
    bigquery.SchemaField("last_seen_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
]

RESYNC_SECONDS = 3600  # move last_seen_at forward at most this often per person


class UserDirectory:
    def __init__(self, client: bigquery.Client, dataset: str):
        self.client = client
        self.table = f"{dataset}.dim_users"
        self.log_table = f"{dataset}.log_interactions"
        self._synced: dict[int, tuple[tuple, float]] = {}  # user_id -> (profile, when synced)
        self._paused: dict[int, float] = {}  # user_id -> monotonic time until which we don't write

    # ---- setup ------------------------------------------------------------- #

    def ensure_table(self, owner_id: int | None, member_ids: list[int]):
        table = bigquery.Table(self.table, schema=USERS_SCHEMA)
        table.clustering_fields = ["user_id"]
        self.client.create_table(table, exists_ok=True)
        self._backfill(owner_id, member_ids)

    def _backfill(self, owner_id: int | None, member_ids: list[int]):
        """Add everyone from the interaction log who has no row yet."""
        sql = f"""
        INSERT INTO `{self.table}`
          (user_id, first_name, username, role, first_seen_at, last_seen_at, updated_at)
        SELECT
          l.user_id,
          ARRAY_AGG(JSON_VALUE(l.details, '$.user_name') IGNORE NULLS ORDER BY l.event_ts DESC LIMIT 1)[SAFE_OFFSET(0)],
          ARRAY_AGG(l.username IGNORE NULLS ORDER BY l.event_ts DESC LIMIT 1)[SAFE_OFFSET(0)],
          CASE WHEN l.user_id = @owner THEN 'owner'
               WHEN l.user_id IN UNNEST(@members) THEN 'member'
               ELSE 'none' END,
          MIN(l.event_ts), MAX(l.event_ts), CURRENT_TIMESTAMP()
        FROM `{self.log_table}` l
        WHERE l.user_id IS NOT NULL
          AND l.user_id NOT IN (SELECT user_id FROM `{self.table}`)
        GROUP BY l.user_id
        """
        params = [
            bigquery.ScalarQueryParameter("owner", "INT64", owner_id),
            bigquery.ArrayQueryParameter("members", "INT64", member_ids),
        ]
        try:
            job = self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
            job.result()
            if job.num_dml_affected_rows:
                log.info("dim_users: added %d users from the interaction log", job.num_dml_affected_rows)
        except Exception:
            log.exception("dim_users backfill failed (it will retry on the next start)")

    # ---- keeping rows current ------------------------------------------------ #

    def _merge(self, row: dict):
        sql = f"""
        MERGE `{self.table}` t
        USING (SELECT @user_id AS user_id) s ON t.user_id = s.user_id
        WHEN MATCHED THEN UPDATE SET
          first_name = @first_name,
          last_name = @last_name,
          username = @username,
          telegram_language = @telegram_language,
          bot_language = COALESCE(@bot_language, t.bot_language),
          is_premium = @is_premium,
          role = @role,
          last_seen_at = GREATEST(IFNULL(t.last_seen_at, @seen), @seen),
          updated_at = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
          (user_id, first_name, last_name, username, telegram_language, bot_language, is_premium, role,
           first_seen_at, last_seen_at, updated_at)
          VALUES (@user_id, @first_name, @last_name, @username, @telegram_language, @bot_language,
                  @is_premium, @role, @seen, @seen, CURRENT_TIMESTAMP())
        """
        p = bigquery.ScalarQueryParameter
        params = [
            p("user_id", "INT64", row["user_id"]),
            p("first_name", "STRING", row["first_name"]),
            p("last_name", "STRING", row["last_name"]),
            p("username", "STRING", row["username"]),
            p("telegram_language", "STRING", row["telegram_language"]),
            p("bot_language", "STRING", row["bot_language"]),
            p("is_premium", "BOOL", row["is_premium"]),
            p("role", "STRING", row["role"]),
            p("seen", "TIMESTAMP", row["seen"]),
        ]
        self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()

    async def observe(self, user, role: str, bot_language: str | None, force: bool = False):
        """Record that `user` did something. Writes only when needed (see module docstring)."""
        if user is None or getattr(user, "is_bot", False):
            return
        if self._paused.get(user.id, 0) > time.monotonic():  # their data was just deleted
            return
        profile = (
            _str(user.first_name), _str(user.last_name), _str(user.username),
            _str(user.language_code), bot_language, bool(getattr(user, "is_premium", False) is True), role,
        )
        last = self._synced.get(user.id)
        if not force and last and last[0] == profile and time.monotonic() - last[1] < RESYNC_SECONDS:
            return
        self._synced[user.id] = (profile, time.monotonic())  # claim first, so bursts don't double-write
        row = dict(zip(
            ("first_name", "last_name", "username", "telegram_language", "bot_language", "is_premium", "role"),
            profile,
        ))
        row.update(user_id=user.id, seen=datetime.now(timezone.utc))
        try:
            await asyncio.to_thread(self._merge, row)
        except Exception:
            self._synced.pop(user.id, None)  # retry on their next interaction
            log.exception("dim_users update failed for %s", user.id)

    def pause(self, user_id: int, seconds: float = 120):
        """After "delete my data": don't re-create their row from in-flight updates."""
        self._paused[user_id] = time.monotonic() + seconds
        self._synced.pop(user_id, None)

    def forget_cache(self, user_id: int | None = None):
        """Force the next observe() to write (e.g. after a role change)."""
        if user_id is None:
            self._synced.clear()
        else:
            self._synced.pop(user_id, None)

    def set_role_sync(self, user_ids: list[int], role: str):
        """Role changes for people who aren't the one interacting (household add/remove/end)."""
        if not user_ids:
            return
        self.client.query(
            f"UPDATE `{self.table}` SET role = @role, updated_at = CURRENT_TIMESTAMP() "
            f"WHERE user_id IN UNNEST(@ids)",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("role", "STRING", role),
                bigquery.ArrayQueryParameter("ids", "INT64", user_ids),
            ]),
        ).result()

    async def set_role(self, user_ids: list[int], role: str):
        for uid in user_ids:
            self.forget_cache(uid)
        try:
            await asyncio.to_thread(self.set_role_sync, user_ids, role)
        except Exception:
            log.exception("dim_users role update failed")


def _str(v) -> str | None:
    return v if isinstance(v, str) and v else None
