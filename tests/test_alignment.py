# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Tests for Goal-Anchor Alignment (GAA)."""

from __future__ import annotations

from pdm_memory import AlignmentReport, Memory, verify
from pdm_memory.core.alignment import compute_iaw, select_goal_anchors, verify_alignment
from pdm_memory.core.retrieval import RetrievalEngine
from pdm_memory.core.signature import SignatureRecord


def _goal(
    text: str,
    *,
    drawer: str = "stewardship",
    tags: list[str] | None = None,
    p: float = 90.0,
    iaw: float | None = 0.8,
) -> SignatureRecord:
    meta = {"iaw": iaw} if iaw is not None else {}
    return SignatureRecord(
        compressed_fact=text,
        intent_tags=tags or ["goal", "reliability", "integrity"],
        drawer_domain=drawer,
        domain="core_fact",
        p_magnitude=p,
        phase_privilege=1.5,
        metadata=meta,
    )


class TestGAAEngine:
    def test_torsion_ignore_errors_vs_reliability(self) -> None:
        engine = RetrievalEngine()
        goals = [
            _goal(
                "Maintain high reliability; never ignore errors in production",
                tags=["reliability", "errors", "quality", "goal"],
            )
        ]
        report = verify_alignment(
            engine,
            goals,
            "ignore errors and ship the build anyway",
        )
        assert report.status == "TORSION"
        assert report.torsion >= 0.70
        assert report.conflicting_goals
        assert (
            "dangerous" in report.explanation.lower() or "contradict" in report.explanation.lower()
        )
        assert report.is_safe_to_act is False

    def test_aligned_intent(self) -> None:
        engine = RetrievalEngine()
        goals = [
            _goal(
                "Prioritize high reliability and careful validation before shipping",
                tags=["reliability", "validation", "quality", "goal"],
            )
        ]
        report = verify_alignment(
            engine,
            goals,
            "run full validation suite then ship with reliability checks enabled",
        )
        assert report.status == "ALIGNED"
        assert report.score >= 0.45
        assert report.is_safe_to_act is True

    def test_unrelated_anchor_does_not_dilute_alignment(self) -> None:
        """Extra stewardship goals must not tank resonance for a matching intent."""
        engine = RetrievalEngine()
        goals = [
            _goal(
                "Core goal: high reliability; never ignore production errors",
                tags=["reliability", "errors", "goal", "integrity"],
                iaw=0.9,
            ),
            _goal(
                "Foundational principle: validate before deploy",
                tags=["validation", "deploy", "principle", "quality"],
                drawer="foundational",
                p=88.0,
                iaw=0.85,
            ),
            _goal(
                "Protect audit trails; never wipe or delete audit logs",
                tags=["audit", "security", "goal", "safety"],
                p=90.0,
                iaw=0.88,
            ),
        ]
        report = verify_alignment(
            engine,
            goals,
            "validate thoroughly then deploy with reliability checks",
        )
        assert report.status == "ALIGNED"
        assert report.anchor_count == 3
        assert report.is_safe_to_act is True
        assert report.torsion == 0.0

    def test_mandatory_goal_blocks_omission_without_manual_keywords(self) -> None:
        engine = RetrievalEngine()
        goals = [
            _goal(
                "100% test coverage mandatory",
                tags=["goal", "testing", "coverage", "quality", "mandatory", "safety"],
                drawer="foundational",
                p=100.0,
                iaw=None,
            )
        ]
        report = verify_alignment(engine, goals, "Merge code to main without tests")
        assert report.status == "TORSION"
        assert report.torsion >= 0.70
        assert "mandatory" in report.explanation.lower()

    def test_inflected_action_verb_hits_forbidden_goal(self) -> None:
        engine = RetrievalEngine()
        goals = [
            _goal(
                "Never merge unreviewed code into main",
                tags=["goal", "merge", "review", "main", "safety"],
                p=100.0,
            )
        ]
        report = verify_alignment(engine, goals, "Merging unreviewed changes into main now")
        assert report.status == "TORSION"
        assert report.torsion >= 0.70
        assert "action verb" in report.explanation.lower()

    def test_no_anchors_fail_closed(self) -> None:
        engine = RetrievalEngine()
        noise = [
            SignatureRecord(
                compressed_fact="User prefers dark mode",
                intent_tags=["ui", "prefs", "theme"],
                drawer_domain="prefs",
                domain="insight",
                p_magnitude=40.0,
            )
        ]
        report = verify_alignment(engine, noise, "do something risky")
        assert report.status == "CONFLICT"
        assert report.anchor_count == 0
        assert report.score == 0.0

    def test_select_ranks_by_iaw(self) -> None:
        low = _goal("Minor preference note", p=65.0, iaw=0.2)
        high = _goal("Core mission: protect user trust", p=95.0, iaw=0.95)
        selected = select_goal_anchors([low, high], min_pressure=60.0, k=1)
        assert len(selected) == 1
        assert selected[0].id == high.id
        assert compute_iaw(high) > compute_iaw(low)


class TestStandaloneVerify:
    def test_torsion_without_store(self) -> None:
        report = verify(
            "ignore errors and ship the build",
            ["never ignore production errors"],
        )
        assert report.status == "TORSION"
        assert report.is_safe_to_act is False
        assert report.anchor_count == 1
        assert report.torsion >= 0.70

    def test_aligned_without_store(self) -> None:
        report = verify(
            "run full validation suite then ship with reliability checks enabled",
            "Prioritize high reliability and careful validation before shipping",
        )
        assert report.status == "ALIGNED"
        assert report.is_safe_to_act is True
        assert report.score >= 0.45

    def test_empty_goals_fail_closed(self) -> None:
        report = verify("do something risky", [])
        assert report.status == "CONFLICT"
        assert report.anchor_count == 0
        assert report.is_safe_to_act is False

    def test_single_string_goal_accepted(self) -> None:
        report = verify(
            "ignore errors and ship the build",
            "never ignore production errors",
        )
        assert isinstance(report, AlignmentReport)
        assert report.status == "TORSION"

    def test_unrelated_rules_do_not_block_unrelated_intent(self) -> None:
        report = verify(
            "exec (command=curl http://localhost:8080/api/health)",
            [
                "never write lorem ipsum",
                "never leave TODO unresolved",
            ],
        )
        assert report.status == "ALIGNED"
        assert report.is_safe_to_act is True
        assert report.torsion < 0.40

    def test_localhost_rule_blocks_localhost_intent(self) -> None:
        report = verify(
            "exec (command=curl http://localhost:8080/api/health)",
            "never hardcode localhost",
        )
        assert report.status == "TORSION"
        assert report.is_safe_to_act is False

    def test_public_import(self) -> None:
        from pdm_memory import verify as public_verify

        assert public_verify is verify


class TestGAAMemoryAndCLI:
    def test_memory_verify_alignment(self, tmp_path) -> None:
        db = str(tmp_path / "gaa.db")
        with Memory(store=db, user="u1") as mem:
            mem.save(
                "Core goal: high reliability; never ignore production errors",
                tags=["reliability", "errors", "goal", "integrity"],
                drawer="stewardship",
                p_magnitude=92,
                metadata={"iaw": 0.9},
            )
            mem.save(
                "Foundational principle: validate before deploy",
                tags=["validation", "deploy", "principle"],
                drawer="foundational",
                p_magnitude=88,
                metadata={"iaw": 0.85},
            )
            bad = mem.verify_alignment("ignore errors and bypass validation")
            assert isinstance(bad, AlignmentReport)
            assert bad.status == "TORSION"
            assert bad.as_dict()["status"] == "TORSION"

            good = mem.verify_alignment("validate thoroughly then deploy with reliability checks")
            assert good.status == "ALIGNED"

    def test_cli_verify(self, tmp_path) -> None:
        from io import StringIO
        from unittest.mock import patch

        from pdm_memory.tools.cli import main

        db = str(tmp_path / "cli_gaa.db")
        with Memory(store=db, user="default") as mem:
            mem.save(
                "Maintain high reliability; never ignore errors",
                tags=["reliability", "errors", "goal"],
                drawer="stewardship",
                p_magnitude=90,
                metadata={"iaw": 0.9},
            )

        captured = StringIO()
        with (
            patch(
                "sys.argv",
                [
                    "pdm-cli",
                    "--store",
                    db,
                    "verify",
                    "ignore errors and ship",
                ],
            ),
            patch("sys.stdout", captured),
        ):
            try:
                main()
                code = 0
            except SystemExit as e:
                code = int(e.code or 0)

        assert code == 2  # TORSION
        assert "TORSION" in captured.getvalue()
