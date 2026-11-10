"""PDM v1.9 changes 7 and 8 at the store: the link table and the validation status."""

from __future__ import annotations

from datetime import datetime, timezone

from pdm_memory.core.signature import LinkRow, SignatureRecord
from pdm_memory.storage.sqlite_driver import SQLiteDriver

USER = "u"
NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)


def _link(a, b, kind="support", status="UNVALIDATED", v=0.5):
    return LinkRow(a, b, kind, status, v, NOW)


def test_links_read_both_ways(tmp_path):
    d = SQLiteDriver(str(tmp_path / "l.db"))
    assert d.write_links(USER, [_link("c", "a"), _link("b", "a"), _link("b", "a", "coaccess")]) == 3
    assert sorted(lnk.pressure_id for lnk in d.links(USER, ["a"], side="in", kind="support")) == [
        "b",
        "c",
    ]
    out = d.links(USER, ["b"], side="out")
    assert sorted(lnk.kind for lnk in out) == ["coaccess", "support"]
    assert out[0].written_at == NOW
    assert d.links("other", ["a"], side="in") == []
    d.close()


def test_the_first_write_of_a_link_stands(tmp_path):
    d = SQLiteDriver(str(tmp_path / "l.db"))
    d.write_links(USER, [_link("b", "a", v=0.5)])
    assert d.write_links(USER, [_link("b", "a", status="VALIDATED", v=0.9)]) == 0
    (link,) = d.links(USER, ["b"], side="out")
    assert (link.status_at_write, link.v_at_write) == ("UNVALIDATED", 0.5)
    d.close()


def test_both_sides_are_index_reads(tmp_path):
    d = SQLiteDriver(str(tmp_path / "l.db"))
    for column, index in (("pressure_id", "idx_pdm_links_out"), ("neighbor_id", "idx_pdm_links_in")):
        plan = " ".join(
            str(tuple(r))
            for r in d._conn().execute(
                f"EXPLAIN QUERY PLAN SELECT * FROM pdm_links WHERE user = ? AND {column} IN (?)",
                (USER, "a"),
            )
        )
        assert index in plan
    d.close()


def test_hard_delete_drops_its_links(tmp_path):
    d = SQLiteDriver(str(tmp_path / "l.db"))
    a = d.save(SignatureRecord(user=USER, compressed_fact="a"))
    d.write_links(USER, [_link("b", a), _link(a, "c")])
    d.hard_delete(a, user=USER)
    assert d.links(USER, [a], side="in") == d.links(USER, [a], side="out") == []
    d.close()


def test_the_status_is_stored_and_old_rows_are_unvalidated(tmp_path):
    path = str(tmp_path / "l.db")
    d = SQLiteDriver(path)
    pid = d.save(SignatureRecord(user=USER, compressed_fact="v", validation_status="VALIDATED"))
    assert d.get(pid, user=USER).validation_status == "VALIDATED"
    d.update(pid, user=USER, validation_status="REOPENED")
    assert d.get(pid, user=USER).validation_status == "REOPENED"
    d.close()

    old = str(tmp_path / "old.db")
    d = SQLiteDriver(old)
    pid = d.save(SignatureRecord(user=USER, compressed_fact="old"))
    d._conn().execute("ALTER TABLE pdm_signatures DROP COLUMN validation_status")
    d._conn().commit()
    d.close()
    d = SQLiteDriver(old)
    assert d.get(pid, user=USER).validation_status == "UNVALIDATED"
    d.close()
