#!/usr/bin/env python3
"""WS-only repro driver for findings F0a (duplicate TTS) and F0b (wrong answer).

Three modes:

  passive            — connect to /ws/display + /ws/audio_out and record
                       everything for --duration seconds while a human drives
                       the browser mic.  Use to capture spontaneous F0a/F0b.

  auto               — synthesize N utterances with Kokoro, stream them into
                       /ws/ingest at wall-clock cadence, record everything,
                       run analysis.  Turn 2 is submitted immediately after
                       turn 1's trailing silence, so it typically dies on
                       synthesis_skipped_no_proposal before the F0a guard is
                       exercised.  Use to verify baseline pipeline health.

  auto-wait-for-tts  — same as auto but turn 2 fires only AFTER turn 1's
                       tts_synthesis_started event appears on /ws/display,
                       optionally with an additional --turn2-delay-after-tts-
                       start-ms offset (default 150 ms).  This puts turn 2
                       inside the real Kokoro playback window so the F0a guard
                       (_decision_in_flight = False in finally:) is actually
                       exercised.  Emits a 4-assertion PASS/FAIL report and
                       exits 0 on all PASS, 1 on any FAIL.
                       Designed to close pre-risk #1 from
                       docs/basic-stack-design-2026-05-16.md.

Sample invocations:
  python tests/manual/repro_f0a_f0b.py auto
  python tests/manual/repro_f0a_f0b.py auto-wait-for-tts \\
      --turn2-delay-after-tts-start-ms 150 \\
      --out-dir /tmp/f0a-driver-verify \\
      --duration 20

Outputs under --out-dir (default: /tmp/repro-<ts>/):
  events.jsonl       — every event from /ws/display
  audio_out.jsonl    — one row per TTS chunk on /ws/audio_out
  report.md          — F0a + F0b analysis (passive/auto) or 4-assertion
                       PASS/FAIL report (auto-wait-for-tts)

Server defaults to http://localhost:8800.  The console must already be running;
this script will not start it.  If /healthz is not 200, the script exits with
code 2.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np
import soxr

DEFAULT_HOST = "ws://localhost:8800"
INGEST_SAMPLE_RATE = 16000
CHUNK_MS = 30
CHUNK_SAMPLES = INGEST_SAMPLE_RATE * CHUNK_MS // 1000  # 480 samples at 16k
CHUNK_BYTES = CHUNK_SAMPLES * 2  # int16

# Test utterances chosen because whisper-tiny.en transcribes them reliably and
# the second one is content-distinguishable from the first — so F0b's
# wrong-answer pattern is detectable.
AUTO_UTTERANCES = [
    "What is two plus two?",
    "And what about three plus three?",
]
INTER_UTTERANCE_SILENCE_MS_DEFAULT = 800


@dataclass
class Recording:
    started_mono: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    audio_out: list[dict[str, Any]] = field(default_factory=list)


_KOKORO_ADAPTER: Any = None


def _kokoro() -> Any:
    global _KOKORO_ADAPTER
    if _KOKORO_ADAPTER is None:
        import os
        from companion_harness.tts_kokoro import KokoroTtsAdapter
        model_path = os.environ.get(
            "KOKORO_MODEL_PATH", "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
        )
        voices_path = os.environ.get(
            "KOKORO_VOICES_PATH", "/raid/yid042/models/kokoro/voices.json"
        )
        _KOKORO_ADAPTER = KokoroTtsAdapter(
            model_path=model_path, voices_path=voices_path, warmup=False
        )
    return _KOKORO_ADAPTER


async def _synth_pcm16_16k(text: str) -> bytes:
    """Synthesize `text` with Kokoro (24k float32) and resample to 16k PCM16."""
    adapter = _kokoro()
    out = bytearray()
    async for chunk in adapter.synthesize(text, prosody_tags=[]):
        out.extend(chunk)
    samples_24k = np.frombuffer(bytes(out), dtype="<i2").astype(np.float32) / 32768.0
    samples_16k = soxr.resample(samples_24k, 24000, INGEST_SAMPLE_RATE)
    samples_16k = np.clip(samples_16k, -1.0, 1.0)
    return (samples_16k * 32767.0).astype("<i2").tobytes()


async def _recorder(ws: aiohttp.ClientWebSocketResponse, sink: list[dict[str, Any]], started: float) -> None:
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            continue
        sink.append({"t_recv_ms": int((time.monotonic() - started) * 1000), "msg": payload})


async def _stream_audio(
    ws: aiohttp.ClientWebSocketResponse,
    pcm16_bytes: bytes,
    client_id: str,
    label: str,
    trailing_silence_ms: int = INTER_UTTERANCE_SILENCE_MS_DEFAULT,
) -> None:
    """Stream raw PCM16 16k as 30ms JSON envelopes at wall-clock cadence."""
    t0 = time.monotonic()
    sent = 0
    chunk_idx = 0
    while sent + CHUNK_BYTES <= len(pcm16_bytes):
        chunk = pcm16_bytes[sent : sent + CHUNK_BYTES]
        sent += CHUNK_BYTES
        envelope = {
            "event_type": "raw_audio",
            "payload_inline_or_ref": base64.b64encode(chunk).decode("ascii"),
            "timestamp_mono_ms": int((time.monotonic() - t0) * 1000),
            "client_id": client_id,
            "device_label": label,
        }
        await ws.send_json(envelope)
        chunk_idx += 1
        # Pace at wall-clock to mimic real microphone cadence.
        target_t = t0 + (chunk_idx * CHUNK_MS) / 1000.0
        delay = target_t - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
    # Trailing silence so VAD/SmartTurn fires the EOU.
    silence = b"\x00" * CHUNK_BYTES
    for _ in range(trailing_silence_ms // CHUNK_MS):
        envelope = {
            "event_type": "raw_audio",
            "payload_inline_or_ref": base64.b64encode(silence).decode("ascii"),
            "timestamp_mono_ms": int((time.monotonic() - t0) * 1000),
            "client_id": client_id,
            "device_label": label,
        }
        await ws.send_json(envelope)
        await asyncio.sleep(CHUNK_MS / 1000.0)


def _analyze_f0a(rec: Recording) -> dict[str, Any]:
    """Detect overlapping TTS streams on /ws/audio_out.

    Heuristic: group audio_out rows by (session_id, contiguous-seq-run). A run
    breaks when seq resets to 0 or jumps non-monotonically. Each run's wall-clock
    span is [first.t_recv_ms, last.t_recv_ms]. Overlap = any two runs whose
    spans intersect.
    """
    rows = [r for r in rec.audio_out if r["msg"].get("type") == "audio_chunk"]
    # Group into contiguous-seq runs per session.
    runs: list[dict[str, Any]] = []
    by_sess: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        sid = r["msg"].get("session_id", "?")
        by_sess.setdefault(sid, []).append(r)
    for sid, rs in by_sess.items():
        rs.sort(key=lambda r: r["t_recv_ms"])
        cur: list[dict[str, Any]] = []
        prev_seq: int | None = None
        for r in rs:
            seq = int(r["msg"].get("seq", 0))
            if prev_seq is not None and seq <= prev_seq:
                if cur:
                    runs.append(_summarize_run(sid, cur))
                cur = []
            cur.append(r)
            prev_seq = seq
        if cur:
            runs.append(_summarize_run(sid, cur))
    # Find overlapping pairs.
    runs.sort(key=lambda x: x["start_ms"])
    overlaps: list[tuple[int, int]] = []
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            a, b = runs[i], runs[j]
            if b["start_ms"] < a["end_ms"]:
                overlaps.append((i, j))
    return {"runs": runs, "overlaps": overlaps}


def _summarize_run(session_id: str, run: list[dict[str, Any]]) -> dict[str, Any]:
    seqs = [int(r["msg"]["seq"]) for r in run]
    total_bytes = sum(
        len(base64.b64decode(r["msg"].get("pcm_bytes_b64", ""))) for r in run
    )
    return {
        "session_id": session_id,
        "start_ms": run[0]["t_recv_ms"],
        "end_ms": run[-1]["t_recv_ms"],
        "duration_ms": run[-1]["t_recv_ms"] - run[0]["t_recv_ms"],
        "chunk_count": len(run),
        "seq_first": seqs[0],
        "seq_last": seqs[-1],
        "total_pcm_bytes": total_bytes,
    }


def _unwrap(row: dict[str, Any]) -> dict[str, Any]:
    """Return the inner event dict from a display-WS row (handles {kind,event} wrapper)."""
    msg = row.get("msg", {})
    return msg.get("event") if isinstance(msg.get("event"), dict) else msg


def _analyze_f0b(rec: Recording) -> dict[str, Any]:
    """Pair each `asr_transcript_emitted` with downstream events via caused_by chain.

    Walks the DAG transitively: any event reachable from the transcript's
    event_id via repeated caused_by[] expansion is considered downstream.
    """
    transcripts: list[dict[str, Any]] = []
    for row in rec.events:
        ev = _unwrap(row)
        if (ev.get("event_type") or "") == "asr_transcript_emitted":
            transcripts.append(row)

    # Build event_id -> row index for fast lookup.
    by_id: dict[str, dict[str, Any]] = {}
    for row in rec.events:
        ev = _unwrap(row)
        eid = ev.get("event_id")
        if eid:
            by_id[eid] = row

    interesting = {
        "policy_decision", "foreground_proposal", "tts_synthesis_started",
        "tts_synthesis_completed", "assistant_generation_start",
        "addressing_classified", "decision_trace_emitted",
    }

    pairs: list[dict[str, Any]] = []
    for trow in transcripts:
        tev = _unwrap(trow)
        tid = tev.get("event_id")
        reachable: set[str] = set()
        if tid:
            frontier = {tid}
            while frontier:
                nxt: set[str] = set()
                for row in rec.events:
                    ev = _unwrap(row)
                    if ev.get("event_id") in reachable:
                        continue
                    if any(c in frontier for c in (ev.get("caused_by") or [])):
                        nxt.add(ev.get("event_id"))
                reachable.update(nxt)
                frontier = nxt

        downstream: list[dict[str, Any]] = []
        for row in rec.events:
            ev = _unwrap(row)
            if ev.get("event_id") in reachable and (ev.get("event_type") or "") in interesting:
                downstream.append({
                    "t_recv_ms": row["t_recv_ms"],
                    "type": ev.get("event_type"),
                    "text": _extract_text(ev),
                    "event_id": ev.get("event_id"),
                    "caused_by": ev.get("caused_by") or [],
                })
        downstream.sort(key=lambda d: d["t_recv_ms"])
        pairs.append({
            "transcript_t_ms": trow["t_recv_ms"],
            "transcript_text": _extract_text(tev),
            "transcript_event_id": tid,
            "downstream": downstream,
        })
    return {"pairs": pairs}


def _extract_text(ev: dict[str, Any]) -> str:
    """Best-effort one-line summary of an event for the report.

    /ws/display strips free-text payloads (SensitiveField rule), so for most
    event types we only have payload_hash. policy_decision is the exception —
    it carries an inline {action_type, primary_reason_code}.
    """
    inline = ev.get("payload_inline")
    if isinstance(inline, dict):
        action = inline.get("action_type")
        reason = inline.get("primary_reason_code")
        if action or reason:
            return f"{action or '?'} / {reason or '?'}"
    # Fall back to payload_hash so each row has at least a stable fingerprint.
    h = ev.get("payload_hash") or ""
    return f"hash={h[:12]}" if h else ""


def _count_by_type(rec: Recording) -> list[tuple[str, int]]:
    from collections import Counter
    c: Counter[str] = Counter()
    for row in rec.events:
        ev = _unwrap(row)
        c[ev.get("event_type") or "?"] += 1
    return c.most_common()


def _write_report(out_dir: Path, rec: Recording, mode: str) -> None:
    f0a = _analyze_f0a(rec)
    f0b = _analyze_f0b(rec)
    by_type = _count_by_type(rec)
    drops = next((c for t, c in by_type if t == "log_drop_or_degrade"), 0)

    lines: list[str] = []
    lines.append(f"# Repro report — mode={mode}")
    lines.append("")
    lines.append(f"- events captured: {len(rec.events)}")
    lines.append(f"- audio_out rows: {len(rec.audio_out)}")
    if drops:
        lines.append(f"- **`log_drop_or_degrade` events: {drops}** (invariant 10: logger backpressure — high count means events were dropped during recording)")
    lines.append("")
    lines.append("## Event-type histogram")
    lines.append("")
    lines.append("| count | event_type |")
    lines.append("|---|---|")
    for t, c in by_type[:25]:
        lines.append(f"| {c} | `{t}` |")
    lines.append("")
    lines.append("## F0a — TTS stream overlap")
    lines.append("")
    if not f0a["runs"]:
        lines.append("_no TTS runs detected_")
    else:
        lines.append("| # | session_id | start_ms | end_ms | dur_ms | seq_first..last | chunks | bytes |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(f0a["runs"]):
            lines.append(
                f"| {i} | `{r['session_id'][:8]}` | {r['start_ms']} | {r['end_ms']} | "
                f"{r['duration_ms']} | {r['seq_first']}..{r['seq_last']} | "
                f"{r['chunk_count']} | {r['total_pcm_bytes']} |"
            )
        lines.append("")
        if f0a["overlaps"]:
            lines.append(f"**F0a OVERLAP DETECTED:** {len(f0a['overlaps'])} pair(s)")
            for i, j in f0a["overlaps"]:
                a, b = f0a["runs"][i], f0a["runs"][j]
                lines.append(
                    f"- runs #{i} and #{j} overlap "
                    f"({a['start_ms']}–{a['end_ms']} vs {b['start_ms']}–{b['end_ms']})"
                )
        else:
            lines.append("no temporal overlap between TTS runs.")
    lines.append("")
    lines.append("## F0b — transcript → downstream alignment")
    lines.append("")
    if not f0b["pairs"]:
        lines.append("_no asr_transcript events seen — pipeline likely produced none._")
    else:
        for p in f0b["pairs"]:
            lines.append(f"### transcript @ {p['transcript_t_ms']} ms")
            lines.append(f"- text: `{p['transcript_text']}`")
            lines.append(f"- event_id: `{p['transcript_event_id']}`")
            if not p["downstream"]:
                lines.append("- **no downstream events with this caused_by** (F0b indicator if a TTS run nonetheless happened)")
            else:
                for d in p["downstream"]:
                    lines.append(
                        f"  - +{d['t_recv_ms'] - p['transcript_t_ms']}ms · {d['type']} · `{d['text']}`"
                    )
            lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# auto-wait-for-tts mode
# ---------------------------------------------------------------------------

async def _wait_for_tts_started(
    rec: Recording,
    timeout_s: float,
) -> dict[str, Any] | None:
    """Poll rec.events until a tts_synthesis_started row appears; return it or None."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for row in rec.events:
            ev = _unwrap(row)
            if (ev.get("event_type") or "") == "tts_synthesis_started":
                return row
        await asyncio.sleep(0.05)
    return None


async def _wait_for_terminal(
    rec: Recording,
    timeout_s: float,
) -> None:
    """Wait until both turn-1 and turn-2 terminal events land, or timeout."""
    terminal_types = {
        "tts_synthesis_completed",
        "coalesced_during_playback",
        "synthesis_skipped_no_proposal",
    }
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        types_seen = {
            _unwrap(r).get("event_type") for r in rec.events
        }
        # We need at least two distinct terminal events (one per turn).
        terminal_count = sum(
            1 for r in rec.events
            if (_unwrap(r).get("event_type") or "") in terminal_types
        )
        if terminal_count >= 2:
            return
        await asyncio.sleep(0.1)


def _assert_f0a_report(rec: Recording, out_dir: Path) -> int:
    """Write the 4-assertion PASS/FAIL report; return exit code (0=all pass, 1=any fail)."""
    events = rec.events
    audio_out = rec.audio_out

    # --- Assertion 1: exactly 1 tts_synthesis_started -----------------------
    tts_started_rows = [
        r for r in events
        if (_unwrap(r).get("event_type") or "") == "tts_synthesis_started"
    ]
    a1_count = len(tts_started_rows)
    a1_pass = a1_count == 1

    # --- Assertion 2: at least 1 coalesced_during_playback ------------------
    coalesced_rows = [
        r for r in events
        if (_unwrap(r).get("event_type") or "") == "coalesced_during_playback"
    ]
    a2_count = len(coalesced_rows)
    a2_pass = a2_count >= 1

    # --- Assertion 3: is_playing was True when turn-2 policy_decision arrived
    # Proxy: find the first policy_decision that is NOT in the causal chain of
    # turn 1's tts_synthesis_started (i.e. arrived after turn 2 was submitted).
    # Simpler heuristic: take the SECOND policy_decision by t_recv_ms, check
    # that there is an assistant_audio_buffer_queued event within <=300 ms
    # before it.
    policy_rows = sorted(
        [r for r in events if (_unwrap(r).get("event_type") or "") == "policy_decision"],
        key=lambda r: r["t_recv_ms"],
    )
    audio_buf_rows = sorted(
        [r for r in events
         if (_unwrap(r).get("event_type") or "") == "assistant_audio_buffer_queued"],
        key=lambda r: r["t_recv_ms"],
    )
    if len(policy_rows) >= 2:
        turn2_policy_t = policy_rows[1]["t_recv_ms"]
        # Most-recent audio_buffer_queued event at or before turn2 policy arrival.
        before = [r for r in audio_buf_rows if r["t_recv_ms"] <= turn2_policy_t]
        if before:
            gap_ms = turn2_policy_t - before[-1]["t_recv_ms"]
            a3_pass = gap_ms <= 300
            a3_detail = f"gap={gap_ms} ms (<=300 required)"
        else:
            a3_pass = False
            a3_detail = "no assistant_audio_buffer_queued before turn-2 policy_decision"
    else:
        a3_pass = False
        a3_detail = f"only {len(policy_rows)} policy_decision event(s) found (need >=2)"

    # --- Assertion 4: no second-utterance audio interleaved -----------------
    # If assertion 1 passes (exactly 1 tts_synthesis_started), the audio stream
    # necessarily belongs to turn 1 alone.  We additionally verify that the
    # audio_out seq numbers are monotonically non-decreasing within each
    # session, which rules out a reset that would indicate a second stream
    # sneaking through.
    audio_chunk_rows = [r for r in audio_out if r["msg"].get("type") == "audio_chunk"]
    by_sess: dict[str, list[int]] = {}
    for r in audio_chunk_rows:
        sid = r["msg"].get("session_id", "?")
        by_sess.setdefault(sid, []).append(int(r["msg"].get("seq", 0)))
    non_monotonic_sessions: list[str] = []
    for sid, seqs in by_sess.items():
        for i in range(1, len(seqs)):
            if seqs[i] <= seqs[i - 1]:
                non_monotonic_sessions.append(sid)
                break
    a4_pass = a1_pass and not non_monotonic_sessions
    if not a1_pass:
        a4_detail = "skipped — assertion 1 already failed (second tts_synthesis_started detected)"
    elif non_monotonic_sessions:
        a4_detail = f"seq non-monotonic in sessions: {non_monotonic_sessions}"
    else:
        a4_detail = f"{len(audio_chunk_rows)} chunk(s) across {len(by_sess)} session(s), all seq monotonic"

    # --- Build report --------------------------------------------------------
    by_type = _count_by_type(rec)
    drops = next((c for t, c in by_type if t == "log_drop_or_degrade"), 0)

    all_pass = a1_pass and a2_pass and a3_pass and a4_pass

    lines: list[str] = []
    lines.append("# Repro report — mode=auto-wait-for-tts")
    lines.append("")
    lines.append(f"- events captured: {len(events)}")
    lines.append(f"- audio_out rows: {len(audio_out)}")
    if drops:
        lines.append(
            f"- **`log_drop_or_degrade` events: {drops}**"
            " (invariant 10: logger backpressure)"
        )
    lines.append("")
    lines.append("## Event-type histogram")
    lines.append("")
    lines.append("| count | event_type |")
    lines.append("|---|---|")
    for t, c in by_type[:25]:
        lines.append(f"| {c} | `{t}` |")
    lines.append("")
    lines.append("## F0a guard assertions")
    lines.append("")

    def _row(n: int, label: str, passed: bool, detail: str) -> str:
        mark = "PASS" if passed else "FAIL"
        return f"| {n} | {label} | **{mark}** | {detail} |"

    lines.append("| # | Assertion | Result | Detail |")
    lines.append("|---|---|---|---|")
    lines.append(_row(
        1,
        "Exactly 1 `tts_synthesis_started`",
        a1_pass,
        f"tts_synthesis_started count: {a1_count}",
    ))
    lines.append(_row(
        2,
        "At least 1 `coalesced_during_playback`",
        a2_pass,
        f"coalesced_during_playback count: {a2_count}",
    ))
    lines.append(_row(
        3,
        "`is_playing` True at turn-2 policy_decision",
        a3_pass,
        a3_detail,
    ))
    lines.append(_row(
        4,
        "No turn-2 audio interleaved with turn-1 playback",
        a4_pass,
        a4_detail,
    ))
    lines.append("")
    lines.append(f"**Overall: {'4/4 PASS' if all_pass else 'FAIL'}**")
    lines.append("")
    if not a1_pass:
        if a1_count == 0:
            lines.append(
                "- Assertion 1 FAIL: no TTS started — pipeline may not have "
                "reached synthesis. Check synthesis_skipped_no_proposal count."
            )
        else:
            lines.append(
                f"- Assertion 1 FAIL: {a1_count} tts_synthesis_started events. "
                "A second synthesis slipped through — F0a (_decision_in_flight "
                "guard) may have regressed."
            )
    if not a2_pass:
        lines.append(
            "- Assertion 2 FAIL: no coalesced_during_playback. "
            "Turn 2 did not arrive while turn 1 was playing. "
            "Increase --turn2-delay-after-tts-start-ms or check that "
            "turn 1's TTS is long enough to still be playing when turn 2 "
            "arrives. Also check F0c (barge-in regression) if coalescing "
            "should have triggered."
        )
    if not a3_pass:
        lines.append(
            "- Assertion 3 FAIL: is_playing may not have been True when "
            "turn 2's policy_decision was evaluated. Race window may have "
            "closed before turn 2 reached policy. Check "
            "assistant_audio_buffer_queued timestamps."
        )
    if not a4_pass and a1_pass:
        lines.append(
            "- Assertion 4 FAIL: audio_out seq non-monotonic — a second "
            "TTS stream may have interleaved. Cross-reference with "
            "assertion 1 and F0a audio-overlap analysis."
        )

    (out_dir / "report.md").write_text("\n".join(lines))
    return 0 if all_pass else 1


async def run_auto_wait_for_tts(args: argparse.Namespace) -> int:
    # Verify console is up.
    http_base = args.host.replace("ws://", "http://").replace("wss://", "https://")
    try:
        async with aiohttp.ClientSession() as probe:
            resp = await probe.get(f"{http_base}/healthz", timeout=aiohttp.ClientTimeout(total=5))
            if resp.status != 200:
                print(
                    f"ERROR: /healthz returned {resp.status}. "
                    "Is the console running at {http_base}?",
                    file=sys.stderr,
                )
                return 2
    except Exception as exc:
        print(
            f"ERROR: could not reach {http_base}/healthz: {exc}. "
            "Start the console first.",
            file=sys.stderr,
        )
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = Recording(started_mono=time.monotonic())

    turn1_text = AUTO_UTTERANCES[0]
    turn2_text = args.turn2_text if args.turn2_text else AUTO_UTTERANCES[1]

    print(f"[auto-wait-for-tts] synthesizing turn 1: {turn1_text!r}", file=sys.stderr)
    turn1_pcm = await _synth_pcm16_16k(turn1_text)
    print(f"[auto-wait-for-tts] synthesizing turn 2: {turn2_text!r}", file=sys.stderr)
    turn2_pcm = await _synth_pcm16_16k(turn2_text)

    async with aiohttp.ClientSession() as http:
        display_ws = await http.ws_connect(f"{args.host}/ws/display", heartbeat=30.0)
        audio_out_ws = await http.ws_connect(f"{args.host}/ws/audio_out", heartbeat=30.0)

        display_task = asyncio.create_task(
            _recorder(display_ws, rec.events, rec.started_mono)
        )
        audio_task = asyncio.create_task(
            _recorder(audio_out_ws, rec.audio_out, rec.started_mono)
        )

        try:
            client_id = f"repro-wft-{int(time.time())}"

            # Turn 1: open a fresh ingest WS, stream, keep alive (the server
            # correlates frames to the same session while the WS stays open).
            ingest_ws1 = await http.ws_connect(f"{args.host}/ws/ingest", heartbeat=30.0)
            print(
                f"[auto-wait-for-tts] streaming turn 1 "
                f"({len(turn1_pcm)/2/INGEST_SAMPLE_RATE:.2f}s)",
                file=sys.stderr,
            )
            await _stream_audio(
                ingest_ws1, turn1_pcm, client_id, "repro-mic-0",
                trailing_silence_ms=INTER_UTTERANCE_SILENCE_MS_DEFAULT,
            )
            # Close turn-1 ingest now; the server has the full audio and will
            # continue processing.  We close here so that turn-2 can open its
            # own session (fresh client_id would also work, but closing is
            # cleaner and matches manual-user behavior).
            await ingest_ws1.close()

            # Wait for tts_synthesis_started to appear in the event stream.
            print(
                "[auto-wait-for-tts] waiting for tts_synthesis_started …",
                file=sys.stderr,
            )
            tts_row = await _wait_for_tts_started(rec, timeout_s=args.duration * 0.6)
            if tts_row is None:
                print(
                    "WARNING: tts_synthesis_started not seen within timeout. "
                    "Turn 2 will fire anyway but assertion 1 will likely FAIL.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[auto-wait-for-tts] tts_synthesis_started seen at "
                    f"+{tts_row['t_recv_ms']} ms",
                    file=sys.stderr,
                )

            extra_delay_s = args.turn2_delay_after_tts_start_ms / 1000.0
            if extra_delay_s > 0:
                print(
                    f"[auto-wait-for-tts] waiting additional "
                    f"{args.turn2_delay_after_tts_start_ms} ms before turn 2 …",
                    file=sys.stderr,
                )
                await asyncio.sleep(extra_delay_s)

            # Warn if audio buffer looks stale (race window may have closed).
            audio_buf_rows = [
                r for r in rec.events
                if (_unwrap(r).get("event_type") or "") == "assistant_audio_buffer_queued"
            ]
            if audio_buf_rows:
                last_buf_ms = max(r["t_recv_ms"] for r in audio_buf_rows)
                now_ms = int((time.monotonic() - rec.started_mono) * 1000)
                if now_ms - last_buf_ms > 500:
                    print(
                        f"WARNING: last assistant_audio_buffer_queued was "
                        f"{now_ms - last_buf_ms} ms ago — race window may have "
                        f"closed. Assertion 3 may FAIL.",
                        file=sys.stderr,
                    )

            # Turn 2: fresh ingest WS.
            ingest_ws2 = await http.ws_connect(f"{args.host}/ws/ingest", heartbeat=30.0)
            print(
                f"[auto-wait-for-tts] streaming turn 2 "
                f"({len(turn2_pcm)/2/INGEST_SAMPLE_RATE:.2f}s)",
                file=sys.stderr,
            )
            await _stream_audio(
                ingest_ws2, turn2_pcm, client_id, "repro-mic-1",
                trailing_silence_ms=INTER_UTTERANCE_SILENCE_MS_DEFAULT,
            )
            await ingest_ws2.close()

            # Wait for terminal events on both turns, hard-bounded by --duration.
            remaining = args.duration - (time.monotonic() - rec.started_mono)
            await _wait_for_terminal(rec, timeout_s=max(remaining, 2.0))

        finally:
            for w in (display_ws, audio_out_ws):
                try:
                    await w.close()
                except Exception:
                    pass
            for t in (display_task, audio_task):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    (out_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rec.events) + "\n"
    )
    (out_dir / "audio_out.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rec.audio_out) + "\n"
    )
    exit_code = _assert_f0a_report(rec, out_dir)
    print(f"\nwrote {out_dir}/events.jsonl ({len(rec.events)} rows)", file=sys.stderr)
    print(f"wrote {out_dir}/audio_out.jsonl ({len(rec.audio_out)} rows)", file=sys.stderr)
    print(f"wrote {out_dir}/report.md", file=sys.stderr)
    return exit_code


async def _main_async(args: argparse.Namespace) -> int:
    if args.mode == "auto-wait-for-tts":
        return await run_auto_wait_for_tts(args)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = Recording(started_mono=time.monotonic())

    async with aiohttp.ClientSession() as http:
        # Subscribe first so we don't miss early events.
        display_ws = await http.ws_connect(f"{args.host}/ws/display", heartbeat=30.0)
        audio_out_ws = await http.ws_connect(f"{args.host}/ws/audio_out", heartbeat=30.0)
        ingest_ws = await http.ws_connect(f"{args.host}/ws/ingest", heartbeat=30.0)

        display_task = asyncio.create_task(
            _recorder(display_ws, rec.events, rec.started_mono)
        )
        audio_task = asyncio.create_task(
            _recorder(audio_out_ws, rec.audio_out, rec.started_mono)
        )

        try:
            if args.mode == "passive":
                print(
                    f"PASSIVE mode: recording for {args.duration}s. "
                    "Drive the browser mic now.",
                    file=sys.stderr,
                )
                await asyncio.sleep(args.duration)
            else:
                client_id = f"repro-{int(time.time())}"
                for i, utt in enumerate(AUTO_UTTERANCES[: args.utterances]):
                    print(f"[auto] synthesizing utterance {i+1}: {utt!r}", file=sys.stderr)
                    pcm = await _synth_pcm16_16k(utt)
                    print(
                        f"[auto] streaming {len(pcm)/2/INGEST_SAMPLE_RATE:.2f}s of audio "
                        f"({len(pcm)} bytes pcm16 @ 16k)",
                        file=sys.stderr,
                    )
                    await _stream_audio(
                        ingest_ws, pcm, client_id, f"repro-mic-{i}",
                        trailing_silence_ms=args.inter_utterance_ms,
                    )
                # Tail wait so any response TTS lands in our recording.
                await asyncio.sleep(args.tail_wait)
        finally:
            for w in (ingest_ws, display_ws, audio_out_ws):
                try:
                    await w.close()
                except Exception:
                    pass
            for t in (display_task, audio_task):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    (out_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rec.events) + "\n"
    )
    (out_dir / "audio_out.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rec.audio_out) + "\n"
    )
    _write_report(out_dir, rec, args.mode)
    print(f"\nwrote {out_dir}/events.jsonl ({len(rec.events)} rows)", file=sys.stderr)
    print(f"wrote {out_dir}/audio_out.jsonl ({len(rec.audio_out)} rows)", file=sys.stderr)
    print(f"wrote {out_dir}/report.md", file=sys.stderr)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("mode", choices=("passive", "auto", "auto-wait-for-tts"))
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--duration", type=float, default=30.0,
                   help="seconds to record / hard timeout")
    p.add_argument("--utterances", type=int, default=2,
                   help="auto mode: number of utterances from AUTO_UTTERANCES")
    p.add_argument("--tail-wait", type=float, default=8.0,
                   help="auto mode: seconds to keep recording after last utterance")
    p.add_argument("--inter-utterance-ms", type=int,
                   default=INTER_UTTERANCE_SILENCE_MS_DEFAULT,
                   help="auto mode: ms of trailing silence after each utterance")
    p.add_argument("--turn2-delay-after-tts-start-ms", type=int, default=150,
                   help="auto-wait-for-tts: additional ms to wait after "
                        "tts_synthesis_started before sending turn 2")
    p.add_argument("--turn2-text", default=None,
                   help="auto-wait-for-tts: override the second utterance text")
    p.add_argument("--out-dir", default=f"/tmp/repro-{int(time.time())}")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
