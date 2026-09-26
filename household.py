"""Household mode — OFF by default.

Off: the bot is private to OWNER_USER_ID. Nothing household-related exists in BigQuery.
On:  the owner started it with /household (or from a join-request notification).
     - dim_household_members lists who may use the bot (owner + members)
     - each member logs into their own fct_expenses_<user_id> table, as before
     - v_expenses_all combines everyone's tables; /family reports on it
Ending the household removes members' access but keeps all their data.

State lives in BigQuery (dim_household_members), so it survives moving the bot to
another machine. Join requests (strangers messaging the bot) live in local SQLite.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

import config
from storage import Warehouse

log = logging.getLogger(__name__)

MEMBERS_SCHEMA = [
    bigquery.SchemaField("user_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("display_name", "STRING"),
    bigquery.SchemaField("role", "STRING"),  # owner | member
    bigquery.SchemaField("is_active", "BOOL"),
    bigquery.SchemaField("added_by", "INT64"),
    bigquery.SchemaField("added_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
]


@dataclass
class Member:
    user_id: int
    display_name: str
    role: str


class Household:
    def __init__(self, warehouse: Warehouse, db_path: str = config.PENDING_DB_PATH):
        self.wh = warehouse
        self.table = f"{warehouse.ds}.dim_household_members"
        self.view = f"{warehouse.ds}.v_expenses_all"
        self.owner_id = config.OWNER_USER_ID
        self.members: dict[int, Member] = {}  # active members incl. owner; empty = household off
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS join_requests (
                   user_id INTEGER PRIMARY KEY,
                   display_name TEXT,
                   username TEXT,
                   last_notified_at TEXT)"""
        )
        self.db.commit()

    # ---- state ------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return bool(self.members)

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id == self.owner_id

    def is_allowed(self, user_id: int | None) -> bool:
        return self.is_owner(user_id) or (self.enabled and user_id in self.members)

    def load(self):
        """Read members from BigQuery. A missing table simply means household mode was never started."""
        try:
            rows = self.wh.client.query(
                f"""SELECT user_id, IFNULL(display_name, CAST(user_id AS STRING)) AS display_name,
                           IFNULL(role, 'member') AS role
                    FROM `{self.table}` WHERE IFNULL(is_active, FALSE)"""
            ).result()
            self.members = {r.user_id: Member(r.user_id, r.display_name, r.role) for r in rows}
        except NotFound:
            self.members = {}
        log.info("Household mode: %s (%d members)", "on" if self.enabled else "off", len(self.members))

    # ---- BigQuery writes ---------------------------------------------------- #

    def _upsert(self, user_id: int, display_name: str, role: str, active: bool, by: int):
        sql = f"""
        MERGE `{self.table}` t
        USING (SELECT @uid AS user_id) s ON t.user_id = s.user_id
        WHEN MATCHED THEN UPDATE SET
          display_name = @name, role = @role, is_active = @active, updated_at = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT (user_id, display_name, role, is_active, added_by, added_at, updated_at)
          VALUES (@uid, @name, @role, @active, @by, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
        """
        params = [
            bigquery.ScalarQueryParameter("uid", "INT64", user_id),
            bigquery.ScalarQueryParameter("name", "STRING", display_name),
            bigquery.ScalarQueryParameter("role", "STRING", role),
            bigquery.ScalarQueryParameter("active", "BOOL", active),
            bigquery.ScalarQueryParameter("by", "INT64", by),
        ]
        self.wh.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()

    def _start(self, owner_name: str):
        self.wh.client.create_table(bigquery.Table(self.table, schema=MEMBERS_SCHEMA), exists_ok=True)
        self._upsert(self.owner_id, owner_name, "owner", True, self.owner_id)
        self.ensure_view()

    def ensure_view(self):
        view = bigquery.Table(self.view)
        view.view_query = f"SELECT * FROM `{self.wh.ds}.{config.FACT_TABLE_PREFIX}*`"
        try:
            self.wh.client.create_table(view, exists_ok=True)
        except Exception as e:  # no fact table exists yet; retried after the first save
            log.debug("v_expenses_all not created yet: %s", e)

    def _end(self):
        self.wh.client.query(
            f"UPDATE `{self.table}` SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP() WHERE TRUE"
        ).result()

    def _family_totals(self, start: date, end: date):
        sql = f"""
        SELECT m.display_name AS member, e.category_id, ANY_VALUE(e.category) AS category,
               e.currency, SUM(e.amount) AS total
        FROM `{self.wh.ds}.{config.FACT_TABLE_PREFIX}*` e
        JOIN `{self.table}` m ON m.user_id = e.user_id AND IFNULL(m.is_active, FALSE)
        WHERE e.expense_date BETWEEN @start AND @end
        GROUP BY member, category_id, currency
        ORDER BY total DESC
        """
        params = [
            bigquery.ScalarQueryParameter("start", "DATE", start),
            bigquery.ScalarQueryParameter("end", "DATE", end),
        ]
        try:
            rows = self.wh.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
        except NotFound:
            return []
        return [(r.member, r.category_id, r.category, r.currency, r.total) for r in rows]

    # ---- async API used by bot.py ------------------------------------------- #

    async def start(self, owner_name: str):
        await asyncio.to_thread(self._start, owner_name)
        self.members[self.owner_id] = Member(self.owner_id, owner_name, "owner")

    async def add(self, user_id: int, display_name: str):
        await asyncio.to_thread(self._upsert, user_id, display_name, "member", True, self.owner_id)
        self.members[user_id] = Member(user_id, display_name, "member")
        self.db.execute("DELETE FROM join_requests WHERE user_id = ?", (user_id,))
        self.db.commit()

    async def remove(self, user_id: int):
        m = self.members.get(user_id)
        if not m or m.role == "owner":
            return
        await asyncio.to_thread(self._upsert, user_id, m.display_name, "member", False, self.owner_id)
        self.members.pop(user_id, None)

    async def end(self):
        await asyncio.to_thread(self._end)
        self.members = {}

    async def family_totals(self, start: date, end: date) -> list[tuple[str, int, str, str, Decimal]]:
        return await asyncio.to_thread(self._family_totals, start, end)

    # ---- join requests (strangers messaging the bot) ----------------------- #

    def forget_request(self, user_id: int):
        self.db.execute("DELETE FROM join_requests WHERE user_id = ?", (user_id,))
        self.db.commit()

    def record_request(self, user_id: int, display_name: str, username: str | None) -> bool:
        """Remember who asked. Returns True if the owner should be notified now (max once a day per person)."""
        row = self.db.execute("SELECT last_notified_at FROM join_requests WHERE user_id = ?", (user_id,)).fetchone()
        now = datetime.now(timezone.utc)
        if row and row[0] and now - datetime.fromisoformat(row[0]) < timedelta(hours=24):
            return False
        self.db.execute(
            "INSERT OR REPLACE INTO join_requests VALUES (?, ?, ?, ?)",
            (user_id, display_name, username, now.isoformat()),
        )
        self.db.commit()
        return True

    def request(self, user_id: int) -> tuple[str, str | None] | None:
        row = self.db.execute(
            "SELECT display_name, username FROM join_requests WHERE user_id = ?", (user_id,)
        ).fetchone()
        return (row[0], row[1]) if row else None
