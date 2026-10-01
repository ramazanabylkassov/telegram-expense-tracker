"""🖥 App status: a local web page showing how the bot itself is doing. For you, on this Mac only.
Users (active, new, who left), traffic, AI tokens and their cost, errors and outages, bot health.
It never shows anyone's expenses: no amounts, no categories.

    http://127.0.0.1:8788/          the page (dashboard.html)
    http://127.0.0.1:8788/api/metrics?days=7   everything the page shows, as JSON

Nothing about it appears in Telegram. It has its own tiny server on 127.0.0.1:DASHBOARD_PORT (8788),
separate from the Action Button upload server that the Tailscale tunnel forwards (8787), so it can't
be reached from outside the Mac. No sign-in: instead every request must come from this Mac directly
(loopback address, the Mac's own Host, nothing forwarded by a proxy or tunnel), and requests made by
other websites open in your browser are refused.

Numbers come from BigQuery (log_interactions, dim_users, dim_spend_variants; never the per-user
fct_expenses_* tables, which hold people's expenses and would bill 10 MB each) and from the bot
itself (limits, blocks, deletions, outages, queues). Each section is queried separately and cached
for 5 minutes, so one failing query never blanks the page and an open page stays well inside
BigQuery's free tier (about 90 MB per refresh, whatever the number of users).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from aiohttp import web
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

import config

log = logging.getLogger(__name__)

PERIODS = (1, 7, 30, 90)
FRESH_MIN_SECONDS = 60  # the ↻ Refresh button re-queries BigQuery at most this often
CACHE_SECONDS = 300  # BigQuery bills ≥ 10 MB per table per query, so the page refreshes every 5 min

# outcome groups in log_interactions
AI_OUTCOMES = ("proposed", "no_expense", "parse_error", "empty_audio")  # the message reached the AI
SAVED_OUTCOMES = ("saved", "saved_corrected", "auto_saved")
ERROR_OUTCOMES = ("error", "parse_error", "save_error", "report_error", "undo_error", "reload_error", "delete_error")
INPUT_EVENTS = ("text", "voice", "upload_audio", "upload_text")


def _sql_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


# --------------------------------------------------------------------------- #
# Metrics                                                                     #
# --------------------------------------------------------------------------- #


class Metrics:
    def __init__(self, client: bigquery.Client, dataset: str, local: Callable[[int], dict]):
        self.client = client
        self.ds = dataset
        self.log_table = f"{dataset}.log_interactions"
        self.local = local  # days -> numbers only the bot knows (queues, limits, households, …)
        self._cache: dict[int, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()
        self._dry_run = False

    def _q(self, sql: str, **params) -> list[dict]:
        types = {int: "INT64", str: "STRING", datetime: "TIMESTAMP"}
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter(k, types[type(v)], v) for k, v in params.items()
        ], dry_run=self._dry_run, use_query_cache=not self._dry_run)
        if self._dry_run:  # validated by BigQuery itself, nothing runs or is billed
            self.client.query(sql, job_config=job_config)
            return []
        try:
            return [dict(r.items()) for r in self.client.query(sql, job_config=job_config).result()]
        except NotFound:
            return []

    # ---- sections ----------------------------------------------------------- #

    def kpis(self, days: int) -> dict:
        """Current period vs the one before: users, traffic, AI tokens, problems."""
        rows = self._q(f"""
        SELECT
          event_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY) AS is_current,  -- not "current": a reserved word
          COUNT(DISTINCT IF(event_type NOT IN ('auto_save', 'membership'), user_id, NULL)) AS active_users,
          COUNTIF(event_type NOT IN ('auto_save', 'membership')) AS requests,
          COUNTIF(event_type IN ({_sql_list(INPUT_EVENTS)}) AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS ai_messages,
          COUNTIF(event_type = 'text' AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS text_messages,
          COUNTIF(event_type = 'voice' AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS voice_messages,
          COUNTIF(event_type IN ('upload_audio', 'upload_text') AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS uploads,
          IFNULL(SUM(audio_seconds), 0) AS audio_seconds,
          IFNULL(SUM(llm_input_tokens), 0) AS tokens_in,
          IFNULL(SUM(llm_output_tokens), 0) AS tokens_out,
          COUNTIF(llm_input_tokens IS NOT NULL) AS ai_calls,
          COUNTIF(outcome IN ({_sql_list(ERROR_OUTCOMES)})) AS errors,
          COUNTIF(outcome IN ('daily_limit', 'voice_too_long')) AS limits_hit,
          COUNTIF(outcome = 'blocked') AS blocked_attempts,
          COUNT(DISTINCT IF(outcome = 'bot_blocked', user_id, NULL)) AS blocked_bot,
          COUNT(DISTINCT IF(outcome = 'bot_unblocked', user_id, NULL)) AS unblocked_bot,
          COUNTIF(outcome = 'saved') AS saved_as_suggested,
          COUNTIF(outcome = 'saved_corrected') AS saved_corrected,
          APPROX_QUANTILES(IF(outcome = 'proposed', latency_ms, NULL), 100)[SAFE_OFFSET(50)] AS p50_ms,
          APPROX_QUANTILES(IF(outcome = 'proposed', latency_ms, NULL), 100)[SAFE_OFFSET(95)] AS p95_ms
        FROM `{self.log_table}`
        WHERE event_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days2 DAY)
        GROUP BY is_current
        """, days=days, days2=2 * days)
        new = self._q(f"""
        SELECT
          COUNTIF(first_seen_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)) AS cur,
          COUNTIF(first_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
                  AND first_seen_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days2 DAY)) AS prev,
          COUNTIF(deleted_at IS NULL) AS total
        FROM `{self.ds}.dim_users`
        """, days=days, days2=2 * days)
        tz = str(config.TIMEZONE)
        overall = self._q(f"""
        WITH last_status AS (
          SELECT user_id, ARRAY_AGG(outcome ORDER BY event_ts DESC LIMIT 1)[OFFSET(0)] AS last
          FROM `{self.log_table}`
          WHERE event_type = 'membership' AND outcome IN ('bot_blocked', 'bot_unblocked')
          GROUP BY user_id
        )
        SELECT
          (SELECT COUNT(*) FROM last_status WHERE last = 'bot_blocked') AS blocked_bot_now,
          (SELECT IFNULL(SUM(llm_input_tokens), 0) FROM `{self.log_table}`
            WHERE event_ts >= TIMESTAMP(DATE_TRUNC(CURRENT_DATE('{tz}'), MONTH), '{tz}')) AS month_tokens_in,
          (SELECT IFNULL(SUM(llm_output_tokens), 0) FROM `{self.log_table}`
            WHERE event_ts >= TIMESTAMP(DATE_TRUNC(CURRENT_DATE('{tz}'), MONTH), '{tz}')) AS month_tokens_out
        """)
        by = {bool(r.pop("is_current")): r for r in rows}
        empty = {k: 0 for k in ("active_users", "requests", "ai_messages", "text_messages", "voice_messages", "uploads",
                                "audio_seconds", "tokens_in", "tokens_out", "ai_calls", "errors", "limits_hit",
                                "blocked_attempts", "blocked_bot", "unblocked_bot", "saved_as_suggested",
                                "saved_corrected")}
        cur, prev = {**empty, **by.get(True, {})}, {**empty, **by.get(False, {})}
        n = new[0] if new else {"cur": 0, "prev": 0, "total": 0}
        cur["new_users"], prev["new_users"] = n["cur"], n["prev"]
        o = overall[0] if overall else {"blocked_bot_now": 0, "month_tokens_in": 0, "month_tokens_out": 0}
        return {"current": cur, "previous": prev, "total_users": n["total"], **o}

    def series(self, days: int) -> dict:
        """Per hour for 24 h, per day otherwise (in TIMEZONE)."""
        tz = str(config.TIMEZONE)  # an IANA name from ZoneInfo, e.g. Asia/Almaty: safe as a literal
        hourly = days <= 1
        bucket = f"TIMESTAMP_TRUNC(event_ts, HOUR, '{tz}')" if hourly else f"TIMESTAMP(DATE(event_ts, '{tz}'), '{tz}')"
        rows = self._q(f"""
        SELECT {bucket} AS bucket,
          COUNT(DISTINCT IF(event_type NOT IN ('auto_save', 'membership'), user_id, NULL)) AS active_users,
          COUNTIF(event_type = 'text' AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS text,
          COUNTIF(event_type = 'voice' AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS voice,
          COUNTIF(event_type IN ('upload_audio', 'upload_text') AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS upload,
          IFNULL(SUM(llm_input_tokens), 0) AS tokens_in,
          IFNULL(SUM(llm_output_tokens), 0) AS tokens_out,
          COUNTIF(outcome IN ({_sql_list(ERROR_OUTCOMES)})) AS errors,
          COUNT(DISTINCT IF(outcome = 'bot_blocked', user_id, NULL)) AS left_bot
        FROM `{self.log_table}`
        WHERE event_ts >= @start
        GROUP BY bucket ORDER BY bucket
        """, start=self._start(days, hourly))
        new = self._q(f"""
        SELECT {bucket.replace("event_ts", "first_seen_at")} AS bucket, COUNT(*) AS new_users
        FROM `{self.ds}.dim_users`
        WHERE first_seen_at >= @start
        GROUP BY bucket
        """, start=self._start(days, hourly))
        keys = ("active_users", "text", "voice", "upload", "tokens_in", "tokens_out", "errors", "left_bot")
        got = {r["bucket"].astimezone(config.TIMEZONE).replace(tzinfo=None): r for r in rows}
        joined = {r["bucket"].astimezone(config.TIMEZONE).replace(tzinfo=None): r["new_users"] for r in new}
        out = []
        t = self._start(days, hourly).astimezone(config.TIMEZONE).replace(tzinfo=None)
        step = timedelta(hours=1) if hourly else timedelta(days=1)
        end = datetime.now(config.TIMEZONE).replace(tzinfo=None)
        while t <= end:
            r = got.get(t, {})
            out.append({"t": t.isoformat(), **{k: r.get(k, 0) for k in keys}, "new_users": joined.get(t, 0)})
            t += step
        return {"unit": "hour" if hourly else "day", "points": out}

    @staticmethod
    def _start(days: int, hourly: bool) -> datetime:
        now = datetime.now(config.TIMEZONE)
        if hourly:
            return (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)
        return (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    def latency(self, days: int) -> list[dict]:
        return self._q(f"""
        SELECT event_type, COUNT(*) AS n,
               APPROX_QUANTILES(latency_ms, 100)[SAFE_OFFSET(50)] AS p50_ms,
               APPROX_QUANTILES(latency_ms, 100)[SAFE_OFFSET(95)] AS p95_ms,
               MAX(latency_ms) AS max_ms,
               AVG(llm_input_tokens + llm_output_tokens) AS avg_tokens
        FROM `{self.log_table}`
        WHERE event_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
          AND event_type IN ({_sql_list(INPUT_EVENTS)}) AND outcome = 'proposed'
        GROUP BY event_type ORDER BY n DESC
        """, days=days)

    def users(self, days: int) -> list[dict]:
        return self._q(f"""
        WITH a AS (
          SELECT user_id,
            ARRAY_AGG(JSON_VALUE(details, '$.user_name') IGNORE NULLS ORDER BY event_ts DESC LIMIT 1)[SAFE_OFFSET(0)] AS log_name,
            ARRAY_AGG(username IGNORE NULLS ORDER BY event_ts DESC LIMIT 1)[SAFE_OFFSET(0)] AS log_username,
            COUNTIF(event_type NOT IN ('auto_save', 'membership')) AS actions,
            COUNTIF(event_type IN ({_sql_list(INPUT_EVENTS)}) AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS ai_messages,
            IFNULL(SUM(llm_input_tokens), 0) + IFNULL(SUM(llm_output_tokens), 0) AS tokens,
            COUNTIF(outcome IN ({_sql_list(ERROR_OUTCOMES)})) AS errors,
            ARRAY_AGG(IF(outcome IN ('bot_blocked', 'bot_unblocked'), outcome, NULL) IGNORE NULLS
                      ORDER BY event_ts DESC LIMIT 1)[SAFE_OFFSET(0)] AS membership,
            MAX(IF(event_type NOT IN ('auto_save', 'membership'), event_ts, NULL)) AS last_seen
          FROM `{self.log_table}`
          WHERE user_id IS NOT NULL AND event_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
          GROUP BY user_id
        )
        SELECT a.user_id,
          COALESCE(NULLIF(TRIM(CONCAT(IFNULL(u.first_name, ''), ' ', IFNULL(u.last_name, ''))), ''), a.log_name) AS name,
          COALESCE(u.username, a.log_username) AS username, u.bot_language AS language, u.first_seen_at,
          a.actions, a.ai_messages, a.tokens, a.errors, a.membership, a.last_seen
        FROM a LEFT JOIN `{self.ds}.dim_users` u USING (user_id)
        WHERE u.deleted_at IS NULL  -- hidden while their deleted data waits to be erased
        ORDER BY a.actions DESC LIMIT 100
        """, days=days)

    def errors(self, days: int) -> list[dict]:
        return self._q(f"""
        SELECT event_ts, user_id, username, event_type, command, button_action, outcome, error
        FROM `{self.log_table}`
        WHERE event_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
          AND outcome IN ({_sql_list(ERROR_OUTCOMES)})
        ORDER BY event_ts DESC LIMIT 20
        """, days=days)

    def dictionary(self, days: int) -> dict:
        rows = self._q(f"""
        SELECT COUNT(*) AS variants, COUNTIF(source = 'learned') AS learned,
               COUNTIF(first_seen_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)) AS new_in_period
        FROM `{self.ds}.dim_spend_variants`
        """, days=days)
        return rows[0] if rows else {"variants": 0, "learned": 0, "new_in_period": 0}

    def check(self) -> list[str]:
        """Dry-run every query (free: BigQuery parses and plans them without running). Returns problems."""
        problems = []
        self._dry_run = True
        try:
            for name in ("kpis", "series", "latency", "users", "errors", "dictionary"):
                for days in (1, 7):
                    try:
                        getattr(self, name)(days)
                    except NotFound:
                        pass  # a table that doesn't exist yet (e.g. no one has used the bot)
                    except Exception as e:
                        problems.append(f"{name}: {type(e).__name__}: {str(e)[:300]}")
                        break
        finally:
            self._dry_run = False
        return problems

    # ---- all together -------------------------------------------------------- #

    async def collect(self, days: int, fresh: bool = False) -> dict:
        """`fresh` (the ↻ Refresh button) skips the 5-minute cache, but not more than once a minute."""
        async with self._lock:
            hit = self._cache.get(days)
            max_age = FRESH_MIN_SECONDS if fresh else CACHE_SECONDS
            if hit and time.monotonic() - hit[0] < max_age:
                data = dict(hit[1])
            else:
                names = ("kpis", "series", "latency", "users", "errors", "dictionary")
                results = await asyncio.gather(
                    *(asyncio.to_thread(getattr(self, n), days) for n in names), return_exceptions=True
                )
                data = {}
                for name, res in zip(names, results):
                    if isinstance(res, Exception):
                        log.warning("Status page section %s failed: %s", name, res)
                        data[name] = {"error": f"{type(res).__name__}: {str(res)[:300]}"}
                    else:
                        data[name] = res
                data["generated_at"] = datetime.now(timezone.utc).isoformat()
                self._cache[days] = (time.monotonic(), data)
        data["local"] = self.local(days)  # always fresh: cheap, and it's what changes minute to minute
        data["days"] = days
        return data


def _json_default(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return str(v)


# --------------------------------------------------------------------------- #
# Web routes                                                                  #
# --------------------------------------------------------------------------- #

PAGE = Path(__file__).with_name("dashboard.html")

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; "
                               "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}

def local_only(port: int):
    """Only this Mac, directly: loopback peer, the Mac's own Host (stops DNS-rebinding pages),
    nothing forwarded by a tunnel or proxy, and no requests made by other websites in the browser."""
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    proxied = ("X-Forwarded-For", "Forwarded", "X-Forwarded-Host", "Tailscale-User-Login", "Tailscale-Funnel-Request")

    @web.middleware
    async def middleware(request: web.Request, handler):
        remote_ok = request.remote in ("127.0.0.1", "::1")
        cross_site = request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none")
        if (not remote_ok or request.host not in allowed_hosts or cross_site
                or any(h in request.headers for h in proxied)):
            log.warning("App status: refused a request for host %r from %s", request.host, request.remote)
            return web.Response(status=403, text="The app status page only opens on the Mac running the bot.")
        return await handler(request)

    return middleware


def build_app(metrics: Metrics, port: int | None = None) -> web.Application:
    app = web.Application(middlewares=[local_only(port or config.DASHBOARD_PORT)])
    add_routes(app, metrics)
    return app


async def start(metrics: Metrics) -> web.AppRunner:
    runner = web.AppRunner(build_app(metrics), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", config.DASHBOARD_PORT).start()  # never all interfaces
    log.info("🖥 App status: open http://127.0.0.1:%s on this Mac", config.DASHBOARD_PORT)
    asyncio.get_running_loop().create_task(_self_check(metrics))
    return runner


async def _self_check(metrics: Metrics):
    problems = await asyncio.to_thread(metrics.check)
    for p in problems:
        log.error("🖥 App status query rejected by BigQuery: %s", p)
    if not problems:
        log.info("🖥 App status queries checked by BigQuery: all OK")


def add_routes(app: web.Application, metrics: Metrics):
    async def page(request: web.Request) -> web.Response:
        return web.Response(text=PAGE.read_text(encoding="utf-8"), content_type="text/html",
                            headers=SECURITY_HEADERS)

    async def api(request: web.Request) -> web.Response:
        try:
            days = int(request.query.get("days", "7"))
        except ValueError:
            days = 7
        days = days if days in PERIODS else 7
        data = await metrics.collect(days, fresh=request.query.get("fresh") == "1")
        return web.Response(text=json.dumps(data, default=_json_default), content_type="application/json",
                            headers=SECURITY_HEADERS)

    for path in ("/", "/dashboard", "/dashboard/", "/status"):
        app.router.add_get(path, page)
    app.router.add_get("/api/metrics", api)
