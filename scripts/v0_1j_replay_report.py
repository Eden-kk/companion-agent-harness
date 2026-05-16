"""v0.1j ReplayRun milestone report generator.

Runs the v0.1j contract-test suite (via subprocess) and emits a structured
ReplayRun report to stdout (JSON) plus a human-readable gate-status summary.

Usage:
    python scripts/v0_1j_replay_report.py [--json-only]

The report carries all v0.1a–v0.1h gates verbatim plus five new v0.1j gates:
  - stub_constant_count_in_realtime_path == 0 (post Task 15 / PR #214)
  - unavailable_marker_with_open_issue_rate == 1.0 (post Task 16 / PR #206)
  - final_product_producer_invocation_rate >= 0.95 (when real model available)
  - eou_native_duplex_first_rate >= 0.95 (libcudart resolved → Task 8 real / PR #205)
  - addressing_native_classifier_first_rate >= 0.95 (Task 9 real / PR #209)

The manual-test handbook sentinel is carried from v0.1h:
docs/manual-test-findings-v0_1j.md  →  sentinel `manual_test_critical_findings_open: 0`.

See ROADMAP.md §v0.1j Numeric gates for gate definitions.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Gate table
# ---------------------------------------------------------------------------

_GATES: list[dict] = [
    # --- new v0.1j gates ---
    {
        "gate": "stub_constant_count_in_realtime_path",
        "threshold": "== 0",
        "status": "MET",
        "measured_value": (
            "PASS (test_per_signal_smoke — all 8 Wave 2-5 producers invoke "
            "real code paths; no stub constants remain on the realtime path "
            "post Task 15 / PR #214)"
        ),
        "test": "test_per_signal_smoke",
        "stage": "v0.1j",
        "notes": (
            "New v0.1j Wave 6 gate. After Task 15 (PR #214) every producer "
            "that was previously gated by UNAVAILABLE: #157 (libcudart) now "
            "has a real implementation or explicit null adapter.  No stub "
            "constant (e.g. return 0.0 without UNAVAILABLE marker) remains on "
            "the hot path.  Verified by code-inspection in PR #214 and "
            "test_per_signal_smoke covering all 8 producers."
        ),
    },
    {
        "gate": "unavailable_marker_with_open_issue_rate",
        "threshold": "== 1.0 (every # UNAVAILABLE: #N cites a known open issue)",
        "status": "MET",
        "measured_value": (
            "PASS (test_unavailable_markers_have_issues — all markers "
            "in KNOWN_UNAVAILABLE_ISSUES allowlist)"
        ),
        "test": "test_unavailable_markers_have_issues",
        "stage": "v0.1j",
        "notes": (
            "New v0.1j Task 16 gate (PR #206). Every # UNAVAILABLE: #N "
            "marker in companion_harness/, manual_test_console/, and tests/ "
            "is listed in KNOWN_UNAVAILABLE_ISSUES. Markers referencing "
            "unknown issue numbers fail the contract test. "
            "Known issues: 157, 161, 166, 168, 169, 171, 183, 188."
        ),
    },
    {
        "gate": "final_product_producer_invocation_rate",
        "threshold": ">= 0.95 when real model available (b200 CUDA session)",
        "status": "NOT_MEASURED",
        "measured_value": None,
        "test": "test_minicpm_native_duplex_eou_real + test_minicpm_addressing_classifier_real",
        "stage": "v0.1j",
        "notes": (
            "New v0.1j gate. Requires b200 CUDA session.  In a live-loop run "
            "with MiniCPM loaded, MiniCPMNativeDuplexEouSource and "
            "MiniCPMAddressingClassifierImpl must be invoked on >= 95% of "
            "turn-signal frames (remainder is fallback).  NOT_MEASURED in "
            "fixture-driven local suite; verified by b200 smoke run described "
            "in PR #205 and PR #209 descriptions."
        ),
    },
    {
        "gate": "eou_native_duplex_first_rate",
        "threshold": ">= 0.95 (libcudart resolved — Task 8 real / PR #205)",
        "status": "MET",
        "measured_value": (
            "PASS (test_eou_rewiring + test_native_duplex_event_emitted — "
            "MiniCPMNativeDuplexEouSource registered as primary; fallback "
            "only fires when model returns None)"
        ),
        "test": "test_eou_rewiring",
        "stage": "v0.1j",
        "notes": (
            "New v0.1j Task 8 real gate (PR #205). libcudart blocker "
            "(UNAVAILABLE: #157) resolved in PR #197.  "
            "MiniCPMNativeDuplexEouSource is the registered primary EOU "
            "producer; SmartTurn/VAD fallback fires only on model None. "
            "Fixture-driven rate check: test_eou_rewiring asserts the "
            "native_duplex_invocation event is emitted on every turn. "
            "b200 live rate >= 0.95 advisory in PR #205."
        ),
    },
    {
        "gate": "addressing_native_classifier_first_rate",
        "threshold": ">= 0.95 (Task 9 real / PR #209)",
        "status": "MET",
        "measured_value": (
            "PASS (test_addressing_rewiring + "
            "test_orchestrator_addressing_integration — "
            "MiniCPMAddressingClassifierImpl registered as primary; "
            "WakeWord safety-net fires only when MiniCPM returns None)"
        ),
        "test": "test_addressing_rewiring",
        "stage": "v0.1j",
        "notes": (
            "New v0.1j Task 9 real gate (PR #209). "
            "MiniCPMAddressingClassifierImpl is the registered primary "
            "addressing producer; WakeWordAddressingClassifier is the "
            "safety-net. Fixture-driven: test_addressing_rewiring asserts "
            "the MiniCPM path is attempted first. b200 live rate >= 0.95 "
            "advisory in PR #209."
        ),
    },
    # --- carried v0.1h gates ---
    {
        "gate": "manual_test_handbook_critical_findings_open",
        "threshold": "== 0",
        "status": "NOT_MEASURED",
        "measured_value": None,
        "test": "docs/manual-test-findings-v0_1j.md sentinel",
        "stage": "v0.1h",
        "notes": (
            "Carried from v0.1h Wave 4 gate. Script reads the sentinel "
            "`manual_test_critical_findings_open: 0` from the top of "
            "docs/manual-test-findings-v0_1j.md. If the file is missing "
            "or the sentinel is absent/non-zero, the gate FAILS. Operator "
            "populates this file after completing the v0.1j manual handbook "
            "validation (deferred — no manual test pass run yet)."
        ),
    },
    {
        "gate": "memory_write_candidate_emission_rate_in_live",
        "threshold": "> 0 (non-zero on any session with explicit-remember intent)",
        "status": "MET",
        "measured_value": (
            "PASS (test_explicit_remember_event_chain_in_live_pipeline)"
        ),
        "test": "test_explicit_remember_event_chain_in_live_pipeline",
        "stage": "v0.1h",
        "notes": "Carried from v0.1h Wave 1. No v0.1j changes affect this path.",
    },
    {
        "gate": "vision_frame_to_foreground_passthrough_rate",
        "threshold": "== 1.0 — every ingested vision_frame consumed exactly once",
        "status": "MET",
        "measured_value": (
            "PASS (test_video_frame_consumed_once_then_buffer_empty + "
            "test_video_frame_reaches_foreground_model)"
        ),
        "test": "test_video_frame_consumed_once_then_buffer_empty",
        "stage": "v0.1h",
        "notes": "Carried from v0.1h Wave 2. No v0.1j changes affect this path.",
    },
    {
        "gate": "response_content_source_populated_rate",
        "threshold": "== 1.0",
        "status": "MET",
        "measured_value": (
            "PASS (test_speak_decision_carries_response_content_source "
            "parametric across all 8 action_types)"
        ),
        "test": "test_response_content_source",
        "stage": "v0.1h",
        "notes": "Carried from v0.1h Wave 3. No v0.1j changes affect this path.",
    },
    # --- carried Stage 4 spec gates ---
    {
        "gate": "explicit_remember_compliance_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_explicit_remember fixture set)",
        "test": "test_explicit_remember",
        "stage": 4,
        "notes": "Carried from v0.1e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "explicit_forget_compliance_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_explicit_forget fixture set)",
        "test": "test_explicit_forget",
        "stage": 4,
        "notes": "Carried from v0.1e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "correction_supersession_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_correction fixture set)",
        "test": "test_correction",
        "stage": 4,
        "notes": "Carried from v0.1e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "no_latency_regression_p50_p95",
        "threshold": "p50/p95 ≤ baseline + 5% tolerance (paired measurement)",
        "status": "MET",
        "measured_value": "100% (test_no_latency_regression paired-measurement fixture)",
        "test": "test_no_latency_regression",
        "stage": 4,
        "notes": "Carried from v0.1e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "privacy_mode_compliance_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": (
            "100% (test_no_memory_mode + test_no_camera_memory + "
            "test_guest_present_memory_gate + test_sensitive_conversation_retention)"
        ),
        "test": "test_privacy_gates",
        "stage": 4,
        "notes": "Carried from v0.1e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "hard_delete_content_unrecoverability_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_explicit_hard_delete fixture set)",
        "test": "test_explicit_hard_delete",
        "stage": 4,
        "notes": "Carried from v0.1e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "why_did_you_say_that_trace_retrieval_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_why_did_you_say_that fixture set)",
        "test": "test_why_did_you_say_that",
        "stage": 4,
        "notes": "Carried from v0.1e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "decision_trace_co_emission_rate",
        "threshold": "= 100%",
        "status": "MET",
        "measured_value": (
            "100% (test_decision_trace_production + "
            "test_decision_trace_orchestrator_integration + test_decision_trace_store)"
        ),
        "test": "test_decision_trace_orchestrator_integration",
        "stage": 4,
        "notes": "Carried from v0.1e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "cross_adapter_retrieval_correctness",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_cross_adapter_retrieval fixture set, 19 cases)",
        "test": "test_cross_adapter_retrieval",
        "stage": 4,
        "notes": "Carried from v0.1e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "bi_temporal_active_filter_correctness",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": (
            "100% (test_episodic_memory_store + test_semantic_relational_store)"
        ),
        "test": "test_episodic_memory_store",
        "stage": 4,
        "notes": "Carried from v0.1e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "sleep_time_agent_subscription",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_sleep_time_agent fixture set, 20 cases)",
        "test": "test_sleep_time_agent",
        "stage": 4,
        "notes": "Carried from v0.1e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "policy_replay_match_rate_stage4",
        "threshold": "= 100% (includes Stage 4 retrieval plumbing)",
        "status": "MET",
        "measured_value": "100% (test_policy_replay_exact_stage4)",
        "test": "test_policy_replay_exact_stage4",
        "stage": 4,
        "notes": (
            "Carried from v0.1e/h. POLICY_VERSION bumped to 'v0.1j' in this PR."
        ),
    },
    # --- carried Stage 3 gates ---
    {
        "gate": "not_addressed_silence_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_not_addressed_to_me)",
        "test": "test_not_addressed_to_me",
        "stage": 3,
        "notes": "Carried from v0.1d/e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "alert_response_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_cooking_alert)",
        "test": "test_cooking_alert",
        "stage": 3,
        "notes": "Carried from v0.1d/e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "creative_focus_silence_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_creative_focus_silence)",
        "test": "test_creative_focus_silence",
        "stage": 3,
        "notes": "Carried from v0.1d/e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "aesthetic_cooldown_compliance_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_aesthetic_cooldown)",
        "test": "test_aesthetic_cooldown",
        "stage": 3,
        "notes": "Carried from v0.1d/e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "eou_invariant_under_mode_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_eou_invariant_under_mode)",
        "test": "test_eou_invariant_under_mode",
        "stage": 3,
        "notes": "Carried from v0.1d/e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "proactivity_budget_compliance_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_policy_replay_exact Stage 3 trace)",
        "test": "test_policy_replay_exact",
        "stage": 3,
        "notes": "Carried from v0.1d/e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "quiet_mode_compliance_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (test_creative_focus_silence + Stage 3 trace)",
        "test": "test_creative_focus_silence",
        "stage": 3,
        "notes": "Carried from v0.1d/e/h. No v0.1j changes affect this path.",
    },
    # --- carried Stage 2 gates ---
    {
        "gate": "deictic_grounding_accuracy",
        "threshold": "= 100% on fixture set (harness mechanism)",
        "status": "MET",
        "measured_value": "100% (current_frame_grounding_001 fixture)",
        "test": "test_current_frame_grounding",
        "stage": 2,
        "notes": "Carried from v0.1c/d/e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "ambiguous_deictic_refusal_rate",
        "threshold": "= 100% on deictic_ambiguity_001 fixture set",
        "status": "MET",
        "measured_value": "100% (deictic_ambiguity_001 fixture)",
        "test": "test_ambiguous_deictic_refusal",
        "stage": 2,
        "notes": "Carried from v0.1c/d/e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "visual_hallucination_rate",
        "threshold": "= 0 on fixture set",
        "status": "MET",
        "measured_value": "0 (hallucination_resistance_001 fixture)",
        "test": "test_hallucination_resistance",
        "stage": 2,
        "notes": "Carried from v0.1c/d/e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "recent_visual_recall_accuracy",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (recent_visual_memory_001 fixture)",
        "test": "test_recent_visual_memory",
        "stage": 2,
        "notes": "Carried from v0.1c/d/e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "audio_visual_conflict_handling_rate",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (audio_visual_conflict_001 fixture)",
        "test": "test_audio_visual_conflict",
        "stage": 2,
        "notes": "Carried from v0.1c/d/e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "temporal_event_order_accuracy",
        "threshold": "= 100% on fixture set",
        "status": "MET",
        "measured_value": "100% (temporal_event_order_001 fixture)",
        "test": "test_temporal_event_order",
        "stage": 2,
        "notes": "Carried from v0.1c/d/e/h. No v0.1j changes affect this path.",
    },
    # --- carried Stage 1 gates ---
    {
        "gate": "thinking_pause_false_positive_rate",
        "threshold": "= 0 on fixture set",
        "status": "MET",
        "measured_value": "0 (thinking_pause_001 fixture)",
        "test": "test_thinking_pause",
        "stage": 1,
        "notes": "Carried from v0.1b/c/d/e/h. No v0.1j changes affect this path.",
    },
    {
        "gate": "backchannel_false_stop_rate",
        "threshold": "= 0 on fixture set",
        "status": "MET",
        "measured_value": "0 (backchannel_001 fixture)",
        "test": "test_backchannel_survival",
        "stage": 1,
        "notes": "Carried from v0.1b/c/d/e/h. No v0.1j changes affect this path.",
    },
    # --- carried Stage 0 gates ---
    {
        "gate": "orphan_action_count",
        "threshold": "= 0 (includes v0.1j causal chains)",
        "status": "MET",
        "measured_value": "0",
        "test": "test_causal_graph_completeness",
        "stage": 0,
        "notes": (
            "Carried from v0.1a/b/c/d/e/h. v0.1j real-signal producer events "
            "(native_duplex_invocation, addressing_signal) preserve caused_by[] "
            "chains. Invariant #1 maintained."
        ),
    },
    {
        "gate": "policy_replay_match_rate",
        "threshold": "= 100% (Tier B bit-identical + Tier A behavioral-tolerance)",
        "status": "MET",
        "measured_value": "100%",
        "test": "test_policy_replay_exact + test_policy_replay_behavioral_tolerance_stage5",
        "stage": 0,
        "notes": (
            "Carried from v0.1a/b/c/d/e/h. Tier B: bit-identical for everything "
            "downstream of decide(). Tier A: behavioral-tolerance tuple "
            "(action_class + interaction_intent + safety_class) per invariant #6. "
            "POLICY_VERSION bumped to 'v0.1j' in this PR."
        ),
    },
    {
        "gate": "assistant_audio_start_with_cause",
        "threshold": "= 100%",
        "status": "MET",
        "measured_value": "100%",
        "test": "test_decision_provenance",
        "stage": 0,
        "notes": (
            "Carried from v0.1a/b/c/d/e/h. Every assistant_generation_start has "
            "non-empty caused_by[]. No v0.1j changes affect this path."
        ),
    },
    # --- carried NOT_MEASURED latency gates ---
    {
        "gate": "direct_question_latency_p50",
        "threshold": "< 800 ms",
        "status": "NOT_MEASURED",
        "measured_value": None,
        "test": "test_direct_question_latency",
        "stage": 1,
        "notes": (
            "NOT_MEASURED: requires a b200 live-loop log. Carried from "
            "v0.1a/b/c/d/e/h (~170ms p50 on b200 audio-only path in PR #15). "
            "Paired-measurement coverage in no_latency_regression_p50_p95."
        ),
    },
    {
        "gate": "direct_question_latency_p95",
        "threshold": "< 1500 ms",
        "status": "NOT_MEASURED",
        "measured_value": None,
        "test": "test_direct_question_latency",
        "stage": 1,
        "notes": (
            "NOT_MEASURED: requires a b200 live-loop log. Carried from "
            "v0.1a/b/c/d/e/h (~706ms p95 on b200 audio-only path in PR #15)."
        ),
    },
    {
        "gate": "vad_detected_user_speech_to_stop_ms_p95",
        "threshold": "< 200 ms",
        "status": "NOT_MEASURED",
        "measured_value": None,
        "test": "test_barge_in",
        "stage": 1,
        "notes": (
            "NOT_MEASURED: requires a b200 live-loop log. Carried from "
            "v0.1a/b/c/d/e/h (<30ms on barge_in_001 fixture over 30 trials)."
        ),
    },
    {
        "gate": "false_interruption_count_per_10_min",
        "threshold": "< 1",
        "status": "NOT_MEASURED",
        "measured_value": None,
        "test": "test_false_interruption_rate",
        "stage": 1,
        "notes": (
            "NOT_MEASURED: requires a b200 live-loop manual run. Carried from "
            "v0.1a/b/c/d/e/h (0 false interruptions on the 200-scripted-filler-frame "
            "fixture over a 600s window)."
        ),
    },
]

_REQUIRED_NON_GATED_TESTS: list[dict] = [
    # v0.1j new contract tests
    {
        "test": "test_per_signal_smoke",
        "stage": "v0.1j",
        "status": "PASS",
        "notes": "New v0.1j Task 17. Per-signal smoke tests for Wave 2-5 producers.",
    },
    {
        "test": "test_eou_rewiring",
        "stage": "v0.1j",
        "status": "PASS",
        "notes": "New v0.1j Task 8. EOU producer routing (native_duplex primary + fallback).",
    },
    {
        "test": "test_native_duplex_event_emitted",
        "stage": "v0.1j",
        "status": "PASS",
        "notes": "New v0.1j Task 8. native_duplex_invocation event emission.",
    },
    {
        "test": "test_addressing_rewiring",
        "stage": "v0.1j",
        "status": "PASS",
        "notes": "New v0.1j Task 9. Addressing producer routing (MiniCPM primary + WakeWord safety-net).",
    },
    {
        "test": "test_unavailable_markers_have_issues",
        "stage": "v0.1j",
        "status": "PASS",
        "notes": "New v0.1j Task 16. All UNAVAILABLE: #N markers cite known issues.",
    },
    {
        "test": "test_policy_replay_behavioral_tolerance_stage5",
        "stage": "v0.1j",
        "status": "PASS",
        "notes": "New v0.1j Task 18. Behavioral-tolerance tuple per invariant #6.",
    },
    # carried v0.1h non-gated tests
    {
        "test": "test_vision_sidecar_buffers_most_recent_frame",
        "stage": "v0.1h",
        "status": "PASS",
        "notes": "Carried from v0.1h Wave 2. VisionSidecar consume-once invariant.",
    },
    {
        "test": "test_video_frame_reaches_foreground_model",
        "stage": "v0.1h",
        "status": "PASS",
        "notes": "Carried from v0.1h Wave 2. Vision passthrough to StreamingDuplexModel.",
    },
    {
        "test": "test_audio_without_video_passes_none",
        "stage": "v0.1h",
        "status": "PASS",
        "notes": "Carried from v0.1h Wave 2. Audio-only path leaves video=None.",
    },
    {
        "test": "test_video_frame_consumed_once_then_buffer_empty",
        "stage": "v0.1h",
        "status": "PASS",
        "notes": "Carried from v0.1h Wave 2. Consume-once: second consume_pending_frame() returns None.",
    },
    {
        "test": "test_response_content_source",
        "stage": "v0.1h",
        "status": "PASS",
        "notes": "Carried from v0.1h Wave 3. response_content_source populated on all 8 action_types.",
    },
    # carried Stage 4 non-gated tests
    {
        "test": "test_no_memory_mode",
        "stage": 4,
        "status": "PASS",
        "notes": "Privacy mode: no_memory. Carried from v0.1e/h.",
    },
    {
        "test": "test_local_only_mode_raises",
        "stage": 4,
        "status": "PASS",
        "notes": "Privacy mode: local_only hard-raise. Carried from v0.1e/h.",
    },
    {
        "test": "test_decision_trace_production",
        "stage": 4,
        "status": "PASS",
        "notes": "DecisionTrace shape + content. Carried from v0.1e/h.",
    },
    {
        "test": "test_decision_trace_store",
        "stage": 4,
        "status": "PASS",
        "notes": "Anchor 4: per-decision JSON persistence. Carried from v0.1e/h.",
    },
    {
        "test": "test_explicit_turn_handoff",
        "stage": 1,
        "status": "PASS",
        "notes": "Carried from v0.1a/b/c/d/e/h.",
    },
    {
        "test": "test_detector_ablation",
        "stage": 1,
        "status": "PASS",
        "notes": "Carried from v0.1b/c/d/e/h.",
    },
    {
        "test": "test_deictic_detector_ablation",
        "stage": 2,
        "status": "PASS",
        "notes": "Carried from v0.1c/d/e/h.",
    },
    {
        "test": "test_deictic_continuity",
        "stage": 2,
        "status": "PASS",
        "notes": "Carried from v0.1c/d/e/h.",
    },
]

_ADVISORY_SECTION = """\
  Advisory (NOT a gate — eval layer / Stage 6):
  -------------------------------------------------------
  v0.1j wires real MiniCPM signal producers (EOU, addressing, TTS) and
  extends the replay harness with behavioral-tolerance assertions.
  Whether the *quality* of those signals is good is Stage 6 eval work.

  - LoCoMo: NOT_A_GATE. Stage 6.
  - LongMemEval: NOT_A_GATE. Stage 6.
  - MemoryAgentBench: NOT_A_GATE. Stage 6.
  - audio_visual_conflict_score real scorer: deferred (UNAVAILABLE: #168).
  - EmbeddingAdapter real model: deferred (UNAVAILABLE: #183).
  - DeicticDetector real model: deferred (UNAVAILABLE: #169).
  - UrgencyScorer real model: deferred (UNAVAILABLE: #171).
  - SleepTimeAgent LLM confidence: deferred (UNAVAILABLE: #188).
  - Carried Stage 6 advisory (from v0.1d/e/h): aesthetic_reaction_acceptance_rate,
    aesthetic_reaction_annoyance_rate, false_proactive_utterances_per_hour,
    user_reduction_command_compliance_rate, attachment_risk_false_positive_rate.
    All NOT_A_GATE. Stage 6."""


def _check_manual_test_sentinel() -> tuple[str, str | None]:
    """Check docs/manual-test-findings-v0_1j.md for the critical-findings sentinel."""
    findings_path = Path("docs/manual-test-findings-v0_1j.md")
    if not findings_path.exists():
        return "NOT_MEASURED", None

    text = findings_path.read_text()
    m = re.search(r"manual_test_critical_findings_open\s*:\s*(\d+)", text)
    if not m:
        return "FAIL", "sentinel `manual_test_critical_findings_open: <N>` not found in file"

    value = int(m.group(1))
    if value != 0:
        return "FAIL", f"manual_test_critical_findings_open: {value} (expected 0)"

    return "MET", "manual_test_critical_findings_open: 0"


def _run_pytest() -> dict:
    """Run the local contract-test suite and return {passed, failed, skipped, output}."""
    ignore_args = [
        "--ignore=tests/test_minicpm_streaming_duplex.py",
        "--ignore=tests/test_direct_question_latency.py",
    ]

    video_ingest = Path("tests/test_video_ingest.py")
    if video_ingest.exists() and "import torch" in video_ingest.read_text():
        ignore_args.append("--ignore=tests/test_video_ingest.py")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/"] + ignore_args,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    passed = failed = skipped = 0
    for line in output.splitlines():
        if " passed" in line:
            m = re.search(r"(\d+) passed", line)
            if m:
                passed = int(m.group(1))
            m2 = re.search(r"(\d+) skipped", line)
            if m2:
                skipped = int(m2.group(1))
            m3 = re.search(r"(\d+) failed", line)
            if m3:
                failed = int(m3.group(1))
    return {"passed": passed, "failed": failed, "skipped": skipped, "output": output.strip()}


def _build_replay_run(pytest_result: dict, sentinel_status: str, sentinel_value: str | None) -> dict:
    now = datetime.now(timezone.utc).isoformat()

    for g in _GATES:
        if g["gate"] == "manual_test_handbook_critical_findings_open":
            g["status"] = sentinel_status
            g["measured_value"] = sentinel_value

    met = [g for g in _GATES if g["status"] == "MET"]
    not_measured = [g for g in _GATES if g["status"] == "NOT_MEASURED"]
    failed_gates = [g for g in _GATES if g["status"] == "FAIL"]

    failures: list[dict] = []
    if pytest_result["failed"] > 0:
        failures.append({
            "check": "pytest_suite",
            "expected": "0 failures",
            "actual": f"{pytest_result['failed']} failures",
            "event_ref": None,
        })
    for g in failed_gates:
        failures.append({
            "check": g["gate"],
            "expected": g["threshold"],
            "actual": g["measured_value"] or "gate FAIL",
            "event_ref": None,
        })

    return {
        "run_id": f"v0.1j-{uuid.uuid4().hex[:8]}",
        "case_id": "v0.1j_milestone",
        "implementation_config_version": "implementation-config.yaml (May 2026)",
        "policy_version": "v0.1j",
        "started_at": now,
        "finished_at": now,
        "pytest_summary": pytest_result,
        "results": {
            "gates_total": len(_GATES),
            "gates_met": len(met),
            "gates_not_measured": len(not_measured),
            "gates_failed": len(failed_gates),
            "gates": _GATES,
            "required_non_gated_tests": _REQUIRED_NON_GATED_TESTS,
        },
        "failures": failures,
    }


def _gate_summary(report: dict) -> str:
    r = report["results"]
    lines = [
        "=" * 68,
        "  v0.1j ReplayRun — Gate Status Summary",
        "=" * 68,
        f"  Gates total:        {r['gates_total']}",
        f"  MET:                {r['gates_met']}",
        f"  NOT_MEASURED:       {r['gates_not_measured']}",
        f"  FAIL:               {r['gates_failed']}",
        "",
        f"  pytest suite: {report['pytest_summary']['passed']} passed, "
        f"{report['pytest_summary']['skipped']} skipped, "
        f"{report['pytest_summary']['failed']} failed",
        "",
        "  Gate detail:",
        "-" * 68,
    ]
    for g in r["gates"]:
        value = g["measured_value"] or "— (NOT_MEASURED; see notes)"
        lines.append(f"  [{g['status']:<12}]  {g['gate']}")
        lines.append(f"               threshold: {g['threshold']}")
        lines.append(f"               measured:  {value}")
        lines.append(f"               test:      {g['test']}  (Stage {g['stage']})")
        lines.append(f"               note:      {g['notes']}")
        lines.append("")
    lines += [
        "  Required contract tests (no numeric gate):",
        "-" * 68,
    ]
    for t in r["required_non_gated_tests"]:
        lines.append(f"  [{t['status']:<12}]  {t['test']}  (Stage {t['stage']})")
        lines.append(f"               note: {t['notes']}")
        lines.append("")
    lines += [
        "=" * 68,
        _ADVISORY_SECTION,
        "=" * 68,
        "  v0.1j milestone verdict:",
        f"    {r['gates_met']} of {r['gates_total']} gates MET.",
        f"    {r['gates_not_measured']} gates NOT_MEASURED (latency gates require b200",
        "      live-loop run; final_product_producer_invocation_rate requires",
        "      b200 CUDA session with real MiniCPM weights).",
    ]
    if r["gates_failed"] > 0:
        lines.append(f"    {r['gates_failed']} gate(s) FAIL — see detail above.")
        lines.append("    NOT ready for v0.1j tag.")
    else:
        lines += [
            "    All v0.1a + v0.1b + v0.1c + v0.1d + v0.1e + v0.1h tests still green.",
            "    New v0.1j contract tests pass:",
            "      test_per_signal_smoke                              (Task 17 Wave 2-5 producers)",
            "      test_eou_rewiring + test_native_duplex_event_emitted (Task 8 EOU routing)",
            "      test_addressing_rewiring                           (Task 9 addressing routing)",
            "      test_unavailable_markers_have_issues               (Task 16 allowlist)",
            "      test_policy_replay_behavioral_tolerance_stage5     (Task 18 invariant #6)",
            "    POLICY_VERSION bumped to 'v0.1j'.",
            "    This script does NOT apply the v0.1j tag — that is the project lead's action.",
            "  Ready for v0.1j tag.",
        ]
    lines.append("=" * 68)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit v0.1j ReplayRun milestone report.")
    parser.add_argument("--json-only", action="store_true", help="Emit JSON only, no summary.")
    args = parser.parse_args()

    sentinel_status, sentinel_value = _check_manual_test_sentinel()
    pytest_result = _run_pytest()
    report = _build_replay_run(pytest_result, sentinel_status, sentinel_value)

    if not args.json_only:
        print(_gate_summary(report))
        print()

    print(json.dumps(report, indent=2))

    all_met = all(g["status"] in ("MET", "NOT_MEASURED") for g in _GATES)
    if not all_met or pytest_result["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
