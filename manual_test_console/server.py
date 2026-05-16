"""Manual-test console server — Phase 3 (live-loop pipeline wiring).

Single aiohttp process exposes five routes:
  GET  /                — static capture+display page (manual_test_console/index.html)
  GET  /healthz         — status / live-pipeline readiness
  GET  /ws/ingest       — WebSocket: browser → harness audio + video envelopes
  GET  /ws/display      — WebSocket: harness → browser event stream
  GET  /ws/audio_out    — WebSocket: harness → browser synthesized audio chunks

Each /ws/ingest connection constructs:
  - an InputIngest session (raw_audio_chunk / raw_video_frame events)
  - a LivePipeline (StreamingRealtimeOrchestrator + VAD/SmartTurn/Backchannel
    detectors + MiniCPM streaming foreground + SpeakPolicy + no-op TTS)

The ingest read loop calls InputIngest.ingest_chunk(...) (non-blocking — invariant
#10) and then pushes (pcm_bytes, raw_audio_event_id) onto the orchestrator's
audio_in queue. The orchestrator emits vad_frame / vad_turn_signal /
backchannel_classification / smart_turn_* / policy_decision / foreground_proposal
events; the existing DisplayBroker fans them to the display WS.

Voice-back is out of scope for this server (NoopTtsAdapter + no-op AudioSink).
See `live_pipeline.py` for the per-session factory and the substitute models
(EnergyVADModel, SilenceSmartTurnModel, ZeroBackchannelModel) that stand in
until production model wiring lands as follow-up PRs.

Run:
  /raid/yid042/venvs/companion-harness/bin/python3 -m manual_test_console.server \
      --host 0.0.0.0 --port 8800 --blob-dir /tmp/manual_test_blobs

The page is reachable from the developer's laptop via `ssh -L`:
  ssh -L 8800:localhost:8800 b200
  open http://localhost:8800/ in the browser
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import dataclasses
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from companion_harness.background_reasoner import FakeBackgroundReasoner, MCPBackgroundReasoner

from aiohttp import WSMsgType, web

from companion_harness.event_logger import EventLogger
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.schemas import Event
from manual_test_console.config_schema import (
    ALLOWLIST,
    HOT_SEAMS,
    tier_a_keys,
    validate_patch,
    validate_seam_patch,
)
from manual_test_console.config_store import ConfigChange, ConfigStore, SeamStateChange
from manual_test_console.live_pipeline import (
    LivePipeline,
    StreamingRawPipeline,
    build_live_pipeline,
    build_streaming_raw_pipeline,
)

__all__ = ["build_app", "main"]

# Per-display-WS queue depth. Drops oldest on overflow.
_DISPLAY_QUEUE_DEPTH = 256

# Event types that are high-rate frame signals — sampled before display fanout.
# The EventLogger ring still records every event (audit invariant #1).
_HIGH_RATE_DISPLAY_TYPES = frozenset({"raw_audio_chunk", "vad_frame"})

# Per-audio_out-WS queue depth. Drops oldest on overflow (invariant #10).
# Synthesized audio is bursty; 256 chunks ≈ several seconds of buffer.
_AUDIO_OUT_QUEUE_DEPTH = 256

# Sample rate / format that AudioOutputController -> sink chunks carry. This is
# advisory metadata for the browser's AudioContext decoder. The real TTS adapter
# (Kokoro, PR #127) produces PCM16 mono at 24 kHz; with NoopTtsAdapter no chunks
# ever fire so the value is unused.
_AUDIO_OUT_SAMPLE_RATE = 24000
_AUDIO_OUT_SAMPLE_FORMAT = "pcm_s16le"

# Module path for the static page.
_STATIC_DIR = Path(__file__).parent

# AppKeys (aiohttp 3.9+) — strongly-typed application state keys.
KEY_BROKER: web.AppKey[object] = web.AppKey("broker", object)
KEY_LOGGER: web.AppKey[object] = web.AppKey("logger", object)
KEY_INGEST: web.AppKey[object] = web.AppKey("ingest", object)
KEY_BLOB_DIR: web.AppKey[Path] = web.AppKey("blob_dir", Path)
KEY_CHUNK_COUNTER: web.AppKey[dict] = web.AppKey("chunk_counter", dict)
KEY_LIVE_PIPELINE_ENABLED: web.AppKey[bool] = web.AppKey("live_pipeline_enabled", bool)
KEY_FOREGROUND_MODEL: web.AppKey[object] = web.AppKey("foreground_model", object)
KEY_ACTIVE_PIPELINES: web.AppKey[dict] = web.AppKey("active_pipelines", dict)
KEY_USE_STUBS: web.AppKey[bool] = web.AppKey("use_stubs", bool)
KEY_VAD_MODEL: web.AppKey[object] = web.AppKey("vad_model", object)
KEY_SMART_TURN_MODEL: web.AppKey[object] = web.AppKey("smart_turn_model", object)
KEY_BACKCHANNEL_MODEL: web.AppKey[object] = web.AppKey("backchannel_model", object)
KEY_ASR_MODEL: web.AppKey[object] = web.AppKey("asr_model", object)
KEY_DETECTOR_LABELS: web.AppKey[dict] = web.AppKey("detector_labels", dict)
KEY_AUDIO_OUT_BROKER: web.AppKey[object] = web.AppKey("audio_out_broker", object)
KEY_AUDIO_OUT_COUNTER: web.AppKey[dict] = web.AppKey("audio_out_counter", dict)
KEY_TTS_ADAPTER: web.AppKey[object] = web.AppKey("tts_adapter", object)
KEY_TTS_LABEL: web.AppKey[str] = web.AppKey("tts_label", str)
KEY_VISION_ENABLED: web.AppKey[bool] = web.AppKey("vision_enabled", bool)
KEY_CONFIG_STORE: web.AppKey[object] = web.AppKey("config_store", object)
KEY_OPERATOR_SEQ: web.AppKey[dict] = web.AppKey("operator_seq", dict)
# Optional real-adapter handles. None means the corresponding null stub is in use.
# Wired via --enable-{clip-scene,grounding,av-conflict,deictic,urgency,embeddings}.
KEY_SCENE_SCORER: web.AppKey[object] = web.AppKey("scene_scorer", object)
KEY_GROUNDING_MODEL: web.AppKey[object] = web.AppKey("grounding_model", object)
KEY_AV_CONFLICT_SCORER: web.AppKey[object] = web.AppKey("av_conflict_scorer", object)
KEY_DEICTIC_MODEL: web.AppKey[object] = web.AppKey("deictic_model", object)
KEY_URGENCY_SCORER: web.AppKey[object] = web.AppKey("urgency_scorer", object)
KEY_EMBEDDER: web.AppKey[object] = web.AppKey("embedder", object)
# v0.2b: per-session diarization adapter factory (None = _NullDiarizationAdapter used by live_pipeline).
KEY_DIARIZATION_ADAPTER_FACTORY: web.AppKey[object] = web.AppKey("diarization_adapter_factory", object)
KEY_ADAPTER_LABELS: web.AppKey[dict] = web.AppKey("adapter_labels", dict)
# True when --minicpm-streaming-raw is set. Mutually exclusive with
# --minicpm-only and --use-stubs. DEMO MODE: bypasses SpeakPolicy + audit gates.
KEY_STREAMING_RAW_MODE: web.AppKey[bool] = web.AppKey("streaming_raw_mode", bool)
KEY_BACKGROUND_REASONER: web.AppKey[object] = web.AppKey("background_reasoner", object)
KEY_EVENT_RATE_COUNTER: web.AppKey[object] = web.AppKey("event_rate_counter", object)
KEY_START_TIME: web.AppKey[float] = web.AppKey("start_time", float)

# Session id stamped onto operator_action + config_change events emitted from
# the /config/* HTTP endpoints. These events are decoupled from any /ws/ingest
# session; they record control-plane mutations against the singleton
# ConfigStore. Per docs/design-config-and-dashboard.md §8.
_OPERATOR_SESSION_ID = "manual_test_console.operator"
_CONFIG_EVENT_SCHEMA_VERSION = "v0.1f"


def _event_to_json(event: Event) -> dict:
    """Serialize an Event to a JSON-safe dict for display."""
    return dataclasses.asdict(event)


def _now_wall() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _null_sink(_event: Event) -> None:
    """Default EventLogger sink — drops events after subscribers have seen them.

    The console does not persist the event store; subscribers (display WS) get
    every event live. Durability is a follow-up concern (storage backend).
    """
    return None


class _EventRateCounter:
    """Sliding-window event-rate counter for /healthz."""

    def __init__(self, window_seconds: int = 60) -> None:
        self._window_ms = window_seconds * 1000
        self._window_seconds = window_seconds
        self._timestamps: collections.deque = collections.deque()
        self._total = 0

    async def on_event(self, event: Event) -> None:
        # Trimming is lazy: stale timestamps survive until the next event arrives.
        now_ms = event.timestamp_mono_ms
        self._timestamps.append(now_ms)
        self._total += 1
        cutoff = now_ms - self._window_ms
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def rate_per_second(self) -> float:
        return len(self._timestamps) / self._window_seconds

    def total_count(self) -> int:
        return self._total


class _NullSceneScorer:
    """Returns 0.0 — scene-change scoring deferred to a follow-up issue."""

    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        return 0.0


class _NullGroundingModel:
    """Returns ('', 0.0) — deictic grounding deferred to a follow-up issue."""

    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return "", 0.0


# ---------------------------------------------------------------------------
# Display subscription machinery
# ---------------------------------------------------------------------------


class DisplayBroker:
    """Tracks connected display WebSockets and fans events out to each.

    Subscribed to EventLogger via callback. The callback runs on the logger's
    drain task and must not block — it does put_nowait per subscriber and
    drops on QueueFull (per-viewer backpressure isolation; the realtime
    ingest path is never blocked).

    High-rate frame events (raw_audio_chunk, vad_frame) are sampled before
    fanout — only every Nth event reaches subscribers. The EventLogger ring
    still records all events (audit invariant #1). High-signal events are
    always forwarded regardless of sampling rate.
    """

    def __init__(self, sampling_rate: int = 5) -> None:
        self._queues: list[asyncio.Queue[dict]] = []
        self._drops_by_queue: dict[int, int] = {}
        self._sampling_rate = max(1, sampling_rate)
        self._counters: dict[str, int] = {}  # per event_type seen count
        self._sampled_in: dict[str, int] = {}   # forwarded this window
        self._sampled_out: dict[str, int] = {}  # dropped-by-sampling this window
        self._window_start_ms: float = time.monotonic() * 1000

    def add(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=_DISPLAY_QUEUE_DEPTH)
        self._queues.append(q)
        self._drops_by_queue[id(q)] = 0
        return q

    def remove(self, q: asyncio.Queue[dict]) -> None:
        if q in self._queues:
            self._queues.remove(q)
        self._drops_by_queue.pop(id(q), None)

    def _should_forward(self, event_type: str) -> bool:
        if event_type not in _HIGH_RATE_DISPLAY_TYPES:
            return True
        n = self._counters.get(event_type, 0) + 1
        self._counters[event_type] = n
        forward = (n - 1) % self._sampling_rate == 0
        if forward:
            self._sampled_in[event_type] = self._sampled_in.get(event_type, 0) + 1
        else:
            self._sampled_out[event_type] = self._sampled_out.get(event_type, 0) + 1
        return forward

    def sampling_summary(self) -> dict:
        """Return and reset the per-window sampling counters."""
        now_ms = time.monotonic() * 1000
        elapsed_s = (now_ms - self._window_start_ms) / 1000
        summary = {
            "elapsed_s": elapsed_s,
            "forwarded": dict(self._sampled_in),
            "sampled_out": dict(self._sampled_out),
        }
        self._sampled_in.clear()
        self._sampled_out.clear()
        self._window_start_ms = now_ms
        return summary

    async def on_event(self, event: Event) -> None:
        if not self._should_forward(event.event_type):
            return
        payload = _event_to_json(event)
        msg = {"kind": "event", "event": payload}
        for q in list(self._queues):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self._drops_by_queue[id(q)] = self._drops_by_queue.get(id(q), 0) + 1


class AudioOutBroker:
    """Fan-out broker for synthesized audio chunks → /ws/audio_out listeners.

    Implements the AudioOutSinkTarget protocol from live_pipeline.py.
    publish() is called from WebSocketAudioSink on the orchestrator's event
    loop; it does put_nowait on each listener queue and drops the oldest entry
    on QueueFull so the realtime path is never blocked (invariant #10).
    """

    def __init__(self, counter: dict[str, int]) -> None:
        self._queues: list[asyncio.Queue[dict]] = []
        self._counter = counter

    def add(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=_AUDIO_OUT_QUEUE_DEPTH)
        self._queues.append(q)
        return q

    def remove(self, q: asyncio.Queue[dict]) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def publish(self, session_id: str, seq: int, chunk: bytes) -> None:
        self._counter["chunks_sent"] = self._counter.get("chunks_sent", 0) + 1
        msg = {
            "type": "audio_chunk",
            "session_id": session_id,
            "seq": seq,
            "pcm_bytes_b64": base64.b64encode(chunk).decode("ascii"),
            "sample_rate": _AUDIO_OUT_SAMPLE_RATE,
            "sample_format": _AUDIO_OUT_SAMPLE_FORMAT,
        }
        for q in list(self._queues):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # drop-oldest: discard head, then enqueue (best-effort)
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    self._counter["chunks_dropped"] = (
                        self._counter.get("chunks_dropped", 0) + 1
                    )


# ---------------------------------------------------------------------------
# /config event helpers
# ---------------------------------------------------------------------------


def _next_operator_seq(counter: dict) -> int:
    n = counter.get("seq", 0)
    counter["seq"] = n + 1
    return n


def _make_operator_action_event(
    *,
    endpoint: str,
    client_ip: str,
    request_id: str,
    seq_counter: dict,
) -> Event:
    """Build an `operator_action` audit event for a /config/* HTTP request.

    See docs/design-config-and-dashboard.md §8 row 1. The event is the root of
    an operator-initiated DAG chain — `caused_by=[]` is correct (per
    causal_graph.py, a root event has empty caused_by[]).
    """
    event_id = f"operator_action-{uuid.uuid4().hex}"
    now_ms = int(time.monotonic() * 1000)
    payload = {
        "endpoint": endpoint,
        "client_ip": client_ip,
        "request_id": request_id,
    }
    return Event(
        event_id=event_id,
        session_id=_OPERATOR_SESSION_ID,
        schema_version=_CONFIG_EVENT_SCHEMA_VERSION,
        seq_no=_next_operator_seq(seq_counter),
        event_type="operator_action",
        timestamp_mono_ms=now_ms,
        timestamp_wall=_now_wall(),
        source="manual_test_console.server",
        caused_by=[],
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="operator",
        sensitivity="safe",
        retention_policy_id="config_change_30d",
        payload_inline=payload,
    )


def _make_config_change_event(
    *,
    change: ConfigChange,
    operator_action_event_id: str,
    seq_counter: dict,
) -> Event:
    """Build a `config_change` event citing its upstream operator_action.

    See docs/design-config-and-dashboard.md §8 row 2. `caused_by` references
    the operator_action event id so the DAG closes (invariant #1).
    """
    event_id = f"config_change-{uuid.uuid4().hex}"
    now_ms = int(time.monotonic() * 1000)
    payload = {
        "key": change.key,
        "previous_value": change.previous_value,
        "new_value": change.new_value,
        "applied_at_ms": now_ms,
        "operator_action_event_id": operator_action_event_id,
    }
    return Event(
        event_id=event_id,
        session_id=_OPERATOR_SESSION_ID,
        schema_version=_CONFIG_EVENT_SCHEMA_VERSION,
        seq_no=_next_operator_seq(seq_counter),
        event_type="config_change",
        timestamp_mono_ms=now_ms,
        timestamp_wall=_now_wall(),
        source="manual_test_console.server",
        caused_by=[operator_action_event_id],
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="config_change_30d",
        payload_inline=payload,
    )


def _make_model_swap_requested_event(
    *,
    seam: str,
    from_enabled: bool,
    to_enabled: bool,
    operator_action_event_id: str,
    seq_counter: dict,
) -> Event:
    event_id = f"model_swap_requested-{uuid.uuid4().hex}"
    now_ms = int(time.monotonic() * 1000)
    payload = {
        "seam": seam,
        "from_enabled": from_enabled,
        "to_enabled": to_enabled,
        "requested_at_ms": now_ms,
        "operator_action_event_id": operator_action_event_id,
    }
    return Event(
        event_id=event_id,
        session_id=_OPERATOR_SESSION_ID,
        schema_version=_CONFIG_EVENT_SCHEMA_VERSION,
        seq_no=_next_operator_seq(seq_counter),
        event_type="model_swap_requested",
        timestamp_mono_ms=now_ms,
        timestamp_wall=_now_wall(),
        source="manual_test_console.server",
        caused_by=[operator_action_event_id],
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="config_change_30d",
        payload_inline=payload,
    )


def _make_model_swap_completed_event(
    *,
    seam: str,
    from_enabled: bool,
    to_enabled: bool,
    applied_at_ms: int,
    latency_ms: int,
    operator_action_event_id: str,
    seq_counter: dict,
) -> Event:
    event_id = f"model_swap_completed-{uuid.uuid4().hex}"
    payload = {
        "seam": seam,
        "from_enabled": from_enabled,
        "to_enabled": to_enabled,
        "applied_at_ms": applied_at_ms,
        "latency_ms": latency_ms,
        "operator_action_event_id": operator_action_event_id,
    }
    return Event(
        event_id=event_id,
        session_id=_OPERATOR_SESSION_ID,
        schema_version=_CONFIG_EVENT_SCHEMA_VERSION,
        seq_no=_next_operator_seq(seq_counter),
        event_type="model_swap_completed",
        timestamp_mono_ms=applied_at_ms,
        timestamp_wall=_now_wall(),
        source="manual_test_console.server",
        caused_by=[operator_action_event_id],
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="config_change_30d",
        payload_inline=payload,
    )


def _make_model_swap_rejected_event(
    *,
    seam: str,
    attempted_enabled: bool | None,
    reason: str,
    operator_action_event_id: str,
    seq_counter: dict,
) -> Event:
    event_id = f"model_swap_rejected-{uuid.uuid4().hex}"
    now_ms = int(time.monotonic() * 1000)
    payload = {
        "seam": seam,
        "attempted_enabled": attempted_enabled,
        "reason": reason,
        "operator_action_event_id": operator_action_event_id,
    }
    return Event(
        event_id=event_id,
        session_id=_OPERATOR_SESSION_ID,
        schema_version=_CONFIG_EVENT_SCHEMA_VERSION,
        seq_no=_next_operator_seq(seq_counter),
        event_type="model_swap_rejected",
        timestamp_mono_ms=now_ms,
        timestamp_wall=_now_wall(),
        source="manual_test_console.server",
        caused_by=[operator_action_event_id],
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="config_change_30d",
        payload_inline=payload,
    )


# ---------------------------------------------------------------------------
# HTTP / WS handlers
# ---------------------------------------------------------------------------


async def _handle_index(request: web.Request) -> web.Response:
    html_path = _STATIC_DIR / "index.html"
    return web.Response(
        body=html_path.read_bytes(),
        content_type="text/html",
        charset="utf-8",
    )


async def _handle_ingest_ws(request: web.Request) -> web.WebSocketResponse:
    """Accept audio + video envelopes and drive the live pipeline per session.

    Envelope shape (JSON over WS):
      {
        "event_type": "raw_audio" | "raw_video",
        "payload_inline_or_ref": "<base64 PCM16 bytes or JPEG bytes>",
        "timestamp_mono_ms": <int>,
        "client_id": "<stable id>",
        "device_label": "<mic or camera label>",
        "timestamp_wall": "<ISO-8601, optional>"
      }
    """
    ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)

    ingest: InputIngest = request.app[KEY_INGEST]  # type: ignore[assignment]
    logger: EventLogger = request.app[KEY_LOGGER]  # type: ignore[assignment]
    chunk_counter: dict[str, int] = request.app[KEY_CHUNK_COUNTER]
    live_enabled: bool = request.app[KEY_LIVE_PIPELINE_ENABLED]
    foreground_model = request.app[KEY_FOREGROUND_MODEL]
    active_pipelines: dict[str, Any] = request.app[KEY_ACTIVE_PIPELINES]
    streaming_raw_mode: bool = request.app[KEY_STREAMING_RAW_MODE]

    client_id = f"ws-{uuid.uuid4().hex[:8]}"
    session = ingest.open_session(client_id)
    chunk_counter["sessions_opened"] = chunk_counter.get("sessions_opened", 0) + 1

    # Per-connection live pipeline (if enabled and a foreground model is available).
    pipeline: LivePipeline | StreamingRawPipeline | None = None
    if streaming_raw_mode and foreground_model is not None:
        # DEMO MODE: bypasses SpeakPolicy + audit gates per spec invariants #2/#4.
        pipeline = build_streaming_raw_pipeline(
            session_id=session.session_id,
            logger=logger,
            foreground_duplex_model=foreground_model,
            tts_adapter=request.app[KEY_TTS_ADAPTER],
            audio_out_broker=request.app[KEY_AUDIO_OUT_BROKER],  # type: ignore[arg-type]
        )
        active_pipelines[session.session_id] = pipeline
        await pipeline.start()
    elif live_enabled and foreground_model is not None:
        # Per-session VisionSidecar when --enable-vision is set. The sidecar
        # buffers the most-recent frame; _bounded_frame_gen pairs it with the
        # next audio chunk so the MiniCPM-o vision tower runs once per frame
        # (not once per audio chunk).
        config_store: ConfigStore = request.app[KEY_CONFIG_STORE]  # type: ignore[assignment]
        sidecar: Any = None
        if request.app[KEY_VISION_ENABLED]:
            from companion_harness.vision_sidecar import VisionSidecar  # noqa: WPS433
            _scene = (
                request.app[KEY_SCENE_SCORER]
                if config_store.get_seam("scene_scorer")
                else None
            )
            _grounding = (
                request.app[KEY_GROUNDING_MODEL]
                if config_store.get_seam("grounding_model")
                else None
            )
            sidecar = VisionSidecar(
                scene_scorer=_scene or _NullSceneScorer(),
                grounding_model=_grounding or _NullGroundingModel(),
                session_id=session.session_id,
                logger=logger,
            )

        pipeline = build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=foreground_model,
            decision_trace_dir=request.app[KEY_BLOB_DIR] / "decision_traces",
            vad_model=request.app[KEY_VAD_MODEL],
            smart_turn_model=request.app[KEY_SMART_TURN_MODEL],
            backchannel_model=request.app[KEY_BACKCHANNEL_MODEL],
            asr_model=request.app[KEY_ASR_MODEL],
            use_stubs=request.app[KEY_USE_STUBS],
            audio_out_broker=request.app[KEY_AUDIO_OUT_BROKER],  # type: ignore[arg-type]
            tts_adapter=request.app[KEY_TTS_ADAPTER],
            vision_sidecar=sidecar,
            config_store=config_store,
            blob_dir=request.app[KEY_BLOB_DIR],
            av_conflict_scorer=request.app[KEY_AV_CONFLICT_SCORER],
            urgency_scorer=request.app[KEY_URGENCY_SCORER],
            deictic_model=request.app[KEY_DEICTIC_MODEL],
            embedder=request.app[KEY_EMBEDDER],
            background_reasoner=request.app[KEY_BACKGROUND_REASONER],
            diarization_adapter_factory=request.app[KEY_DIARIZATION_ADAPTER_FACTORY],
        )
        active_pipelines[session.session_id] = pipeline
        await pipeline.start()

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                envelope = json.loads(msg.data)
            except json.JSONDecodeError:
                continue

            event_type = envelope.get("event_type")
            if event_type not in ("raw_audio", "raw_video"):
                continue

            payload_b64 = envelope.get("payload_inline_or_ref", "")
            try:
                payload_bytes = base64.b64decode(payload_b64)
            except (ValueError, TypeError):
                continue

            ts_mono = int(envelope.get("timestamp_mono_ms", 0))
            ts_wall = envelope.get("timestamp_wall") or _now_wall()
            inbound_client_id = envelope.get("client_id") or client_id

            meta = CaptureMetadata(
                client_id=inbound_client_id,
                timestamp_mono_ms=ts_mono,
                timestamp_wall=ts_wall,
            )
            if event_type == "raw_audio":
                evt = ingest.ingest_chunk(session, payload_bytes, meta)
                chunk_counter["chunks_ingested"] = chunk_counter.get("chunks_ingested", 0) + 1
                if pipeline is not None:
                    pipeline.push_audio(payload_bytes, evt.event_id, ts_mono)
            else:  # raw_video
                video_evt = ingest.ingest_video_frame(session, payload_bytes, meta)
                chunk_counter["frames_ingested"] = chunk_counter.get("frames_ingested", 0) + 1
                if pipeline is not None and pipeline.vision_sidecar is not None:
                    pipeline.vision_sidecar.ingest_frame_bytes(
                        payload_bytes, video_evt.event_id, ts_mono
                    )
    finally:
        if pipeline is not None:
            try:
                await pipeline.stop()
            except Exception:
                pass
            finally:
                active_pipelines.pop(session.session_id, None)
        await ws.close()
    return ws


async def _handle_display_ws(request: web.Request) -> web.WebSocketResponse:
    """Push every harness Event to one connected viewer."""
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    broker: DisplayBroker = request.app[KEY_BROKER]  # type: ignore[assignment]
    queue = broker.add()

    async def writer() -> None:
        while True:
            msg = await queue.get()
            try:
                await ws.send_json(msg)
            except ConnectionResetError:
                return

    writer_task = asyncio.create_task(writer())
    try:
        # Read loop: ignore inbound (the page does not send to display WS).
        async for _msg in ws:
            pass
    finally:
        writer_task.cancel()
        broker.remove(queue)
        try:
            await writer_task
        except asyncio.CancelledError:
            pass
        await ws.close()
    return ws


async def _handle_audio_out_ws(request: web.Request) -> web.WebSocketResponse:
    """Push synthesized audio chunks to one connected browser listener.

    Envelope (JSON per chunk):
      {
        "type": "audio_chunk",
        "session_id": "<live-pipeline session id>",
        "seq": <int, per-session monotonic>,
        "pcm_bytes_b64": "<base64 of raw PCM bytes from the TTS adapter>",
        "sample_rate": 24000,
        "sample_format": "pcm_s16le"
      }

    The broker fans every active session's synthesized chunks to every listener.
    Browsers may filter by session_id; the manual-test console has one tab per
    session in practice.
    """
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    broker: AudioOutBroker = request.app[KEY_AUDIO_OUT_BROKER]  # type: ignore[assignment]
    queue = broker.add()

    async def writer() -> None:
        while True:
            msg = await queue.get()
            try:
                await ws.send_json(msg)
            except ConnectionResetError:
                return

    writer_task = asyncio.create_task(writer())
    try:
        async for _msg in ws:
            pass  # browser does not send to audio_out WS
    finally:
        writer_task.cancel()
        broker.remove(queue)
        try:
            await writer_task
        except asyncio.CancelledError:
            pass
        await ws.close()
    return ws


def _label_is_ready(label: str | None) -> bool:
    """Return True iff label indicates a real (non-stub) loaded adapter."""
    if not label:
        return False
    return not label.startswith("stub:")


async def _handle_health(request: web.Request) -> web.Response:
    chunk_counter: dict[str, int] = request.app[KEY_CHUNK_COUNTER]
    logger: EventLogger = request.app[KEY_LOGGER]  # type: ignore[assignment]
    live_enabled: bool = request.app[KEY_LIVE_PIPELINE_ENABLED]
    foreground_model = request.app[KEY_FOREGROUND_MODEL]
    active_pipelines: dict[str, Any] = request.app[KEY_ACTIVE_PIPELINES]
    streaming_raw_mode: bool = request.app[KEY_STREAMING_RAW_MODE]
    detector_labels: dict[str, str] = request.app[KEY_DETECTOR_LABELS]
    audio_out_counter: dict[str, int] = request.app[KEY_AUDIO_OUT_COUNTER]
    tts_label: str = request.app[KEY_TTS_LABEL]
    # Aggregate vision-sidecar state across all active sessions.
    vision_enabled: bool = request.app[KEY_VISION_ENABLED]
    frames_buffered = 0
    last_frame_event_id: str | None = None
    if vision_enabled and not streaming_raw_mode:
        for p in active_pipelines.values():
            if p.vision_sidecar is None:
                continue
            frames_buffered += p.vision_sidecar.frames_buffered()
            efid = p.vision_sidecar.last_frame_event_id()
            if efid is not None:
                last_frame_event_id = efid
    adapter_labels: dict[str, str] = request.app[KEY_ADAPTER_LABELS]
    if streaming_raw_mode:
        server_mode = "minicpm_streaming_raw"
    elif not live_enabled:
        server_mode = "capture_only"
    elif request.app[KEY_USE_STUBS]:
        server_mode = "stubs"
    else:
        server_mode = "live"

    # T4: per-adapter readiness — bool iff label is a non-stub loaded label.
    vad_ready = _label_is_ready(detector_labels.get("vad"))
    smart_turn_ready = _label_is_ready(detector_labels.get("smart_turn"))
    backchannel_ready = _label_is_ready(detector_labels.get("backchannel"))
    asr_ready = _label_is_ready(detector_labels.get("asr"))
    tts_ready = _label_is_ready(tts_label)
    vision_ready = _label_is_ready(adapter_labels.get("scene_scorer")) and vision_enabled
    foreground_model_ready = foreground_model is not None

    # T5: GPU memory budget.
    gpu_memory_allocated_mb: int | None = None
    gpu_memory_reserved_mb: int | None = None
    gpu_memory_total_mb: int | None = None
    gpu_device_name: str | None = None
    try:
        import torch  # noqa: WPS433
        if torch.cuda.is_available():
            free_bytes, total_bytes = torch.cuda.mem_get_info()
            gpu_memory_total_mb = total_bytes // (1024 * 1024)
            gpu_memory_allocated_mb = torch.cuda.memory_allocated() // (1024 * 1024)
            gpu_memory_reserved_mb = torch.cuda.memory_reserved() // (1024 * 1024)
            gpu_device_name = torch.cuda.get_device_name(0)
    except Exception:
        pass

    # T6: event-rate counters.
    counter: _EventRateCounter = request.app[KEY_EVENT_RATE_COUNTER]  # type: ignore[assignment]
    events_per_second_last_60s = round(counter.rate_per_second())
    events_total_since_start = counter.total_count()

    return web.json_response({
        "status": "ok",
        "mode": server_mode,
        "sessions_opened": chunk_counter.get("sessions_opened", 0),
        "chunks_ingested": chunk_counter.get("chunks_ingested", 0),
        "frames_ingested": chunk_counter.get("frames_ingested", 0),
        "logger_drain_running": logger._task is not None and not logger._task.done(),
        "live_pipeline_enabled": live_enabled,
        "minicpm_loaded": foreground_model is not None,
        "active_sessions": len(active_pipelines),
        "use_stubs": request.app[KEY_USE_STUBS],
        "vad_model": detector_labels.get("vad", "unknown"),
        "smart_turn_model": detector_labels.get("smart_turn", "unknown"),
        "backchannel_model": detector_labels.get("backchannel", "unknown"),
        "asr_model": detector_labels.get("asr", "unknown"),
        "audio_out_enabled": True,
        "audio_out_chunks_sent": audio_out_counter.get("chunks_sent", 0),
        "tts_model": tts_label,
        "vision_enabled": vision_enabled,
        "frames_buffered": frames_buffered,
        "last_frame_event_id": last_frame_event_id,
        "scene_scorer": adapter_labels.get("scene_scorer", "stub:_NullSceneScorer"),
        "grounding_model": adapter_labels.get("grounding_model", "stub:_NullGroundingModel"),
        "av_conflict_scorer": adapter_labels.get("av_conflict_scorer", "stub:_NullAudioVisualConflictScorer"),
        "deictic_model": adapter_labels.get("deictic_model", "stub:_NullDeicticModel"),
        "urgency_scorer": adapter_labels.get("urgency_scorer", "stub:_NullUrgencyScorer"),
        "embedder": adapter_labels.get("embedder", "stub:_NullEmbeddingAdapter"),
        # T4: per-adapter readiness
        "vad_ready": vad_ready,
        "smart_turn_ready": smart_turn_ready,
        "backchannel_ready": backchannel_ready,
        "asr_ready": asr_ready,
        "tts_ready": tts_ready,
        "vision_ready": vision_ready,
        "foreground_model_ready": foreground_model_ready,
        # T5: GPU memory budget
        "gpu_memory_allocated_mb": gpu_memory_allocated_mb,
        "gpu_memory_reserved_mb": gpu_memory_reserved_mb,
        "gpu_memory_total_mb": gpu_memory_total_mb,
        "gpu_device_name": gpu_device_name,
        # T6: event-rate counters
        "events_per_second_last_60s": events_per_second_last_60s,
        "events_total_since_start": events_total_since_start,
    })


async def _handle_metrics(request: web.Request) -> web.Response:
    """Prometheus text-format metrics endpoint."""
    counter: _EventRateCounter = request.app[KEY_EVENT_RATE_COUNTER]  # type: ignore[assignment]
    uptime = time.monotonic() - request.app[KEY_START_TIME]
    events_per_second = counter.rate_per_second()

    detector_labels: dict[str, str] = request.app[KEY_DETECTOR_LABELS]
    adapter_labels: dict[str, str] = request.app[KEY_ADAPTER_LABELS]
    tts_label: str = request.app[KEY_TTS_LABEL]
    vision_enabled: bool = request.app[KEY_VISION_ENABLED]

    adapters: dict[str, bool] = {
        "vad": _label_is_ready(detector_labels.get("vad")),
        "smart_turn": _label_is_ready(detector_labels.get("smart_turn")),
        "backchannel": _label_is_ready(detector_labels.get("backchannel")),
        "asr": _label_is_ready(detector_labels.get("asr")),
        "tts": _label_is_ready(tts_label),
        "vision": _label_is_ready(adapter_labels.get("scene_scorer")) and vision_enabled,
    }

    gpu_allocated_mb: float | None = None
    gpu_device = "cuda:0"
    try:
        import torch  # noqa: WPS433
        if torch.cuda.is_available():
            gpu_allocated_mb = torch.cuda.memory_allocated() / (1024 * 1024)
    except Exception:
        pass

    lines: list[str] = [
        "# HELP harness_uptime_seconds Uptime since process start",
        "# TYPE harness_uptime_seconds gauge",
        f"harness_uptime_seconds {uptime:.3f}",
        "# HELP harness_events_per_second_last_60s Event throughput",
        "# TYPE harness_events_per_second_last_60s gauge",
        f"harness_events_per_second_last_60s {events_per_second:.3f}",
        "# HELP harness_adapter_ready Adapter readiness state (1 = real, 0 = stub/disabled)",
        "# TYPE harness_adapter_ready gauge",
    ]
    for name, ready in adapters.items():
        lines.append(f'harness_adapter_ready{{adapter="{name}"}} {1 if ready else 0}')

    if gpu_allocated_mb is not None:
        lines += [
            "# HELP harness_gpu_memory_allocated_mb GPU memory allocated",
            "# TYPE harness_gpu_memory_allocated_mb gauge",
            f'harness_gpu_memory_allocated_mb{{device="{gpu_device}"}} {gpu_allocated_mb:.1f}',
        ]

    lines.append("")
    body = "\n".join(lines)
    return web.Response(
        body=body.encode(),
        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
    )


# ---------------------------------------------------------------------------
# /config HTTP endpoints
# ---------------------------------------------------------------------------


def _schema_entry_dict(key: str) -> dict[str, Any]:
    entry = ALLOWLIST[key]
    return {
        "default": entry.default,
        "min": entry.min,
        "max": entry.max,
        "step": entry.step,
        "value_type": entry.value_type.__name__,
        "description": entry.description,
        "code_location": entry.code_location,
    }


async def _handle_get_config(request: web.Request) -> web.Response:
    """Return current effective Tier-B values + schema metadata.

    Dashboard (Task F) uses this on page load to render sliders.
    See docs/design-config-and-dashboard.md §5.
    """
    config_store: ConfigStore = request.app[KEY_CONFIG_STORE]  # type: ignore[assignment]
    return web.json_response({
        "values": config_store.current_state(),
        "schema": {key: _schema_entry_dict(key) for key in ALLOWLIST.keys()},
        "seams": config_store.current_seam_state(),
    })


async def _handle_get_config_seams(request: web.Request) -> web.Response:
    """Return the enabled/disabled state of all 12 hot seams."""
    config_store: ConfigStore = request.app[KEY_CONFIG_STORE]  # type: ignore[assignment]
    seam_state = config_store.current_seam_state()
    return web.json_response({
        "seams": [{"seam": seam, "enabled": seam_state[seam]} for seam in HOT_SEAMS],
    })


async def _handle_post_config_patch(request: web.Request) -> web.Response:
    """Apply a single Tier-B key override.

    Body: {key: str, value: float|int}. See design doc §5.

    Emits:
      - operator_action event (root of the chain)
      - config_change event (caused_by=[operator_action_event_id])
    """
    config_store: ConfigStore = request.app[KEY_CONFIG_STORE]  # type: ignore[assignment]
    logger: EventLogger = request.app[KEY_LOGGER]  # type: ignore[assignment]
    seq_counter: dict = request.app[KEY_OPERATOR_SEQ]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response(
            {"error": "request body is not valid JSON", "key": None, "tier": "unknown"},
            status=400,
        )

    if not isinstance(body, dict) or "key" not in body or "value" not in body:
        return web.json_response(
            {"error": "body must be {key, value}", "key": None, "tier": "unknown"},
            status=400,
        )

    key = body["key"]
    value = body["value"]

    # Rejection routing per design doc §5:
    #   - Tier-A key      → HTTP 403, tier="A"
    #   - Unknown key     → HTTP 403, tier="unknown"
    #   - Type mismatch / out of range on Tier-B → HTTP 400, tier="B"
    if key in tier_a_keys():
        return web.json_response(
            {"error": f"Tier-A key not patchable: {key!r}", "key": key, "tier": "A"},
            status=403,
        )
    if key not in ALLOWLIST:
        return web.json_response(
            {"error": f"unknown key: {key!r}", "key": key, "tier": "unknown"},
            status=403,
        )

    ok, msg = validate_patch(key, value)
    if not ok:
        return web.json_response(
            {"error": msg, "key": key, "tier": "B"},
            status=400,
        )

    # Emit operator_action FIRST so config_change can cite its event_id.
    op_event = _make_operator_action_event(
        endpoint="/config/patch",
        client_ip=request.remote or "",
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        seq_counter=seq_counter,
    )
    logger.log(op_event)

    change = config_store.set(key, value)
    cc_event = _make_config_change_event(
        change=change,
        operator_action_event_id=op_event.event_id,
        seq_counter=seq_counter,
    )
    logger.log(cc_event)

    return web.json_response({
        "key": change.key,
        "previous_value": change.previous_value,
        "new_value": change.new_value,
        "operator_action_event_id": op_event.event_id,
        "config_change_event_id": cc_event.event_id,
    })


async def _handle_post_config_reset(request: web.Request) -> web.Response:
    """Reset a single key, a section, or all Tier-B keys.

    Body: {key?: str, section?: str}. Empty body resets all keys. See §5.

    Emits one operator_action plus one config_change per actual change. If
    nothing was non-default, only operator_action fires (audit trail of the
    request itself).
    """
    config_store: ConfigStore = request.app[KEY_CONFIG_STORE]  # type: ignore[assignment]
    logger: EventLogger = request.app[KEY_LOGGER]  # type: ignore[assignment]
    seq_counter: dict = request.app[KEY_OPERATOR_SEQ]

    try:
        body = await request.json() if request.body_exists else {}
    except json.JSONDecodeError:
        return web.json_response(
            {"error": "request body is not valid JSON"},
            status=400,
        )
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object or empty"},
            status=400,
        )

    key = body.get("key")
    section = body.get("section")

    changes: list[ConfigChange] = []
    if key is not None:
        if key not in ALLOWLIST:
            return web.json_response(
                {"error": f"unknown key: {key!r}", "key": key, "tier": "unknown"},
                status=403,
            )
        c = config_store.reset(key)
        if c is not None:
            changes.append(c)
    elif section is not None:
        prefix = f"{section}."
        matching = [k for k in ALLOWLIST.keys() if k.startswith(prefix)]
        if not matching:
            return web.json_response(
                {"error": f"unknown section: {section!r}", "section": section},
                status=403,
            )
        for k in matching:
            c = config_store.reset(k)
            if c is not None:
                changes.append(c)
    else:
        changes = config_store.reset_all()

    op_event = _make_operator_action_event(
        endpoint="/config/reset",
        client_ip=request.remote or "",
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        seq_counter=seq_counter,
    )
    logger.log(op_event)

    cc_event_ids: list[str] = []
    cc_dicts: list[dict] = []
    for change in changes:
        cc_event = _make_config_change_event(
            change=change,
            operator_action_event_id=op_event.event_id,
            seq_counter=seq_counter,
        )
        logger.log(cc_event)
        cc_event_ids.append(cc_event.event_id)
        cc_dicts.append({
            "key": change.key,
            "previous_value": change.previous_value,
            "new_value": change.new_value,
        })

    return web.json_response({
        "changes": cc_dicts,
        "operator_action_event_id": op_event.event_id,
        "config_change_event_ids": cc_event_ids,
    })


async def _handle_post_model_swap(request: web.Request) -> web.Response:
    """Toggle a hot-seam enabled/disabled state.

    Body: {seam: str, enabled: bool}.

    Emits:
      - operator_action (root)
      - model_swap_requested (caused_by=[operator_action_event_id])
      - model_swap_completed OR model_swap_rejected (caused_by=[operator_action_event_id])

    Event payload shape is locked per plan §F3.
    """
    config_store: ConfigStore = request.app[KEY_CONFIG_STORE]  # type: ignore[assignment]
    logger: EventLogger = request.app[KEY_LOGGER]  # type: ignore[assignment]
    seq_counter: dict = request.app[KEY_OPERATOR_SEQ]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "request body is not valid JSON"}, status=400)

    if not isinstance(body, dict) or "seam" not in body or "enabled" not in body:
        return web.json_response({"error": "body must be {seam, enabled}"}, status=400)

    seam = body["seam"]
    enabled = body["enabled"]

    op_event = _make_operator_action_event(
        endpoint="/config/model-swap",
        client_ip=request.remote or "",
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        seq_counter=seq_counter,
    )
    logger.log(op_event)

    ok, msg = validate_seam_patch(seam, enabled)
    _is_unknown_seam = seam not in HOT_SEAMS
    if not ok:
        rej_event = _make_model_swap_rejected_event(
            seam=seam if isinstance(seam, str) else str(seam),
            attempted_enabled=enabled if isinstance(enabled, bool) else None,
            reason="unknown_seam" if _is_unknown_seam else "invalid_enabled",
            operator_action_event_id=op_event.event_id,
            seq_counter=seq_counter,
        )
        logger.log(rej_event)
        return web.json_response(
            {"error": msg, "model_swap_event_id": rej_event.event_id},
            status=403 if _is_unknown_seam else 400,
        )

    from_enabled = config_store.get_seam(seam)

    req_event = _make_model_swap_requested_event(
        seam=seam,
        from_enabled=from_enabled,
        to_enabled=enabled,
        operator_action_event_id=op_event.event_id,
        seq_counter=seq_counter,
    )
    logger.log(req_event)

    t0_ns = time.monotonic_ns()
    config_store.set_seam(seam, enabled)
    latency_ms = max(0, (time.monotonic_ns() - t0_ns) // 1_000_000)
    applied_at_ms = int(time.monotonic() * 1000)

    cc_event = _make_model_swap_completed_event(
        seam=seam,
        from_enabled=from_enabled,
        to_enabled=enabled,
        applied_at_ms=applied_at_ms,
        latency_ms=latency_ms,
        operator_action_event_id=op_event.event_id,
        seq_counter=seq_counter,
    )
    logger.log(cc_event)

    return web.json_response({
        "accepted": True,
        "model_swap_event_id": cc_event.event_id,
        "restart_required": False,
        "requested_at_ms": req_event.payload_inline["requested_at_ms"],
        "applied_at_ms": applied_at_ms,
        "latency_ms": latency_ms,
    })


# ---------------------------------------------------------------------------
# Background reasoner env-var dispatcher (v0.2a T4)
# ---------------------------------------------------------------------------


def _construct_background_reasoner() -> "FakeBackgroundReasoner | MCPBackgroundReasoner":
    """Construct the background reasoner from BACKGROUND_REASONER env var.

    BACKGROUND_REASONER=fake (default) → FakeBackgroundReasoner
    BACKGROUND_REASONER=mcp           → MCPBackgroundReasoner (requires MCP_SERVER_URL)
    Absent env var                    → FakeBackgroundReasoner (default)

    Fails loudly on unknown choice or missing MCP_SERVER_URL — no silent fallback.
    """
    choice = os.environ.get("BACKGROUND_REASONER", "fake").lower()
    if choice == "fake":
        from companion_harness.background_reasoner import FakeBackgroundReasoner
        return FakeBackgroundReasoner()
    if choice == "mcp":
        url = os.environ.get("MCP_SERVER_URL")
        if not url:
            raise RuntimeError(
                "BACKGROUND_REASONER=mcp requires MCP_SERVER_URL env var"
            )
        from companion_harness.background_reasoner import MCPBackgroundReasoner
        return MCPBackgroundReasoner(mcp_server_url=url)
    raise RuntimeError(f"Unknown BACKGROUND_REASONER={choice!r}")


# ---------------------------------------------------------------------------
# App factory + lifecycle
# ---------------------------------------------------------------------------


def build_app(
    blob_dir: Path,
    *,
    live_pipeline_enabled: bool = True,
    foreground_model: Any = None,
    foreground_model_factory: Optional[Callable[[], Any]] = None,
    use_stubs: bool = False,
    vad_model_factory: Optional[Callable[[], Any]] = None,
    smart_turn_model_factory: Optional[Callable[[], Any]] = None,
    backchannel_model_factory: Optional[Callable[[], Any]] = None,
    tts_adapter: Any = None,
    tts_adapter_factory: Optional[Callable[[], Any]] = None,
    tts_adapter_name: str = "Kokoro-82M-ONNX",
    asr_model_factory: Optional[Callable[[], Any]] = None,
    vision_enabled: bool = False,
    scene_scorer: Any = None,
    grounding_model: Any = None,
    av_conflict_scorer: Any = None,
    deictic_model: Any = None,
    urgency_scorer: Any = None,
    embedder: Any = None,
    diarization_adapter_factory: Any = None,
    streaming_raw_mode: bool = False,
    seam_defaults: dict[str, bool] | None = None,
    blob_retention_days: int = 30,
    event_log_maxsize: int = 16384,
    display_sampling_rate: int = 1,
) -> web.Application:
    """Build the aiohttp Application. Caller is responsible for run/cleanup.

    Args:
        blob_dir: filesystem path for raw audio/video blob storage.
        live_pipeline_enabled: when False, the server runs in capture-only mode
            (no orchestrator per session). Useful for fallback if model load fails.
        foreground_model: pre-constructed StreamingDuplexModel singleton. Tests
            inject a fake here. When None and `foreground_model_factory` is also
            None, the live pipeline is disabled regardless of the flag.
        foreground_model_factory: zero-arg callable invoked at startup to
            construct the singleton (used by main() to load MiniCPM lazily so
            that --no-live-pipeline avoids the load entirely).
        use_stubs: when True, every per-session live pipeline uses the
            CPU-only stub detectors (EnergyVADModel / SilenceSmartTurnModel /
            ZeroBackchannelModel) and any real-model factories are ignored.
            Tests and the `--use-stubs` CLI fallback rely on this.
        vad_model_factory / smart_turn_model_factory / backchannel_model_factory:
            zero-arg callables invoked at startup to construct each real
            detector singleton. When None (and `use_stubs=False`), the
            corresponding stub is used for that detector only.
        tts_adapter: pre-constructed TtsAdapter singleton (tests inject a fake).
        tts_adapter_factory: zero-arg callable invoked at startup to construct
            the TTS singleton (used by main() to load Kokoro lazily). When both
            are None or use_stubs=True, NoopTtsAdapter is used so no audio bytes
            fire.
    """
    app = web.Application()
    broker = DisplayBroker(sampling_rate=display_sampling_rate)
    logger = EventLogger(_null_sink, maxsize=event_log_maxsize)
    logger.subscribe(broker.on_event)
    event_rate_counter = _EventRateCounter(window_seconds=60)
    logger.subscribe(event_rate_counter.on_event)
    ingest = InputIngest(logger, blob_dir)

    audio_out_counter: dict[str, int] = {"chunks_sent": 0, "chunks_dropped": 0}
    audio_out_broker = AudioOutBroker(audio_out_counter)

    app[KEY_BROKER] = broker
    app[KEY_LOGGER] = logger
    app[KEY_INGEST] = ingest
    app[KEY_EVENT_RATE_COUNTER] = event_rate_counter
    app[KEY_START_TIME] = time.monotonic()
    app[KEY_BLOB_DIR] = blob_dir
    app[KEY_CHUNK_COUNTER] = {"sessions_opened": 0, "chunks_ingested": 0, "frames_ingested": 0}
    app[KEY_AUDIO_OUT_BROKER] = audio_out_broker
    app[KEY_AUDIO_OUT_COUNTER] = audio_out_counter
    app[KEY_LIVE_PIPELINE_ENABLED] = live_pipeline_enabled
    app[KEY_FOREGROUND_MODEL] = foreground_model
    app[KEY_ACTIVE_PIPELINES] = {}
    app[KEY_USE_STUBS] = use_stubs
    app[KEY_STREAMING_RAW_MODE] = streaming_raw_mode
    app[KEY_BACKGROUND_REASONER] = _construct_background_reasoner()
    app[KEY_VAD_MODEL] = None
    app[KEY_SMART_TURN_MODEL] = None
    app[KEY_BACKCHANNEL_MODEL] = None
    app[KEY_ASR_MODEL] = None
    app[KEY_DETECTOR_LABELS] = {
        "vad": "stub:EnergyVAD" if use_stubs else "stub:EnergyVAD",
        "smart_turn": "stub:SilenceSmartTurn" if use_stubs else "stub:SilenceSmartTurn",
        "backchannel": "stub:ZeroBackchannel" if use_stubs else "stub:ZeroBackchannel",
        "asr": "stub:EmptyTranscript" if use_stubs else "stub:EmptyTranscript",
    }
    app[KEY_TTS_ADAPTER] = tts_adapter
    app[KEY_TTS_LABEL] = "stub:NoopTtsAdapter" if (tts_adapter is None or use_stubs) else "injected"
    app[KEY_VISION_ENABLED] = vision_enabled
    # ConfigStore: in-memory authoritative Tier-B runtime state. Phase 1 Task E
    # owns the write side (HTTP endpoints below); Task D will wire the same
    # singleton into the orchestrator's read side once it merges. Until then
    # we construct our own ConfigStore here so /config/{patch,reset} can run.
    app[KEY_CONFIG_STORE] = ConfigStore(ALLOWLIST, seam_defaults=seam_defaults)
    app[KEY_OPERATOR_SEQ] = {"seq": 0}
    # Optional real-adapter wiring (default None → null stubs continue).
    app[KEY_SCENE_SCORER] = scene_scorer
    app[KEY_GROUNDING_MODEL] = grounding_model
    app[KEY_AV_CONFLICT_SCORER] = av_conflict_scorer
    app[KEY_DEICTIC_MODEL] = deictic_model
    app[KEY_URGENCY_SCORER] = urgency_scorer
    app[KEY_EMBEDDER] = embedder
    app[KEY_DIARIZATION_ADAPTER_FACTORY] = diarization_adapter_factory
    app[KEY_ADAPTER_LABELS] = {
        "scene_scorer": (
            f"real:{type(scene_scorer).__name__}" if scene_scorer is not None
            else "stub:_NullSceneScorer"
        ),
        "grounding_model": (
            f"real:{type(grounding_model).__name__}" if grounding_model is not None
            else "stub:_NullGroundingModel"
        ),
        "av_conflict_scorer": (
            f"real:{type(av_conflict_scorer).__name__}" if av_conflict_scorer is not None
            else "stub:_NullAudioVisualConflictScorer"
        ),
        "deictic_model": (
            f"real:{type(deictic_model).__name__}" if deictic_model is not None
            else "stub:_NullDeicticModel"
        ),
        "urgency_scorer": (
            f"real:{type(urgency_scorer).__name__}" if urgency_scorer is not None
            else "stub:_NullUrgencyScorer"
        ),
        "embedder": (
            f"real:{type(embedder).__name__}" if embedder is not None
            else "stub:_NullEmbeddingAdapter"
        ),
        "diarization_adapter": (
            f"real:{diarization_adapter_factory.__name__}" if diarization_adapter_factory is not None
            else "stub:_NullDiarizationAdapter"
        ),
    }

    app.router.add_get("/", _handle_index)
    app.router.add_get("/healthz", _handle_health)
    app.router.add_get("/metrics", _handle_metrics)
    app.router.add_get("/ws/ingest", _handle_ingest_ws)
    app.router.add_get("/ws/display", _handle_display_ws)
    app.router.add_get("/ws/audio_out", _handle_audio_out_ws)
    app.router.add_get("/config", _handle_get_config)
    app.router.add_post("/config/patch", _handle_post_config_patch)
    app.router.add_post("/config/reset", _handle_post_config_reset)
    app.router.add_get("/config/seams", _handle_get_config_seams)
    app.router.add_post("/config/model-swap", _handle_post_model_swap)

    # --- Eval console PR1 (E4) ---
    from manual_test_console.eval_routes import register_eval_routes
    _eval_reports_dir = blob_dir / "eval_reports"
    _eval_reports_dir.mkdir(parents=True, exist_ok=True)
    register_eval_routes(app, eval_reports_dir=_eval_reports_dir)

    async def _on_startup(_app: web.Application) -> None:
        await logger.start()
        needs_foreground = live_pipeline_enabled or streaming_raw_mode
        if needs_foreground and foreground_model is None and foreground_model_factory is not None:
            print("Loading MiniCPM-o foreground model (this may take minutes)...", flush=True)
            t0 = time.monotonic()
            try:
                model = foreground_model_factory()
            except Exception as exc:
                print(f"MiniCPM-o load FAILED: {type(exc).__name__}: {exc}", flush=True)
                print("Falling back to capture-only mode (no live pipeline).", flush=True)
                _app[KEY_LIVE_PIPELINE_ENABLED] = False
                _app[KEY_STREAMING_RAW_MODE] = False
                return
            elapsed = time.monotonic() - t0
            _app[KEY_FOREGROUND_MODEL] = model
            print(f"MiniCPM-o loaded in {elapsed:.1f}s", flush=True)

        # In streaming_raw_mode: load TTS then return (no detectors needed).
        if _app[KEY_STREAMING_RAW_MODE]:
            if _app[KEY_TTS_ADAPTER] is None and tts_adapter_factory is not None:
                print(f"Loading {tts_adapter_name} TTS adapter (raw mode)...", flush=True)
                t0 = time.monotonic()
                try:
                    _app[KEY_TTS_ADAPTER] = tts_adapter_factory()
                except Exception as exc:
                    print(
                        f"{tts_adapter_name} TTS load FAILED: {type(exc).__name__}: {exc} "
                        "— falling back to stub:NoopTtsAdapter",
                        flush=True,
                    )
                    from manual_test_console.live_pipeline import NoopTtsAdapter  # noqa: WPS433
                    _app[KEY_TTS_ADAPTER] = NoopTtsAdapter()
                    _app[KEY_TTS_LABEL] = "stub:NoopTtsAdapter"
                else:
                    elapsed = time.monotonic() - t0
                    _app[KEY_TTS_LABEL] = f"{tts_adapter_name} (loaded in {elapsed:.2f}s)"
                    print(f"{tts_adapter_name} loaded in {elapsed:.2f}s", flush=True)
            elif _app[KEY_TTS_ADAPTER] is None:
                from manual_test_console.live_pipeline import NoopTtsAdapter  # noqa: WPS433
                _app[KEY_TTS_ADAPTER] = NoopTtsAdapter()
                _app[KEY_TTS_LABEL] = "stub:NoopTtsAdapter"
            return  # skip detector loading in raw mode

        if not _app[KEY_LIVE_PIPELINE_ENABLED] or _app[KEY_USE_STUBS]:
            # Stub/disabled mode: ensure tts_adapter falls back to Noop.
            if _app[KEY_TTS_ADAPTER] is None:
                from manual_test_console.live_pipeline import NoopTtsAdapter  # noqa: WPS433
                _app[KEY_TTS_ADAPTER] = NoopTtsAdapter()
                _app[KEY_TTS_LABEL] = "stub:NoopTtsAdapter"
            return

        # Load TTS singleton. Falls back to NoopTtsAdapter on failure so the
        # server still starts (voice-back simply silent).
        if _app[KEY_TTS_ADAPTER] is None and tts_adapter_factory is not None:
            print(f"Loading {tts_adapter_name} TTS adapter...", flush=True)
            t0 = time.monotonic()
            try:
                _app[KEY_TTS_ADAPTER] = tts_adapter_factory()
            except Exception as exc:
                print(
                    f"{tts_adapter_name} TTS load FAILED: {type(exc).__name__}: {exc} "
                    "— falling back to stub:NoopTtsAdapter (voice-back disabled)",
                    flush=True,
                )
                from manual_test_console.live_pipeline import NoopTtsAdapter  # noqa: WPS433
                _app[KEY_TTS_ADAPTER] = NoopTtsAdapter()
                _app[KEY_TTS_LABEL] = "stub:NoopTtsAdapter"
            else:
                elapsed = time.monotonic() - t0
                _app[KEY_TTS_LABEL] = f"{tts_adapter_name} (loaded in {elapsed:.2f}s)"
                print(f"{tts_adapter_name} loaded in {elapsed:.2f}s", flush=True)
        elif _app[KEY_TTS_ADAPTER] is None:
            from manual_test_console.live_pipeline import NoopTtsAdapter  # noqa: WPS433
            _app[KEY_TTS_ADAPTER] = NoopTtsAdapter()
            _app[KEY_TTS_LABEL] = "stub:NoopTtsAdapter"

        # Load real detector models. Each one falls back to its stub on failure.
        for key, label_key, factory, stub_label, real_label in (
            (KEY_VAD_MODEL, "vad", vad_model_factory,
             "stub:EnergyVAD", "Silero (ONNX)"),
            (KEY_SMART_TURN_MODEL, "smart_turn", smart_turn_model_factory,
             "stub:SilenceSmartTurn", "Pipecat SmartTurn v3 (ONNX, CPU)"),
            (KEY_BACKCHANNEL_MODEL, "backchannel", backchannel_model_factory,
             "stub:ZeroBackchannel", "whisper-tiny + lexicon"),
            (KEY_ASR_MODEL, "asr", asr_model_factory,
             "stub:EmptyTranscript", "whisper-tiny.en (faster-whisper)"),
        ):
            if factory is None:
                _app[KEY_DETECTOR_LABELS][label_key] = stub_label
                continue
            print(f"Loading {real_label}...", flush=True)
            t0 = time.monotonic()
            try:
                _app[key] = factory()
            except Exception as exc:
                print(
                    f"{real_label} load FAILED: {type(exc).__name__}: {exc} "
                    f"— falling back to {stub_label}",
                    flush=True,
                )
                _app[KEY_DETECTOR_LABELS][label_key] = stub_label
                continue
            elapsed = time.monotonic() - t0
            _app[KEY_DETECTOR_LABELS][label_key] = f"{real_label} (loaded in {elapsed:.2f}s)"
            print(f"{real_label} loaded in {elapsed:.2f}s", flush=True)

    async def _on_startup_finalize_deictic(_app: web.Application) -> None:
        """If --enable-deictic was requested but no detector was injected,
        try to construct MiniCPMDeicticDetector against the loaded foreground
        model. Requires the foreground model to expose `chat()` (the
        MiniCPMDuplexModel API). Falls back to null stub otherwise.
        """
        if _app[KEY_DEICTIC_MODEL] is not None:
            return
        # Only auto-construct when the operator opted in via labels
        # (set by main() before build_app when --enable-deictic was passed).
        labels = _app[KEY_ADAPTER_LABELS]
        if labels.get("deictic_model", "").startswith("stub:"):
            return
        fg = _app[KEY_FOREGROUND_MODEL]
        if fg is None or not hasattr(fg, "chat"):
            print(
                "--enable-deictic: foreground model lacks .chat(); "
                "falling back to stub:_NullDeicticModel.",
                flush=True,
            )
            labels["deictic_model"] = "stub:_NullDeicticModel"
            return
        try:
            from companion_harness.deictic_detector_minicpm import (  # noqa: WPS433
                MiniCPMDeicticDetector,
            )
            _app[KEY_DEICTIC_MODEL] = MiniCPMDeicticDetector(fg)
            labels["deictic_model"] = "real:MiniCPMDeicticDetector"
            print("MiniCPMDeicticDetector wired (reusing foreground model).", flush=True)
        except Exception as exc:
            print(
                f"MiniCPMDeicticDetector wiring FAILED: {type(exc).__name__}: {exc} "
                "— falling back to stub:_NullDeicticModel",
                flush=True,
            )
            labels["deictic_model"] = "stub:_NullDeicticModel"

    async def _on_startup_summary(_app: web.Application) -> None:
        labels = _app[KEY_DETECTOR_LABELS]
        adapter_labels = _app[KEY_ADAPTER_LABELS]
        print("-" * 72, flush=True)
        print(f"  VAD:               {labels.get('vad', 'unknown')}", flush=True)
        print(f"  SmartTurn:         {labels.get('smart_turn', 'unknown')}", flush=True)
        print(f"  Backchannel:       {labels.get('backchannel', 'unknown')}", flush=True)
        print(f"  TTS:               {_app[KEY_TTS_LABEL]}", flush=True)
        print(f"  ASR:               {labels.get('asr', 'unknown')}", flush=True)
        print(f"  SceneScorer:       {adapter_labels.get('scene_scorer', 'unknown')}", flush=True)
        print(f"  GroundingModel:    {adapter_labels.get('grounding_model', 'unknown')}", flush=True)
        print(f"  AVConflictScorer:  {adapter_labels.get('av_conflict_scorer', 'unknown')}", flush=True)
        print(f"  DeicticModel:      {adapter_labels.get('deictic_model', 'unknown')}", flush=True)
        print(f"  UrgencyScorer:     {adapter_labels.get('urgency_scorer', 'unknown')}", flush=True)
        print(f"  Embedder:          {adapter_labels.get('embedder', 'unknown')}", flush=True)
        print("=" * 72, flush=True)

    async def _blob_rotation_worker() -> None:
        """Delete blob files older than blob_retention_days. Runs hourly."""
        if blob_retention_days <= 0:
            return
        retention_seconds = blob_retention_days * 86400
        tick = min(3600, retention_seconds // 24)
        print(
            f"Blob rotation: retaining {blob_retention_days} days; "
            f"tick every {tick}s.",
            flush=True,
        )
        while True:
            await asyncio.sleep(tick)
            cutoff = time.time() - retention_seconds
            for p in blob_dir.iterdir():
                if p.is_file():
                    try:
                        if p.stat().st_mtime < cutoff:
                            p.unlink()
                    except Exception:
                        pass

    async def _display_sampling_reporter() -> None:
        """Every 60s emit a display_event_sampled summary event so the dashboard
        can show how many high-rate events were sampled out of the display fanout.
        The EventLogger ring still received every event (audit invariant #1).
        """
        while True:
            await asyncio.sleep(60)
            summary = broker.sampling_summary()
            total_in = sum(summary["forwarded"].values())
            total_out = sum(summary["sampled_out"].values())
            if total_in + total_out == 0:
                continue
            now_ms = int(time.monotonic() * 1000)
            evt = Event(
                event_id=f"display-sampled-{now_ms}",
                session_id="",
                schema_version="0.1",
                seq_no=0,
                event_type="display_event_sampled",
                timestamp_mono_ms=now_ms,
                timestamp_wall=_now_wall(),
                source="display_broker",
                caused_by=[],
                payload_hash="",
                payload_ref=None,
                payload_kind="signal",
                subject_class="unknown",
                sensitivity="safe",
                retention_policy_id="default",
                payload_inline={
                    "forwarded": summary["forwarded"],
                    "sampled_out": summary["sampled_out"],
                    "elapsed_s": round(summary["elapsed_s"], 1),
                    "sampling_rate": display_sampling_rate,
                },
            )
            logger.log(evt)

    async def _on_cleanup(_app: web.Application) -> None:
        # Stop any still-active live pipelines first (they share the logger).
        for pipeline in list(_app[KEY_ACTIVE_PIPELINES].values()):
            try:
                await pipeline.stop()
            except Exception:
                pass
        _app[KEY_ACTIVE_PIPELINES].clear()
        await logger.stop()

    app.on_startup.append(_on_startup)
    app.on_startup.append(_on_startup_finalize_deictic)
    app.on_startup.append(_on_startup_summary)

    async def _on_startup_rotation(_app: web.Application) -> None:
        if blob_retention_days > 0:
            asyncio.get_running_loop().create_task(_blob_rotation_worker())
        asyncio.get_running_loop().create_task(_display_sampling_reporter())

    app.on_startup.append(_on_startup_rotation)
    app.on_cleanup.append(_on_cleanup)
    return app


def _load_minicpm_streaming_model(*, init_vision: bool = False) -> Any:
    """Lazy import + construct MiniCPMStreamingModel. b200 only.

    Imported here (not at module top) so the server module remains importable
    on machines without torch/CUDA. Tests never reach this path. When
    `init_vision=True` the MiniCPM-o vision tower is loaded (+~18 GB VRAM
    per b200 pre-verification).
    """
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel  # noqa: WPS433
    return MiniCPMStreamingModel(init_vision=init_vision)


def _load_silero_vad_model() -> Any:
    """Lazy import + construct SileroVADModel. b200 / any host with onnxruntime."""
    from companion_harness.vad_silero import SileroVADModel  # noqa: WPS433
    return SileroVADModel()


def _load_pipecat_smart_turn_model() -> Any:
    """Lazy import + construct PipecatSmartTurnModel."""
    from companion_harness.smart_turn_pipecat import PipecatSmartTurnModel  # noqa: WPS433
    return PipecatSmartTurnModel()


def _load_asr_lexicon_backchannel_model() -> Any:
    """Lazy import + construct ASRLexiconBackchannelModel."""
    from companion_harness.backchannel_asr_lexicon import ASRLexiconBackchannelModel  # noqa: WPS433
    return ASRLexiconBackchannelModel()


# Default Kokoro model paths on b200. Override via env var if your install
# location differs (mirrors tests/test_tts_kokoro_real.py).
_KOKORO_DEFAULT_MODEL = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
_KOKORO_DEFAULT_VOICES = "/raid/yid042/models/kokoro/voices.json"


def _load_kokoro_tts_adapter() -> Any:
    """Lazy import + construct KokoroTtsAdapter singleton. b200 only.

    Reads KOKORO_MODEL_PATH / KOKORO_VOICES_PATH env vars if set; otherwise uses
    the canonical b200 paths. Imported lazily so the server module remains
    importable on machines without kokoro_onnx installed.
    """
    import os  # noqa: WPS433
    from companion_harness.tts_kokoro import KokoroTtsAdapter  # noqa: WPS433
    model_path = os.environ.get("KOKORO_MODEL_PATH", _KOKORO_DEFAULT_MODEL)
    voices_path = os.environ.get("KOKORO_VOICES_PATH", _KOKORO_DEFAULT_VOICES)
    return KokoroTtsAdapter(
        model_path=model_path,
        voices_path=voices_path,
        warmup=True,
    )


def _load_native_minicpm_tts_adapter() -> Any:
    """Lazy import + construct MiniCPMNativeTtsAdapter. b200 only (requires CUDA + init_tts)."""
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel  # noqa: WPS433
    from companion_harness.tts_minicpm_native import MiniCPMNativeTtsAdapter  # noqa: WPS433
    streaming_model = MiniCPMStreamingModel()
    return MiniCPMNativeTtsAdapter(streaming_model)


def _load_asr_model() -> Any:
    """Lazy import + construct FasterWhisperASRModel (whisper-tiny.en). b200 only.

    Imported here (not at module top) so the server module remains importable
    on machines without faster_whisper. Mirrors _load_minicpm_streaming_model.
    """
    from companion_harness.asr_faster_whisper import FasterWhisperASRModel  # noqa: WPS433
    return FasterWhisperASRModel(device="cuda", compute_type="float16")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument(
        "--blob-dir",
        type=Path,
        default=Path("/tmp/manual_test_blobs"),
        help="Filesystem path where raw audio chunks are written.",
    )
    parser.add_argument(
        "--enable-live-pipeline",
        dest="live_pipeline",
        action="store_true",
        default=True,
        help="Enable the StreamingRealtimeOrchestrator + MiniCPM-o foreground model.",
    )
    parser.add_argument(
        "--no-live-pipeline",
        dest="live_pipeline",
        action="store_false",
        help="Run in capture-only mode (skip MiniCPM load, no live pipeline).",
    )
    parser.add_argument(
        "--use-stubs",
        dest="use_stubs",
        action="store_true",
        default=False,
        help=(
            "Fall back to CPU-only stub detectors "
            "(EnergyVAD / SilenceSmartTurn / ZeroBackchannel / EmptyTranscript) "
            "instead of loading Silero / Pipecat SmartTurn v3 / whisper-tiny "
            "/ whisper-tiny.en."
        ),
    )
    parser.add_argument(
        "--enable-vision",
        dest="enable_vision",
        action="store_true",
        default=False,
        help=(
            "Load MiniCPM-o with init_vision=True (+~18 GB VRAM) and construct "
            "a per-session VisionSidecar so video frames reach the foreground "
            "model alongside audio. Default OFF — audio-only path unchanged."
        ),
    )
    parser.add_argument(
        "--tts-adapter",
        dest="tts_adapter",
        choices=["kokoro", "native_minicpm"],
        default="kokoro",
        help=(
            "TTS adapter to load at startup. "
            "'kokoro' (default): Kokoro-82M-ONNX via KokoroTtsAdapter. "
            "'native_minicpm': MiniCPM-o native duplex TTS via MiniCPMNativeTtsAdapter."
        ),
    )
    # Real-adapter wiring flags. Default ON (v0.2e) — each flag swaps in the
    # corresponding real impl for its null stub. See live_pipeline.py.
    # Use --no-enable-X to revert to the null stub (operator rollback path).
    parser.add_argument(
        "--enable-clip-scene",
        dest="enable_clip_scene",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wire CLIPSceneChangeScorer in place of _NullSceneScorer (issue #166). Default ON since v0.2e.",
    )
    parser.add_argument(
        "--enable-grounding",
        dest="enable_grounding",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wire GroundingDINOAdapter in place of _NullGroundingModel (issue #172). Default ON since v0.2e.",
    )
    parser.add_argument(
        "--enable-av-conflict",
        dest="enable_av_conflict",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wire HeuristicAVConflictScorer in place of _NullAudioVisualConflictScorer (issue #168). Default ON since v0.2e.",
    )
    parser.add_argument(
        "--enable-deictic",
        dest="enable_deictic",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Wire MiniCPMDeicticDetector in place of _NullDeicticModel (issue #169). "
            "Reuses the already-loaded foreground model. No effect without a "
            "foreground model that exposes .chat(). Default ON since v0.2e."
        ),
    )
    parser.add_argument(
        "--enable-urgency",
        dest="enable_urgency",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wire ProsodyLexiconUrgencyScorer in place of _NullUrgencyScorer (issue #171). Default ON since v0.2e.",
    )
    parser.add_argument(
        "--enable-embeddings",
        dest="enable_embeddings",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wire SentenceTransformerEmbedder in place of _NullEmbeddingAdapter (issue #183). Default ON since v0.2e.",
    )
    parser.add_argument(
        "--enable-diarization",
        dest="enable_diarization",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Wire PyannoteDiarizationAdapter in place of _NullDiarizationAdapter. Default OFF (v0.2b). Requires pyannote.audio>=3.0.",
    )
    parser.add_argument(
        "--minicpm-only",
        dest="minicpm_only",
        action="store_true",
        default=False,
        help=(
            "Run with MiniCPM-o foreground + native_minicpm TTS only. "
            "Stubs VAD, SmartTurn, and Backchannel; keeps ASR real so the "
            "addressing classifier has transcripts. "
            "Mutually exclusive with --use-stubs (--use-stubs wins)."
        ),
    )
    parser.add_argument(
        "--minicpm-streaming-raw",
        dest="minicpm_streaming_raw",
        action="store_true",
        default=False,
        help=(
            "DEMO MODE: bypass SpeakPolicy, ASR, addressing, and all detector "
            "gates. Audio flows mic → MiniCPM infer_stream → native TTS → "
            "audio_out, with no policy filtering. "
            "Mutually exclusive with --minicpm-only and --use-stubs "
            "(most restrictive wins). "
            "Explicitly bypasses spec invariants #2, #4, and parts of #1."
        ),
    )
    parser.add_argument(
        "--disable-seam",
        dest="disable_seams",
        action="append",
        metavar="SEAM",
        default=[],
        help=(
            "Disable a hot seam at startup (repeatable). SEAM must be one of "
            "the 12 names in HOT_SEAMS. Example: --disable-seam asr "
            "--disable-seam tts. Overrides any default; the dashboard can "
            "re-enable at runtime via POST /config/model-swap."
        ),
    )
    parser.add_argument(
        "--blob-retention-days",
        dest="blob_retention_days",
        type=int,
        default=30,
        help=(
            "Delete blob files older than N days. Default 30. Set to 0 to disable "
            "rotation. Blob retention window should be >= event-log retention; "
            "rotating blobs still referenced by live event-log entries breaks replay."
        ),
    )
    parser.add_argument(
        "--event-log-maxsize",
        dest="event_log_maxsize",
        type=int,
        default=16384,
        help="EventLogger internal ring size (default 16384). Raise if log_drop_or_degrade rate is high.",
    )
    parser.add_argument(
        "--display-sampling-rate",
        dest="display_sampling_rate",
        type=int,
        default=5,
        help=(
            "Display-broker sampling rate N for high-rate frame events "
            "(raw_audio_chunk, vad_frame): only 1-in-N forwarded to dashboard subscribers. "
            "Default 5. Set to 1 to disable sampling (backward-compat). "
            "The EventLogger ring always records every event."
        ),
    )
    args = parser.parse_args(argv)

    if args.minicpm_only and args.use_stubs:
        print(
            "WARNING: --use-stubs overrides --minicpm-only; TTS will be NoOp.",
            flush=True,
        )
    elif args.minicpm_only:
        args.tts_adapter = "native_minicpm"

    # --minicpm-streaming-raw: mutually exclusive with --minicpm-only and --use-stubs.
    # Most-restrictive-wins: use_stubs > minicpm_only > minicpm_streaming_raw.
    if args.minicpm_streaming_raw:
        if args.use_stubs:
            print(
                "WARNING: --use-stubs overrides --minicpm-streaming-raw; "
                "running with stub detectors and NoOp TTS.",
                flush=True,
            )
            args.minicpm_streaming_raw = False
        elif args.minicpm_only:
            print(
                "WARNING: --minicpm-only overrides --minicpm-streaming-raw; "
                "running with minicpm-only mode (SpeakPolicy active).",
                flush=True,
            )
            args.minicpm_streaming_raw = False
        else:
            # Force all opt-in vision/aux adapter flags off.
            for flag_name in (
                "enable_clip_scene", "enable_grounding", "enable_av_conflict",
                "enable_deictic", "enable_urgency", "enable_embeddings", "enable_diarization",
            ):
                if getattr(args, flag_name, False):
                    print(
                        f"WARNING: --minicpm-streaming-raw: ignoring --{flag_name.replace('_', '-')} "
                        "(no aux adapters in raw mode).",
                        flush=True,
                    )
                    setattr(args, flag_name, False)
            args.tts_adapter = "native_minicpm"
            print(
                "WARNING: --minicpm-streaming-raw bypasses SpeakPolicy, ASR, addressing, "
                "and audit gates. Use only for demo/comparison.",
                flush=True,
            )

    blob_dir: Path = args.blob_dir
    blob_dir.mkdir(parents=True, exist_ok=True)

    if args.live_pipeline or args.minicpm_streaming_raw:
        if args.enable_vision and not args.minicpm_streaming_raw:
            def factory() -> Any:  # noqa: WPS430
                return _load_minicpm_streaming_model(init_vision=True)
        else:
            factory = _load_minicpm_streaming_model
    else:
        factory = None
    if args.minicpm_streaming_raw:
        # DEMO MODE: bypasses SpeakPolicy + audit gates per spec invariants #2/#4.
        # No detector factories; TTS is native MiniCPM for the raw path.
        vad_factory: Optional[Callable[[], Any]] = None
        smart_turn_factory: Optional[Callable[[], Any]] = None
        backchannel_factory: Optional[Callable[[], Any]] = None
        tts_factory: Optional[Callable[[], Any]] = _load_native_minicpm_tts_adapter
        asr_factory: Optional[Callable[[], Any]] = None
    elif args.live_pipeline and not args.use_stubs:
        if args.minicpm_only:
            vad_factory = None
            smart_turn_factory = None
            backchannel_factory = None
            tts_factory = _load_native_minicpm_tts_adapter
            asr_factory = _load_asr_model
        else:
            vad_factory = _load_silero_vad_model
            smart_turn_factory = _load_pipecat_smart_turn_model
            backchannel_factory = _load_asr_lexicon_backchannel_model
            tts_factory = (
                _load_native_minicpm_tts_adapter
                if args.tts_adapter == "native_minicpm"
                else _load_kokoro_tts_adapter
            )
            asr_factory = _load_asr_model
    else:
        vad_factory = smart_turn_factory = backchannel_factory = tts_factory = asr_factory = None

    # Construct opt-in real adapters. Each construction is eager so a startup
    # failure surfaces immediately rather than at first session. Failures fall
    # back to None (null stub continues). Deictic detector is deferred to
    # startup because it depends on the loaded foreground model — see
    # `_on_startup_finalize_deictic` in build_app.
    real_scene_scorer: Any = None
    real_grounding_model: Any = None
    real_av_conflict_scorer: Any = None
    real_urgency_scorer: Any = None
    real_embedder: Any = None
    real_deictic_model: Any = None
    real_diarization_adapter_factory: Any = None

    if args.live_pipeline and not args.use_stubs:
        if args.enable_clip_scene:
            try:
                from companion_harness.clip_scene_scorer import CLIPSceneChangeScorer  # noqa: WPS433
                real_scene_scorer = CLIPSceneChangeScorer()
            except Exception as exc:
                print(f"CLIPSceneChangeScorer init FAILED: {type(exc).__name__}: {exc}", flush=True)
        if args.enable_grounding:
            try:
                from companion_harness.grounding_dino_adapter import GroundingDINOAdapter  # noqa: WPS433
                real_grounding_model = GroundingDINOAdapter()
            except Exception as exc:
                print(f"GroundingDINOAdapter init FAILED: {type(exc).__name__}: {exc}", flush=True)
        if args.enable_av_conflict:
            try:
                from companion_harness.av_conflict_scorer_heuristic import HeuristicAVConflictScorer  # noqa: WPS433
                real_av_conflict_scorer = HeuristicAVConflictScorer()
            except Exception as exc:
                print(f"HeuristicAVConflictScorer init FAILED: {type(exc).__name__}: {exc}", flush=True)
        if args.enable_urgency:
            try:
                from companion_harness.urgency_scorer_prosody_lexicon import ProsodyLexiconUrgencyScorer  # noqa: WPS433
                real_urgency_scorer = ProsodyLexiconUrgencyScorer()
            except Exception as exc:
                print(f"ProsodyLexiconUrgencyScorer init FAILED: {type(exc).__name__}: {exc}", flush=True)
        if args.enable_embeddings:
            try:
                from companion_harness.embedder_sentence_transformer import SentenceTransformerEmbedder  # noqa: WPS433
                real_embedder = SentenceTransformerEmbedder()
            except Exception as exc:
                print(f"SentenceTransformerEmbedder init FAILED: {type(exc).__name__}: {exc}", flush=True)
        if args.enable_diarization:
            try:
                from companion_harness.diarization_pyannote import PyannoteDiarizationAdapter  # noqa: WPS433
                # Factory so each session gets its own per-session adapter instance.
                real_diarization_adapter_factory = PyannoteDiarizationAdapter
                print("PyannoteDiarizationAdapter: ready (per-session instances)", flush=True)
            except Exception as exc:
                print(f"PyannoteDiarizationAdapter init FAILED: {type(exc).__name__}: {exc}", flush=True)

    # Build seam_defaults from --disable-seam flags.  Unknown seam names are
    # silently ignored here; the HTTP route rejects them at runtime.
    seam_defaults: dict[str, bool] | None = None
    if args.disable_seams:
        seam_defaults = {s: False for s in args.disable_seams if s in HOT_SEAMS}
        unknown = [s for s in args.disable_seams if s not in HOT_SEAMS]
        for s in unknown:
            print(f"WARNING: --disable-seam {s!r}: unknown seam (ignored)", flush=True)
        if not seam_defaults:
            seam_defaults = None

    tts_name = "MiniCPM-o native TTS" if args.tts_adapter == "native_minicpm" else "Kokoro-82M-ONNX"
    app = build_app(
        blob_dir,
        live_pipeline_enabled=args.live_pipeline,
        foreground_model_factory=factory,
        use_stubs=args.use_stubs,
        vad_model_factory=vad_factory,
        smart_turn_model_factory=smart_turn_factory,
        backchannel_model_factory=backchannel_factory,
        tts_adapter_factory=tts_factory,
        tts_adapter_name=tts_name,
        asr_model_factory=asr_factory,
        vision_enabled=args.enable_vision,
        scene_scorer=real_scene_scorer,
        grounding_model=real_grounding_model,
        av_conflict_scorer=real_av_conflict_scorer,
        deictic_model=real_deictic_model,
        urgency_scorer=real_urgency_scorer,
        embedder=real_embedder,
        diarization_adapter_factory=real_diarization_adapter_factory,
        streaming_raw_mode=args.minicpm_streaming_raw,
        seam_defaults=seam_defaults,
        blob_retention_days=args.blob_retention_days,
        event_log_maxsize=args.event_log_maxsize,
        display_sampling_rate=args.display_sampling_rate,
    )
    # Stamp the deictic_model label as "pending-foreground-load" so
    # _on_startup_finalize_deictic knows the operator asked for it.
    if args.live_pipeline and not args.use_stubs and args.enable_deictic:
        app[KEY_ADAPTER_LABELS]["deictic_model"] = "real:pending-foreground-load"

    if args.minicpm_streaming_raw:
        pipeline_label = (
            "MINICPM-STREAMING-RAW (DEMO MODE: SpeakPolicy/ASR/addressing bypassed; "
            "MiniCPM infer_stream → native TTS → audio_out)"
        )
    elif not args.live_pipeline:
        pipeline_label = "disabled"
    elif args.use_stubs:
        pipeline_label = "ENABLED (loading MiniCPM-o + CPU-stub detectors)"
    elif args.minicpm_only:
        pipeline_label = "MINICPM-ONLY (MiniCPM-o + native TTS + ASR; VAD/SmartTurn/Backchannel stubbed)"
    else:
        pipeline_label = "ENABLED (loading MiniCPM-o + real detectors at startup)"
    if args.minicpm_streaming_raw:
        lanes_label = "DEMO MODE: no VAD/ASR/SpeakPolicy — raw audio → MiniCPM → TTS"
    elif args.live_pipeline:
        lanes_label = "VAD, SmartTurn, Backchannel, SpeakPolicy, MiniCPM proposals"
    else:
        lanes_label = "capture-only"

    if args.enable_vision:
        vision_label = "ENABLED (init_vision=True, +~18 GB VRAM)"
    else:
        vision_label = "disabled"

    adapter_flags = ", ".join(
        name for flag, name in (
            (args.enable_clip_scene, "clip-scene"),
            (args.enable_grounding, "grounding"),
            (args.enable_av_conflict, "av-conflict"),
            (args.enable_deictic, "deictic"),
            (args.enable_urgency, "urgency"),
            (args.enable_embeddings, "embeddings"),
            (args.enable_diarization, "diarization"),
        )
        if flag
    ) or "(none — all null stubs)"

    print("=" * 72)
    print("manual-test console — Phase 3 (live-loop pipeline wiring, no voice-back)")
    print("-" * 72)
    print(f"  bind:        {args.host}:{args.port}")
    print(f"  blob store:  {blob_dir}")
    print(f"  python:      {sys.executable}")
    print(f"  live loop:   {pipeline_label}")
    print(f"  sessions:    {lanes_label}")
    print(f"  Vision:      {vision_label}")
    print(f"  adapters:    {adapter_flags}")
    print(f"  open page:   http://localhost:{args.port}/")
    print(f"  ingest WS:   ws://localhost:{args.port}/ws/ingest")
    print(f"  display WS:  ws://localhost:{args.port}/ws/display")
    print(f"  audio_out:   ws://localhost:{args.port}/ws/audio_out")
    print(f"  healthz:     http://localhost:{args.port}/healthz")
    print("=" * 72)
    sys.stdout.flush()

    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
