"""📊 Monitoring dashboard: a web page with the bot's key metrics, for the owner only.

    GET /dashboard                  the page (dashboard.html); asks you to sign in from the bot if you aren't
    GET /dashboard/login?t=<token>  one-time sign-in link, sent by the bot's 📊 Dashboard button
    GET /dashboard/api/metrics?days=7   everything the page shows, as JSON
    GET /dashboard/logout

Mac only: it has its own tiny server on 127.0.0.1:DASHBOARD_PORT (8788), separate from the Action
Button upload server that the Tailscale tunnel forwards (8787). Requests whose Host isn't this Mac's
own address are refused, and so are proxied ones. Sign-in: the bot sends the owner a link that
works once, within 10 minutes; opening it sets a 30-day cookie. "Sign out everywhere" in the bot
rotates the signing key, which ends every session and every unused link.

Numbers come from BigQuery (log_interactions, dim_users, dim_spend_variants; never the per-user fct_expenses_* tables,
because BigQuery bills at least 10 MB per table referenced) and
from the bot itself (limits, blocks, households, queues). Each section is queried separately and
cached for 5 minutes, so one failing query never blanks the page and an open page stays well
inside BigQuery's free tier (about 90 MB per refresh, whatever the number of users).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
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

LOGIN_TTL = 600  # seconds a sign-in link stays valid
SESSION_TTL = 30 * 24 * 3600
COOKIE = "expbot_dash"
PERIODS = (1, 7, 30, 90)
CACHE_SECONDS = 300  # BigQuery bills ≥ 10 MB per table per query, so the page refreshes every 5 min

# outcome groups in log_interactions
AI_OUTCOMES = ("proposed", "no_expense", "parse_error", "empty_audio")  # the message reached the AI
SAVED_OUTCOMES = ("saved", "saved_corrected", "auto_saved")
ERROR_OUTCOMES = ("error", "parse_error", "save_error", "report_error", "undo_error", "reload_error", "delete_error")
INPUT_EVENTS = ("text", "voice", "upload_audio", "upload_text")


def _sql_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


# --------------------------------------------------------------------------- #
# Sign-in                                                                     #
# --------------------------------------------------------------------------- #


class Auth:
    """HMAC-signed tokens. The key lives in pending.sqlite3 and is rotated to sign everyone out."""

    def __init__(self, db):
        self.db = db
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self.db.commit()
        self._used: dict[str, float] = {}  # login nonces already used -> expiry

    def _key(self) -> bytes:
        row = self.db.execute("SELECT v FROM kv WHERE k = 'dashboard_key'").fetchone()
        if row:
            return row[0].encode()
        return self.rotate().encode()

    def rotate(self) -> str:
        key = secrets.token_urlsafe(32)
        self.db.execute("INSERT OR REPLACE INTO kv VALUES ('dashboard_key', ?)", (key,))
        self.db.commit()
        return key

    def _sign(self, payload: str) -> str:
        sig = hmac.new(self._key(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def _check(self, token: str, kind: str) -> list[str] | None:
        payload, _, sig = (token or "").rpartition(".")
        good = hmac.new(self._key(), payload.encode(), hashlib.sha256).hexdigest()
        if not payload or not hmac.compare_digest(sig, good):
            return None
        parts = payload.split(":")
        if parts[0] != kind or len(parts) < 3 or not parts[1].isdigit() or int(parts[1]) < time.time():
            return None
        return parts

    def login_token(self) -> str:
        return self._sign(f"login:{int(time.time()) + LOGIN_TTL}:{secrets.token_urlsafe(8)}")

    def use_login(self, token: str) -> bool:
        parts = self._check(token, "login")
        now = time.time()
        self._used = {n: exp for n, exp in self._used.items() if exp > now}
        if parts is None or parts[2] in self._used:
            return False
        self._used[parts[2]] = int(parts[1])  # each link works once
        return True

    def session_token(self) -> str:
        return self._sign(f"session:{int(time.time()) + SESSION_TTL}:{secrets.token_urlsafe(8)}")

    def valid_session(self, token: str | None) -> bool:
        return self._check(token or "", "session") is not None


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

    def _q(self, sql: str, **params) -> list[dict]:
        types = {int: "INT64", str: "STRING", datetime: "TIMESTAMP"}
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter(k, types[type(v)], v) for k, v in params.items()
        ])
        try:
            return [dict(r.items()) for r in self.client.query(sql, job_config=job_config).result()]
        except NotFound:
            return []

    # ---- sections ----------------------------------------------------------- #

    def kpis(self, days: int) -> dict:
        rows = self._q(f"""
        SELECT
          event_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY) AS current,
          COUNT(DISTINCT IF(event_type != 'auto_save', user_id, NULL)) AS active_users,
          COUNTIF(event_type IN ({_sql_list(INPUT_EVENTS)}) AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS ai_messages,
          COUNTIF(outcome = 'proposed') AS proposals_msgs,
          COUNTIF(outcome = 'saved') AS saved_as_suggested,
          COUNTIF(outcome = 'saved_corrected') AS saved_corrected,
          COUNTIF(outcome = 'auto_saved') AS auto_saved,
          COUNTIF(outcome = 'discarded') AS discarded,
          COUNTIF(outcome IN ({_sql_list(ERROR_OUTCOMES)})) AS errors,
          COUNT(*) AS events,
          COUNTIF(outcome IN ('daily_limit', 'voice_too_long')) AS limits_hit,
          COUNTIF(outcome = 'blocked') AS blocked_attempts,
          APPROX_QUANTILES(IF(outcome = 'proposed', latency_ms, NULL), 100)[SAFE_OFFSET(50)] AS p50_ms,
          APPROX_QUANTILES(IF(outcome = 'proposed', latency_ms, NULL), 100)[SAFE_OFFSET(95)] AS p95_ms
        FROM `{self.log_table}`
        WHERE event_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days2 DAY)
        GROUP BY current
        """, days=days, days2=2 * days)
        new = self._q(f"""
        SELECT
          COUNTIF(first_seen_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)) AS cur,
          COUNTIF(first_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
                  AND first_seen_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days2 DAY)) AS prev,
          COUNT(*) AS total
        FROM `{self.ds}.dim_users`
        """, days=days, days2=2 * days)
        by = {bool(r.pop("current")): r for r in rows}
        empty = {k: 0 for k in ("active_users", "ai_messages", "saved_as_suggested", "saved_corrected", "auto_saved",
                                "discarded", "errors", "events", "limits_hit", "blocked_attempts", "proposals_msgs")}
        cur, prev = {**empty, **by.get(True, {})}, {**empty, **by.get(False, {})}
        n = new[0] if new else {"cur": 0, "prev": 0, "total": 0}
        cur["new_users"], prev["new_users"], cur["total_users"] = n["cur"], n["prev"], n["total"]
        return {"current": cur, "previous": prev}

    def series(self, days: int) -> dict:
        """Per hour for 24 h, per day otherwise (in TIMEZONE)."""
        tz = str(config.TIMEZONE)  # an IANA name from ZoneInfo, e.g. Asia/Almaty: safe as a literal
        hourly = days <= 1
        bucket = f"TIMESTAMP_TRUNC(event_ts, HOUR, '{tz}')" if hourly else f"TIMESTAMP(DATE(event_ts, '{tz}'), '{tz}')"
        rows = self._q(f"""
        SELECT {bucket} AS bucket,
          COUNT(DISTINCT IF(event_type != 'auto_save', user_id, NULL)) AS active_users,
          COUNTIF(event_type = 'text' AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS text,
          COUNTIF(event_type = 'voice' AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS voice,
          COUNTIF(event_type IN ('upload_audio', 'upload_text') AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS upload,
          COUNTIF(outcome IN ({_sql_list(SAVED_OUTCOMES)})) AS saved,
          COUNTIF(outcome IN ({_sql_list(ERROR_OUTCOMES)})) AS errors
        FROM `{self.log_table}`
        WHERE event_ts >= @start
        GROUP BY bucket ORDER BY bucket
        """, start=self._start(days, hourly))
        got = {r["bucket"].astimezone(config.TIMEZONE).replace(tzinfo=None): r for r in rows}
        out = []
        t = self._start(days, hourly).astimezone(config.TIMEZONE).replace(tzinfo=None)
        step = timedelta(hours=1) if hourly else timedelta(days=1)
        end = datetime.now(config.TIMEZONE).replace(tzinfo=None)
        while t <= end:
            r = got.get(t, {})
            out.append({"t": t.isoformat(), **{k: r.get(k, 0) for k in
                        ("active_users", "text", "voice", "upload", "saved", "errors")}})
            t += step
        return {"unit": "hour" if hourly else "day", "points": out}

    @staticmethod
    def _start(days: int, hourly: bool) -> datetime:
        now = datetime.now(config.TIMEZONE)
        if hourly:
            return (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)
        return (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    def categories(self, days: int) -> list[dict]:
        # From the log, not fct_expenses_*: the wildcard would bill 10 MB per user table on every refresh.
        return self._q(f"""
        SELECT category, COUNT(*) AS saved,
               COUNTIF(outcome = 'saved_corrected') AS corrected,
               COUNTIF(outcome = 'auto_saved') AS auto_saved,
               COUNTIF(suggestion_source = 'dictionary') AS from_dictionary,
               COUNT(DISTINCT user_id) AS users
        FROM `{self.log_table}`
        WHERE event_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
          AND outcome IN ({_sql_list(SAVED_OUTCOMES)}) AND category IS NOT NULL
        GROUP BY category ORDER BY saved DESC
        """, days=days)

    def latency(self, days: int) -> list[dict]:
        return self._q(f"""
        SELECT event_type, COUNT(*) AS n,
               APPROX_QUANTILES(latency_ms, 100)[SAFE_OFFSET(50)] AS p50_ms,
               APPROX_QUANTILES(latency_ms, 100)[SAFE_OFFSET(95)] AS p95_ms,
               MAX(latency_ms) AS max_ms
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
            COUNTIF(event_type != 'auto_save') AS actions,
            COUNTIF(event_type IN ({_sql_list(INPUT_EVENTS)}) AND outcome IN ({_sql_list(AI_OUTCOMES)})) AS ai_messages,
            COUNTIF(outcome IN ({_sql_list(SAVED_OUTCOMES)})) AS saved,
            COUNTIF(outcome IN ({_sql_list(ERROR_OUTCOMES)})) AS errors,
            MAX(IF(event_type != 'auto_save', event_ts, NULL)) AS last_seen
          FROM `{self.log_table}`
          WHERE user_id IS NOT NULL AND event_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
          GROUP BY user_id
        )
        SELECT a.user_id,
          COALESCE(NULLIF(TRIM(CONCAT(IFNULL(u.first_name, ''), ' ', IFNULL(u.last_name, ''))), ''), a.log_name) AS name,
          COALESCE(u.username, a.log_username) AS username, u.bot_language AS language, u.first_seen_at,
          a.actions, a.ai_messages, a.saved, a.errors, a.last_seen
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

    # ---- all together -------------------------------------------------------- #

    async def collect(self, days: int) -> dict:
        async with self._lock:
            hit = self._cache.get(days)
            if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
                data = dict(hit[1])
            else:
                names = ("kpis", "series", "categories", "latency", "users", "errors", "dictionary")
                results = await asyncio.gather(
                    *(asyncio.to_thread(getattr(self, n), days) for n in names), return_exceptions=True
                )
                data = {}
                for name, res in zip(names, results):
                    if isinstance(res, Exception):
                        log.warning("Dashboard section %s failed: %s", name, res)
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

SIGNED_OUT = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Expense bot · sign in</title>
<style>body{font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;display:grid;place-items:center;
min-height:100vh;background:#f9f9f7;color:#0b0b0b}main{max-width:26rem;padding:24px}
@media (prefers-color-scheme:dark){body{background:#0d0d0d;color:#fff}}p{color:#898781}</style></head>
<body><main><h1>📊 Expense bot</h1><p>__MSG__</p></main></body></html>"""


def local_only(port: int):
    """Only this Mac: the Host must be its own loopback address (stops DNS-rebinding pages in the
    browser) and nothing may have forwarded the request (a tunnel or proxy adds these headers)."""
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    proxied = ("X-Forwarded-For", "Forwarded", "X-Forwarded-Host", "Tailscale-User-Login", "Tailscale-Funnel-Request")

    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.host not in allowed or any(h in request.headers for h in proxied):
            log.warning("Dashboard: refused a request for host %r from %s", request.host, request.remote)
            return web.Response(status=403, text="The dashboard only opens on the Mac running the bot.")
        return await handler(request)

    return middleware


def build_app(auth: Auth, metrics: Metrics, port: int | None = None) -> web.Application:
    app = web.Application(middlewares=[local_only(port or config.DASHBOARD_PORT)])
    add_routes(app, auth, metrics)
    return app


async def start(auth: Auth, metrics: Metrics) -> web.AppRunner:
    runner = web.AppRunner(build_app(auth, metrics), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", config.DASHBOARD_PORT).start()  # never all interfaces
    log.info("Dashboard on http://127.0.0.1:%s/dashboard (this Mac only)", config.DASHBOARD_PORT)
    return runner


def add_routes(app: web.Application, auth: Auth, metrics: Metrics):
    def secure(request: web.Request) -> bool:
        return request.secure

    def signed_out(msg: str, status: int = 401) -> web.Response:
        return web.Response(text=SIGNED_OUT.replace("__MSG__", msg), content_type="text/html",
                            status=status, headers=SECURITY_HEADERS)

    async def login(request: web.Request) -> web.Response:
        if not auth.use_login(request.query.get("t", "")):
            log.warning("Dashboard: rejected sign-in link from %s", request.remote)
            return signed_out("This sign-in link has expired or was already used. "
                              "Tap 📊 Dashboard in the bot for a new one.")
        resp = web.HTTPFound("/dashboard", headers=SECURITY_HEADERS)
        resp.set_cookie(COOKIE, auth.session_token(), max_age=SESSION_TTL, httponly=True,
                        samesite="Strict", secure=secure(request), path="/dashboard")
        log.info("Dashboard: signed in from %s", request.remote)
        return resp

    async def logout(request: web.Request) -> web.Response:
        resp = signed_out("Signed out. Tap 📊 Dashboard in the bot to sign in again.", status=200)
        resp.del_cookie(COOKIE, path="/dashboard")
        return resp

    async def page(request: web.Request) -> web.Response:
        if not auth.valid_session(request.cookies.get(COOKIE)):
            return signed_out("Open the bot and tap <b>▶️ Start → 📊 Dashboard</b> to get a sign-in link.")
        return web.Response(text=PAGE.read_text(encoding="utf-8"), content_type="text/html",
                            headers=SECURITY_HEADERS)

    async def api(request: web.Request) -> web.Response:
        if not auth.valid_session(request.cookies.get(COOKIE)):
            return web.json_response({"error": "signed out"}, status=401, headers=SECURITY_HEADERS)
        try:
            days = int(request.query.get("days", "7"))
        except ValueError:
            days = 7
        days = days if days in PERIODS else 7
        data = await metrics.collect(days)
        return web.Response(text=json.dumps(data, default=_json_default), content_type="application/json",
                            headers=SECURITY_HEADERS)

    app.router.add_get("/dashboard", page)
    app.router.add_get("/dashboard/", page)
    app.router.add_get("/dashboard/login", login)
    app.router.add_get("/dashboard/logout", logout)
    app.router.add_get("/dashboard/api/metrics", api)
