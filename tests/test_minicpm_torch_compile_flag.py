"""Tests for MiniCPMStreamingModel enable_torch_compile kwarg.

CPU-only — no real model weights loaded.  Two cases:
  1. enable_torch_compile=False → _torch_compile_active is False (no compile attempt).
  2. enable_torch_compile=True, torch.compile monkeypatched to raise → no crash,
     warning printed, _torch_compile_active remains False.
"""

from __future__ import annotations

import types
import unittest.mock as mock
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Minimal fake base model so MiniCPMStreamingModel.__init__ never touches GPU.
# ---------------------------------------------------------------------------

class _FakeDuplex:
    pass


class _FakeLLM:
    pass


class _FakeBase:
    llm = _FakeLLM()
    device = "cpu"

    def eval(self) -> "_FakeBase":
        return self

    def cuda(self) -> "_FakeBase":
        return self

    def as_duplex(self, generate_audio: bool, sliding_window_mode: str = "off") -> _FakeDuplex:
        return _FakeDuplex()


def _make_fake_model_patches():
    """Return a dict of patches that prevent any real model load."""
    fake_base = _FakeBase()
    fake_tokenizer = MagicMock()

    automodel_patch = patch(
        "companion_harness.foreground_model_minicpm.AutoModel.from_pretrained",
        return_value=fake_base,
    )
    autotokenizer_patch = patch(
        "companion_harness.foreground_model_minicpm.AutoTokenizer.from_pretrained",
        return_value=fake_tokenizer,
    )
    return automodel_patch, autotokenizer_patch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_torch_compile_flag_accepted() -> None:
    """enable_torch_compile=False leaves _torch_compile_active=False (no compile)."""
    am, at = _make_fake_model_patches()
    with am, at:
        from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
        model = MiniCPMStreamingModel(enable_torch_compile=False)
    assert model._torch_compile_active is False


def test_torch_compile_failure_falls_back(capsys: pytest.CaptureFixture) -> None:
    """If torch.compile raises, _torch_compile_active stays False and warning is printed."""
    am, at = _make_fake_model_patches()

    def _compile_that_raises(module, **kwargs):
        raise RuntimeError("simulated torch.compile failure")

    with am, at:
        with patch("companion_harness.foreground_model_minicpm.torch.compile", side_effect=_compile_that_raises):
            from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
            model = MiniCPMStreamingModel(enable_torch_compile=True)

    assert model._torch_compile_active is False
    captured = capsys.readouterr()
    assert "warning: torch.compile failed" in captured.out
    assert "RuntimeError" in captured.out
