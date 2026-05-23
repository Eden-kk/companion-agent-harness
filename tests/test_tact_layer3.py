"""Stage 1 — Layer-3 mechanical scorer (pure; no model, no network).

Loads the vendored 28-case Layer-3 bank and exercises gt expansion, action
parsing, and scoring against oracle + degraded decision sets.
"""
from companion_harness.evals.adapters.tact_bench_layer3 import (
    Action,
    load_layer3,
    parse_action,
    perfect_decisions,
    score_all,
    score_item,
)


def test_parse_action():
    assert parse_action("NOW:SPEAK_BRIEF+RA") == Action("NOW", "SPEAK_BRIEF", ra=True)
    assert parse_action("NOW:SPEAK_BRIEF+INT") == Action("NOW", "SPEAK_BRIEF", interrupt=True)
    assert parse_action("NOW:SILENT_NOTIFY") == Action("NOW", "SILENT_NOTIFY")
    assert parse_action("DROP") == Action("DROP")
    assert parse_action(None) == Action("WAIT")
    assert parse_action("NOW:BOGUS").form is None  # unknown form normalized away


def test_load_layer3_has_28_cases_with_expanded_state():
    cases = load_layer3()
    assert len(cases) == 28
    ids = {c.id for c in cases}
    assert "TC1" in ids and "TC28" in ids  # renumbered to TC1–28 contiguous (master 5986d40)
    tc1 = next(c for c in cases if c.id == "TC1")
    assert len(tc1.user_state) == tc1.ticks == 11
    # "m:0-8","b:9-10" → mid through 8, breakpoint 9-10
    assert tc1.user_state[0] == "m" and tc1.user_state[8] == "m"
    assert tc1.user_state[9] == "b" and tc1.user_state[10] == "b"
    item = tc1.items[0]
    assert item.id == "weather" and item.t_avail == 3 and item.decisive_tick == 9
    assert item.expected == Action("NOW", "SPEAK_BRIEF", ra=True)


def test_multi_item_and_third_party_parse():
    cases = {c.id: c for c in load_layer3()}
    tc19 = cases["TC19"]  # 4 items incl supersession + a drop + an urgent NOW (was TC20)
    assert len(tc19.items) == 4
    smoke = next(i for i in tc19.items if i.id == "smoke")
    assert smoke.expected.kind == "NOW" and smoke.expected.interrupt
    eta_old = next(i for i in tc19.items if i.id == "eta_old")
    assert eta_old.expected.kind == "DROP"
    eta_new = next(i for i in tc19.items if i.id == "eta_new")
    assert eta_new.extra.get("supersedes") == "eta_old"
    tc21 = cases["TC21"]  # third_party present throughout; privacy → SILENT_NOTIFY (was TC22)
    assert all(tc21.third_party)
    meds = tc21.items[0]
    assert meds.expected == Action("NOW", "SILENT_NOTIFY")


def test_oracle_decisions_score_perfect():
    cases = load_layer3()
    m = score_all(cases, perfect_decisions(cases))
    assert m["action_accuracy"] == 1.0
    assert m["form_accuracy"] == 1.0
    assert m["cried_wolf"] == 0.0
    assert m["urgent_miss"] == 0.0
    assert m["outcomes"].get("miss", 0) == 0 and m["outcomes"].get("cried_wolf", 0) == 0


def test_never_deliver_baseline():
    # The "always silent" floor: misses every NOW, but never cries wolf.
    cases = load_layer3()
    m = score_all(cases, {})  # no decisions at all
    assert m["cried_wolf"] == 0.0          # no deliveries → can't cry wolf
    assert m["urgent_miss"] == 1.0         # every urgent NOW missed
    assert m["n_deliveries"] == 0
    assert m["action_accuracy"] < 1.0      # DROP items correct, NOW items missed


def test_score_item_outcomes():
    cases = {c.id: c for c in load_layer3()}
    tc2 = cases["TC2"]  # cafe: expected DROP
    cafe = tc2.items[0]
    # delivering a DROP item = cried-wolf
    r = score_item(tc2, cafe, {"tick": 5, "action": "NOW:SPEAK_BRIEF"})
    assert r.outcome == "cried_wolf"
    # never delivering it = correct
    assert score_item(tc2, cafe, None).outcome == "correct"

    tc3 = cases["TC3"]  # meeting: NOW:SPEAK_BRIEF+INT at t6, stale 8
    meeting = tc3.items[0]
    assert score_item(tc3, meeting, {"tick": 6, "action": "NOW:SPEAK_BRIEF"}).outcome == "correct"
    assert score_item(tc3, meeting, {"tick": 4, "action": "NOW:SPEAK_BRIEF"}).outcome == "cried_wolf"  # premature
    assert score_item(tc3, meeting, {"tick": 10, "action": "NOW:SPEAK_BRIEF"}).outcome == "miss"       # past stale
    assert score_item(tc3, meeting, {"tick": 6, "action": "NOW:SPEAK_FULL"}).outcome == "wrong_form"
    assert score_item(tc3, meeting, None).outcome == "miss"
