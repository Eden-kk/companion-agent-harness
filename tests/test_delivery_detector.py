"""Delivery detector contract tests (§4.7 fixtures)."""

import pytest

from companion_harness.evals.adapters.delivery_detector import (
    T_LEX,
    detect,
    annotate_emissions,
)
from companion_harness.evals.adapters.tact_bench_stream_types import Emission


# --- §4.7 fixture tests --- (these gate S2)

def test_true_paraphrase_positive():
    """A clear paraphrase of the payload is detected as delivered."""
    delivered, form, _, conf = detect(
        "Hey, just so you know — your meeting got moved to right now.",
        "your 3pm meeting was just moved to NOW",
    )
    assert delivered, f"Expected delivered=True, got confidence={conf:.2f}"
    assert form is not None


def test_negation_negative():
    """'not done yet' against a 'deploy succeeded' payload is NOT a delivery."""
    delivered, form, _, conf = detect(
        "No, the deploy is not done yet, still building.",
        "deploy succeeded",
    )
    assert not delivered, "Negation should not count as delivery"


def test_coincidental_keyword_overlap_negative():
    """Random overlap of one keyword without real payload content is not delivery."""
    delivered, form, _, conf = detect(
        "I was just thinking about meetings in general, you know how it is.",
        "disk full - writes are failing now",
    )
    assert not delivered, f"Coincidental overlap should not match; conf={conf:.2f}"


def test_verbose_unrelated_chatter_negative():
    """Long fluent text unrelated to payload is not detected as delivery."""
    delivered, form, _, conf = detect(
        "That was such a great episode, I loved the twist at the end with the "
        "main character. The cinematography was just stunning this season.",
        "deploy succeeded",
    )
    assert not delivered, f"Unrelated chatter should not match; conf={conf:.2f}"


def test_brief_correct_delivery_positive():
    """A short, direct delivery of the payload is detected."""
    delivered, form, _, conf = detect(
        "Done — deploy succeeded.",
        "deploy succeeded",
    )
    assert delivered, f"Brief correct delivery should match; conf={conf:.2f}"
    assert form == "SPEAK_BRIEF"


# --- annotate_emissions ---

def test_annotate_emissions_basic():
    case = {"items": [{"id": "meeting", "t_avail": 3}]}
    pending = {"meeting": "your 3pm meeting was just moved to NOW"}
    raw = [
        (0, False, ""),
        (3, False, ""),
        (4, True, "Hey, your meeting was just moved to now."),
        (5, True, "What a lovely day it is outside today."),
    ]
    emissions = annotate_emissions(raw, case, pending)
    assert len(emissions) == 4
    assert emissions[2].spoke
    assert emissions[2].detected_item == "meeting"
    assert emissions[3].detected_item is None  # unrelated chatter


def test_annotate_emissions_before_t_avail_not_matched():
    """A delivery at tick < t_avail should not match the item."""
    case = {"items": [{"id": "deploy", "t_avail": 7}]}
    pending = {"deploy": "deploy succeeded"}
    raw = [(5, True, "Done — deploy succeeded.")]
    emissions = annotate_emissions(raw, case, pending)
    assert emissions[0].detected_item is None


def test_reanchor_detected():
    delivered, form, reanchored, conf = detect(
        "About that deploy you asked earlier — it succeeded.",
        "deploy succeeded",
        earlier_ask="when will the deploy finish",
    )
    assert delivered
    assert reanchored


# --- B1: broader negation guard ---

def test_negation_did_not_finish():
    """'did NOT finish' against a 'deploy succeeded' payload is NOT a delivery."""
    delivered, _, _, _ = detect(
        "the deploy did NOT finish",
        "deploy succeeded",
        lexical_only=True,
    )
    assert not delivered, "did NOT finish should not count as delivery"


def test_negation_still_building():
    """'still building' is a negation of a completion payload."""
    delivered, _, _, _ = detect(
        "No, the deploy is not done yet, still building.",
        "deploy succeeded",
        lexical_only=True,
    )
    assert not delivered


def test_negation_hasnt_gone_through():
    """'hasn't gone through' negates a completion payload."""
    delivered, _, _, _ = detect(
        "the deploy hasn't gone through yet",
        "deploy succeeded",
        lexical_only=True,
    )
    assert not delivered


def test_positive_deploy_finished_not_blocked():
    """'deploy finished' is a positive delivery — negation guard must not over-block."""
    delivered, _, _, conf = detect(
        "deploy finished successfully",
        "deploy succeeded",
        lexical_only=True,
    )
    assert delivered, f"'deploy finished' should be delivered; conf={conf:.2f}"
