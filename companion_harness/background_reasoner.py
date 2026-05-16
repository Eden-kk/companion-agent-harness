"""BackgroundReasoner Protocol + FakeBackgroundReasoner (v0.1f Task 8 — Stage 5).

Smart-path reasoner: given a ToolDispatchRequest, select and call tools
asynchronously and yield Events as they arrive; then summarize results
into MemoryItems for context injection via set_context() (OQ-7 / OQ-12).

No recursive ToolRouter calls at v0.1f (deferred to v0.1g per OQ-1).
No model SDK imports (adapter-first invariant).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol, runtime_checkable

from companion_harness.schemas import Event, MemoryItem, SensitiveField
from companion_harness.tool_router import ToolDispatchRequest

__all__ = [
    "ToolReasonerResult",
    "BackgroundReasoner",
    "FakeBackgroundReasoner",
]

_SCHEMA_VERSION = "0.1"
_SOURCE = "fake_background_reasoner"


@dataclass(frozen=True)
class ToolReasonerResult:
    """Aggregated output of a BackgroundReasoner select_and_call() pass."""
    tool_call_id: str
    tool_name:    str
    summary_text: str
    caused_by:    list[str]


@runtime_checkable
class BackgroundReasoner(Protocol):
    """Adapter seam for the smart-path background reasoner (OQ-12).

    Callers subscribe via asyncio queue per OQ-12; the orchestrator
    forwards yielded Events to EventLogger.log() (invariant #10) and
    calls set_context() with summarize() results (OQ-7).
    """

    def select_and_call(
        self, request: ToolDispatchRequest
    ) -> AsyncIterator[Event]: ...

    def summarize(self, results: ToolReasonerResult) -> list[MemoryItem]: ...


class FakeBackgroundReasoner:
    """Deterministic in-process BackgroundReasoner for contract tests.

    Emits two Events per select_and_call(): a reasoning_started and a
    reasoning_completed.  summarize() returns a single MemoryItem whose
    content echoes the result summary_text.

    No recursive ToolRouter calls (v0.1f scope per OQ-1).
    """

    def __init__(self, session_id: str = "test-session") -> None:
        self._session_id = session_id
        self._seq = 0

    def select_and_call(
        self, request: ToolDispatchRequest
    ) -> AsyncIterator[Event]:
        return self._generate_events(request)

    async def _generate_events(
        self, request: ToolDispatchRequest
    ) -> AsyncIterator[Event]:
        yield self._make_event(
            "background_reasoning_started",
            caused_by=list(request.caused_by),
        )
        yield self._make_event(
            "background_reasoning_completed",
            caused_by=list(request.caused_by),
        )

    def summarize(self, results: ToolReasonerResult) -> list[MemoryItem]:
        now = datetime.now(timezone.utc).isoformat()
        return [
            MemoryItem(
                item_id=f"br-{results.tool_call_id}",
                store="episodic",
                content={"text": results.summary_text, "tool_name": results.tool_name},
                source_event_id=results.caused_by[0] if results.caused_by else "",
                created_at=now,
                last_confirmed_at=now,
                confidence=1.0,
                salience=0.5,
                privacy_level="user_content",
                mutability="system_revisable",
                valid_from=now,
                valid_to=None,
                superseded_by=None,
                user_visible_summary=SensitiveField(
                    retention_policy_id="ep_default_30d",
                    value=results.summary_text,
                    sensitivity="sensitive",
                    source_event_ids=list(results.caused_by),
                ),
            )
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _make_event(self, event_type: str, caused_by: list[str]) -> Event:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-fbr-{seq}-{now_ms}"
        payload_hash = hashlib.sha256(
            f"{event_type}:{event_id}".encode()
        ).hexdigest()[:16]
        return Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type=event_type,
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="tool_event",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="tool_result_default",
        )
