"""Shared pytest fixtures — loads evaluation-case fixtures at collection time."""

import pytest

from companion_harness.fixtures.loader import load_fixture


@pytest.fixture(scope="session")
def thinking_pause_001():
    return load_fixture("thinking_pause_001")
