"""Tier 2 feature tests — export/import, surface, save_many, judge, sync."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest

from pdm_memory import Memory
from pdm_memory.models import TorsionReport
from pdm_memory.storage.sqlite_driver import SQLiteDriver
from pdm_memory.storage.cloud_driver import CloudDriver


@pytest.fixture
def mem(tmp_path):
    m = Memory(store=str(tmp_path / "tier2.db"), user="test_user")
    yield m
    m.close()


class TestExportImport:
    def test_export_import_roundtrip(self, tmp_path):
        src = str(tmp_path / "src.db")
        dst = str(tmp_path / "dst.db")
        export_path = tmp_path / "backup.json"

        with Memory(store=src, user="alice") as mem:
            mid = mem.save("Export me", tags=["export", "test", "unit"], p_magnitude=77)
            mem.export_json(export_path)
            assert export_path.is_file()

        with Memory(store=dst, user="alice") as mem:
            counts = mem.import_json(export_path)
            assert counts["saved"] == 1
            assert mem.count() == 1
            hit = mem.get(mid)
            assert hit is not None
            assert hit.text == "Export me"

    def test_import_skip_duplicates(self, tmp_path):
        db = str(tmp_path / "dup.db")
        export_path = tmp_path / "backup.json"

        with Memory(store=db, user="u") as mem:
            mem.save("Same", tags=["a", "b", "c"])
            mem.export_json(export_path)
            counts = mem.import_json(export_path, skip_duplicates=True)
            assert counts["saved"] == 0
            assert counts["skipped"] == 1
            assert mem.count() == 1

    def test_cli_export_import(self, tmp_path):
        from pdm_memory.tools.cli import main

        db = str(tmp_path / "cli_io.db")
        out = tmp_path / "cli_backup.json"

        with Memory(store=db, user="default") as mem:
            mem.save("CLI export fact", tags=["cli", "io", "test"])

        with patch("sys.argv", ["pdm-cli", "--store", db, "export", "--out", str(out)]):
            with patch("sys.stdout", StringIO()):
                main()

        db2 = str(tmp_path / "cli_io2.db")
        with patch("sys.argv", ["pdm-cli", "--store", db2, "import", str(out)]):
            with patch("sys.stdout", StringIO()):
                main()

        with Memory(store=db2, user="default") as mem:
            assert mem.count() == 1


class TestSaveMany:
    def test_save_many_batch(self, mem):
        items = [
            {"text": f"Fact {i}", "tags": ["batch", "test", str(i)]}
            for i in range(5)
        ]
        counts = mem.save_many(items)
        assert counts["saved"] == 5
        assert counts["errors"] == 0
        assert mem.count() == 5

    def test_save_many_dedupe(self, mem):
        mem.save("Dup", tags=["a", "b", "c"])
        counts = mem.save_many([{"text": "Dup", "tags": ["x", "y", "z"]}])
        assert counts["saved"] == 0
        assert counts["skipped"] == 1
        assert mem.count() == 1


class TestSurface:
    def test_surface_returns_combined_report(self, mem):
        mem.save(
            "Core goal: always validate before deploy",
            tags=["validation", "deploy", "goal", "safety"],
            drawer="stewardship",
            p_magnitude=90,
            metadata={"iaw": 0.9},
        )
        mem.save("User prefers dark mode", tags=["ui", "theme"], p_magnitude=60)

        report = mem.surface("run validation then deploy", k=3, search_cost=0.7)
        assert report.hits
        assert report.alignment in {"ALIGNED", "CONFLICT", "TORSION"}
        payload = report.as_dict()
        assert "hits" in payload
        assert "torsion_count" in payload
        assert "alignment" in payload


class TestTorsionJudge:
    def test_judge_adds_paraphrase_pair(self, tmp_path):
        db = str(tmp_path / "judge.db")
        with Memory(store=db, user="test_user") as mem:
            love_id = mem.save("I love football", tags=["football", "prefs"], p_magnitude=55)
            para_id = mem.save(
                "Soccer is not really my thing",
                tags=["football", "prefs"],
                p_magnitude=50,
            )

        def judge(a, b):
            ids = {a.id, b.id}
            if ids == {love_id, para_id}:
                return TorsionReport(
                    signature_a_id=a.id,
                    signature_b_id=b.id,
                    signature_a_text=a.compressed_fact,
                    signature_b_text=b.compressed_fact,
                    drawer=a.drawer_domain,
                    domain=a.domain,
                    torsion_score=0.82,
                    topic_similarity=0.9,
                    contradiction_strength=0.85,
                    explanation="AI judge: paraphrase polarity",
                    conflict_kind="semantic",
                )
            return None

        with Memory(store=db, user="test_user", torsion_judge=judge) as mem_judge:
            reports = mem_judge.detect_torsion(threshold=0.7)

        paraphrase = [
            r
            for r in reports
            if {r.signature_a_id, r.signature_b_id} == {love_id, para_id}
        ]
        assert len(paraphrase) == 1
        assert paraphrase[0].conflict_kind == "semantic"


class TestSyncAnyStorage:
    def test_sync_rejects_cloud_only_storage(self):
        from pdm_memory.storage.cloud_driver import CloudDriver
        from pdm_memory.auth.jwt_handler import JWTAuth

        auth = JWTAuth(token="fake-token-for-type-check")
        cloud = CloudDriver(auth=auth, base_url="http://localhost:8000", user="u")
        mem = Memory(storage=cloud, store="cloud", user="u", token="fake-token-for-type-check")
        with pytest.raises(RuntimeError, match="local storage"):
            mem.sync(token="t", cloud_url="http://localhost:8000")
        mem.close()

    def test_local_storage_is_not_cloud(self, tmp_path):
        local = SQLiteDriver(db_path=str(tmp_path / "local.db"))
        assert not isinstance(local, CloudDriver)
        local.close()
