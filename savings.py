"""Savings: income, money put aside, withdrawals and savings goals.

Every money record lives in the person's own fct_expenses_<user_id>, told apart by `kind`:
    expense      what reports, family totals and the dictionary have always counted (kind NULL = expense)
    income       salary, payments received…
    saving       money put aside (optionally towards a goal: goal_id)
    withdrawal   money taken back out of savings (optionally from a goal)

dim_savings_goals   one row per goal: name, target, currency, optional deadline. Closed goals stay
                    (with closed_at) so their history still adds up; "Delete my data" hides them
                    (deleted_at) and the hard delete erases them.
Goals are cached in memory, like households.
"""
from __future__ import annotations

import asyncio
import logging
import math
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from storage import Warehouse

log = logging.getLogger(__name__)

KINDS = ("expense", "income", "saving", "withdrawal")
ICONS = {"expense": "🧾", "income": "💵", "saving": "💰", "withdrawal": "🏦"}
# category / category_id written on non-expense rows (the columns are REQUIRED; 0 = not a spending category)
ROW_CATEGORY = {"income": "Income", "saving": "Savings", "withdrawal": "Savings withdrawal"}
MAX_OPEN_GOALS = 8

GOALS_SCHEMA = [
    bigquery.SchemaField("goal_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("user_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("target_amount", "NUMERIC"),  # NULL = no target, just a pot
    bigquery.SchemaField("currency", "STRING"),
    bigquery.SchemaField("deadline", "DATE"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("closed_at", "TIMESTAMP"),
    bigquery.SchemaField("deleted_at", "TIMESTAMP"),  # set by "Delete my data", cleared by restore
]


@dataclass
class Goal:
    goal_id: str
    user_id: int
    name: str
    target: Decimal | None
    currency: str
    deadline: date | None = None
    created_at: datetime | None = None
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class Goals:
    def __init__(self, warehouse: Warehouse):
        self.wh = warehouse
        self.table = f"{warehouse.ds}.dim_savings_goals"
        self.goals: dict[str, Goal] = {}  # not deleted (open and closed)
        self.lock = asyncio.Lock()

    # ---- reading the cache ------------------------------------------------- #

    def of(self, user_id: int, include_closed: bool = False) -> list[Goal]:
        found = [g for g in self.goals.values() if g.user_id == user_id and (include_closed or g.is_open)]
        return sorted(found, key=lambda g: (g.closed_at is not None, g.created_at or datetime.min.replace(tzinfo=timezone.utc)))

    def get(self, goal_id: str | None, user_id: int | None = None) -> Goal | None:
        g = self.goals.get(goal_id) if goal_id else None
        return g if g and (user_id is None or g.user_id == user_id) else None

    def by_name(self, user_id: int, name: str | None) -> Goal | None:
        if not name:
            return None
        key = name.strip().lower()
        return next((g for g in self.of(user_id) if g.name.lower() == key), None)

    # ---- startup ----------------------------------------------------------- #

    def setup(self):
        client = self.wh.client
        table = client.create_table(bigquery.Table(self.table, schema=GOALS_SCHEMA), exists_ok=True)
        have = {f.name for f in table.schema}
        missing = [f for f in GOALS_SCHEMA if f.name not in have]
        if missing:
            table.schema = [*table.schema, *[bigquery.SchemaField(f.name, f.field_type) for f in missing]]
            client.update_table(table, ["schema"])
        self.load()

    def load(self):
        try:
            rows = self.wh.client.query(
                f"""SELECT goal_id, user_id, IFNULL(name, 'Goal') AS name, target_amount, currency, deadline,
                           created_at, closed_at
                    FROM `{self.table}` WHERE deleted_at IS NULL"""
            ).result()
        except NotFound:
            rows = []
        self.goals = {
            r.goal_id: Goal(r.goal_id, r.user_id, r.name, r.target_amount, r.currency or "", r.deadline,
                            r.created_at, r.closed_at)
            for r in rows
        }
        log.info("Savings goals: %d", len(self.goals))

    # ---- changes (callers hold self.lock) ---------------------------------- #

    def _create(self, user_id: int, name: str, target: Decimal | None, currency: str, deadline: date | None) -> Goal:
        g = Goal("g_" + secrets.token_hex(4), user_id, name, target, currency, deadline, datetime.now(timezone.utc))
        self.wh._load(
            [{
                "goal_id": g.goal_id, "user_id": user_id, "name": name,
                "target_amount": str(target) if target is not None else None, "currency": currency,
                "deadline": deadline.isoformat() if deadline else None, "created_at": g.created_at.isoformat(),
                "closed_at": None, "deleted_at": None,
            }],
            self.table, GOALS_SCHEMA,
        )
        self.goals[g.goal_id] = g
        return g

    async def create(self, user_id: int, name: str, target: Decimal | None, currency: str,
                     deadline: date | None) -> Goal:
        return await asyncio.to_thread(self._create, user_id, name, target, currency, deadline)

    async def close(self, goal_id: str):
        g = self.goals.get(goal_id)
        if g is None or not g.is_open:
            return
        await asyncio.to_thread(
            self.wh._dml, f"UPDATE `{self.table}` SET closed_at = CURRENT_TIMESTAMP() WHERE goal_id = @gid",
            gid=goal_id,
        )
        g.closed_at = datetime.now(timezone.utc)

    # ---- "Delete my data" (sync: run in a thread) -------------------------- #

    def soft_delete_user(self, user_id: int, when: str):
        self._ts_dml(f"UPDATE `{self.table}` SET deleted_at = @ts WHERE user_id = @uid AND deleted_at IS NULL",
                     user_id, when)
        for gid in [gid for gid, g in self.goals.items() if g.user_id == user_id]:
            del self.goals[gid]

    def restore_user(self, user_id: int, when: str):
        self._ts_dml(f"UPDATE `{self.table}` SET deleted_at = NULL WHERE user_id = @uid AND deleted_at = @ts",
                     user_id, when)
        self.load()

    def hard_delete_user(self, user_id: int):
        self.wh._dml(f"DELETE FROM `{self.table}` WHERE user_id = @uid AND deleted_at IS NOT NULL", uid=user_id)

    def _ts_dml(self, sql: str, user_id: int, when: str):
        try:
            self.wh.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("uid", "INT64", user_id),
                bigquery.ScalarQueryParameter("ts", "TIMESTAMP", when),
            ])).result()
        except NotFound:
            pass


# --------------------------------------------------------------------------- #
# Adding it up                                                                #
# --------------------------------------------------------------------------- #


@dataclass
class Overview:
    """Totals for one period, plus all-time savings balances. Every amount is per currency."""

    income: dict[str, Decimal] = field(default_factory=dict)
    spent: dict[str, Decimal] = field(default_factory=dict)
    put_aside: dict[str, Decimal] = field(default_factory=dict)
    taken_out: dict[str, Decimal] = field(default_factory=dict)
    balance: dict[str, Decimal] = field(default_factory=dict)  # all time: put aside − taken out
    by_goal: dict[str, dict[str, Decimal]] = field(default_factory=dict)  # goal_id -> currency -> all time

    @property
    def left(self) -> dict[str, Decimal]:
        """Income − spending − (put aside − taken out), in every currency the person earned in
        (spending in a currency with no income, e.g. on a trip, isn't "left" of anything)."""
        out: dict[str, Decimal] = {}
        for cur in self.income:
            out[cur] = (self.income.get(cur, Decimal(0)) - self.spent.get(cur, Decimal(0))
                        - self.put_aside.get(cur, Decimal(0)) + self.taken_out.get(cur, Decimal(0)))
        return out

    def savings_rate(self, currency: str) -> int | None:
        """Share of income put aside (net of withdrawals) in this period, in %."""
        inc = self.income.get(currency, Decimal(0))
        if inc <= 0:
            return None
        net = self.put_aside.get(currency, Decimal(0)) - self.taken_out.get(currency, Decimal(0))
        return round(net * 100 / inc)

    @property
    def empty(self) -> bool:
        return not (self.income or self.spent or self.put_aside or self.taken_out or any(self.balance.values()))


def _add(d: dict[str, Decimal], cur: str, v) -> None:
    if v:
        d[cur] = d.get(cur, Decimal(0)) + Decimal(str(v))


def overview(rows: list[dict]) -> Overview:
    """From Warehouse.money_flow rows (kind, goal_id, currency, period_total, all_time)."""
    o = Overview()
    for r in rows:
        kind, cur = r.get("kind") or "expense", r["currency"]
        period, all_time = r.get("period_total") or 0, r.get("all_time") or 0
        if kind == "income":
            _add(o.income, cur, period)
        elif kind == "saving":
            _add(o.put_aside, cur, period)
            _add(o.balance, cur, all_time)
            if r.get("goal_id"):
                _add(o.by_goal.setdefault(r["goal_id"], {}), cur, all_time)
        elif kind == "withdrawal":
            _add(o.taken_out, cur, period)
            _add(o.balance, cur, -Decimal(str(all_time)))
            if r.get("goal_id"):
                _add(o.by_goal.setdefault(r["goal_id"], {}), cur, -Decimal(str(all_time)))
        else:
            _add(o.spent, cur, period)
    return o


@dataclass
class Progress:
    saved: Decimal  # in the goal's currency
    others: dict[str, Decimal]  # saved towards it in other currencies (shown, not converted)
    percent: int | None
    remaining: Decimal | None
    per_month: Decimal | None  # still needed per month to make the deadline
    months_left: int | None
    reached: bool
    overdue: bool


def months_until(today: date, deadline: date) -> int:
    """Whole months left, counting the current one if any days remain (at least 1)."""
    months = (deadline.year - today.year) * 12 + (deadline.month - today.month)
    if deadline.day >= today.day:
        months += 1
    return max(months, 1)


def progress(goal: Goal, saved_by_currency: dict[str, Decimal], today: date) -> Progress:
    saved = saved_by_currency.get(goal.currency, Decimal(0))
    others = {c: v for c, v in saved_by_currency.items() if c != goal.currency and v}
    if not goal.target:
        return Progress(saved, others, None, None, None, None, False, False)
    remaining = max(goal.target - saved, Decimal(0))
    percent = max(0, min(100, math.floor(saved * 100 / goal.target)))
    reached = remaining == 0
    per_month = months = None
    overdue = False
    if goal.deadline and not reached:
        if goal.deadline < today:
            overdue = True
        else:
            months = months_until(today, goal.deadline)
            per_month = (remaining / months).quantize(Decimal("1"), rounding="ROUND_CEILING")
    return Progress(saved, others, percent, remaining, per_month, months, reached, overdue)


def bar(percent: int | None, width: int = 10) -> str:
    if percent is None:
        return ""
    filled = round(percent * width / 100)
    return "▰" * filled + "▱" * (width - filled)
