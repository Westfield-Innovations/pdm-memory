# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Tests for Reverse Resonance / detect_torsion."""

from __future__ import annotations

from datetime import datetime, timezone

from pdm_memory import Memory, TorsionReport
from pdm_memory.core.math import calculate_v
from pdm_memory.core.retrieval import RetrievalEngine
from pdm_memory.core.signature import SignatureRecord


class TestDetectTorsionEngine:
    def test_deadline_conflict(self) -> None:
        engine = RetrievalEngine()
        a = SignatureRecord(
            id="a1",
            compressed_fact="Project Alpha deadline is July 10",
            intent_tags=["project", "alpha", "deadline"],
            drawer_domain="projects",
            domain="insight",
            t_deadline=datetime(2026, 7, 10, tzinfo=timezone.utc),
            p_magnitude=70.0,
        )
        b = SignatureRecord(
            id="b1",
            compressed_fact="Project Alpha deadline is July 15",
            intent_tags=["project", "alpha", "deadline"],
            drawer_domain="projects",
            domain="insight",
            t_deadline=datetime(2026, 7, 15, tzinfo=timezone.utc),
            p_magnitude=70.0,
        )
        reports = engine.detect_torsion([a, b], threshold=0.5)
        assert len(reports) >= 1
        hit = reports[0]
        assert isinstance(hit, TorsionReport)
        assert hit.conflict_kind == "deadline"
        assert "Conflict found between Signature A" in hit.explanation
        assert "July" in hit.explanation or "2026-07" in hit.explanation

    def test_unrelated_no_torsion(self) -> None:
        engine = RetrievalEngine()
        a = SignatureRecord(
            id="a2",
            compressed_fact="User prefers metric units for export",
            intent_tags=["units", "metric", "export"],
            drawer_domain="prefs",
            domain="preference",
            p_magnitude=60.0,
        )
        b = SignatureRecord(
            id="b2",
            compressed_fact="Server maintenance window is Sunday night",
            intent_tags=["ops", "maintenance", "schedule"],
            drawer_domain="ops",
            domain="insight",
            p_magnitude=55.0,
        )
        assert engine.detect_torsion([a, b], threshold=0.7) == []

    def test_numeric_conflict_same_drawer(self) -> None:
        engine = RetrievalEngine()
        a = SignatureRecord(
            id="a3",
            compressed_fact="Budget cap for Q3 is 10000 dollars",
            intent_tags=["budget", "q3", "finance"],
            drawer_domain="finance",
            domain="insight",
            p_magnitude=65.0,
        )
        b = SignatureRecord(
            id="b3",
            compressed_fact="Budget cap for Q3 is 25000 dollars",
            intent_tags=["budget", "q3", "finance"],
            drawer_domain="finance",
            domain="insight",
            p_magnitude=65.0,
        )
        reports = engine.detect_torsion([a, b], threshold=0.5)
        assert len(reports) == 1
        assert reports[0].conflict_kind in {"factual", "semantic"}
        assert reports[0].torsion_score >= 0.5
        assert "10000" in reports[0].explanation
        assert "25000" in reports[0].explanation
        # Q3 must not leak as a standalone '3'
        assert "['3'" not in reports[0].explanation
        assert ", 3" not in reports[0].explanation.split("vs")[0]

    def test_weekday_attribute_clash_without_antonyms(self) -> None:
        """Friday/Saturday are values, not antonyms; shared attributes must clash."""
        engine = RetrievalEngine()
        friday = SignatureRecord(
            id="fri",
            compressed_fact="Sprint release deadline is Friday",
            intent_tags=["release", "deadline", "sprint", "friday"],
            drawer_domain="release",
            domain="reminder",
            p_magnitude=80.0,
        )
        saturday = SignatureRecord(
            id="sat",
            compressed_fact="Sprint release deadline is Saturday",
            intent_tags=["release", "deadline", "sprint", "saturday"],
            drawer_domain="release",
            domain="reminder",
            p_magnitude=80.0,
        )

        reports = engine.detect_torsion([friday, saturday])
        assert len(reports) == 1
        assert reports[0].conflict_kind == "attribute_clash"
        assert reports[0].torsion_score > 0.85
        assert "Friday" in reports[0].signature_a_text + reports[0].signature_b_text
        assert "Saturday" in reports[0].signature_a_text + reports[0].signature_b_text

    def test_status_attribute_clash_with_identical_tags(self) -> None:
        engine = RetrievalEngine()
        merging = SignatureRecord(
            id="merge-now",
            compressed_fact="Merging now without tests",
            intent_tags=["merge", "tests", "main", "testing"],
            drawer_domain="testing",
            p_magnitude=80.0,
        )
        impossible = SignatureRecord(
            id="merge-no",
            compressed_fact="Tests failing, merge impossible",
            intent_tags=["merge", "tests", "main", "testing"],
            drawer_domain="testing",
            p_magnitude=80.0,
        )

        reports = engine.detect_torsion([merging, impossible])
        assert len(reports) == 1
        assert reports[0].conflict_kind == "attribute_clash"
        assert reports[0].torsion_score > 0.85

    def test_polarity_dont_typo(self) -> None:
        """Users type 'dont' without apostrophe — must still flag polarity."""
        engine = RetrievalEngine()
        a = SignatureRecord(
            id="p1",
            compressed_fact="I love football",
            intent_tags=["football", "prefs", "sports"],
            drawer_domain="prefs",
            domain="preference",
            p_magnitude=60.0,
        )
        b = SignatureRecord(
            id="p2",
            compressed_fact="I dont love football",
            intent_tags=["football", "prefs", "sports"],
            drawer_domain="prefs",
            domain="preference",
            p_magnitude=60.0,
        )
        reports = engine.detect_torsion([a, b], threshold=0.5)
        assert len(reports) == 1
        assert reports[0].conflict_kind == "polarity"

    def test_polarity_dont_with_apostrophe(self) -> None:
        engine = RetrievalEngine()
        a = SignatureRecord(
            id="p3",
            compressed_fact="I love football",
            intent_tags=["football", "prefs"],
            drawer_domain="prefs",
            domain="preference",
            p_magnitude=55.0,
        )
        b = SignatureRecord(
            id="p4",
            compressed_fact="I don't love football",
            intent_tags=["football", "prefs"],
            drawer_domain="prefs",
            domain="preference",
            p_magnitude=55.0,
        )
        reports = engine.detect_torsion([a, b], threshold=0.5)
        assert len(reports) == 1
        assert reports[0].conflict_kind == "polarity"


class TestDetectTorsionMemory:
    def test_memory_api_and_v_penalty(self, tmp_path) -> None:
        db = str(tmp_path / "torsion.db")
        with Memory(store=db, user="u1") as mem:
            mem.save(
                "Launch date for Orion is 2026-08-01",
                tags=["orion", "launch", "date"],
                drawer="product",
                p_magnitude=70,
                deadline=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
            mem.save(
                "Launch date for Orion is 2026-09-01",
                tags=["orion", "launch", "date"],
                drawer="product",
                p_magnitude=70,
                deadline=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
            before = mem._storage.list(user="u1", limit=10)
            assert len(before) == 2
            v_before = [
                calculate_v(r.validation_prediction_correct, r.validation_prediction_total)
                for r in before
            ]

            reports = mem.detect_torsion(threshold=0.5, apply_v_penalty=True)
            assert len(reports) >= 1
            assert all(isinstance(r, TorsionReport) for r in reports)

            after = mem._storage.list(user="u1", limit=10)
            v_after = [
                calculate_v(r.validation_prediction_correct, r.validation_prediction_total)
                for r in after
            ]
            assert all(va < vb for va, vb in zip(v_after, v_before))

    def test_cluster_id_limits_search(self, tmp_path) -> None:
        db = str(tmp_path / "cluster.db")
        with Memory(store=db, user="u1") as mem:
            mem.save(
                "Deadline is July 10 for module X",
                tags=["deadline", "module", "x"],
                drawer="eng",
                deadline=datetime(2026, 7, 10, tzinfo=timezone.utc),
                metadata={"cluster_id": "c1"},
            )
            mem.save(
                "Deadline is July 15 for module X",
                tags=["deadline", "module", "x"],
                drawer="eng",
                deadline=datetime(2026, 7, 15, tzinfo=timezone.utc),
                metadata={"cluster_id": "c1"},
            )
            # Same drawer/domain-ish but different cluster — should not pair
            mem.save(
                "Deadline is July 20 for module Y",
                tags=["deadline", "module", "y"],
                drawer="eng",
                deadline=datetime(2026, 7, 20, tzinfo=timezone.utc),
                metadata={"cluster_id": "c2"},
            )
            reports = mem.detect_torsion(threshold=0.5)
            assert len(reports) == 1
            assert reports[0].cluster_key == "cluster:c1"

    def test_auto_discovery_clusters_without_cluster_id(self) -> None:
        """Cross-drawer sensors must still collide via resonance > 0.85."""
        engine = RetrievalEngine()
        a = SignatureRecord(
            id="sa",
            compressed_fact="Sensor A wellhead pressure reading is 0.85",
            intent_tags=["sensor", "pressure", "drilling", "wellhead", "reading"],
            drawer_domain="drilling_reports",
            domain="insight",
            p_magnitude=88.0,
        )
        b = SignatureRecord(
            id="sb",
            compressed_fact="Sensor B wellhead pressure reading is 0.70",
            intent_tags=["sensor", "pressure", "drilling", "wellhead", "reading"],
            drawer_domain="ops",
            domain="insight",
            p_magnitude=70.0,
        )
        assert engine._topic_similarity(a, b) > 0.85
        reports = engine.detect_torsion([a, b], threshold=0.5)
        assert len(reports) == 1
        assert reports[0].conflict_kind == "factual"
        assert reports[0].cluster_key.startswith("auto:")
        assert reports[0].torsion_score > 0.85


class TestCLIDetectTorsion:
    def test_cli_detect_torsion(self, tmp_path) -> None:
        from tests.test_cli import run_cli

        db = str(tmp_path / "cli_torsion.db")
        with Memory(store=db, user="default") as mem:
            mem.save(
                "Sprint deadline is July 10",
                tags=["sprint", "deadline", "ship"],
                drawer="sprints",
                deadline=datetime(2026, 7, 10, tzinfo=timezone.utc),
            )
            mem.save(
                "Sprint deadline is July 15",
                tags=["sprint", "deadline", "ship"],
                drawer="sprints",
                deadline=datetime(2026, 7, 15, tzinfo=timezone.utc),
            )

        output, code = run_cli(["--store", db, "detect-torsion", "--threshold", "0.5"])
        assert code == 0
        assert "torsion" in output.lower() or "Conflict found" in output
