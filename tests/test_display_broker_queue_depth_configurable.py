"""DisplayBroker queue_depth is configurable via build_app(display_queue_depth=N)."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_build_app_queue_depth_propagates_to_broker(tmp_path: Path) -> None:
    from manual_test_console.server import KEY_BROKER, build_app  # noqa: WPS433

    app = build_app(blob_dir=tmp_path / "blobs", display_queue_depth=128)
    broker = app[KEY_BROKER]
    q = broker.add()
    assert q.maxsize == 128


def test_display_broker_queue_depth_clamped_to_min() -> None:
    from manual_test_console.server import DisplayBroker  # noqa: WPS433

    broker = DisplayBroker(queue_depth=10)  # below minimum of 64
    q = broker.add()
    assert q.maxsize == 64


def test_display_broker_queue_depth_clamped_to_max() -> None:
    from manual_test_console.server import DisplayBroker  # noqa: WPS433

    broker = DisplayBroker(queue_depth=99999)  # above maximum of 16384
    q = broker.add()
    assert q.maxsize == 16384


def test_display_broker_default_queue_depth_is_1024() -> None:
    from manual_test_console.server import DisplayBroker  # noqa: WPS433

    broker = DisplayBroker()
    q = broker.add()
    assert q.maxsize == 1024
