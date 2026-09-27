"""Upload endpoint for the iPhone Action Button (or any Shortcut / script).

Why it exists: a Shortcut that posts a recording through the Telegram Bot API makes the *bot*
the sender, and Telegram never delivers a bot's own messages back to it. So recordings come
here instead, straight to the bot, and the ✅/✏️/🗑 proposal still arrives in your chat.

    POST /ingest
    Authorization: Bearer <key>   (INGEST_SECRET for the owner, or a personal key from the bot's
                                   📲 Action Button menu; the key decides whose chat it goes to)
    Body, any of:
      - multipart/form-data with a "file" field (audio: m4a, mp3, ogg, wav…) and/or a "text" field
      - raw audio with Content-Type: audio/*
      - JSON {"text": "coffee 1500"}  or  text/plain

    200 {"ok": true, "expenses": 2, "transcript": "…"}
    401 wrong/missing key · 413 too large · 415 nothing usable in the body · 500 processing failed

    GET /health -> 200 "ok" (no secret needed; for checking the tunnel)

Runs inside the bot process on INGEST_HOST:INGEST_PORT (default 127.0.0.1:8787). Expose it to
the phone with a tunnel such as Tailscale Funnel; see README.
"""
from __future__ import annotations

import hmac
import logging
from typing import Awaitable, Callable

from aiohttp import web

import config

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024  # 20 MB is ~20 minutes of m4a; spending notes are seconds long

# (user id, audio bytes or None, audio mime type, text or None) -> response fields
Handler = Callable[[int, bytes | None, str, str | None], Awaitable[dict]]
# key -> Telegram user id, or None if the key is unknown
Resolver = Callable[[str], int | None]


def _given_key(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    return (header[7:] if header.lower().startswith("bearer ") else request.headers.get("X-Api-Key", "")).strip()


def owner_key_matches(given: str) -> bool:
    secret = config.INGEST_SECRET or ""
    return bool(secret) and bool(given) and hmac.compare_digest(given.encode(), secret.encode())


def _audio_mime(content_type: str | None, filename: str | None) -> str:
    """Shortcuts often label recordings vaguely; the file name is the better hint."""
    by_ext = {"m4a": "audio/mp4", "mp4": "audio/mp4", "aac": "audio/aac", "mp3": "audio/mpeg",
              "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg", "wav": "audio/wav", "caf": "audio/x-caf"}
    ext = (filename or "").rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
    if ext in by_ext:
        return by_ext[ext]
    if content_type and content_type.startswith("audio/"):
        return content_type
    return "audio/mp4"  # iPhone recordings are m4a


async def _read_body(request: web.Request) -> tuple[bytes | None, str, str | None]:
    ctype = request.content_type or ""
    if ctype.startswith("multipart/"):
        audio, mime, text = None, "audio/mp4", None
        reader = await request.multipart()
        async for part in reader:
            if part.name == "text":
                text = (await part.text()).strip() or None
            elif part.name in ("file", "audio", "recording") or part.filename:
                audio = bytes(await part.read(decode=False))
                mime = _audio_mime(part.headers.get("Content-Type"), part.filename)
        return audio, mime, text
    if ctype.startswith("audio/") or ctype == "application/octet-stream":
        return await request.read(), _audio_mime(ctype, request.query.get("filename")), None
    if ctype == "application/json":
        data = await request.json()
        return None, "", (str(data.get("text") or "").strip() or None)
    if ctype.startswith("text/"):
        return None, "", ((await request.text()).strip() or None)
    return None, "", None


def build_app(handler: Handler, resolve: Resolver) -> web.Application:
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def ingest(request: web.Request) -> web.Response:
        user_id = resolve(_given_key(request))
        if user_id is None:
            log.warning("Ingest: rejected request from %s (unknown key)", request.remote)
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            audio, mime, text = await _read_body(request)
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"ok": False, "error": "file too large (max 20 MB)"}, status=413)
        except Exception:
            log.exception("Ingest: could not read the request body")
            return web.json_response({"ok": False, "error": "could not read the request"}, status=400)
        if not audio and not text:
            return web.json_response(
                {"ok": False, "error": "send an audio file (field 'file') or text (field 'text')"}, status=415
            )
        try:
            result = await handler(user_id, audio or None, mime, text)
        except Exception as e:
            log.exception("Ingest: processing failed")
            return web.json_response({"ok": False, "error": f"{type(e).__name__}"}, status=500)
        return web.json_response({"ok": True, **result})

    app = web.Application(client_max_size=MAX_BYTES)
    app.router.add_get("/health", health)
    app.router.add_post("/ingest", ingest)
    return app


async def start(handler: Handler, resolve: Resolver) -> web.AppRunner:
    runner = web.AppRunner(build_app(handler, resolve), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, config.INGEST_HOST, config.INGEST_PORT).start()
    log.info("Upload endpoint listening on http://%s:%s/ingest", config.INGEST_HOST, config.INGEST_PORT)
    return runner
