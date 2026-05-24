"""Scenario timeline builder for the TACT-Bench streaming harness.

Converts a layer-3 case (ticks count + items) plus the matching scenarios.yaml
entry (user_script) into a per-tick plan consumed by both streaming runners.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

_CASES_DIR = Path(__file__).parent / "tact_bench_data" / "cases"
_LAYER3_PATH = _CASES_DIR / "layer3-formal-trajectories.yaml"
_LAYER2_PATH = _CASES_DIR / "layer2-semistructured.yaml"
_SCENARIOS_PATH = _CASES_DIR / "scenarios.yaml"

# --- PREREGISTERED (§4.2) ---
def _load_pending_template() -> str:
    try:
        data = yaml.safe_load((_CASES_DIR / "arms.yaml").read_text())
        return data["pending_template"]
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("arms.yaml unreadable; falling back to hardcoded PENDING_TEMPLATE")
        return "[PENDING — from {source}: {payload}]"


PENDING_TEMPLATE = _load_pending_template()
# ----------------------------

_CHUNK_SAMPLES = 16000  # 1s @ 16 kHz


@dataclass
class Tick:
    t: int
    kind: str          # speech | pause | idle | context
    text: str = ""     # user speech text (or context text when kind='context')
    pending_note: str | None = None  # PENDING note injected this tick (if any)
    context_note: str | None = None  # context text when same tick also has speech


@dataclass
class Timeline:
    case_id: str       # layer3 id, e.g. "TC1"
    ticks: list[Tick]

    def text_view(self) -> list[str | None]:
        """Per-tick text for text-input arms. None for silent ticks."""
        out: list[str | None] = []
        for tick in self.ticks:
            if tick.kind in ("speech", "context"):
                out.append(tick.text)
            else:
                out.append(None)
        return out

    def audio_view(self) -> list[np.ndarray]:
        """Per-tick 1s PCM @16kHz float32. Requires Kokoro; raises if unavailable."""
        kokoro = _load_kokoro()
        if kokoro is None:
            raise RuntimeError(
                "Kokoro TTS unavailable — audio_view() requires the Kokoro model. "
                "Check KOKORO_MODEL_PATH / KOKORO_VOICES_PATH or use text_view()."
            )
        out: list[np.ndarray] = []
        for tick in self.ticks:
            if tick.kind == "speech":
                pcm = _synthesize_1s(kokoro, tick.text)
            else:
                pcm = np.zeros(_CHUNK_SAMPLES, dtype=np.float32)
            out.append(pcm)
        return out


def build_timeline(case_id: str) -> Timeline:
    """Build a per-tick plan for *case_id* (e.g. "TC1").

    Maps the layer3 entry (ticks count, items with t_avail) to the matching
    scenarios.yaml entry (user_script), producing a list of Tick objects of
    length == layer3 ticks.
    """
    l3 = yaml.safe_load(_LAYER3_PATH.read_text())
    l2 = yaml.safe_load(_LAYER2_PATH.read_text())
    sc = yaml.safe_load(_SCENARIOS_PATH.read_text())

    l3_case = _find_l3(l3["cases"], case_id)
    sc_case = _find_sc(sc["scenarios"], case_id)

    n_ticks: int = l3_case["ticks"]
    items: list[dict] = _normalize_items(l3_case, l2["scenarios"])
    user_script: list[dict] = sc_case.get("user_script", [])

    # build a map: tick -> list[entry]
    script_at: dict[int, list[dict]] = {}
    for entry in user_script:
        t = int(entry["t"])
        script_at.setdefault(t, []).append(entry)

    # build per-tick plan
    ticks: list[Tick] = []
    for t in range(n_ticks):
        entries = script_at.get(t, [])
        speech_entry = next((e for e in entries if e.get("speaker") == "user"), None)
        context_entry = next((e for e in entries if e.get("type") == "context"), None)
        pause_entry = next((e for e in entries if e.get("type") == "pause"), None)

        if speech_entry is not None:
            # speech wins; attach context as context_note so neither is lost
            tick = Tick(t=t, kind="speech", text=speech_entry.get("text", ""))
            if context_entry is not None:
                tick.context_note = context_entry.get("text", "")
        elif context_entry is not None:
            tick = Tick(t=t, kind="context", text=context_entry.get("text", ""))
        elif pause_entry is not None:
            tick = Tick(t=t, kind="pause")
        else:
            tick = Tick(t=t, kind="idle")
        ticks.append(tick)

    # inject PENDING notes at t_avail
    for item in items:
        t_avail = int(item.get("t_avail", 0))
        source = str(item.get("source", item.get("id", "")))
        payload = str(item.get("payload", ""))
        if 0 <= t_avail < n_ticks:
            note = PENDING_TEMPLATE.format(source=source, payload=payload)
            ticks[t_avail].pending_note = note

    return Timeline(case_id=case_id, ticks=ticks)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_l3(cases: list[dict], case_id: str) -> dict:
    for c in cases:
        if c.get("id") == case_id:
            return c
    raise KeyError(f"Layer3 case not found: {case_id!r}")


def _find_l2(scenarios: list[dict], case_id: str) -> dict:
    """Find a layer2 case by exact id."""
    for s in scenarios:
        if s.get("id") == case_id:
            return s
    raise KeyError(f"Layer2 case not found: {case_id!r}")


def _find_sc(scenarios: list[dict], case_id: str) -> dict:
    """Match by TCn prefix (e.g. 'TC1' matches 'TC1-urgent')."""
    prefix = case_id.upper()
    for s in scenarios:
        sid = s.get("id", "")
        # exact match or prefix-dash match
        if sid.upper() == prefix or re.match(rf"^{re.escape(prefix)}[-_]", sid.upper()):
            return s
    raise KeyError(f"Scenario not found for case_id {case_id!r}")


def _normalize_items(l3_case: dict, l2_scenarios: list[dict]) -> list[dict]:
    """Return items list regardless of singular/plural key and enrich with source/payload.

    Layer2 item ids match layer3 exactly; layer3 items don't carry source/payload.
    """
    raw = l3_case.get("items") or ([l3_case["item"]] if "item" in l3_case else [])
    case_id = l3_case["id"]
    l2_case = _find_l2(l2_scenarios, case_id)
    l2_items: dict[str, dict] = {}
    for si in _sc_items(l2_case):
        l2_items[si.get("id", "")] = si
    result = []
    for item in raw:
        iid = item.get("id", "")
        l2_item = l2_items.get(iid, {})
        merged = dict(item)
        merged.setdefault("source", l2_item.get("source", iid))
        merged.setdefault("payload", l2_item.get("payload", ""))
        result.append(merged)
    return result


def _sc_items(sc_case: dict) -> list[dict]:
    if "items" in sc_case:
        return sc_case["items"]
    if "item" in sc_case:
        return [sc_case["item"]]
    return []


# ---------------------------------------------------------------------------
# TTS helpers (reuse pattern from tact_bench.py)
# ---------------------------------------------------------------------------

_KOKORO_MODEL = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
_KOKORO_VOICES = "/raid/yid042/models/kokoro/voices.json"


def _load_kokoro() -> object | None:
    model_path = os.environ.get("KOKORO_MODEL_PATH", _KOKORO_MODEL)
    voices_path = os.environ.get("KOKORO_VOICES_PATH", _KOKORO_VOICES)
    if not Path(model_path).exists() or not Path(voices_path).exists():
        return None
    try:
        from companion_harness.tts_kokoro import KokoroTtsAdapter  # noqa: WPS433

        return KokoroTtsAdapter(model_path=model_path, voices_path=voices_path, warmup=False)
    except Exception:  # noqa: BLE001
        return None


_TTS_CACHE: dict[str, np.ndarray] = {}


def _synthesize_1s(kokoro: object, text: str) -> np.ndarray:
    """TTS-synthesize text, cached by sha1(text), padded/trimmed to 1s @16kHz."""
    key = hashlib.sha1(text.encode()).hexdigest()
    if key in _TTS_CACHE:
        return _TTS_CACHE[key]

    import asyncio
    import concurrent.futures

    async def _collect() -> np.ndarray:
        out: list[np.ndarray] = []
        async for pcm16 in kokoro.synthesize(text, []):  # type: ignore[attr-defined]
            out.append(np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0)
        return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        raw_24k = ex.submit(lambda: asyncio.run(_collect())).result()

    if len(raw_24k) == 0:
        pcm = np.zeros(_CHUNK_SAMPLES, dtype=np.float32)
    else:
        ratio = 16000 / 24000.0
        out_len = int(len(raw_24k) * ratio)
        idx = np.clip(np.round(np.arange(out_len) / ratio).astype(int), 0, len(raw_24k) - 1)
        pcm_raw = raw_24k[idx]
        if len(pcm_raw) >= _CHUNK_SAMPLES:
            pcm = pcm_raw[:_CHUNK_SAMPLES]
        else:
            pcm = np.zeros(_CHUNK_SAMPLES, dtype=np.float32)
            pcm[: len(pcm_raw)] = pcm_raw

    _TTS_CACHE[key] = pcm
    return pcm
