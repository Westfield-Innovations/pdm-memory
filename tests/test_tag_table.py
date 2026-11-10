"""PDM v1.9 change 11: tags live in a table indexed on word."""

from __future__ import annotations

from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.sqlite_driver import SQLiteDriver

USER = "u"


def _tags(driver, pid):
    rows = driver._conn().execute(
        "SELECT word FROM pdm_tags WHERE pressure_id = ? ORDER BY word", (pid,)
    ).fetchall()
    return [r["word"] for r in rows]


def _rec(text, tags, **kw):
    return SignatureRecord(user=USER, compressed_fact=text, intent_tags=tags, **kw)


def test_a_save_writes_its_tags(tmp_path):
    d = SQLiteDriver(str(tmp_path / "t.db"))
    pid = d.save(_rec("north flare", ["Flare", " north ", "flare", ""]))
    batch = d.save_batch([_rec("tank three", ["Tank"]), _rec("valve", ["valve"])])
    assert _tags(d, pid) == ["flare", "north"]
    assert _tags(d, batch[0].id) == ["tank"]
    assert _tags(d, batch[1].id) == ["valve"]
    d.close()


def test_a_tag_update_rewrites_the_rows(tmp_path):
    d = SQLiteDriver(str(tmp_path / "t.db"))
    a = d.save(_rec("a", ["old"]))
    b = d.save(_rec("b", ["old"]))
    d.update(a, user=USER, intent_tags=["new"])
    d.update_batch([(b, {"intent_tags": ["newer", "New"]})], user=USER)
    assert _tags(d, a) == ["new"]
    assert _tags(d, b) == ["new", "newer"]
    d.update(a, user="someone-else", intent_tags=["stolen"])
    assert _tags(d, a) == ["new"]
    d.close()


def test_hard_delete_drops_the_rows(tmp_path):
    d = SQLiteDriver(str(tmp_path / "t.db"))
    pid = d.save(_rec("gone", ["gone"]))
    d.hard_delete(pid, user=USER)
    assert _tags(d, pid) == []
    d.close()


def test_a_cyrillic_tag_is_found(tmp_path):
    d = SQLiteDriver(str(tmp_path / "t.db"))
    pid = d.save(_rec("Марія живе в Києві", ["Київ", "Марія"]))
    assert [r.id for r in d.list(user=USER, tag_any=["київ"])] == [pid]
    assert d.list(user="other", tag_any=["київ"]) == []
    d.close()


def test_the_tag_route_reads_the_index(tmp_path):
    d = SQLiteDriver(str(tmp_path / "t.db"))
    d.save(_rec("north flare", ["north"]))
    plan = " ".join(
        str(tuple(r))
        for r in d._conn().execute(
            "EXPLAIN QUERY PLAN SELECT id FROM pdm_signatures WHERE user = ? AND id IN "
            "(SELECT pressure_id FROM pdm_tags WHERE user = ? AND word IN (?))",
            (USER, USER, "north"),
        ).fetchall()
    )
    assert "pdm_tags" in plan
    assert "SCAN pdm_tags" not in plan
    d.close()


def test_upgrade_fills_tags_from_existing_rows(tmp_path):
    path = str(tmp_path / "old.db")
    d = SQLiteDriver(path)
    live = d.save(_rec("live", ["North", "flare"]))
    gone = d.save(_rec("gone", ["silent"], is_deleted=True))
    d._conn().execute("DROP TABLE pdm_tags")
    d._conn().commit()
    d.close()

    d = SQLiteDriver(path)
    assert _tags(d, live) == ["flare", "north"]
    assert _tags(d, gone) == ["silent"]
    d.close()
