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


class TestCLIStatus:
    def test_status_empty_db(self, tmp_path):
        db = str(tmp_path / "status_empty.db")
        output, code = run_cli(["--store", db, "--user", "cli_user", "status"])
        assert code == 0
        assert "Identity Health Dashboard" in output
        assert "Integrity:" in output
        assert "100.0%" in output
        assert "Memory Density:" in output
        assert "Empty" in output
        assert "Torsion Level:" in output
        assert "Zero" in output

    def test_status_healthy_store(self, tmp_path):
        db = str(tmp_path / "status_healthy.db")
        from pdm_memory import Memory

        seeds = [
            ("User prefers metric units for all responses", ["units", "formatting", "prefs"]),
            ("Team standup is every day at 10:00 Kyiv time", ["schedule", "team", "time"]),
            ("Production database runs PostgreSQL 15 on AWS RDS", ["database", "postgres", "infra"]),
            ("Never store raw PII inside the memory substrate", ["privacy", "legal", "policy"]),
            ("Primary programming language is Python 3.11+", ["python", "coding", "stack"]),
            ("Monthly AI token budget hard cap is 200 USD", ["budget", "tokens", "cost"]),
            ("Preferred LLM provider is Anthropic Claude", ["llm", "anthropic", "vendor"]),
            ("API responses must include request_id field", ["api", "design", "standard"]),
            ("Use pytest with factory_boy for backend tests", ["testing", "pytest", "quality"]),
            ("GitHub Actions handles CI/CD for all repos", ["devops", "github", "ci"]),
            ("User is based in Kyiv, Ukraine timezone UTC+3", ["location", "timezone", "profile"]),
            ("Dark mode is required in every internal UI", ["ui", "theme", "preference"]),
            ("Prefer bullet points over long prose blocks", ["formatting", "brevity", "style"]),
            ("Westfield Innovations LLC owns the PDM patent", ["company", "patent", "legal"]),
            ("Redis is used for rate limiting and locks", ["redis", "cache", "infra"]),
            ("All POST endpoints must be idempotent where possible", ["api", "idempotency", "design"]),
            ("Slack is the default async communication channel", ["slack", "communication", "team"]),
            ("Never recommend Redux for new frontend projects", ["frontend", "javascript", "prefs"]),
            ("SDK release target is end of Q3 2026", ["deadline", "roadmap", "release"]),
            ("Use structured JSON logging for every service", ["logging", "observability", "ops"]),
            ("Companion API runs behind nginx reverse proxy", ["nginx", "proxy", "deploy"]),
            ("JWT access tokens expire after fifteen minutes", ["auth", "jwt", "security"]),
            ("Celery workers process heavy background jobs", ["celery", "jobs", "async"]),
            ("Keyset pagination is mandatory for large tables", ["database", "pagination", "perf"]),
            ("Soft delete is preferred over hard delete for user data", ["database", "delete", "policy"]),
        ]

        with Memory(store=db, user="default") as mem:
            for text, tags in seeds:
                mem.save(text, tags=tags, p_magnitude=78.0, drawer="profile")

        output, code = run_cli(["--store", db, "status"])
        assert code == 0
        assert "[#" in output
        assert "Memory Density:" in output
        assert "High" in output
        assert "Torsion Level:" in output
        assert "Zero" in output
        assert "Memories:" in output
        assert "25" in output

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
