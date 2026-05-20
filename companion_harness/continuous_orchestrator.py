"""ContinuousOrchestrator — turn-free continuous companion loop (PR1).

Consumes audio_in continuously via the foreground model's stream_chunks()
adapter, emits per-chunk audit events, and applies a placeholder always-silence
policy hook. No TTS is dispatched in PR1.

Adapter-first (CLAUDE.md): this module imports no model SDK and never touches
_duplex / streaming_prefill / streaming_generate. All per-chunk mechanics live
inside the ForegroundModel adapter's stream_chunks() generator.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from companion_harness.schemas import Event

if TYPE_CHECKING:
    from companion_harness.event_logger import EventLogger

__all__ = ["ContinuousOrchestrator"]

_SOURCE = "continuous_orchestrator"
_SCHEMA_VERSION = "0.1"


class _ForegroundModelProtocol(Protocol):
    async def stream_chunks(
        self,
        audio_in: "asyncio.Queue[tuple[bytes, str]]",
    ) -> AsyncGenerator[tuple[bool, str, int | None, str], None]:
        ...


class ContinuousOrchestrator:
    """Flat continuous loop: audio_in → per-chunk model call → per-chunk event.

    Constructor args mirror StreamingRealtimeOrchestrator for the shared
    session_id / logger / audio_in contract so the eval feeder path is
    identical.
    """

    def __init__(
        self,
        *,
        session_id: str,
        logger: "EventLogger",
        audio_in: "asyncio.Queue[tuple[bytes, str]]",
        foreground_model: _ForegroundModelProtocol,
    ) -> None:
        self._session_id = session_id
        self._logger = logger
        self._audio_in = audio_in
        self._foreground = foreground_model
        self._seq = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Consume stream_chunks until the feeder pushes the sentinel (b"", "")."""
        prior_kv_len: int | None = None

        async for is_listen, text, audio_kv_len, caused_by_evt_id in self._foreground.stream_chunks(self._audio_in):
            self._emit_chunk_processed(
                is_listen=is_listen,
                audio_kv_len=audio_kv_len,
                n_chars=len(text),
                caused_by_evt_id=caused_by_evt_id,
            )

            if (
                audio_kv_len is not None
                and prior_kv_len is not None
                and audio_kv_len < prior_kv_len
            ):
                self._emit_audio_kv_reset(
                    prior_len=prior_kv_len,
                    new_len=audio_kv_len,
                    caused_by_evt_id=caused_by_evt_id,
                )

            if audio_kv_len is not None:
                prior_kv_len = audio_kv_len

            self._policy_hook(is_listen=is_listen, caused_by_evt_id=caused_by_evt_id)

    # ------------------------------------------------------------------
    # Placeholder policy hook (PR2 replaces this with decide_chunk)
    # ------------------------------------------------------------------

    def _policy_hook(self, *, is_listen: bool, caused_by_evt_id: str) -> None:
        pass  # PR2 replaces with decide_chunk

    # ------------------------------------------------------------------
    # Event emission helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit_chunk_processed(
        self,
        *,
        is_listen: bool,
        audio_kv_len: "int | None",
        n_chars: int,
        caused_by_evt_id: str,
    ) -> None:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-cco-{seq}-{now_ms}"
        payload_inline = {
            "is_listen": is_listen,
            "audio_kv_len": audio_kv_len,
            "n_chars": n_chars,
        }
        payload_hash = hashlib.sha256(
            json.dumps(payload_inline, sort_keys=True).encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type="continuous_chunk_processed",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=[caused_by_evt_id],
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline=payload_inline,
        )
        self._logger.log(evt)

    def _emit_audio_kv_reset(
        self,
        *,
        prior_len: int,
        new_len: int,
        caused_by_evt_id: str,
    ) -> None:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-kvreset-{seq}-{now_ms}"
        kv_payload = {"prior_len": prior_len, "new_len": new_len}
        payload_hash = hashlib.sha256(
            json.dumps(kv_payload, sort_keys=True).encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type="audio_kv_reset",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=[caused_by_evt_id],
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline=kv_payload,
        )
        self._logger.log(evt)
