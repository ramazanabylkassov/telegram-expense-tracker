"""Speech-to-text for providers that can't hear audio themselves (Claude, ChatGPT).

TRANSCRIBER=local  -> Whisper running on this machine (free, private; first run
                      downloads the model, ~500 MB for "small")
TRANSCRIBER=openai -> OpenAI's transcription API (paid, needs OPENAI_API_KEY)
"""
from __future__ import annotations

import asyncio
import io
import logging

import config

log = logging.getLogger(__name__)

# Vocabulary hints (numbers, shops, currencies), one per language. A hint in the wrong
# language pulls Whisper's language detection that way, so they are never mixed.
_HINTS = {
    "ru": "Расходы: кофе 1500 тенге, такси 2000, Magnum, Small, аптека 3500, продукты.",
    "en": "Expenses: coffee 1500 tenge, taxi 2000, Magnum, Small, pharmacy 3500, groceries.",
}
# Languages we tell Whisper outright. Anything else (e.g. English UI) is auto-detected,
# because people often speak Russian even with the English interface.
_FORCED = {"ru"}

_whisper = None


def _local_model():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        log.info("Loading Whisper model '%s' (first run downloads it)…", config.WHISPER_MODEL)
        _whisper = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper


def _transcribe_local(audio: bytes, language: str | None = None) -> str:
    # PyAV (bundled with faster-whisper) decodes Telegram's OGG/Opus directly; no ffmpeg needed.
    forced = language if language in _FORCED else None
    segments, info = _local_model().transcribe(
        io.BytesIO(audio),
        language=forced,  # None = auto-detect
        task="transcribe",  # never translate
        beam_size=5,
        vad_filter=True,
        initial_prompt=_HINTS.get(forced) if forced else None,
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    log.info("Whisper: language=%s (%s), %d chars", info.language, "forced" if forced else "detected", len(text))
    duration = getattr(info, "duration", None)
    if isinstance(duration, (int, float)):
        from interactions import note

        note(audio_seconds=round(float(duration), 1))  # how much audio this Mac transcribed
    return text


async def _transcribe_openai(audio: bytes, mime: str, language: str | None = None) -> str:
    from openai import AsyncOpenAI

    ext = "ogg" if "ogg" in mime else mime.split("/")[-1]
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    kwargs = {"language": language} if language in _FORCED else {}
    tr = await client.audio.transcriptions.create(
        model=config.OPENAI_TRANSCRIBE_MODEL, file=(f"voice.{ext}", audio, mime), **kwargs
    )
    return tr.text.strip()


async def transcribe(audio: bytes, mime: str = "audio/ogg", language: str | None = None) -> str:
    """`language` is the user's chosen bot language; "ru" makes the transcriber expect Russian."""
    if config.TRANSCRIBER == "openai":
        return await _transcribe_openai(audio, mime, language)
    return await asyncio.to_thread(_transcribe_local, audio, language)


def warm_up():
    """Load the local model at startup so the first voice note isn't slow."""
    if config.TRANSCRIBER == "local" and config.LLM_PROVIDER != "gemini":
        _local_model()
