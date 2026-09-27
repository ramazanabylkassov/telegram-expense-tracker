"""Interaction log: one row per update the bot receives, written to BigQuery `log_interactions`.

Usage in bot.py:
    @logged("text")                      # wraps a handler; always emits exactly one row
    async def handle_text(update, context): ...
        note(outcome="proposed", expenses_found=2)   # handlers enrich the current row

Rows are buffered and streamed to BigQuery in the background every few seconds,
so logging never slows down a reply. If BigQuery is unreachable, rows are
retried, and after that appended to a local JSONL file so nothing is lost.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import json
import logging
import secrets
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import bigquery

import config

log = logging.getLogger(__name__)

LOG_SCHEMA = [
    bigquery.SchemaField("event_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("event_ts", "TIMESTAMP", mode="REQUIRED"),  # when the user acted (Telegram time)
    bigquery.SchemaField("logged_at", "TIMESTAMP", mode="REQUIRED"),  # when the bot finished handling it
    bigquery.SchemaField("update_id", "INT64"),
    bigquery.SchemaField("user_id", "INT64"),
    bigquery.SchemaField("username", "STRING"),
    bigquery.SchemaField("chat_id", "INT64"),
    bigquery.SchemaField("chat_type", "STRING"),  # private | group | supergroup
    bigquery.SchemaField("message_id", "INT64"),
    bigquery.SchemaField("event_type", "STRING"),  # text | voice | command | button | other
    bigquery.SchemaField("command", "STRING"),  # /today, /undo, ...
    bigquery.SchemaField("button_action", "STRING"),  # ok | ed | set | no | bk
    bigquery.SchemaField("input_text", "STRING"),  # message text, command args or button data
    bigquery.SchemaField("voice_duration_s", "INT64"),
    bigquery.SchemaField("transcript", "STRING"),
    bigquery.SchemaField("outcome", "STRING"),  # proposed | no_expense | saved | discarded | denied | error | ...
    bigquery.SchemaField("expenses_found", "INT64"),
    bigquery.SchemaField("pending_ids", "STRING", mode="REPEATED"),
    bigquery.SchemaField("expense_id", "STRING"),  # -> fct_expenses_<user_id>.expense_id
    bigquery.SchemaField("category", "STRING"),
    bigquery.SchemaField("suggestion_source", "STRING"),  # dictionary | ai
    bigquery.SchemaField("llm_provider", "STRING"),
    bigquery.SchemaField("llm_model", "STRING"),
    bigquery.SchemaField("latency_ms", "INT64"),
    bigquery.SchemaField("error", "STRING"),
    bigquery.SchemaField("details", "JSON"),  # anything else worth keeping
]

_FIELDS = {f.name for f in LOG_SCHEMA}
_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("interaction", default=None)


def _model_name() -> str:
    return {
        "gemini": config.GEMINI_MODEL,
        "claude": config.CLAUDE_MODEL,
        "openai": config.OPENAI_MODEL,
    }[config.LLM_PROVIDER]


def _ts(dt: datetime | None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def start_record(update, event_type: str) -> dict:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    rec: dict = {
        "event_id": secrets.token_hex(8),
        "event_ts": _ts(getattr(msg, "date", None) if update.callback_query is None else None),
        "update_id": update.update_id,
        "user_id": user.id if user else None,
        "username": user.username if user else None,
        "chat_id": chat.id if chat else None,
        "chat_type": chat.type if chat else None,
        "message_id": msg.message_id if msg else None,
        "event_type": event_type,
        "llm_provider": config.LLM_PROVIDER,
        "llm_model": _model_name(),
        "details": {},
    }
    if user is not None:
        name = " ".join(p for p in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if isinstance(p, str))
        if name:
            rec["details"]["user_name"] = name
    if update.callback_query is not None:
        data = update.callback_query.data or ""
        rec["input_text"] = data
        rec["button_action"] = data.split(":", 1)[0]
    elif msg is not None:
        if msg.voice or msg.audio:
            rec["voice_duration_s"] = (msg.voice or msg.audio).duration
        text = msg.text or msg.caption
        if text:
            rec["input_text"] = text
            if event_type == "command":
                rec["command"] = text.split()[0].split("@")[0]
        if event_type == "other":
            kinds = [k for k in ("photo", "sticker", "document", "video", "location", "contact") if getattr(msg, k, None)]
            rec["details"]["message_kind"] = kinds[0] if kinds else "unknown"
    return rec


def note(**fields):
    """Add facts to the interaction currently being handled. Unknown keys go to `details`."""
    rec = _current.get()
    if rec is None:
        return
    for k, v in fields.items():
        if k in _FIELDS and k != "details":
            rec[k] = v
        else:
            rec["details"][k] = v


class InteractionLogger:
    def __init__(
        self,
        table_id: str,
        client: bigquery.Client,
        fallback_path: str = config.INTERACTION_LOG_FALLBACK,
        max_attempts: int = 60,  # x 5 s flushes ≈ 5 min: covers BigQuery's delay on brand-new tables
    ):
        self.max_attempts = max_attempts
        self.table_id = table_id
        self.client = client
        self.fallback = Path(fallback_path)
        self.buffer: list[dict] = []
        self.attempts: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    def ensure_table(self):
        table = bigquery.Table(self.table_id, schema=LOG_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(field="event_ts")
        table.clustering_fields = ["user_id", "event_type", "outcome"]
        self.client.create_table(table, exists_ok=True)

    def emit(self, rec: dict):
        rec["logged_at"] = _ts(None)
        row = {k: v for k, v in rec.items() if k in _FIELDS and v is not None}
        row["details"] = json.dumps(rec.get("details") or {}, ensure_ascii=False, default=str)
        self.buffer.append(row)
        if len(self.buffer) >= config.INTERACTION_LOG_BATCH:
            self._kick()

    def _kick(self):
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self.flush())

    async def flush(self):
        if not self.buffer:
            return
        rows, self.buffer = self.buffer, []
        try:
            errors = await asyncio.to_thread(
                self.client.insert_rows_json, self.table_id, rows, row_ids=[r["event_id"] for r in rows]
            )
        except Exception as e:  # network, table not yet visible, ...
            errors = [{"index": i, "errors": [str(e)]} for i in range(len(rows))]
        failed = [rows[e["index"]] for e in errors] if errors else []
        for row in failed:
            n = self.attempts.get(row["event_id"], 0) + 1
            if n < self.max_attempts:
                self.attempts[row["event_id"]] = n
                self.buffer.append(row)  # retry on the next flush
            else:
                self.attempts.pop(row["event_id"], None)
                self._to_fallback(row, errors)
        for row in rows:
            if row not in failed:
                self.attempts.pop(row["event_id"], None)
        if failed:
            log.warning("Interaction log: %d row(s) not written yet: %s", len(failed), errors[:1])

    def drop_user(self, user_id: int):
        """"Delete my data": forget this person's rows that haven't reached BigQuery yet."""
        self.buffer = [r for r in self.buffer if r.get("user_id") != user_id]
        if self.fallback.exists():
            kept = []
            for line in self.fallback.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("user_id") == user_id:
                        continue
                except ValueError:
                    pass
                kept.append(line)
            self.fallback.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    def _to_fallback(self, row: dict, errors):
        with self.fallback.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        log.error("Interaction row %s written to %s after repeated failures", row["event_id"], self.fallback)

    async def run(self):
        while True:
            await asyncio.sleep(config.INTERACTION_LOG_FLUSH_SECONDS)
            try:
                await self.flush()
            except Exception:
                log.exception("Interaction log flush failed")

    async def close(self):
        await self.flush()
        for row in self.buffer:  # still failing at shutdown: keep them locally
            self._to_fallback(row, None)
        self.buffer = []


_logger: InteractionLogger | None = None


def set_logger(logger: InteractionLogger):
    global _logger
    _logger = logger


@contextlib.asynccontextmanager
async def record(event_type: str, user_id: int | None, chat_id: int | None = None, **fields):
    """Log something that isn't a Telegram update (e.g. an Action Button upload): exactly one row,
    and note() works inside it just like inside a @logged handler."""
    rec: dict = {
        "event_id": secrets.token_hex(8),
        "event_ts": _ts(None),
        "user_id": user_id,
        "chat_id": chat_id,
        "chat_type": "upload",
        "event_type": event_type,
        "llm_provider": config.LLM_PROVIDER,
        "llm_model": _model_name(),
        "details": {},
        **fields,
    }
    token = _current.set(rec)
    t0 = time.perf_counter()
    try:
        yield rec
    except Exception as e:
        rec.setdefault("outcome", "error")  # keep a more specific outcome (e.g. parse_error) if one was noted
        rec.setdefault("error", f"{type(e).__name__}: {e}"[:1000])
        rec["details"]["traceback"] = traceback.format_exc()[-4000:]
        raise
    finally:
        rec["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        rec.setdefault("outcome", "handled")
        _current.reset(token)
        if _logger is not None:
            try:
                _logger.emit(rec)
            except Exception:
                log.exception("Could not queue interaction log row")


def logged(event_type: str):
    """Decorator: exactly one log row per handled update, even if the handler crashes."""

    def wrap(handler):
        @functools.wraps(handler)
        async def inner(update, context):
            rec = start_record(update, event_type)
            token = _current.set(rec)
            t0 = time.perf_counter()
            try:
                return await handler(update, context)
            except Exception as e:
                rec["outcome"] = "error"
                rec["error"] = f"{type(e).__name__}: {e}"[:1000]
                rec["details"]["traceback"] = traceback.format_exc()[-4000:]
                raise
            finally:
                rec["latency_ms"] = int((time.perf_counter() - t0) * 1000)
                rec.setdefault("outcome", "handled")
                _current.reset(token)
                skip = rec["details"].get("_skip_log")  # e.g. the "delete my data" confirmation itself
                if _logger is not None and not skip:
                    try:
                        _logger.emit(rec)
                    except Exception:
                        log.exception("Could not queue interaction log row")

        return inner

    return wrap
