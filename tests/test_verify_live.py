# © 2026 Westfield Innovations LLC. Patent Pending.
"""
Live GO/NO-GO for standalone GAA.

These are not engine unit tests. They call the public `verify()` door the way
a stranger would, wrap a real side-effecting tool, and emit a receipt for
every attempt: action, rule, gate status, whether the tool actually ran,
resulting state, timestamp, runtime version.

A screenshot is not proof. A receipt is.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from pdm_memory import __version__, verify

STRICT_RULE = "never ignore production errors"
RELAXED_RULE = "Prioritize high reliability and careful validation before shipping"
VIOLATING_INTENT = "ignore errors and ship the build"
ALIGNED_INTENT = "run full validation suite then ship with reliability checks enabled"


class ShipTool:
    """Stand-in for a real agent tool. Mutates state only if it actually runs."""

    def __init__(self) -> None:
        self.shipments: list[str] = []

    def ship(self, payload: str = "build-42") -> str:
        self.shipments.append(payload)
        return f"shipped:{payload}"


def guarded_ship(intent: str, goals, tool: ShipTool, payload: str = "build-42") -> dict:
    before = list(tool.shipments)
    report = verify(intent, goals)
    executed = False
    result = None
    if report.is_safe_to_act:
        result = tool.ship(payload)
        executed = True
    receipt = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "runtime": "pdm-memory",
        "version": __version__,
        "proposed_action": intent,
        "governing_rule": goals if isinstance(goals, str) else list(goals),
        "gate_status": report.status,
        "is_safe_to_act": report.is_safe_to_act,
        "explanation": report.explanation,
        "tool_executed": executed,
        "tool_result": result,
        "resulting_state": {"shipments": list(tool.shipments)},
        "state_before": {"shipments": before},
    }
    print(json.dumps(receipt, indent=2))
    return receipt


class TestLiveStandaloneGate:
    def test_violating_action_is_stopped_not_logged(self) -> None:
        tool = ShipTool()
        receipt = guarded_ship(VIOLATING_INTENT, STRICT_RULE, tool)

        assert receipt["gate_status"] == "TORSION"
        assert receipt["tool_executed"] is False
        assert receipt["tool_result"] is None
        assert receipt["resulting_state"]["shipments"] == []
        assert receipt["state_before"]["shipments"] == []
        assert tool.shipments == []

    def test_aligned_action_actually_runs_the_tool(self) -> None:
        tool = ShipTool()
        receipt = guarded_ship(ALIGNED_INTENT, RELAXED_RULE, tool, payload="build-42")

        assert receipt["gate_status"] == "ALIGNED"
        assert receipt["tool_executed"] is True
        assert receipt["tool_result"] == "shipped:build-42"
        assert receipt["resulting_state"]["shipments"] == ["build-42"]
        assert tool.shipments == ["build-42"]

    def test_block_then_relax_then_matching_action_goes_through(self) -> None:
        tool = ShipTool()

        blocked = guarded_ship(VIOLATING_INTENT, STRICT_RULE, tool)
        assert blocked["gate_status"] == "TORSION"
        assert blocked["tool_executed"] is False
        assert tool.shipments == []

        permitted = guarded_ship(ALIGNED_INTENT, RELAXED_RULE, tool, payload="build-99")
        assert permitted["gate_status"] == "ALIGNED"
        assert permitted["tool_executed"] is True
        assert tool.shipments == ["build-99"]
        assert permitted["state_before"]["shipments"] == []

    def test_empty_rules_fail_closed_and_stop_the_tool(self) -> None:
        tool = ShipTool()
        receipt = guarded_ship(VIOLATING_INTENT, [], tool)

        assert receipt["gate_status"] == "CONFLICT"
        assert receipt["tool_executed"] is False
        assert tool.shipments == []

    def test_verify_creates_no_store_file(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        verify(VIOLATING_INTENT, STRICT_RULE)
        leftover = [p.name for p in tmp_path.iterdir()]
        assert leftover == [], leftover


class TestLiveCliAndExample:
    def test_cli_goal_blocks_without_creating_a_store(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pdm_memory.tools.cli",
                "verify",
                VIOLATING_INTENT,
                "--goal",
                STRICT_RULE,
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        print(result.stdout)
        assert result.returncode == 2
        assert "TORSION" in result.stdout
        assert not (tmp_path / "pdm_memory.db").exists()
        assert list(tmp_path.iterdir()) == []

    def test_cli_goal_permits_aligned_intent(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pdm_memory.tools.cli",
                "verify",
                ALIGNED_INTENT,
                "--goal",
                RELAXED_RULE,
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        print(result.stdout)
        assert result.returncode == 0
        assert "ALIGNED" in result.stdout
        assert not (tmp_path / "pdm_memory.db").exists()

    def test_bundled_example_blocks_then_permits(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "pdm_memory.examples.standalone_guard"],
            capture_output=True,
            text=True,
            check=False,
        )
        print(result.stdout)
        assert result.returncode == 0, result.stderr
        assert "status     = TORSION" in result.stdout
        assert "status     = ALIGNED" in result.stdout
        assert "blocked: GAA blocked ACT: TORSION" in result.stdout
        assert "permitted: 'shipped'" in result.stdout
