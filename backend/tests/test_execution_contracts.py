"""Contract tests for checkpointed plans consumed by prompts 01-03."""

from pathlib import Path

from scripts.validate_execution_contracts import validate_plan, validate_plans


def _plan(status: str = "Active", *, checked: bool = False) -> str:
    box = "x" if checked else " "
    return f"""# Maintenance plan

Status: {status}

## Goal
## Agreed scope
## Out of scope
## Decisions and assumptions
## Commands that must work
## Acceptance criteria
## Implementation checkpoints

### P1 — First checkpoint

Dependencies: none

- [{box}] Implement and test the agreed behavior

Human review required before application: none.

## Reference map

| Checkpoint | Governing sources | What to extract |
| --- | --- | --- |
| P1 | `AGENTS.md` | contributor rules |

## API, data and security impact
## Validation plan
## Review and delivery
"""


def test_valid_active_and_complete_plans() -> None:
    assert validate_plan(Path("active.md"), _plan()) == []
    assert validate_plan(Path("complete.md"), _plan("Complete", checked=True)) == []


def test_active_plan_requires_executable_checkpoint_contract() -> None:
    invalid = _plan().replace("Dependencies: none\n", "").replace("| P1 |", "| P2 |")
    errors = validate_plan(Path("invalid.md"), invalid)
    assert any("Dependencies" in error for error in errors)
    assert any("reference map" in error for error in errors)


def test_active_plan_rejects_unresolved_placeholder() -> None:
    errors = validate_plan(Path("invalid.md"), _plan() + "\nTBD\n")
    assert any("unresolved" in error for error in errors)


def test_complete_plan_cannot_have_unchecked_items() -> None:
    errors = validate_plan(Path("invalid.md"), _plan("Complete"))
    assert any("still has unchecked" in error for error in errors)


def test_only_one_plan_may_be_active(tmp_path: Path) -> None:
    (tmp_path / "one.md").write_text(_plan(), encoding="utf-8")
    (tmp_path / "two.md").write_text(_plan(), encoding="utf-8")
    errors = validate_plans(tmp_path)
    assert any("multiple Active plans" in error for error in errors)


def test_historical_descriptive_status_is_not_opted_in() -> None:
    text = "# Old plan\n\nStatus: Complete (implemented previously)\n"
    assert validate_plan(Path("old.md"), text) == []
