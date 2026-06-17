"""Non-native monitor_stream runner for the TACT-Bench streaming harness (S5b).

Out-of-band per-tick text: at each tick build a prompt = the monitor_stream policy +
the ACCUMULATED real transcript so far (ticks 0..t, including any [PENDING …] notes
that arrived by t) + the pending items available by t (source · payload only — no
urgency/relevance/standing labels).  Ask for one <monitor>…</monitor><speak>…</speak>
response.  Parse <monitor> with _parse_monitor for the per-item NOW/WAIT/DROP+form
decision; enforce single-channel (≤1 NOW per tick, first by t_avail).

Works for both MiniCPM and gpt-realtime models via their stateless .chat() interface.
"""

from __future__ import annotations

from companion_harness.evals.adapters.scenario_timeline import build_timeline
from companion_harness.evals.adapters.tact_bench import _ARMS
from companion_harness.evals.adapters.tact_bench_layer3 import load_layer3
from companion_harness.evals.adapters.tact_bench_layer3_run import (
    _extract_block,
    _parse_monitor,
)
from companion_harness.evals.adapters.tact_bench_stream_types import Emission


def run_case(case_id: str, model_obj: object, model_kind: str) -> list[Emission]:
    """Run *case_id* under the monitor_stream arm (non-native, out-of-band text).

    Parameters
    ----------
    case_id
        Layer-3 case id, e.g. "TC1".
    model_obj
        A loaded model with .chat(prompt, max_new_tokens=160) -> str.
        model_kind=="minicpm" → MiniCPMStreamingModel (uses .chat).
        model_kind=="gpt"     → GptRealtimeModel (stateless; adds reasoning headroom).
    model_kind
        "minicpm" or "gpt".
    """
    if model_kind not in ("minicpm", "gpt"):
        raise ValueError(f"unknown model_kind {model_kind!r}; expected 'minicpm' or 'gpt'")

    timeline = build_timeline(case_id)
    ticks = timeline.ticks

    # Load layer-3 items for this case (id, t_avail)
    l3_cases = load_layer3()
    l3 = next((c for c in l3_cases if c.id == case_id), None)
    if l3 is None:
        raise KeyError(f"Layer3 case not found: {case_id!r}")
    items = l3.items  # list[Layer3Item]

    policy = _ARMS["monitor_stream"]

    delivered: set[str] = set()   # item ids already given NOW
    emissions: list[Emission] = []

    # Accumulated transcript lines (one per tick with text content)
    transcript_lines: list[str] = []
    # Accumulated pending notes that have arrived (for transcript)
    pending_note_lines: list[str] = []

    for tick in ticks:
        t = tick.t

        # Accumulate this tick's transcript text
        if tick.kind in ("speech", "context") and tick.text:
            transcript_lines.append(f"[t={t}] user: {tick.text}")
        if tick.pending_note is not None:
            pending_note_lines.append(tick.pending_note)

        # Items available by this tick and not yet delivered
        pending = [it for it in items if it.t_avail <= t and it.id not in delivered]
        if not pending:
            continue

        prompt = _build_prompt(
            policy=policy,
            transcript_lines=transcript_lines,
            pending_note_lines=pending_note_lines,
            pending=pending,
        )

        raw = model_obj.chat(prompt, max_new_tokens=160)  # type: ignore[attr-defined]

        per_item = _parse_monitor(raw, [it.id for it in pending])
        speak_text = _extract_block(raw, "speak").strip()

        # Collect NOW bids; enforce single-channel
        now_bids: list[tuple] = []
        for it in pending:
            action = per_item.get(it.id, "WAIT")
            if action == "DROP":
                delivered.add(it.id)
            elif action.startswith("NOW"):
                now_bids.append((it, action))

        if now_bids:
            # Single-channel: keep earliest t_avail (labels hidden → urgency-free ordering)
            now_bids.sort(key=lambda x: (x[0].t_avail, x[0].id))
            chosen_item, chosen_action = now_bids[0]
            form = _action_to_form(chosen_action)
            delivered.add(chosen_item.id)
            emissions.append(Emission(
                tick=t,
                spoke=True,
                text=speak_text,
                detected_item=chosen_item.id,
                form=form,
                confidence=1.0,
            ))

    return emissions


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_prompt(
    *,
    policy: str,
    transcript_lines: list[str],
    pending_note_lines: list[str],
    pending,
) -> str:
    parts = [policy, ""]

    if transcript_lines:
        parts.append("Conversation so far:")
        parts.extend(transcript_lines)
    else:
        parts.append("Conversation so far: (none)")

    if pending_note_lines:
        parts.append("")
        parts.append("Background notes received:")
        for note in pending_note_lines:
            parts.append(f"  {note}")

    parts.append("")
    parts.append("Pending items available now:")
    for it in pending:
        parts.append(f"- {it.id}: {_item_payload(it)}")

    parts.append("")
    parts.append("Emit your <monitor> and <speak> now.")
    return "\n".join(parts)


def _item_payload(it) -> str:
    """Source · payload only — NO urgency/relevance/standing labels."""
    # The item's payload is in the layer2 context, but layer3 items carry only
    # structural fields.  The PENDING note already has "from {source}: {payload}"
    # — we can reconstruct from the item id as a minimal identifier.  The runner
    # intentionally omits urgency/relevance/standing per §4.2 / §4.5.
    return f"(from {it.id})"


def _action_to_form(action: str) -> str | None:
    up = action.upper()
    if "SPEAK_FULL" in up or "NOW:FULL" in up or "NOW_FULL" in up:
        return "SPEAK_FULL"
    if "SILENT_NOTIFY" in up or "NOW:SILENT" in up or "NOW_SILENT" in up:
        return "SILENT_NOTIFY"
    if "CHIME" in up or "NOW:CHIME" in up or "NOW_CHIME" in up:
        return "CHIME"
    if "SPEAK_BRIEF" in up or "NOW:BRIEF" in up or "NOW_BRIEF" in up or "NOW" in up:
        return "SPEAK_BRIEF"
    return None
