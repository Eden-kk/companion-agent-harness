# v0.1e Memory-State Fixture Schema — SKETCH

> **Status: SKETCH.** Final schema locks before Task 16 (`correction_001`
> fixture activation). This document exists so Task 16 (and any parallel
> Stage 4 fixture tasks) don't re-open the structural question. Downstream
> tasks may add fields; they should not remove the fields listed here without
> a roadmap note.

This sketch covers three questions that every Stage 4 memory-state fixture
must answer:

1. Shape of pre-populated store snapshots.
2. Shape of scripted memory event sequences.
3. How fixtures bind to contract tests.

---

## 1. Pre-populated store snapshots

A fixture that needs the memory stores to contain items before the scenario
runs supplies a `memory_state` object in `case.json`:

```json
{
  "memory_state": {
    "<store_name>": {
      "<item_id>": {
        "item_id": "<item_id>",
        "store": "<session|core_profile|episodic|semantic_relational>",
        "content": {},
        "source_event_id": "<event_id from signal_trace or a sentinel string>",
        "created_at": "<ISO-8601>",
        "last_confirmed_at": "<ISO-8601>",
        "confidence": 0.9,
        "salience": 0.8,
        "privacy_level": "safe",
        "mutability": "system_revisable",
        "valid_from": "<ISO-8601>",
        "valid_to": null,
        "superseded_by": null,
        "user_visible_summary": {
          "retention_policy_id": "<id from Anchor 2 alphabet>",
          "value": "<human-readable description>",
          "sensitivity": "safe"
        }
      }
    }
  }
}
```

Required provenance fields (invariant #3): `source_event_id`, `created_at`,
`confidence`, `salience`, `valid_from`, `valid_to`, `superseded_by`,
`user_visible_summary`. All must be present; `null` is a valid value for
nullable fields.

`source_event_id` in a snapshot may reference an event in `signal_trace`
or a fixture-internal sentinel string (e.g. `"FIXTURE_BOOTSTRAP"`) when
the item predates the scripted scenario window.

---

## 2. Scripted memory event sequences

A fixture supplies a `memory_op_trace` object listing the memory operations
the scenario exercises, in causal order:

```json
{
  "memory_op_trace": {
    "status": "active",
    "ops": [
      {
        "event_type": "memory_write_candidate",
        "payload": {
          "item_id": "<item_id>",
          "store": "episodic",
          "content": {}
        },
        "caused_by": ["<event_id from signal_trace>"],
        "expected_outcome": "item written to store with provenance fields populated"
      },
      {
        "event_type": "memory_commit_completed",
        "payload": {
          "item_id": "<item_id>"
        },
        "caused_by": ["<memory_write_candidate event_id>"],
        "expected_outcome": "commit receipt logged; foreground latency unaffected"
      }
    ]
  }
}
```

Each op entry:

| field | required | meaning |
|---|---|---|
| `event_type` | yes | one of the 5 memory_* types in MEMORY_EVENT_SCHEMAS |
| `payload` | yes | subset of fields relevant to this event; not the full MemoryItem |
| `caused_by` | yes | list of event_ids that causally precede this op |
| `expected_outcome` | yes | human-readable assertion; contract test asserts this holds |

`status` is `"skeleton"` (placeholder, not yet filled) or `"active"`
(ready for Stage 4 contract test execution).

---

## 3. How fixtures bind to contract tests

Contract tests follow this pattern:

```python
def test_correction(fixture_loader):
    case = fixture_loader.load("correction_001")

    # 1. Seed stores from snapshot
    store = build_stores_from_snapshot(case["memory_state"])

    # 2. Run scripted event sequence through the system under test
    for op in case["memory_op_trace"]["ops"]:
        emit_and_process(op, store)

    # 3. Assert post-condition for each op
    for op in case["memory_op_trace"]["ops"]:
        assert_outcome(op["expected_outcome"], store)
```

The test must NOT mutate `case.json`; it reads fixtures as immutable inputs.

---

## 4. Worked example — `example_001` (hypothetical)

This illustrates all three sections for a single-fact write scenario.

### case.json (excerpt)

```json
{
  "case_id": "example_001",
  "stage": 4,
  "scenario": "single_episodic_fact_write",
  "modalities": ["audio"],
  "fixture_ref": "companion_harness/fixtures/example_001/",
  "sensitivity": "safe_eval_fixture",
  "consent_class": "synthetic_eval",

  "memory_state": {
    "episodic": {}
  },

  "signal_trace": {
    "status": "active",
    "frames": [
      {
        "event_id": "evt_001",
        "event_type": "user_speech_end",
        "timestamp_wall": "2026-05-15T10:00:00Z",
        "payload": {"transcript": "I just moved to Seattle"}
      }
    ]
  },

  "memory_op_trace": {
    "status": "active",
    "ops": [
      {
        "event_type": "memory_write_candidate",
        "payload": {
          "item_id": "item_abc",
          "store": "episodic",
          "content": {"fact": "user_location", "value": "Seattle"}
        },
        "caused_by": ["evt_001"],
        "expected_outcome": "episodic store contains item_abc with valid_to=null and superseded_by=null"
      },
      {
        "event_type": "memory_commit_completed",
        "payload": {"item_id": "item_abc"},
        "caused_by": ["evt_001"],
        "expected_outcome": "commit_audit_30d event logged; item_abc retrievable"
      }
    ]
  }
}
```

### Contract test (pseudocode)

```python
def test_example_001(fixture_loader):
    case = fixture_loader.load("example_001")
    store = build_stores_from_snapshot(case["memory_state"])   # empty episodic store

    emit_signal(case["signal_trace"]["frames"][0])             # user_speech_end

    ops = case["memory_op_trace"]["ops"]
    emit_and_process(ops[0], store)   # memory_write_candidate
    emit_and_process(ops[1], store)   # memory_commit_completed

    item = store["episodic"]["item_abc"]
    assert item["valid_to"] is None
    assert item["superseded_by"] is None
    assert item["source_event_id"] == "evt_001"
```

---

## Open items (resolve before Task 16)

- Exact Python type for `build_stores_from_snapshot` (dict vs Protocol).
- Whether `source_event_id = "FIXTURE_BOOTSTRAP"` needs a dedicated sentinel
  constant in `schemas.py` or stays a string convention.
- Bi-temporal snapshot for `correction_001`: needs two items in `episodic`
  (old fact + correction), with `superseded_by` chain pre-set on the old
  item if the test asserts post-correction state directly.
