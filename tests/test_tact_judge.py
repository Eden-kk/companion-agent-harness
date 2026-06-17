"""PR3 — DeliveryJudge (Mode-B delivery judge behind the Examiner seam).

Hermetic: a fake OpenAI-shaped client is injected (no network, no key). The
no-client path returns a PENDING label so metrics degrade cleanly.
"""
import asyncio
import json
from types import SimpleNamespace

from companion_harness.evals.adapters.tact_bench import DeliveryJudge
from companion_harness.evals.protocols import Examiner

_TRAJ = [
    {"t": 0, "injected": False, "is_listen": True, "text": ""},
    {"t": 1, "injected": True, "is_listen": False, "text": "deploy done"},
]
_ITEM = {"payload": "deploy succeeded", "t_available": 1, "labels": {"urgency": "low"}}
_VERDICT = json.dumps(
    {"delivered": True, "first_delivery_chunk": 1, "form": "BRIEF", "echo_only": False, "rationale": "ok"}
)


def _fake_client(content: str, counter: list[int]):
    def create(**kwargs):
        counter[0] += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_judge_satisfies_examiner_protocol():
    judge = DeliveryJudge(client_factory=lambda: _fake_client(_VERDICT, [0]))
    assert isinstance(judge, Examiner)  # has respond()


def test_judge_label_parses_verdict():
    judge = DeliveryJudge(client_factory=lambda: _fake_client(_VERDICT, [0]))
    label = judge.label(_TRAJ, _ITEM)
    assert label["delivered"] is True
    assert label["first_delivery_chunk"] == 1
    assert label["form"] == "BRIEF"
    assert label["echo_only"] is False


def test_judge_caches_by_input():
    counter = [0]
    judge = DeliveryJudge(client_factory=lambda: _fake_client(_VERDICT, counter))
    judge.label(_TRAJ, _ITEM)
    judge.label(_TRAJ, _ITEM)  # identical input → served from cache
    assert counter[0] == 1


def test_judge_pending_without_client(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    judge = DeliveryJudge(client_factory=None)
    label = judge.label(_TRAJ, _ITEM)
    assert label["delivered"] is None
    assert "unavailable" in label["rationale"]


def test_judge_normalizes_bad_form():
    bad = json.dumps({"delivered": True, "first_delivery_chunk": "x", "form": "WEIRD", "echo_only": False})
    judge = DeliveryJudge(client_factory=lambda: _fake_client(bad, [0]))
    label = judge.label(_TRAJ, _ITEM)
    assert label["form"] is None
    assert label["first_delivery_chunk"] is None  # non-int coerced to None


def test_judge_respond_delegates_from_event_payload():
    judge = DeliveryJudge(client_factory=lambda: _fake_client(_VERDICT, [0]))
    event = SimpleNamespace(payload_inline={"trajectory": _TRAJ, "item": _ITEM, "user_script": []})
    label = asyncio.run(judge.respond(event))
    assert label["delivered"] is True and label["form"] == "BRIEF"
