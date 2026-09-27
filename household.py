"""Households: anyone can create one and invite people with a link.

- dim_households         one row per household: name, who created it, the current invite code
- dim_household_members  one row per person: which household they're in (one at a time) and
                         their role there (owner = created it, member = joined by link)
- Everyone still logs into their own fct_expenses_<user_id>. 👨‍👩‍👧 Family reports add up the
  members of the caller's household through the wildcard over all fact tables.

Leaving, being removed or ending a household never deletes anyone's expenses.
State lives in BigQuery, so it survives moving the bot to another machine; it's cached in memory.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

import config
from storage import Warehouse

log = logging.getLogger(__name__)

HOUSEHOLDS_SCHEMA = [
    bigquery.SchemaField("household_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("created_by", "INT64"),
    bigquery.SchemaField("invite_code", "STRING"),  # the part after ?start=join_ ; replaced by "New link"
    bigquery.SchemaField("is_active", "BOOL"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
]

MEMBERS_SCHEMA = [
    bigquery.SchemaField("user_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("display_name", "STRING"),
    bigquery.SchemaField("role", "STRING"),  # owner (created the household) | member
    bigquery.SchemaField("is_active", "BOOL"),
    bigquery.SchemaField("added_by", "INT64"),
    bigquery.SchemaField("added_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
    bigquery.SchemaField("household_id", "STRING"),  # -> dim_households
]


@dataclass
class Member:
    user_id: int
    display_name: str
    role: str  # owner | member
    household_id: str


@dataclass
class Home:
    household_id: str
    name: str
    created_by: int
    invite_code: str


def new_invite_code() -> str:
    return secrets.token_urlsafe(9)  # 12 characters, fine inside a t.me/...?start= link


class Households:
    def __init__(self, warehouse: Warehouse):
        self.wh = warehouse
        self.table = f"{warehouse.ds}.dim_household_members"
        self.homes_table = f"{warehouse.ds}.dim_households"
        self.view = f"{warehouse.ds}.v_expenses_all"
        self.homes: dict[str, Home] = {}
        self.members: dict[int, Member] = {}  # active memberships only
        self.lock = asyncio.Lock()  # household changes one at a time

    # ---- reading the cache ------------------------------------------------- #

    @property
    def enabled(self) -> bool:
        """At least one household exists (so the combined view is needed)."""
        return bool(self.homes)

    def member(self, user_id: int | None) -> Member | None:
        return self.members.get(user_id) if user_id is not None else None

    def home_of(self, user_id: int | None) -> Home | None:
        m = self.member(user_id)
        return self.homes.get(m.household_id) if m else None

    def is_creator(self, user_id: int | None) -> bool:
        m = self.member(user_id)
        return m is not None and m.role == "owner"

    def members_of(self, household_id: str) -> list[Member]:
        return sorted(
            (m for m in self.members.values() if m.household_id == household_id),
            key=lambda m: (m.role != "owner", m.display_name.lower()),
        )

    def by_code(self, code: str) -> Home | None:
        return next((h for h in self.homes.values() if code and h.invite_code == code), None)

    # ---- startup ----------------------------------------------------------- #

    def setup(self, legacy_owner_id: int | None):
        """Create the tables, upgrade the single-household layout, then load."""
        client = self.wh.client
        client.create_table(bigquery.Table(self.homes_table, schema=HOUSEHOLDS_SCHEMA), exists_ok=True)
        table = client.create_table(bigquery.Table(self.table, schema=MEMBERS_SCHEMA), exists_ok=True)
        have = {f.name for f in table.schema}
        missing = [f for f in MEMBERS_SCHEMA if f.name not in have]
        if missing:  # table from the single-household version
            table.schema = list(table.schema) + missing
            client.update_table(table, ["schema"])
        self._migrate_legacy(legacy_owner_id)
        self.load()

    def _migrate_legacy(self, owner_id: int | None):
        """Before households could be created by anyone there was one, owned by the bot owner.
        Its active members become a regular household with that owner as its creator."""
        rows = list(self.wh.client.query(
            f"""SELECT user_id, IFNULL(display_name, CAST(user_id AS STRING)) AS display_name, role
                FROM `{self.table}` WHERE IFNULL(is_active, FALSE) AND household_id IS NULL"""
        ).result())
        if not rows:
            return
        creator = next((r for r in rows if r.user_id == owner_id), None) or next(
            (r for r in rows if r.role == "owner"), rows[0]
        )
        hid = "h_" + secrets.token_hex(6)
        self._insert_home(Home(hid, f"{creator.display_name}'s household", creator.user_id, new_invite_code()))
        self._query(
            f"""UPDATE `{self.table}`
                SET household_id = @hid, role = IF(user_id = @creator, 'owner', 'member'),
                    updated_at = CURRENT_TIMESTAMP()
                WHERE IFNULL(is_active, FALSE) AND household_id IS NULL""",
            [("hid", "STRING", hid), ("creator", "INT64", creator.user_id)],
        )
        log.info("Moved the existing household (%d people) to %s", len(rows), hid)

    def load(self):
        try:
            homes = self.wh.client.query(
                f"""SELECT household_id, IFNULL(name, 'Household') AS name, created_by, invite_code
                    FROM `{self.homes_table}` WHERE IFNULL(is_active, FALSE)"""
            ).result()
            self.homes = {r.household_id: Home(r.household_id, r.name, r.created_by, r.invite_code) for r in homes}
            rows = self.wh.client.query(
                f"""SELECT user_id, IFNULL(display_name, CAST(user_id AS STRING)) AS display_name,
                           IFNULL(role, 'member') AS role, household_id
                    FROM `{self.table}` WHERE IFNULL(is_active, FALSE) AND household_id IS NOT NULL"""
            ).result()
            self.members = {
                r.user_id: Member(r.user_id, r.display_name, r.role, r.household_id)
                for r in rows if r.household_id in self.homes
            }
        except NotFound:
            self.homes, self.members = {}, {}
        log.info("Households: %d (%d people)", len(self.homes), len(self.members))

    def ensure_view(self):
        view = bigquery.Table(self.view)
        view.view_query = f"SELECT * FROM `{self.wh.ds}.{config.FACT_TABLE_PREFIX}*`"
        try:
            self.wh.client.create_table(view, exists_ok=True)
        except Exception as e:  # no fact table exists yet; retried after the first save
            log.debug("v_expenses_all not created yet: %s", e)

    # ---- BigQuery writes (DML, so rows can be updated right away) ----------- #

    def _query(self, sql: str, params: list[tuple[str, str, object]]):
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter(n, t, v) for n, t, v in params]
        )
        self.wh.client.query(sql, job_config=job_config).result()

    def _insert_home(self, home: Home):
        self._query(
            f"""INSERT INTO `{self.homes_table}`
                  (household_id, name, created_by, invite_code, is_active, created_at, updated_at)
                VALUES (@hid, @name, @by, @code, TRUE, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())""",
            [("hid", "STRING", home.household_id), ("name", "STRING", home.name),
             ("by", "INT64", home.created_by), ("code", "STRING", home.invite_code)],
        )

    def _upsert_member(self, user_id: int, name: str, role: str, household_id: str, active: bool, by: int):
        self._query(
            f"""MERGE `{self.table}` t
                USING (SELECT @uid AS user_id) s ON t.user_id = s.user_id
                WHEN MATCHED THEN UPDATE SET
                  display_name = @name, role = @role, household_id = @hid, is_active = @active,
                  added_by = IF(@active, @by, t.added_by),
                  added_at = IF(@active, CURRENT_TIMESTAMP(), t.added_at),
                  updated_at = CURRENT_TIMESTAMP()
                WHEN NOT MATCHED THEN INSERT
                  (user_id, display_name, role, household_id, is_active, added_by, added_at, updated_at)
                  VALUES (@uid, @name, @role, @hid, @active, @by, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())""",
            [("uid", "INT64", user_id), ("name", "STRING", name), ("role", "STRING", role),
             ("hid", "STRING", household_id), ("active", "BOOL", active), ("by", "INT64", by)],
        )

    def _set_code(self, household_id: str, code: str):
        self._query(
            f"UPDATE `{self.homes_table}` SET invite_code = @code, updated_at = CURRENT_TIMESTAMP() "
            f"WHERE household_id = @hid",
            [("code", "STRING", code), ("hid", "STRING", household_id)],
        )

    def _end(self, household_id: str):
        self._query(
            f"UPDATE `{self.homes_table}` SET is_active = FALSE, invite_code = NULL, "
            f"updated_at = CURRENT_TIMESTAMP() WHERE household_id = @hid",
            [("hid", "STRING", household_id)],
        )
        self._query(
            f"UPDATE `{self.table}` SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP() "
            f"WHERE household_id = @hid AND IFNULL(is_active, FALSE)",
            [("hid", "STRING", household_id)],
        )

    def _family_totals(self, household_id: str, start: date, end: date):
        sql = f"""
        SELECT m.display_name AS member, e.category_id, ANY_VALUE(e.category) AS category,
               e.currency, SUM(e.amount) AS total
        FROM `{self.wh.ds}.{config.FACT_TABLE_PREFIX}*` e
        JOIN `{self.table}` m
          ON m.user_id = e.user_id AND IFNULL(m.is_active, FALSE) AND m.household_id = @hid
        WHERE e.expense_date BETWEEN @start AND @end
        GROUP BY member, category_id, currency
        ORDER BY total DESC
        """
        params = [
            bigquery.ScalarQueryParameter("hid", "STRING", household_id),
            bigquery.ScalarQueryParameter("start", "DATE", start),
            bigquery.ScalarQueryParameter("end", "DATE", end),
        ]
        try:
            rows = self.wh.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
        except NotFound:
            return []
        return [(r.member, r.category_id, r.category, r.currency, r.total) for r in rows]

    # ---- async API used by bot.py (callers hold self.lock) ------------------- #

    async def create(self, user_id: int, display_name: str, name: str) -> Home:
        home = Home("h_" + secrets.token_hex(6), name, user_id, new_invite_code())
        await asyncio.to_thread(self._insert_home, home)
        await asyncio.to_thread(self._upsert_member, user_id, display_name, "owner", home.household_id, True, user_id)
        self.homes[home.household_id] = home
        self.members[user_id] = Member(user_id, display_name, "owner", home.household_id)
        return home

    async def join(self, user_id: int, display_name: str, home: Home) -> Member:
        await asyncio.to_thread(
            self._upsert_member, user_id, display_name, "member", home.household_id, True, home.created_by
        )
        m = Member(user_id, display_name, "member", home.household_id)
        self.members[user_id] = m
        return m

    async def leave(self, user_id: int):
        """A member leaves, or is removed by the household's creator."""
        m = self.members.get(user_id)
        if m is None or m.role == "owner":
            return
        await asyncio.to_thread(self._upsert_member, user_id, m.display_name, "member", m.household_id, False, user_id)
        self.members.pop(user_id, None)

    async def new_link(self, household_id: str) -> str:
        code = new_invite_code()
        await asyncio.to_thread(self._set_code, household_id, code)
        self.homes[household_id].invite_code = code
        return code

    async def end(self, household_id: str) -> list[int]:
        """Returns everyone who was in it."""
        former = [m.user_id for m in self.members_of(household_id)]
        await asyncio.to_thread(self._end, household_id)
        self.homes.pop(household_id, None)
        for uid in former:
            self.members.pop(uid, None)
        return former

    async def family_totals(self, household_id: str, start: date, end: date) -> list[tuple[str, int, str, str, Decimal]]:
        return await asyncio.to_thread(self._family_totals, household_id, start, end)
