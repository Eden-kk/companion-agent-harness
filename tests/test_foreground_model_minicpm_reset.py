"""Unit tests for MiniCPMStreamingModel.reset_streaming_session()."""
from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel


class _FakeModel:
    """Mirrors MiniCPMO — the class that ACTUALLY owns reset_session."""
    def __init__(self):
        self.reset_calls = []

    def reset_session(self, reset_token2wav_cache: bool) -> None:
        self.reset_calls.append({"reset_token2wav_cache": reset_token2wav_cache})


class _FakeDuplex:
    """Mirrors MiniCPMODuplex — does NOT have reset_session directly; only .model does."""
    def __init__(self):
        self.model = _FakeModel()


class _FakeLogger:
    def __init__(self):
        self.events = []

    def log(self, evt) -> None:
        self.events.append(evt)


def _build_model() -> tuple[MiniCPMStreamingModel, _FakeDuplex, _FakeLogger]:
    model = MiniCPMStreamingModel.__new__(MiniCPMStreamingModel)
    fake_duplex = _FakeDuplex()
    fake_logger = _FakeLogger()
    model._duplex = fake_duplex
    model._logger = fake_logger
    model._session_id = "test-session"
    model._last_is_listen = False
    model._last_native_duplex_event_id = "prior-event-id"
    model._seq = 0
    return model, fake_duplex, fake_logger


def test_reset_calls_underlying_reset_with_token2wav_false():
    model, fake_duplex, _ = _build_model()
    model.reset_streaming_session(caused_by=["some-policy-event-id"])
    assert len(fake_duplex.model.reset_calls) == 1
    assert fake_duplex.model.reset_calls[0]["reset_token2wav_cache"] is False


def test_reset_emits_session_reset_event():
    model, _, fake_logger = _build_model()
    evt = model.reset_streaming_session(caused_by=["pol-evt-1"])
    assert evt.event_type == "minicpm_session_reset"
    assert evt.caused_by == ["pol-evt-1"]
    assert evt.payload_inline["reset_token2wav_cache"] is False
    assert len(fake_logger.events) == 1
    assert fake_logger.events[0] is evt


def test_reset_resets_last_is_listen_and_clears_invocation_id():
    model, _, _ = _build_model()
    model.reset_streaming_session(caused_by=["x"])
    assert model._last_is_listen is True
    assert model._last_native_duplex_event_id is None


def test_reset_event_payload_inline_fields():
    model, _, fake_logger = _build_model()
    evt = model.reset_streaming_session(caused_by=["evt-id"])
    assert evt.payload_inline["trigger"] == "post_turn"
    assert "reset_at_ms" in evt.payload_inline
