"""In-memory authoritative state for Tier-B runtime config.

See ``docs/design-config-and-dashboard.md`` §3 (loading + override mechanism),
§5 (server API → ConfigStore side effects), and §6 (replay reproducibility).

The store is intentionally decoupled from the allowlist's concrete type: it
accepts any mapping whose values expose a ``.default`` attribute. The
orchestrator passes ``manual_test_console.config_schema.ALLOWLIST`` (Task B);
tests pass a structurally-equivalent in-memory stub.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from manual_test_console.config_schema import HOT_SEAMS, validate_patch

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfigChange:
    """Returned by ConfigStore mutator methods for the caller to log as an event."""

    key: str
    previous_value: Any
    new_value: Any


@dataclass(frozen=True)
class SeamStateChange:
    """Returned by ConfigStore.set_seam() for the caller to log as an event."""

    seam: str
    previous_enabled: bool
    new_enabled: bool


class ConfigStore:
    """In-memory authoritative state for Tier-B runtime config.

    Lifecycle:
      1. ``__init__``: load defaults from the injected allowlist.
      2. (optional) ``load_from_yaml(path)``: override defaults from config.yaml.
      3. (live) ``get(key)`` / ``set(key, value)`` / ``reset(key)`` / ``reset_all()``.

    Thread-safety: asyncio-level only. All public methods are sync but the
    expectation is they're called from a single event loop. The store does NOT
    hold an asyncio.Lock — read/write contention is impossible on a single loop
    because there's no ``await`` between read and write inside this module.

    Replay note: the orchestrator reads ``ConfigStore.get(key)`` once per EOU
    boundary (per design doc §6), so mid-utterance threshold flips cannot
    happen. The store itself does NOT emit events; the caller of ``set()`` /
    ``reset()`` receives a ``ConfigChange`` payload and is responsible for
    logging.
    """

    def __init__(
        self,
        allowlist: dict[str, Any],
        seam_defaults: dict[str, bool] | None = None,
    ) -> None:
        # The allowlist entry is treated structurally: every value must expose
        # a ``.default`` attribute (and, in production, also ``.min`` /
        # ``.max`` / ``.value_type`` used by validate_patch elsewhere).
        self._allowlist = allowlist
        self._state: dict[str, Any] = {k: v.default for k, v in allowlist.items()}
        self._seam_state: dict[str, bool] = {seam: True for seam in HOT_SEAMS}
        if seam_defaults is not None:
            for seam, enabled in seam_defaults.items():
                if seam in self._seam_state:
                    self._seam_state[seam] = enabled

    def get(self, key: str) -> Any:
        """Return the current effective value for ``key``.

        Raises ``KeyError`` if the key is not in the allowlist.
        """
        if key not in self._allowlist:
            raise KeyError(key)
        return self._state[key]

    def current_state(self) -> dict[str, Any]:
        """Snapshot of all key→value pairs. Used by the ``GET /config`` endpoint."""
        return dict(self._state)

    def current_seam_state(self) -> dict[str, bool]:
        """Snapshot of all seam→enabled pairs."""
        return dict(self._seam_state)

    def get_seam(self, seam: str) -> bool:
        """Return the current enabled state for ``seam``.

        Raises ``KeyError`` if the seam is not in ``HOT_SEAMS``.
        """
        if seam not in self._seam_state:
            raise KeyError(seam)
        return self._seam_state[seam]

    def set_seam(self, seam: str, enabled: bool) -> SeamStateChange:
        """Set the enabled state for ``seam``.

        Raises ``KeyError`` if the seam is not in ``HOT_SEAMS``.
        """
        if seam not in self._seam_state:
            raise KeyError(seam)
        previous = self._seam_state[seam]
        self._seam_state[seam] = enabled
        return SeamStateChange(seam=seam, previous_enabled=previous, new_enabled=enabled)

    def set(self, key: str, value: Any) -> ConfigChange:
        """Apply a runtime override.

        Caller MUST validate against the allowlist first (use
        ``config_schema.validate_patch``). Returns the ``ConfigChange`` for the
        caller to log as a ``config_change`` event.

        Raises ``KeyError`` if the key is not in the allowlist.
        """
        if key not in self._allowlist:
            raise KeyError(key)
        previous = self._state[key]
        self._state[key] = value
        return ConfigChange(key=key, previous_value=previous, new_value=value)

    def reset(self, key: str) -> ConfigChange | None:
        """Restore a single key to its allowlist default.

        Returns a ``ConfigChange`` if the value actually changed; ``None`` if
        it was already at default. Raises ``KeyError`` if the key is not in
        the allowlist.
        """
        if key not in self._allowlist:
            raise KeyError(key)
        default = self._allowlist[key].default
        if self._state[key] == default:
            return None
        previous = self._state[key]
        self._state[key] = default
        return ConfigChange(key=key, previous_value=previous, new_value=default)

    def reset_all(self) -> list[ConfigChange]:
        """Restore every key to its allowlist default.

        Returns the list of ``ConfigChange`` events for each key that actually
        changed.
        """
        changes: list[ConfigChange] = []
        for key in list(self._allowlist.keys()):
            c = self.reset(key)
            if c is not None:
                changes.append(c)
        return changes

    def load_from_yaml(self, path: Path) -> list[ConfigChange]:
        """Read a YAML file and merge its leaf values into state.

        Returns the ``ConfigChange``s applied. Missing file → silently no-op.
        Missing keys → keep defaults. Unknown keys → log warning, skip.
        """
        import yaml  # lazy import; the dashboard's aiohttp stack already pulls yaml.

        if not path.exists():
            return []
        with path.open("r") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            _log.warning(
                "config_store: yaml at %s does not parse to dict; ignoring", path
            )
            return []
        flat = _flatten(data)
        changes: list[ConfigChange] = []
        for dotted_key, value in flat.items():
            if dotted_key not in self._allowlist:
                _log.warning(
                    "config_store: yaml key %r not in allowlist; ignoring",
                    dotted_key,
                )
                continue
            ok, msg = validate_patch(dotted_key, value)
            if not ok:
                _log.warning(
                    "YAML load skipped invalid key %s=%r: %s", dotted_key, value, msg
                )
                continue
            c = self.set(dotted_key, value)
            changes.append(c)
        return changes


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Recursive helper: ``{'a': {'b': 1}}`` → ``{'a.b': 1}``."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out
