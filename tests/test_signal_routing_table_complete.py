"""Contract test: every PolicyInputs field appears in docs/signal-source-routing-table.md."""
import dataclasses
import pathlib
import re

from companion_harness.schemas import PolicyInputs

_TABLE_PATH = pathlib.Path(__file__).parent.parent / "docs" / "signal-source-routing-table.md"


def _parse_table_field_names() -> set[str]:
    text = _TABLE_PATH.read_text()
    names: set[str] = set()
    in_table = False
    for line in text.splitlines():
        if "| Field name |" in line:
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            # skip separator rows (e.g. |---|---|)
            if re.fullmatch(r"[|\s\-]+", line):
                continue
            # column 0 is the field name cell: | `field_name` | ...
            cell = line.split("|")[1].strip()
            m = re.search(r"`(\w+)`", cell)
            if m:
                names.add(m.group(1))
    return names


def test_all_policy_inputs_fields_in_routing_table() -> None:
    dataclass_fields = {f.name for f in dataclasses.fields(PolicyInputs)}
    table_fields = _parse_table_field_names()
    missing = dataclass_fields - table_fields
    assert not missing, (
        f"PolicyInputs fields missing from signal-source-routing-table.md: {sorted(missing)}"
    )
