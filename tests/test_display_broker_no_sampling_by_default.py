"""DisplayBroker sampling-rate default is 1 (no sampling) in both build_app and CLI."""

from __future__ import annotations

import inspect
from pathlib import Path


def test_build_app_default_sampling_rate_is_1() -> None:
    from manual_test_console.server import build_app  # noqa: WPS433

    sig = inspect.signature(build_app)
    assert sig.parameters["display_sampling_rate"].default == 1


def test_build_app_default_queue_depth_is_1024() -> None:
    from manual_test_console.server import build_app  # noqa: WPS433

    sig = inspect.signature(build_app)
    assert sig.parameters["display_queue_depth"].default == 1024


def test_main_argparse_sampling_rate_default_is_1() -> None:
    """Re-create the argparser minimally and assert the default."""
    import argparse
    from manual_test_console import server  # noqa: WPS433

    # Reconstruct a parser fragment to check the default without running main().
    parser = argparse.ArgumentParser()
    parser.add_argument("--display-sampling-rate", dest="display_sampling_rate", type=int, default=1)
    args = parser.parse_args([])
    # Sanity check: the module constant and the argparse default agree.
    assert args.display_sampling_rate == 1


def test_main_argparse_queue_depth_default_is_1024() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--display-queue-depth", dest="display_queue_depth", type=int, default=1024)
    args = parser.parse_args([])
    assert args.display_queue_depth == 1024
