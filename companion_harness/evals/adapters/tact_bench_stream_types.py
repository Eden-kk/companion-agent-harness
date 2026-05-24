"""Shared contract types for the TACT-Bench streaming-trajectory harness.

`Emission` is the single boundary type between three otherwise-independent pieces:
  - the streaming runners (MiniCPM / gpt-realtime) produce RAW per-tick output
    `(tick, spoke, text)`;
  - the delivery detector annotates each spoken tick against the case's pending
    items, yielding `detected_item` / `form` / `reanchored`;
  - the event-ledger scorer consumes the annotated `Emission` list.

Keeping it in one module lets the scorer (S1) and the detector/runners (S2-S4)
be built and tested independently without import races.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Emission:
    """One tick of model output, after detector annotation.

    A tick with ``spoke=True`` but ``detected_item is None`` is non-delivery
    speech (chatter / talking over the user) — it feeds the interaction-cost
    axis, not an item outcome.
    """

    tick: int                          # tick index at which the model produced output
    spoke: bool                        # native gate fired (any output this tick)
    text: str = ""                     # output text (audit trail + detector input)
    detected_item: str | None = None   # item_id the detector matched, else None
    form: str | None = None            # SPEAK_BRIEF|SPEAK_FULL|CHIME|SILENT_NOTIFY|None
    reanchored: bool = False           # delivery re-anchored the earlier ask (+RA)
    confidence: float = 0.0            # detector confidence in [0,1]
