"""Deterministic delivery detector for the TACT-Bench streaming harness.

Decides whether a model utterance delivered a held item's payload, and in what form.
Pure function — no network, no state.
"""

from __future__ import annotations

import logging
import re
import string

from companion_harness.evals.adapters.tact_bench_stream_types import Emission

# --- PREREGISTERED (§4.7) ---
T_LEX = 0.35          # content-word token-set F1 threshold for lexical match
T_EMB = 0.70          # embedding cosine threshold
_EMB_MODEL_ID = "all-MiniLM-L6-v2"
_EMB_MODEL_REVISION = "8b3219a92973c328a8e22fadcfa821b5dc75636a"
# ----------------------------

_logger = logging.getLogger(__name__)

# module-level flag: set to True when the embedding model cannot load
_LEXICAL_ONLY: bool = False
_EMB_MODEL = None  # lazily loaded


def _get_embedding_model():
    global _EMB_MODEL, _LEXICAL_ONLY  # noqa: PLW0603
    if _LEXICAL_ONLY:
        return None
    if _EMB_MODEL is not None:
        return _EMB_MODEL
    try:
        from sentence_transformers import SentenceTransformer  # noqa: WPS433

        try:
            _EMB_MODEL = SentenceTransformer(_EMB_MODEL_ID, revision=_EMB_MODEL_REVISION)
        except Exception:  # noqa: BLE001
            _logger.warning(
                "delivery_detector: revision %r unavailable; loading %r without pin.",
                _EMB_MODEL_REVISION,
                _EMB_MODEL_ID,
            )
            _EMB_MODEL = SentenceTransformer(_EMB_MODEL_ID)
        return _EMB_MODEL
    except Exception as exc:  # noqa: BLE001
        _LEXICAL_ONLY = True
        _logger.warning(
            "delivery_detector: embedding model %r unavailable (%s); "
            "falling back to lexical-only mode (T_LEX=%.2f). "
            "Lexical-only runs are tagged and must not be merged with embedding runs.",
            _EMB_MODEL_ID,
            exc,
            T_LEX,
        )
        return None


_STOPWORDS = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would could should may might shall can not no nor and or but "
    "if so then that this these those it its i you he she we they what "
    "which who when where how of in on at to for with by from up out".split()
)


def _content_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 1}


def _lexical_f1(utt: str, payload: str) -> float:
    u_toks = _content_tokens(utt)
    p_toks = _content_tokens(payload)
    if not p_toks:
        return 0.0
    if not u_toks:
        return 0.0
    tp = len(u_toks & p_toks)
    precision = tp / len(u_toks)
    recall = tp / len(p_toks)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _embedding_cos(utt: str, payload: str) -> float:
    model = _get_embedding_model()
    if model is None:
        return 0.0
    import numpy as np

    vecs = model.encode([utt, payload], convert_to_numpy=True)
    u, p = vecs[0], vecs[1]
    denom = float(np.linalg.norm(u) * np.linalg.norm(p))
    if denom == 0:
        return 0.0
    return float(np.dot(u, p) / denom)


def _is_negation(utt: str, payload: str) -> bool:
    """Heuristic: does the utterance negate the key content of the payload?"""
    neg_patterns = [
        r"\bnot\s+(?:done|ready|finished|finish|complete|available|yet|going|through)\b",
        r"\b(?:did|does|do|is|was|has|have|had|will|won[''`]?t|can[''`]?t|couldn[''`]?t)\s+not\b",
        r"\bn[''`]?t\b",
        r"\bstill\s+(?:building|running|pending|waiting|in\s+progress)\b",
        r"\bfailed\b",
    ]
    utt_lower = utt.lower()
    p_toks = _content_tokens(payload)
    u_toks = _content_tokens(utt)
    if not (p_toks & u_toks):
        return False
    for pat in neg_patterns:
        if re.search(pat, utt_lower):
            return True
    return False


def detect(
    utterance_text: str,
    item_payload: str,
    earlier_ask: str | None = None,
    lexical_only: bool = False,
) -> tuple[bool, str | None, bool, float]:
    """Detect whether *utterance_text* delivers *item_payload*.

    Returns ``(delivered, form, reanchored, confidence)``.

    delivered
        True if content-word F1 >= T_LEX OR embedding cosine >= T_EMB, and not
        a negation of the payload.

    form
        ``CHIME`` / ``SILENT_NOTIFY`` if a non-verbal marker is present in the
        utterance; else ``SPEAK_FULL`` if payload-slot completeness is high;
        else ``SPEAK_BRIEF``. ``None`` if not delivered.

    reanchored
        True if the utterance references *earlier_ask* (when provided).

    confidence
        max(lexical_f1, embedding_cos); 0.0 if not delivered.
    """
    lex = _lexical_f1(utterance_text, item_payload)
    emb = 0.0
    if not lexical_only and not _LEXICAL_ONLY:
        emb = _embedding_cos(utterance_text, item_payload)

    confidence = max(lex, emb)

    if _is_negation(utterance_text, item_payload):
        return False, None, False, 0.0

    delivered = lex >= T_LEX or emb >= T_EMB

    if not delivered:
        return False, None, False, 0.0

    form = _detect_form(utterance_text, item_payload)
    reanchored = _detect_reanchor(utterance_text, earlier_ask)
    return True, form, reanchored, confidence


def _detect_form(utt: str, payload: str) -> str:
    utt_lower = utt.lower().strip()
    # non-verbal markers first
    if re.search(r"\bchime\b|<chime>|\[chime\]|🔔", utt_lower):
        return "CHIME"
    if re.search(r"\bsilent.notify\b|<silent_notify>|\[silent\]", utt_lower):
        return "SILENT_NOTIFY"
    # payload-slot completeness: fraction of payload content tokens in utterance
    p_toks = _content_tokens(payload)
    u_toks = _content_tokens(utt)
    if p_toks:
        coverage = len(p_toks & u_toks) / len(p_toks)
    else:
        coverage = 1.0
    # length tiebreak: > 20 words suggests a fuller response
    word_count = len(utt.split())
    if coverage >= 0.8 and word_count > 20:
        return "SPEAK_FULL"
    return "SPEAK_BRIEF"


def _detect_reanchor(utt: str, earlier_ask: str | None) -> bool:
    if not earlier_ask:
        return False
    anchor_patterns = [
        r"\babout\s+that\b",
        r"\bregarding\s+your\b",
        r"\byou\s+asked\b",
        r"\byou\s+mentioned\b",
        r"\bfollowing\s+up\b",
        r"\bearlier\b",
    ]
    utt_lower = utt.lower()
    for pat in anchor_patterns:
        if re.search(pat, utt_lower):
            return True
    ask_toks = _content_tokens(earlier_ask)
    utt_toks = _content_tokens(utt)
    if ask_toks and len(ask_toks & utt_toks) / len(ask_toks) >= 0.4:
        return True
    return False


def annotate_emissions(
    raw: list[tuple[int, bool, str]],
    case: dict,
    pending_payloads: dict[str, str],
    earlier_asks: dict[str, str] | None = None,
    lexical_only: bool = False,
) -> list[Emission]:
    """Annotate raw per-tick (tick, spoke, text) tuples into Emissions.

    For each spoken tick, run detect() against every pending item whose
    t_avail <= tick; pick the best-confidence match as detected_item.

    The FIRST tick that matches an item is the delivery; later ticks that
    match the same item count as duplicate false-positives (cried-wolf) in
    the scorer. detected_item is intentionally set on every matching tick —
    the scorer distinguishes delivery vs duplicate by tick order.

    Parameters
    ----------
    raw
        List of ``(tick, spoke, text)`` from a streaming runner.
    case
        Layer-3 case dict (for t_avail lookup per item).
    pending_payloads
        Map ``{item_id: payload_text}``.
    earlier_asks
        Optional map ``{item_id: earlier_ask_text}`` for +RA detection.
    """
    items: list[dict] = case.get("items") or ([case["item"]] if "item" in case else [])
    avail_at: dict[str, int] = {it["id"]: int(it.get("t_avail", 0)) for it in items}
    asks = earlier_asks or {}

    result: list[Emission] = []
    for tick, spoke, text in raw:
        if not spoke:
            result.append(Emission(tick=tick, spoke=False, text=text))
            continue

        best_id: str | None = None
        best_form: str | None = None
        best_reanchored = False
        best_conf = 0.0

        for item_id, payload in pending_payloads.items():
            if avail_at.get(item_id, 999) > tick:
                continue
            delivered, form, reanchored, conf = detect(
                text, payload, asks.get(item_id), lexical_only=lexical_only
            )
            if delivered and conf > best_conf:
                best_id = item_id
                best_form = form
                best_reanchored = reanchored
                best_conf = conf

        result.append(
            Emission(
                tick=tick,
                spoke=True,
                text=text,
                detected_item=best_id,
                form=best_form,
                reanchored=best_reanchored,
                confidence=best_conf,
            )
        )
    return result
