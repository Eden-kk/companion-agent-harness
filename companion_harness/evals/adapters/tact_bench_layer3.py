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
    suppress_until_tick: int | None = None  # case-level suppress (TC26/TC27)


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
        su = c.get("suppress_until")
        suppress_tick = int(su["tick"]) if isinstance(su, dict) and "tick" in su else None
        cases.append(Layer3Case(
            id=c["id"], ticks=ticks, user_state=user_state, third_party=third_party,
            modality_gated=c.get("modality_gated", False), items=items,
            suppress_until_tick=suppress_tick,
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


# --------------------------------------------------------------------------- #
# Event-ledger scorer (§4.6) — deterministic, no LLM judge
# --------------------------------------------------------------------------- #

# --- PREREGISTERED --- (pin in meta.json on every run)
_COST = {
    "correct":    0.0,
    "early":      0.5,   # in-window but before decisive_tick
    "late":       0.5,   # in-window but after decisive_tick (deferred only)
    "wrong_form": 1.0,
    "cried_wolf": 3.0,
    "miss":       5.0,   # worst case — used for normalization denominator too
}
# urgency-conditioned miss cost: urgent miss is max (5); deferred miss is lower
_COST_MISS_URGENT = 5.0
_COST_MISS_DEFER  = 2.0

# ARS slopes
_W_EARLY_URGENT  = 0.1   # small: early delivery for urgent items is not severely penalized
_W_LATE_URGENT   = 1.0   # large: late delivery for urgent items is penalized hard
_W_EARLY_LOW     = 0.5   # symmetric or early≥late for low-value items
_W_LATE_LOW      = 0.3

_DELTA_URGENT = 2         # ticks: urgent delivery window width beyond t_avail
_TICK_TOLERANCE = 0       # ticks: ±tolerance for emission→tick attribution (configurable)


def preregistered_config() -> dict:
    """Dump pre-registered constants for meta.json."""
    return {
        "cost_matrix": _COST,
        "cost_miss_urgent": _COST_MISS_URGENT,
        "cost_miss_defer": _COST_MISS_DEFER,
        "ars_w_early_urgent": _W_EARLY_URGENT,
        "ars_w_late_urgent": _W_LATE_URGENT,
        "ars_w_early_low": _W_EARLY_LOW,
        "ars_w_late_low": _W_LATE_LOW,
        "delta_urgent": _DELTA_URGENT,
        "tick_tolerance": _TICK_TOLERANCE,
    }


def _is_breakpoint(state: str) -> bool:
    """State-semantics table: only 'b' is a true breakpoint/yielded seam.
    'm' and 'h' are non-seam. 'i' is idle (deliverable but not a breakpoint)."""
    return state == "b"


def _is_deliverable(state: str) -> bool:
    """State-semantics table: 'b' (breakpoint) and 'i' (idle) are deliverable
    for non-urgent items. 'm' and 'h' are not (user speaking or held pause)."""
    return state in ("b", "i")


def _delivery_window(case: Layer3Case, item: Layer3Item) -> tuple[int, int] | None:
    """Return (t_lo, t_hi) for the item's acceptable delivery window, or None for DROP."""
    if item.expected.kind == "DROP":
        return None
    if item.u == "high":
        # urgent items override suppress_until
        return (item.t_avail, item.t_avail + _DELTA_URGENT)
    # deferred: first deliverable tick (b or i) >= t_avail
    t_lo = next((t for t in range(item.t_avail, case.ticks) if _is_deliverable(case.user_state[t])), None)
    if t_lo is None:
        return None  # no deliverable tick available → treat like DROP for delivery
    # apply case-level suppress_until (TC26: t_lo must be >= suppress_tick)
    if case.suppress_until_tick is not None:
        t_lo = max(t_lo, case.suppress_until_tick)
    t_hi = item.stale if item.stale is not None else (case.ticks - 1)
    return (t_lo, t_hi)


def _item_outcome_ledger(
    item: Layer3Item,
    window: tuple[int, int] | None,
    matched_tick: int | None,
    matched_form: str | None,
    is_b_tick: bool,
) -> tuple[str, float]:
    """Classify item outcome and return (outcome_name, cost)."""
    exp = item.expected

    if exp.kind == "DROP":
        if matched_tick is not None:
            return "cried_wolf", _COST["cried_wolf"]
        return "correct", _COST["correct"]

    # expected NOW
    if matched_tick is None:
        # miss — undelivered
        cost = _COST_MISS_URGENT if item.u == "high" else _COST_MISS_DEFER
        return "miss", cost

    if window is None:
        # no valid window (e.g. no breakpoint available) but delivered
        return "cried_wolf", _COST["cried_wolf"]

    t_lo, t_hi = window
    if item.stale is not None and matched_tick > item.stale:
        # past stale = miss, not late (§4.6 MAJOR-6)
        cost = _COST_MISS_URGENT if item.u == "high" else _COST_MISS_DEFER
        return "miss", cost

    if matched_tick < t_lo:
        return "cried_wolf", _COST["cried_wolf"]

    if matched_tick > t_hi:
        # within stale but past window → late for urgent; miss cost for deferred
        if item.u == "high":
            ars = _W_LATE_URGENT * (matched_tick - t_hi)
            cost = min(ars, _COST_MISS_URGENT)
            return "late", cost
        cost = _COST_MISS_DEFER
        return "miss", cost

    # in-window — compute timing cost first, then overlay wrong_form if needed (C3)
    decisive = item.decisive_tick
    timing_outcome: str
    timing_cost: float
    if matched_tick < decisive:
        w_e = _W_EARLY_URGENT if item.u == "high" else _W_EARLY_LOW
        ars = w_e * (decisive - matched_tick)
        timing_cost = min(ars, _COST["cried_wolf"])
        timing_outcome = "early"
    elif matched_tick > decisive:
        w_l = _W_LATE_URGENT if item.u == "high" else _W_LATE_LOW
        ars = w_l * (matched_tick - decisive)
        timing_cost = min(ars, _COST_MISS_URGENT if item.u == "high" else _COST_MISS_DEFER)
        timing_outcome = "late"
    else:
        timing_cost = _COST["correct"]
        timing_outcome = "correct"

    if matched_form != exp.form:
        return "wrong_form", max(timing_cost, _COST["wrong_form"])

    return timing_outcome, timing_cost


def _arbitration_accuracy(cases: list[Layer3Case], run: dict) -> float | None:
    """Fraction of multi-item cases with zero arbitration violations (C2 / §4.6).

    Violations per case:
      (a) same item delivered more than once;
      (b) superseded item delivered at/after its superseder's t_avail;
      (c) lower-priority item delivered while a higher-priority item is
          undelivered and within its window;
      (d) item delivered before its window opens.
    """
    multi_cases = [c for c in cases if len(c.items) > 1]
    if not multi_cases:
        return None

    def _priority_key(it: Layer3Item):
        sev = int(it.extra.get("sev", 0))
        urg = 1 if it.u == "high" else 0
        return (-sev, -urg, it.t_avail)

    clean = 0
    for case in multi_cases:
        emissions = run.get(case.id, [])
        item_map = {it.id: it for it in case.items}
        # build delivered ticks per item (first match only for dup check)
        delivery_map: dict[str, list[int]] = {}
        for em in emissions:
            if em.spoke and em.detected_item in item_map:
                delivery_map.setdefault(em.detected_item, []).append(em.tick)

        # build superseder lookup: superseder_id → superseded_id
        supersedes: dict[str, str] = {}
        for it in case.items:
            sup = it.extra.get("supersedes")
            if sup:
                supersedes[it.id] = sup  # it supersedes sup

        violation = False
        windows = {it.id: _delivery_window(case, it) for it in case.items}

        for iid, ticks_list in delivery_map.items():
            # (a) duplicate
            if len(ticks_list) > 1:
                violation = True
                break
            tick = ticks_list[0]
            item = item_map[iid]
            win = windows[iid]
            # (d) before window opens
            if win is not None and tick < win[0]:
                violation = True
                break

        if not violation:
            # (b) superseded item delivered after superseder available
            for superseder_id, superseded_id in supersedes.items():
                superseder = item_map.get(superseder_id)
                if superseder and superseded_id in delivery_map:
                    tick = delivery_map[superseded_id][0]
                    if tick >= superseder.t_avail:
                        violation = True
                        break

        if not violation:
            # (c) lower-priority delivered while higher-priority undelivered + in window
            delivered_ids = set(delivery_map)
            for it in sorted(case.items, key=_priority_key):
                if it.id not in delivered_ids:
                    continue
                tick = delivery_map[it.id][0]
                for other in case.items:
                    if other.id == it.id or other.id in delivered_ids:
                        continue
                    # other is undelivered; if it has higher priority AND its window includes tick
                    if _priority_key(other) < _priority_key(it):
                        win = windows[other.id]
                        if win is not None and win[0] <= tick <= win[1]:
                            violation = True
                            break
                if violation:
                    break

        if not violation:
            clean += 1

    return clean / len(multi_cases)


def score_run(cases: list[Layer3Case], run: dict) -> dict:
    """Score one model run against the layer-3 GT using the event ledger.

    ``run`` = {case_id: list[Emission]}  (annotated emissions from the detector).
    Returns a metrics dict including ``cost_weighted_score`` (the headline) plus slices.
    """
    from companion_harness.evals.adapters.tact_bench_stream_types import Emission  # local to avoid circular at module level

    # accumulators
    item_costs: list[float] = []
    worst_costs: list[float] = []
    outcomes: dict[str, int] = {}
    urgent_items = 0
    urgent_missed = 0
    cried_wolf_deliveries = 0
    total_deliveries = 0  # detector-positive emissions matched to items
    breakpoint_delivered = 0
    breakpoint_deliverable = 0  # deferred items that were eventually delivered
    # T_F1 / dead-time
    tp_ticks = 0
    fp_ticks = 0
    fn_ticks = 0
    # interaction cost: spoke-but-not-delivery ticks overlapping m/h or dead-time
    spoke_non_delivery = 0
    spoke_in_mh_or_idle = 0
    spoke_total = 0
    never_spoke_cases = 0
    total_cases = len(cases)
    no_delivery_cases = 0

    case_map = {c.id: c for c in cases}

    for case in cases:
        emissions: list = run.get(case.id, [])
        # build per-item matched tick
        item_map = {it.id: it for it in case.items}
        item_to_emission: dict[str, Emission] = {}
        unmatched_spoke: list[Emission] = []

        for em in emissions:
            if em.spoke:
                spoke_total += 1
                if em.detected_item is not None and em.detected_item in item_map:
                    if em.detected_item not in item_to_emission:
                        item_to_emission[em.detected_item] = em
                        total_deliveries += 1
                    else:
                        # duplicate delivery of same item → interaction cost (B2)
                        unmatched_spoke.append(em)
                else:
                    unmatched_spoke.append(em)

        case_spoke = any(em.spoke for em in emissions)
        if not case_spoke:
            never_spoke_cases += 1
        case_any_delivery = bool(item_to_emission)
        if not case_any_delivery:
            no_delivery_cases += 1

        for item in case.items:
            window = _delivery_window(case, item)
            em = item_to_emission.get(item.id)
            matched_tick = em.tick if em else None
            matched_form = em.form if em else None

            # +RA: if item expects re-anchor (ra=True) and emission has reanchored=False → wrong_form
            if em and item.expected.ra and not em.reanchored:
                matched_form = "__wrong_ra__"  # force wrong_form class

            is_b = matched_tick is not None and _is_breakpoint(case.user_state[matched_tick])
            outcome, cost = _item_outcome_ledger(item, window, matched_tick, matched_form, is_b)

            # worst-case cost for normalization
            if item.expected.kind == "DROP":
                worst = _COST["cried_wolf"]
            else:
                worst = _COST_MISS_URGENT if item.u == "high" else _COST_MISS_DEFER

            item_costs.append(cost)
            worst_costs.append(worst)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

            if item.expected.kind == "NOW" and item.u == "high":
                urgent_items += 1
                if outcome == "miss":
                    urgent_missed += 1

            if outcome == "cried_wolf":
                if em is not None:
                    cried_wolf_deliveries += 1

            # breakpoint_hit: deferred item delivered on a b or i tick (non-interrupting)
            if item.expected.kind == "NOW" and item.u != "high" and em is not None:
                breakpoint_deliverable += 1
                if matched_tick is not None and case.user_state[matched_tick] in ("b", "i"):
                    breakpoint_delivered += 1

            # T_F1: acceptable-window ticks vs delivery ticks (N2: ±_TICK_TOLERANCE)
            if window is not None:
                t_lo, t_hi = window
                tol = _TICK_TOLERANCE
                if matched_tick is not None and (t_lo - tol) <= matched_tick <= (t_hi + tol):
                    tp_ticks += 1
                elif matched_tick is not None:
                    fp_ticks += 1
                else:
                    fn_ticks += 1
            elif em is not None:
                fp_ticks += 1  # delivery outside any valid window

        # interaction cost: unmatched spoke ticks in m/h or idle (dead-time)
        for em in unmatched_spoke:
            st = case.user_state[em.tick] if em.tick < len(case.user_state) else "i"
            spoke_non_delivery += 1
            if st in ("m", "h", "i"):
                spoke_in_mh_or_idle += 1

        # duplicate recognized-item emissions as FP (C1: only recognized items)
        delivery_ticks = {em.tick for em in item_to_emission.values()}
        for em in emissions:
            if (em.spoke and em.detected_item is not None
                    and em.detected_item in item_map and em.tick not in delivery_ticks):
                fp_ticks += 1

    total_cost = sum(item_costs)
    total_worst = sum(worst_costs)
    cws = 1.0 - (total_cost / total_worst) if total_worst > 0 else 1.0

    t_precision = tp_ticks / (tp_ticks + fp_ticks) if (tp_ticks + fp_ticks) else None
    t_recall    = tp_ticks / (tp_ticks + fn_ticks) if (tp_ticks + fn_ticks) else None
    t_f1 = (2 * t_precision * t_recall / (t_precision + t_recall)
            if t_precision and t_recall else None)

    # arbitration_accuracy (C2): fraction of multi-item cases with zero violations
    arb_acc = _arbitration_accuracy(cases, run)

    legacy = score_all(cases, {
        cid: {
            em.detected_item: {"tick": em.tick, "action": f"NOW:{em.form or 'SPEAK_BRIEF'}"}
            for em in emissions if em.spoke and em.detected_item
        }
        for cid, emissions in run.items()
    })

    return {
        "cost_weighted_score": cws,
        "cried_wolf": cried_wolf_deliveries / total_deliveries if total_deliveries else 0.0,
        "urgent_miss": urgent_missed / urgent_items if urgent_items else None,
        "breakpoint_hit": breakpoint_delivered / breakpoint_deliverable if breakpoint_deliverable else None,
        "T_F1": t_f1,
        "dead_time_fp": fp_ticks,
        "interaction_cost": spoke_in_mh_or_idle / spoke_non_delivery if spoke_non_delivery else 0.0,
        "never_spoke_rate": never_spoke_cases / total_cases if total_cases else 0.0,
        "no_delivery_rate": no_delivery_cases / total_cases if total_cases else 0.0,
        "arbitration_accuracy": arb_acc,
        "form_accuracy": legacy["form_accuracy"],
        "legacy_action_accuracy": legacy["action_accuracy"],
        "outcomes": outcomes,
        "n_items": len(item_costs),
    }


def aggregate_runs(cases: list[Layer3Case], runs: list[dict]) -> dict:
    """Mean ± spread (max−min) over k runs. Each run is a score_run result dict."""
    if not runs:
        return {}
    scored = [score_run(cases, r) for r in runs]
    keys = [k for k, v in scored[0].items() if isinstance(v, (int, float)) and v is not None]
    result: dict = {}
    for k in keys:
        vals = [s[k] for s in scored if s.get(k) is not None]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        spread = max(vals) - min(vals)
        result[k] = {"mean": mean, "spread": spread}
    return result


# --------------------------------------------------------------------------- #
# Baseline synthetic runs (§3)
# --------------------------------------------------------------------------- #

def oracle_run(cases: list[Layer3Case]) -> dict:
    """Ceiling: deliver each non-DROP item exactly at its decisive tick with gt form."""
    from companion_harness.evals.adapters.tact_bench_stream_types import Emission
    run = {}
    for case in cases:
        emissions = []
        for item in case.items:
            if item.expected.kind == "NOW":
                form = item.expected.form or "SPEAK_BRIEF"
                emissions.append(Emission(
                    tick=item.decisive_tick,
                    spoke=True,
                    text=f"[oracle delivery of {item.id}]",
                    detected_item=item.id,
                    form=form,
                    reanchored=item.expected.ra,
                    confidence=1.0,
                ))
        run[case.id] = emissions
    return run


def always_silent_run(cases: list[Layer3Case]) -> dict:
    """Floor: never emit anything."""
    from companion_harness.evals.adapters.tact_bench_stream_types import Emission
    return {case.id: [] for case in cases}


def always_deliver_run(cases: list[Layer3Case]) -> dict:
    """Cried-wolf floor: deliver every item at its t_avail as SPEAK_BRIEF."""
    from companion_harness.evals.adapters.tact_bench_stream_types import Emission
    run = {}
    for case in cases:
        emissions = []
        for item in case.items:
            emissions.append(Emission(
                tick=item.t_avail,
                spoke=True,
                text=f"[always-deliver {item.id}]",
                detected_item=item.id,
                form="SPEAK_BRIEF",
                reanchored=False,
                confidence=1.0,
            ))
        run[case.id] = emissions
    return run
