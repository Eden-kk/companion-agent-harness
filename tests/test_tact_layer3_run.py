"""Stage 2 — Layer-3 Mode-A elicitation (hermetic; fake model, no GPU/network)."""
from companion_harness.evals.adapters.tact_bench_layer3 import load_layer3, score_all
import numpy as np

from companion_harness.evals.adapters.tact_bench_layer3_run import (
    _parse_monitor,
    _parse_token,
    elicit_decisions,
    elicit_decisions_audio,
    elicit_decisions_monitor,
    load_context,
    run_arm,
)


class _AlwaysNow:
    """Says NOW_BRIEF for every item — exercises the single-channel serializer."""

    def chat(self, text: str, max_new_tokens: int = 8) -> str:
        return "NOW_BRIEF"


class _Oracleish:
    """Reads the prompt: DROP low-relevance/low-urgency, NOW high-urgency, else WAIT."""

    def chat(self, text: str, max_new_tokens: int = 8) -> str:
        if "urgency: high" in text:
            return "NOW_BRIEF"
        return "WAIT"


def _ctx():
    return load_context()


def test_parse_token():
    assert _parse_token("NOW_SILENT") == "NOW:SILENT_NOTIFY"
    assert _parse_token("now_brief please") == "NOW:SPEAK_BRIEF"
    assert _parse_token("DROP") == "DROP"
    assert _parse_token("hmm WAIT") == "WAIT"
    assert _parse_token("NOW") == "NOW:SPEAK_BRIEF"
    assert _parse_token("garbage") == "WAIT"


def test_single_channel_serializes_simultaneous_nows():
    cases = {c.id: c for c in load_layer3()}
    tc18 = cases["TC17"]  # meeting (u=high) + weather (u=low), both t_avail=6
    dec = elicit_decisions(tc18, _AlwaysNow(), "", _ctx().get("TC17", {}))
    # higher-urgency meeting delivers first at t6; weather serialized to t7
    assert dec["meeting"]["tick"] == 6
    assert dec["weather"]["tick"] == 7
    assert dec["meeting"]["action"].startswith("NOW")


def test_elicit_produces_scorable_decisions_for_all_cases():
    cases = load_layer3()
    decisions = run_arm(cases, _AlwaysNow(), "vanilla", _ctx())
    assert set(decisions) == {c.id for c in cases}
    m = score_all(cases, decisions)
    # always-NOW delivers everything → it cries wolf on the DROP items
    assert m["n_deliveries"] > 0
    assert m["cried_wolf"] > 0.0
    assert m["urgent_miss"] == 0.0  # it never misses an urgent (it delivers all)


def test_oracleish_beats_always_now_on_cried_wolf():
    cases = load_layer3()
    aggressive = score_all(cases, run_arm(cases, _AlwaysNow(), "vanilla", _ctx()))
    selective = score_all(cases, run_arm(cases, _Oracleish(), "prompted", _ctx()))
    # the selective model (only delivers urgent) cries wolf less than always-NOW
    assert selective["cried_wolf"] < aggressive["cried_wolf"]


# --- monitor-stream arm (intervention 2b) ---

class _UrgentMonitor:
    """Emits a <monitor>/<speak> block: NOW:BRIEF for urgent items, WAIT otherwise,
    parsing the prompt's pending-item lines."""

    def chat(self, text: str, max_new_tokens: int = 8) -> str:
        out = ["<monitor>"]
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("- ") and ":" in line:
                pid = line[2:].split(":", 1)[0].strip()
                out.append(f"{pid}: {'NOW:BRIEF' if 'urgency high' in line else 'WAIT'}")
        out.append("</monitor><speak></speak>")
        return "\n".join(out)


class _TC17Monitor:
    def chat(self, text: str, max_new_tokens: int = 8) -> str:
        weather = "NOW:BRIEF" if "meeting:" not in text and "meeting" not in text else "WAIT"
        body = []
        if "meeting" in text:
            body.append("meeting: NOW:BRIEF — urgent")
        if "weather" in text:
            body.append(f"weather: {weather} — defer")
        return "<monitor>\n" + "\n".join(body) + "\n</monitor><speak></speak>"


def test_parse_monitor_block():
    raw = "<monitor>\nmeeting: NOW:BRIEF — urgent\nweather: WAIT — defer\ncafe: DROP — stale\n</monitor><speak>hi</speak>"
    parsed = _parse_monitor(raw, ["meeting", "weather", "cafe"])
    assert parsed == {"meeting": "NOW:SPEAK_BRIEF", "weather": "WAIT", "cafe": "DROP"}


def test_monitor_single_channel_serializes():
    tc18 = next(c for c in load_layer3() if c.id == "TC17")
    dec = elicit_decisions_monitor(tc18, _TC17Monitor(), _ctx().get("TC17", {}))
    assert dec["meeting"]["tick"] == 6
    assert dec["weather"]["tick"] == 7  # serialized to the next tick


def test_monitor_arm_runs_all_cases_and_catches_urgent():
    cases = load_layer3()
    decisions = run_arm(cases, _UrgentMonitor(), "monitor_stream", _ctx())
    assert set(decisions) == {c.id for c in cases}
    m = score_all(cases, decisions)
    assert m["urgent_miss"] == 0.0   # urgents delivered at availability
    assert m["cried_wolf"] == 0.0    # only urgents delivered → no false alarms


# --- audio arm (prompted policy, probe fed as audio; text output) ----------

class _AudioModel:
    """Fake audio model: receives a waveform + system prompt, returns a token.
    Records what it was handed so the test can assert audio (not text) reached it."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def chat_audio(self, audio, system_prompt: str = "", max_new_tokens: int = 6) -> str:
        self.calls.append((np.asarray(audio).shape, system_prompt))
        return "NOW_BRIEF"


def _fake_tts(text: str):
    # 0.5 s of 16 kHz silence — length proportional to text so it's clearly a waveform
    return np.zeros(8000 + len(text), dtype=np.float32)


def test_audio_arm_feeds_waveform_and_serializes_single_channel():
    cases = {c.id: c for c in load_layer3()}
    tc = cases["TC17"]  # meeting (u=high) + weather (u=low), both t_avail=6
    model = _AudioModel()
    dec = elicit_decisions_audio(tc, model, _ctx().get("TC17", {}), _fake_tts)
    # higher-urgency meeting delivers first at t6; weather serialized to t7
    assert dec["meeting"]["tick"] == 6
    assert dec["weather"]["tick"] == 7
    # the model was handed a waveform (ndarray shape), not text
    assert model.calls and isinstance(model.calls[0][0], tuple)
    assert all(sp for _, sp in model.calls)  # prompted system prompt passed through


def test_audio_arm_runs_all_cases_via_run_arm():
    cases = load_layer3()
    decisions = run_arm(cases, _AudioModel(), "audio", _ctx(), tts=_fake_tts)
    assert set(decisions) == {c.id for c in cases}
    m = score_all(cases, decisions)
    assert m["n_deliveries"] > 0
    assert m["urgent_miss"] == 0.0  # always-NOW never misses an urgent
