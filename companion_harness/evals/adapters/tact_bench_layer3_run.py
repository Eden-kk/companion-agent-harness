"""TACT-Bench Layer-3 Stage 2 — Mode-A mechanical elicitation (no LLM judge).

Drives a model tick-by-tick to produce a per-item NOW/WAIT/DROP+form *decision
set*, which tact_bench_layer3.score_all grades against the formal gt. Pure
structured-state probe: at each tick the model is shown the topic, the user's state
(mid-utterance / at a pause / idle), the held item(s) with urgency/relevance/
standing + key §3.3 context, and asked to emit one control token. The single-channel
rule (≤1 NOW per tick) is enforced by serializing simultaneous NOWs (higher urgency
first), so consecutive deliveries land on successive ticks.

No OpenAI judge anywhere — scoring is mechanical and reproducible.
"""

from __future__ import annotations

import re
from pathlib import Path

from companion_harness.evals.adapters.tact_bench import _ARMS, _load_real_model
from companion_harness.evals.adapters.tact_bench_layer3 import (
    Layer3Case,
    Layer3Item,
    load_layer3,
)

_LAYER2 = Path(__file__).parent / "tact_bench_data" / "cases" / "layer2-semistructured.yaml"

_STATE_WORD = {
    "m": "speaking (mid-utterance)",
    "b": "at a natural pause (between utterances)",
    "i": "idle / away from the conversation",
}

_TOKEN_TO_ACTION = [
    ("NOW_BRIEF", "NOW:SPEAK_BRIEF"),
    ("NOW_FULL", "NOW:SPEAK_FULL"),
    ("NOW_SILENT", "NOW:SILENT_NOTIFY"),
    ("NOW_CHIME", "NOW:CHIME"),
    ("DROP", "DROP"),
    ("WAIT", "WAIT"),
]


def _load_layer2_context(path: Path = _LAYER2) -> dict:
    import yaml

    raw = yaml.safe_load(path.read_text())["scenarios"]
    ctx: dict[str, dict] = {}
    for c in raw:
        payloads: dict[str, str] = {}
        if c.get("item"):
            payloads[c["item"]["id"]] = c["item"].get("payload", "")
        for it in (c.get("items") or []):
            payloads[it["id"]] = it.get("payload", "")
        ctx[c["id"]] = {
            "topic": c.get("topic", ""),
            "standing": c.get("standing_instruction"),
            "payloads": payloads,
        }
    return ctx


def _parse_token(raw: str) -> str:
    up = (raw or "").upper()
    for tok, action in _TOKEN_TO_ACTION:
        if tok in up:
            return action
    if "NOW" in up:
        return "NOW:SPEAK_BRIEF"  # bare NOW → default form
    return "WAIT"


def _payload_for(item: Layer3Item, ctx_case: dict) -> str:
    p = (ctx_case.get("payloads") or {}).get(item.id)
    return p or f"a {item.id.replace('_', ' ')} update"


def _probe_prompt(case: Layer3Case, item: Layer3Item, t: int, arm_prompt: str, ctx_case: dict) -> str:
    lines = [arm_prompt, ""]
    lines.append(f"Conversation topic: {ctx_case.get('topic', '?')}.")
    lines.append(f"Right now (second {t}) the user is {_STATE_WORD.get(case.user_state[t], '?')}.")
    if ctx_case.get("standing"):
        lines.append(f"Earlier the user said: {ctx_case['standing']}.")
    if case.third_party[t]:
        lines.append("Another person is physically present and can overhear the channel.")
    s_word = "yes" if item.s else "no"
    lines.append(
        f'You are holding this result to deliver: "{_payload_for(item, ctx_case)}" '
        f"(urgency: {item.u}; relevance to the current topic: {item.r}; "
        f"the user earlier asked to be told this: {s_word})."
    )
    extra = item.extra
    if extra.get("supersedes"):
        lines.append(f"This update supersedes an earlier '{extra['supersedes']}' item you were holding.")
    if extra.get("privacy"):
        lines.append(f"This information is {extra['privacy']} (privacy-sensitive).")
    if extra.get("retryable"):
        lines.append("This is a transient/retryable status that may resolve on its own.")
    if extra.get("cond") is not None:
        lines.append(f"The condition the user attached to this is {'met' if extra['cond'] else 'NOT met'}.")
    lines.append(
        "Decide for THIS item, right now. Answer with exactly one token: "
        "WAIT, DROP, NOW_BRIEF, NOW_FULL, NOW_SILENT, or NOW_CHIME."
    )
    return "\n".join(lines)


def _priority(item: Layer3Item) -> tuple:
    # higher urgency first, then the gt's own decisive order as a tiebreak
    return (0 if item.u == "high" else 1, item.decisive_tick)


def elicit_decisions(case: Layer3Case, model: object, arm_prompt: str, ctx_case: dict) -> dict:
    """Walk ticks; per available unresolved item, ask the model NOW/WAIT/DROP+form.
    Enforce one NOW per tick (serialize simultaneous NOWs by urgency)."""
    resolved: dict[str, dict] = {}
    for t in range(case.ticks):
        pending = [it for it in case.items if it.t_avail <= t and it.id not in resolved]
        now_bids: list[tuple] = []
        for item in pending:
            raw = model.chat(_probe_prompt(case, item, t, arm_prompt, ctx_case), max_new_tokens=6)  # type: ignore[attr-defined]
            action = _parse_token(raw)
            if action == "DROP":
                resolved[item.id] = {"tick": t, "action": "DROP"}
            elif action.startswith("NOW"):
                now_bids.append((item, action))
            # WAIT → stays pending
        if now_bids:  # single-channel: deliver the top-priority NOW this tick
            now_bids.sort(key=lambda x: _priority(x[0]))
            item, action = now_bids[0]
            resolved[item.id] = {"tick": t, "action": action}
            # the rest stay pending and are re-asked next tick (serialized delivery)
    return resolved


# --------------------------------------------------------------------------- #
# Monitor-stream arm (intervention 2b) — per-tick <monitor>/<speak> elicitation
# docs/multi-stream-simulation-and-tact-bench.md
# --------------------------------------------------------------------------- #
def _extract_block(raw: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", raw or "", re.S | re.I)
    if m:
        return m.group(1)
    return raw or "" if tag == "monitor" else ""


def _line_action(line: str) -> str:
    up = line.upper()
    for tok, act in (
        ("NOW:FULL", "NOW:SPEAK_FULL"), ("NOW:BRIEF", "NOW:SPEAK_BRIEF"),
        ("NOW:SILENT", "NOW:SILENT_NOTIFY"), ("NOW:CHIME", "NOW:CHIME"),
        ("NOW_FULL", "NOW:SPEAK_FULL"), ("NOW_BRIEF", "NOW:SPEAK_BRIEF"),
        ("NOW_SILENT", "NOW:SILENT_NOTIFY"), ("NOW_CHIME", "NOW:CHIME"),
    ):
        if tok in up:
            return act
    if "DROP" in up:
        return "DROP"
    if "WAIT" in up:
        return "WAIT"
    if "NOW" in up:
        return "NOW:SPEAK_BRIEF"
    return "WAIT"


def _parse_monitor(raw: str, pending_ids: list[str]) -> dict[str, str]:
    """Pull a per-item NOW/WAIT/DROP+form decision out of the <monitor> block."""
    block = _extract_block(raw, "monitor")
    lines = block.splitlines()
    out: dict[str, str] = {}
    for pid in pending_ids:
        action = "WAIT"
        for line in lines:
            if pid.lower() in line.lower():
                action = _line_action(line)
                break
        out[pid] = action
    return out


def _monitor_tick_prompt(case: Layer3Case, pending: list[Layer3Item], t: int, ctx_case: dict) -> str:
    lines = [_ARMS["monitor_stream"], ""]
    lines.append(f"Topic: {ctx_case.get('topic', '?')}. The user is currently {_STATE_WORD.get(case.user_state[t], '?')}.")
    if ctx_case.get("standing"):
        lines.append(f"Earlier the user said: {ctx_case['standing']}.")
    if case.third_party[t]:
        lines.append("Another person is physically present and can overhear the channel.")
    lines.append("Pending items this tick:")
    for it in pending:
        notes = []
        if it.extra.get("supersedes"):
            notes.append(f"supersedes {it.extra['supersedes']}")
        if it.extra.get("privacy"):
            notes.append(f"{it.extra['privacy']} (private)")
        if it.extra.get("retryable"):
            notes.append("transient/retryable")
        if it.extra.get("cond") is not None:
            notes.append("condition " + ("met" if it.extra["cond"] else "not met"))
        tag = f" [{', '.join(notes)}]" if notes else ""
        lines.append(
            f'- {it.id}: "{_payload_for(it, ctx_case)}" '
            f"(urgency {it.u}, relevance {it.r}, requested {'yes' if it.s else 'no'}){tag}"
        )
    lines.append("Emit your <monitor> and <speak> now.")
    return "\n".join(lines)


def elicit_decisions_monitor(case: Layer3Case, model: object, ctx_case: dict) -> dict:
    """Per tick: one <monitor>/<speak> pass over the whole pending queue; parse the
    per-item decisions from <monitor>; enforce one NOW per tick (top urgency)."""
    resolved: dict[str, dict] = {}
    for t in range(case.ticks):
        pending = [it for it in case.items if it.t_avail <= t and it.id not in resolved]
        if not pending:
            continue
        raw = model.chat(_monitor_tick_prompt(case, pending, t, ctx_case), max_new_tokens=160)  # type: ignore[attr-defined]
        per_item = _parse_monitor(raw, [it.id for it in pending])
        now_bids: list[tuple] = []
        for item in pending:
            action = per_item.get(item.id, "WAIT")
            if action == "DROP":
                resolved[item.id] = {"tick": t, "action": "DROP"}
            elif action.startswith("NOW"):
                now_bids.append((item, action))
        if now_bids:
            now_bids.sort(key=lambda x: _priority(x[0]))
            item, action = now_bids[0]
            resolved[item.id] = {"tick": t, "action": action}
    return resolved


def run_arm(cases: list[Layer3Case], model: object, arm: str, ctx: dict) -> dict:
    if arm == "monitor_stream":
        return {c.id: elicit_decisions_monitor(c, model, ctx.get(c.id, {})) for c in cases}
    arm_prompt = _ARMS[arm]
    return {c.id: elicit_decisions(c, model, arm_prompt, ctx.get(c.id, {})) for c in cases}


def load_context() -> dict:
    return _load_layer2_context()
