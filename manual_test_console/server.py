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
import dataclasses
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from aiohttp import WSMsgType, web

from companion_harness.event_logger import EventLogger
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.schemas import Event
from manual_test_console.live_pipeline import LivePipeline, build_live_pipeline

__all__ = ["build_app", "main"]

# Per-display-WS queue depth. Drops oldest on overflow.
_DISPLAY_QUEUE_DEPTH = 256

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
KEY_DETECTOR_LABELS: web.AppKey[dict] = web.AppKey("detector_labels", dict)
KEY_AUDIO_OUT_BROKER: web.AppKey[object] = web.AppKey("audio_out_broker", object)
KEY_AUDIO_OUT_COUNTER: web.AppKey[dict] = web.AppKey("audio_out_counter", dict)
KEY_TTS_ADAPTER: web.AppKey[object] = web.AppKey("tts_adapter", object)
KEY_TTS_LABEL: web.AppKey[str] = web.AppKey("tts_label", str)


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


# ---------------------------------------------------------------------------
# Display subscription machinery
# ---------------------------------------------------------------------------


class DisplayBroker:
    """Tracks connected display WebSockets and fans events out to each.

    Subscribed to EventLogger via callback. The callback runs on the logger's
    drain task and must not block — it does put_nowait per subscriber and
    drops on QueueFull (per-viewer backpressure isolation; the realtime
    ingest path is never blocked).
    """

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[dict]] = []
        self._drops_by_queue: dict[int, int] = {}

    def add(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=_DISPLAY_QUEUE_DEPTH)
        self._queues.append(q)
        self._drops_by_queue[id(q)] = 0
        return q

    def remove(self, q: asyncio.Queue[dict]) -> None:
        if q in self._queues:
            self._queues.remove(q)
        self._drops_by_queue.pop(id(q), None)

    async def on_event(self, event: Event) -> None:
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
    active_pipelines: dict[str, LivePipeline] = request.app[KEY_ACTIVE_PIPELINES]

    client_id = f"ws-{uuid.uuid4().hex[:8]}"
    session = ingest.open_session(client_id)
    chunk_counter["sessions_opened"] = chunk_counter.get("sessions_opened", 0) + 1

    # Per-connection live pipeline (if enabled and a foreground model is available).
    pipeline: LivePipeline | None = None
    if live_enabled and foreground_model is not None:
        pipeline = build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=foreground_model,
            decision_trace_dir=request.app[KEY_BLOB_DIR] / "decision_traces",
            vad_model=request.app[KEY_VAD_MODEL],
            smart_turn_model=request.app[KEY_SMART_TURN_MODEL],
            backchannel_model=request.app[KEY_BACKCHANNEL_MODEL],
            use_stubs=request.app[KEY_USE_STUBS],
            audio_out_broker=request.app[KEY_AUDIO_OUT_BROKER],  # type: ignore[arg-type]
            tts_adapter=request.app[KEY_TTS_ADAPTER],
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
                    pipeline.push_audio(payload_bytes, evt.event_id)
            else:  # raw_video
                ingest.ingest_video_frame(session, payload_bytes, meta)
                chunk_counter["frames_ingested"] = chunk_counter.get("frames_ingested", 0) + 1
    finally:
        if pipeline is not None:
            try:
                await pipeline.stop()
            except Exception:
                pass
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


async def _handle_health(request: web.Request) -> web.Response:
    chunk_counter: dict[str, int] = request.app[KEY_CHUNK_COUNTER]
    logger: EventLogger = request.app[KEY_LOGGER]  # type: ignore[assignment]
    live_enabled: bool = request.app[KEY_LIVE_PIPELINE_ENABLED]
    foreground_model = request.app[KEY_FOREGROUND_MODEL]
    active_pipelines: dict[str, LivePipeline] = request.app[KEY_ACTIVE_PIPELINES]
    detector_labels: dict[str, str] = request.app[KEY_DETECTOR_LABELS]
    audio_out_counter: dict[str, int] = request.app[KEY_AUDIO_OUT_COUNTER]
    tts_label: str = request.app[KEY_TTS_LABEL]
    return web.json_response({
        "status": "ok",
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
        "audio_out_enabled": True,
        "audio_out_chunks_sent": audio_out_counter.get("chunks_sent", 0),
        "tts_model": tts_label,
    })


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
    broker = DisplayBroker()
    logger = EventLogger(_null_sink, maxsize=4096)
    logger.subscribe(broker.on_event)
    ingest = InputIngest(logger, blob_dir)

    audio_out_counter: dict[str, int] = {"chunks_sent": 0, "chunks_dropped": 0}
    audio_out_broker = AudioOutBroker(audio_out_counter)

    app[KEY_BROKER] = broker
    app[KEY_LOGGER] = logger
    app[KEY_INGEST] = ingest
    app[KEY_BLOB_DIR] = blob_dir
    app[KEY_CHUNK_COUNTER] = {"sessions_opened": 0, "chunks_ingested": 0, "frames_ingested": 0}
    app[KEY_AUDIO_OUT_BROKER] = audio_out_broker
    app[KEY_AUDIO_OUT_COUNTER] = audio_out_counter
    app[KEY_LIVE_PIPELINE_ENABLED] = live_pipeline_enabled
    app[KEY_FOREGROUND_MODEL] = foreground_model
    app[KEY_ACTIVE_PIPELINES] = {}
    app[KEY_USE_STUBS] = use_stubs
    app[KEY_VAD_MODEL] = None
    app[KEY_SMART_TURN_MODEL] = None
    app[KEY_BACKCHANNEL_MODEL] = None
    app[KEY_DETECTOR_LABELS] = {
        "vad": "stub:EnergyVAD" if use_stubs else "stub:EnergyVAD",
        "smart_turn": "stub:SilenceSmartTurn" if use_stubs else "stub:SilenceSmartTurn",
        "backchannel": "stub:ZeroBackchannel" if use_stubs else "stub:ZeroBackchannel",
    }
    app[KEY_TTS_ADAPTER] = tts_adapter
    app[KEY_TTS_LABEL] = "stub:NoopTtsAdapter" if (tts_adapter is None or use_stubs) else "injected"

    app.router.add_get("/", _handle_index)
    app.router.add_get("/healthz", _handle_health)
    app.router.add_get("/ws/ingest", _handle_ingest_ws)
    app.router.add_get("/ws/display", _handle_display_ws)
    app.router.add_get("/ws/audio_out", _handle_audio_out_ws)

    async def _on_startup(_app: web.Application) -> None:
        await logger.start()
        if live_pipeline_enabled and foreground_model is None and foreground_model_factory is not None:
            print("Loading MiniCPM-o foreground model (this may take minutes)...", flush=True)
            t0 = time.monotonic()
            try:
                model = foreground_model_factory()
            except Exception as exc:
                print(f"MiniCPM-o load FAILED: {type(exc).__name__}: {exc}", flush=True)
                print("Falling back to capture-only mode (no live pipeline).", flush=True)
                _app[KEY_LIVE_PIPELINE_ENABLED] = False
                return
            elapsed = time.monotonic() - t0
            _app[KEY_FOREGROUND_MODEL] = model
            print(f"MiniCPM-o loaded in {elapsed:.1f}s", flush=True)

        if not _app[KEY_LIVE_PIPELINE_ENABLED] or _app[KEY_USE_STUBS]:
            # Stub/disabled mode: ensure tts_adapter falls back to Noop.
            if _app[KEY_TTS_ADAPTER] is None:
                from manual_test_console.live_pipeline import NoopTtsAdapter  # noqa: WPS433
                _app[KEY_TTS_ADAPTER] = NoopTtsAdapter()
                _app[KEY_TTS_LABEL] = "stub:NoopTtsAdapter"
            return

        # Load TTS singleton (Kokoro by default). Falls back to NoopTtsAdapter
        # on failure so the server still starts (voice-back simply silent).
        if _app[KEY_TTS_ADAPTER] is None and tts_adapter_factory is not None:
            print("Loading Kokoro-82M-ONNX TTS adapter...", flush=True)
            t0 = time.monotonic()
            try:
                _app[KEY_TTS_ADAPTER] = tts_adapter_factory()
            except Exception as exc:
                print(
                    f"Kokoro TTS load FAILED: {type(exc).__name__}: {exc} "
                    "— falling back to stub:NoopTtsAdapter (voice-back disabled)",
                    flush=True,
                )
                from manual_test_console.live_pipeline import NoopTtsAdapter  # noqa: WPS433
                _app[KEY_TTS_ADAPTER] = NoopTtsAdapter()
                _app[KEY_TTS_LABEL] = "stub:NoopTtsAdapter"
            else:
                elapsed = time.monotonic() - t0
                _app[KEY_TTS_LABEL] = f"Kokoro-82M-ONNX (loaded in {elapsed:.2f}s)"
                print(f"Kokoro-82M-ONNX loaded in {elapsed:.2f}s", flush=True)
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

    async def _on_startup_summary(_app: web.Application) -> None:
        labels = _app[KEY_DETECTOR_LABELS]
        print("-" * 72, flush=True)
        print(f"  VAD:         {labels.get('vad', 'unknown')}", flush=True)
        print(f"  SmartTurn:   {labels.get('smart_turn', 'unknown')}", flush=True)
        print(f"  Backchannel: {labels.get('backchannel', 'unknown')}", flush=True)
        print(f"  TTS:         {_app[KEY_TTS_LABEL]}", flush=True)
        print("=" * 72, flush=True)

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
    app.on_startup.append(_on_startup_summary)
    app.on_cleanup.append(_on_cleanup)
    return app


def _load_minicpm_streaming_model() -> Any:
    """Lazy import + construct MiniCPMStreamingModel. b200 only.

    Imported here (not at module top) so the server module remains importable
    on machines without torch/CUDA. Tests never reach this path.
    """
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel  # noqa: WPS433
    return MiniCPMStreamingModel()


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
            "(EnergyVAD / SilenceSmartTurn / ZeroBackchannel) "
            "instead of loading Silero / Pipecat SmartTurn v3 / whisper-tiny."
        ),
    )
    args = parser.parse_args(argv)

    blob_dir: Path = args.blob_dir
    blob_dir.mkdir(parents=True, exist_ok=True)

    factory = _load_minicpm_streaming_model if args.live_pipeline else None
    if args.live_pipeline and not args.use_stubs:
        vad_factory: Optional[Callable[[], Any]] = _load_silero_vad_model
        smart_turn_factory: Optional[Callable[[], Any]] = _load_pipecat_smart_turn_model
        backchannel_factory: Optional[Callable[[], Any]] = _load_asr_lexicon_backchannel_model
        tts_factory: Optional[Callable[[], Any]] = _load_kokoro_tts_adapter
    else:
        vad_factory = smart_turn_factory = backchannel_factory = tts_factory = None

    app = build_app(
        blob_dir,
        live_pipeline_enabled=args.live_pipeline,
        foreground_model_factory=factory,
        use_stubs=args.use_stubs,
        vad_model_factory=vad_factory,
        smart_turn_model_factory=smart_turn_factory,
        backchannel_model_factory=backchannel_factory,
        tts_adapter_factory=tts_factory,
    )

    if not args.live_pipeline:
        pipeline_label = "disabled"
    elif args.use_stubs:
        pipeline_label = "ENABLED (loading MiniCPM-o + CPU-stub detectors)"
    else:
        pipeline_label = "ENABLED (loading MiniCPM-o + real detectors at startup)"
    lanes_label = (
        "VAD, SmartTurn, Backchannel, SpeakPolicy, MiniCPM proposals"
        if args.live_pipeline
        else "capture-only"
    )

    print("=" * 72)
    print("manual-test console — Phase 3 (live-loop pipeline wiring, no voice-back)")
    print("-" * 72)
    print(f"  bind:        {args.host}:{args.port}")
    print(f"  blob store:  {blob_dir}")
    print(f"  python:      {sys.executable}")
    print(f"  live loop:   {pipeline_label}")
    print(f"  sessions:    {lanes_label}")
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
