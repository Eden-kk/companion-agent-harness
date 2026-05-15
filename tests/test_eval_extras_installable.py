"""Tests for Task A6: [eval] optional-dependency group in pyproject.toml."""

import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-reattr]

from pathlib import Path

_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"
_REQUIREMENTS_DEV = Path(__file__).parent.parent / "requirements-dev.txt"

_EVAL_LIBS = ("datasets", "pandas", "matplotlib", "rich", "soundfile")


def _load_toml() -> dict:
    with open(_PYPROJECT, "rb") as f:
        return tomllib.load(f)


def test_pyproject_has_eval_optional_dependency_group() -> None:
    data = _load_toml()
    optional_deps = data.get("project", {}).get("optional-dependencies", {})
    assert "eval" in optional_deps, (
        "[project.optional-dependencies] is missing the 'eval' key"
    )


def test_eval_extras_contains_required_libs() -> None:
    data = _load_toml()
    eval_deps: list[str] = data["project"]["optional-dependencies"]["eval"]
    for lib in _EVAL_LIBS:
        matching = [d for d in eval_deps if d.startswith(lib + ">=")]
        assert matching, (
            f"eval extra missing '{lib}>=' entry; found: {eval_deps}"
        )


def test_requirements_dev_does_not_contain_eval_libs() -> None:
    text = _REQUIREMENTS_DEV.read_text()
    for lib in _EVAL_LIBS:
        assert lib not in text, (
            f"requirements-dev.txt must not contain '{lib}' — eval libs live behind [eval] extra only"
        )
