"""Tests for CLI commands via argparse."""

from unittest.mock import patch
from io import StringIO


def run_cli(args: list) -> tuple[str, int]:
    """Run CLI and capture stdout output."""
    from pdm_memory.tools.cli import main
    captured = StringIO()
    with patch("sys.argv", ["pdm-cli"] + args):
        with patch("sys.stdout", captured):
            try:
                main()
                return captured.getvalue(), 0
            except SystemExit as e:
                return captured.getvalue(), int(e.code or 0)


class TestCLIStats:
    def test_stats_empty_db(self, tmp_path):
        db = str(tmp_path / "cli_test.db")
        output, code = run_cli(["--store", db, "--user", "cli_user", "stats"])
        assert code == 0
        assert "Total memories" in output

    def test_stats_with_data(self, tmp_path):
        db = str(tmp_path / "cli_test.db")
        from pdm_memory import Memory
        with Memory(store=db, user="cli_user") as mem:
            mem.save("CLI test memory", tags=["cli", "test", "unit"], p_magnitude=70)

        output, code = run_cli(["--store", db, "--user", "cli_user", "stats"])
        assert code == 0
        assert "1" in output  # 1 memory


class TestCLIListMemories:
    def test_list_empty(self, tmp_path):
        db = str(tmp_path / "empty.db")
        output, code = run_cli(["--store", db, "list-memories"])
        assert code == 0
        assert "No memories found" in output

    def test_list_with_data(self, tmp_path):
        db = str(tmp_path / "list_test.db")
        from pdm_memory import Memory
        with Memory(store=db, user="default") as mem:
            mem.save("List test memory", tags=["list", "test", "cli"], p_magnitude=65)

        output, code = run_cli(["--store", db, "list-memories"])
        assert code == 0
        assert "List test memory" in output

    def test_list_min_pressure(self, tmp_path):
        db = str(tmp_path / "filter_test.db")
        from pdm_memory import Memory
        with Memory(store=db, user="default") as mem:
            mem.save("Low pressure", tags=["low", "pressure", "test"], p_magnitude=20)
            mem.save("High pressure", tags=["high", "pressure", "test"], p_magnitude=80)

        output, code = run_cli(["--store", db, "list-memories", "--min-pressure", "60"])
        assert "High pressure" in output
        assert "Low pressure" not in output


class TestCLIDecay:
    def test_decay_dry_run(self, tmp_path):
        db = str(tmp_path / "decay_test.db")
        from pdm_memory import Memory
        with Memory(store=db, user="default") as mem:
            mem.save("Test memory", tags=["a", "b", "c"], p_magnitude=60)

        output, code = run_cli(["--store", db, "decay", "--dry-run"])
        assert code == 0
        assert "DRY RUN" in output


class TestCLIDrawers:
    def test_drawers_empty(self, tmp_path):
        db = str(tmp_path / "drawers_test.db")
        output, code = run_cli(["--store", db, "drawers"])
        assert code == 0
        assert "No drawers found" in output

    def test_drawers_with_data(self, tmp_path):
        db = str(tmp_path / "drawers_test.db")
        from pdm_memory import Memory
        with Memory(store=db, user="default") as mem:
            mem.save("Science fact", tags=["science", "fact", "core"], drawer="science", p_magnitude=70)

        output, code = run_cli(["--store", db, "drawers"])
        assert "science" in output


class TestCLIExplain:
    def test_explain(self, tmp_path):
        db = str(tmp_path / "explain_test.db")
        from pdm_memory import Memory
        with Memory(store=db, user="default") as mem:
            mid = mem.save("Explainable memory", tags=["explain", "test", "unit"], p_magnitude=75)

        output, code = run_cli(["--store", db, "explain", mid])
        assert code == 0
        assert "P_effective" in output

    def test_explain_missing_id(self, tmp_path):
        db = str(tmp_path / "explain_test2.db")
        output, code = run_cli(["--store", db, "explain", "nonexistent-id-123456789abc"])
        assert code == 1
