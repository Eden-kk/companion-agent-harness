"""TACT-Bench Layer-3 mechanical scorer (no LLM judge).

Grades a model's per-item NOW/WAIT/DROP+form *decision* against the formal
per-tick ground truth in ``cases/layer3-formal-trajectories.yaml`` (28 cases,
TC1–TC29 no TC5). Layer 3 is the benchmark's source of truth (cases/README.md):
deterministic, reproducible, no judge — which removes the run-to-run variance the
Mode-B + LLM-judge path suffers from.

This module is the pure scoring core (Stage 1): load + expand cases, and score a
supplied decision set. The model elicitation that *produces* decisions is Stage 2
(tact_bench_layer3_run). Pure functions only here — no model, no network.

gt-expansion rule (layer3 header): from an item's t_avail the action is WAIT every
tick until its first listed gt tick; that tick's action applies; after a NOW or
DROP the item is DONE (terminal). Actions: ``NOW:<FORM>`` (SPEAK_BRIEF | SPEAK_FULL
| SILENT_NOTIFY | CHIME) with optional ``+RA`` (re-anchor) / ``+INT`` (interrupt
permitted), or ``DROP``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_DATA = Path(__file__).parent / "tact_bench_data" / "cases"
_LAYER3 = _DATA / "layer3-formal-trajectories.yaml"
_LAYER2 = _DATA / "layer2-semistructured.yaml"

_FORMS = {"SPEAK_BRIEF", "SPEAK_FULL", "SILENT_NOTIFY", "CHIME"}


# --------------------------------------------------------------------------- #
# Action parsing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Action:
    kind: str               # "NOW" | "DROP" | "WAIT"
    form: str | None = None  # for NOW
    ra: bool = False         # +RA re-anchor
    interrupt: bool = False  # +INT


def parse_action(s: str | None) -> Action:
    """Parse 'NOW:SPEAK_BRIEF+RA' / 'DROP' / 'WAIT' (case-insensitive kind)."""
    if not s:
        return Action("WAIT")
    s = s.strip()
    up = s.upper()
    if up.startswith("DROP"):
        return Action("DROP")
    if up.startswith("WAIT"):
        return Action("WAIT")
    if up.startswith("NOW"):
        body = s.split(":", 1)[1] if ":" in s else ""
        ra = "+RA" in body.upper()
        interrupt = "+INT" in body.upper()
        form = body.upper().replace("+RA", "").replace("+INT", "").strip() or None
        if form is not None and form not in _FORMS:
            form = None
        return Action("NOW", form=form, ra=ra, interrupt=interrupt)
    return Action("WAIT")


# --------------------------------------------------------------------------- #
# Case loading + expansion
# --------------------------------------------------------------------------- #
@dataclass
class Layer3Item:
    id: str
    t_avail: int
    stale: int | None
    u: str
    r: str
    s: bool
    decisive_tick: int           # first listed gt tick
    expected: Action             # the gt action at the decisive tick
    extra: dict = field(default_factory=dict)  # §3.3 fields: supersedes, sev, retryable, cond, privacy, allowed_forms, suppress_until


@dataclass
class Layer3Case:
    id: str
    ticks: int
    user_state: list[str]        # per-tick: 'm' | 'b' | 'i'
    third_party: list[bool]      # per-tick
    modality_gated: str | bool   # "audio" | "video" | False
    items: list[Layer3Item]


def _expand_runlength(segments, ticks: int, default):
    """'m:0-8' / 'yes:0-8' run-length → per-tick list of length `ticks`."""
    out = [default] * ticks
    if not segments:
        return out
    if isinstance(segments, str):
        segments = [segments]
    for seg in segments:
        tag, span = seg.split(":", 1)
        lo, hi = (span.split("-", 1) + [span])[:2] if "-" in span else (span, span)
        for t in range(int(lo), int(hi) + 1):
            if 0 <= t < ticks:
                out[t] = tag
    return out


def _to_bool(v) -> bool:
    return v is True or str(v).lower() in ("yes", "true")


def load_layer3(path: Path = _LAYER3) -> list[Layer3Case]:
    import yaml

    raw = yaml.safe_load(path.read_text())["cases"]
    cases: list[Layer3Case] = []
    for c in raw:
        ticks = int(c["ticks"])
        user_state = _expand_runlength(c.get("user_state"), ticks, "i")
        tp_raw = c.get("third_party")
        third_party = (
            [False] * ticks if tp_raw in (False, None)
            else [v == "yes" for v in _expand_runlength(tp_raw, ticks, "no")]
        )
        gt = c.get("gt") or {}
        items: list[Layer3Item] = []
        for it in c.get("items", []):
            iid = it["id"]
            imap = {int(k): v for k, v in (gt.get(iid) or {}).items()}
            decisive_tick = min(imap) if imap else ticks  # no gt → treat as never (defensive)
            expected = parse_action(imap.get(decisive_tick)) if imap else Action("WAIT")
            known = {"id", "t_avail", "stale", "u", "r", "s"}
            items.append(Layer3Item(
                id=iid,
                t_avail=int(it["t_avail"]),
                stale=(None if it.get("stale") is None else int(it["stale"])),
                u=str(it.get("u", "low")),
                r=str(it.get("r", "low")),
                s=_to_bool(it.get("s", False)),
                decisive_tick=decisive_tick,
                expected=expected,
                extra={k: v for k, v in it.items() if k not in known},
            ))
        cases.append(Layer3Case(
            id=c["id"], ticks=ticks, user_state=user_state, third_party=third_party,
            modality_gated=c.get("modality_gated", False), items=items,
        ))
    return cases


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ItemResult:
    case_id: str
    item_id: str
    expected: Action
    expected_tick: int
    model: Action
    model_tick: int | None
    outcome: str   # correct | wrong_form | miss | cried_wolf

    @property
    def delivered(self) -> bool:
        return self.model.kind == "NOW"


def score_item(case: Layer3Case, item: Layer3Item, decision: dict | None) -> ItemResult:
    """Grade one item. ``decision`` = {'tick': int|None, 'action': str|None};
    None / absent = the model never acted (stayed WAIT)."""
    decision = decision or {}
    m_action = parse_action(decision.get("action"))
    m_tick = decision.get("tick")
    exp = item.expected

    if exp.kind == "DROP":
        outcome = "cried_wolf" if m_action.kind == "NOW" else "correct"
    else:  # NOW expected
        if m_action.kind != "NOW" or m_tick is None:
            outcome = "miss"
        elif m_tick < item.decisive_tick:
            outcome = "cried_wolf"          # delivered before it was correct to
        elif item.stale is not None and m_tick > item.stale:
            outcome = "miss"                # delivered past its deadline
        elif m_action.form != exp.form:
            outcome = "wrong_form"
        else:
            outcome = "correct"

    return ItemResult(
        case_id=case.id, item_id=item.id, expected=exp, expected_tick=item.decisive_tick,
        model=m_action, model_tick=m_tick, outcome=outcome,
    )


def _rate(num: int, den: int) -> float | None:
    return (num / den) if den else None


def score_all(cases: list[Layer3Case], decisions: dict[str, dict]) -> dict:
    """Aggregate mechanical metrics over all cases.

    ``decisions`` = {case_id: {item_id: {'tick': int|None, 'action': str|None}}}.
    Returns rates: action_accuracy (deliver/suppress+timing right, form aside),
    form_accuracy (of on-time deliveries), cried_wolf, urgent_miss, plus counts.
    """
    results: list[ItemResult] = []
    for case in cases:
        cd = decisions.get(case.id, {})
        for item in case.items:
            results.append(score_item(case, item, cd.get(item.id)))

    n = len(results)
    # action_accuracy: the deliver/suppress decision (with timing) was right, form aside.
    action_ok = sum(
        (r.expected.kind == "DROP" and r.outcome == "correct")
        or (r.expected.kind == "NOW" and r.outcome in ("correct", "wrong_form"))
        for r in results
    )
    # form: only over NOW items delivered on time.
    on_time_now = [r for r in results if r.expected.kind == "NOW" and r.outcome in ("correct", "wrong_form")]
    form_ok = sum(r.outcome == "correct" for r in on_time_now)
    # cried-wolf: deliveries that were premature or should-have-dropped ÷ deliveries.
    deliveries = [r for r in results if r.delivered]
    cried = sum(r.outcome == "cried_wolf" for r in results)
    # urgent-miss: high-urgency NOW items not delivered on time.
    urgent = [r for r in results if r.expected.kind == "NOW" and _item_urgency(cases, r) == "high"]
    urgent_missed = sum(r.outcome == "miss" for r in urgent)

    return {
        "action_accuracy": _rate(action_ok, n),
        "form_accuracy": _rate(form_ok, len(on_time_now)),
        "cried_wolf": _rate(cried, len(deliveries)) if deliveries else 0.0,
        "urgent_miss": _rate(urgent_missed, len(urgent)),
        "n_items": n,
        "n_deliveries": len(deliveries),
        "outcomes": _count_outcomes(results),
    }


def _item_urgency(cases: list[Layer3Case], r: ItemResult) -> str:
    for c in cases:
        if c.id == r.case_id:
            for it in c.items:
                if it.id == r.item_id:
                    return it.u
    return "low"


def _count_outcomes(results: list[ItemResult]) -> dict:
    out: dict[str, int] = {}
    for r in results:
        out[r.outcome] = out.get(r.outcome, 0) + 1
    return out


def perfect_decisions(cases: list[Layer3Case]) -> dict[str, dict]:
    """The oracle decision set straight from gt — for testing the scorer and as a
    ceiling baseline. Each item: act at its decisive tick with the gt action."""
    out: dict[str, dict] = {}
    for c in cases:
        out[c.id] = {}
        for it in c.items:
            if it.expected.kind == "DROP":
                out[c.id][it.id] = {"tick": it.decisive_tick, "action": "DROP"}
            else:
                form = it.expected.form or "SPEAK_BRIEF"
                out[c.id][it.id] = {"tick": it.decisive_tick, "action": f"NOW:{form}"}
    return out
