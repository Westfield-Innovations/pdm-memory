"""PDM v1.9: the silent set is counted, never deleted; old deletes come back on upgrade."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from pdm_memory import Memory
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.sqlite_driver import SQLiteDriver

USER = "u"
NOW = datetime.now(tz=timezone.utc)


def _save(mem, text, p, **kw):
    # Fresh, inside grace: weight = P × 0.5.
    return mem.save(text, tags=["a", "b", "c"], p_magnitude=p, **kw)


def test_count_silent_counts_what_a_default_gate_misses(tmp_path):
    with Memory(store=str(tmp_path / "s.db"), user=USER) as mem:
        _save(mem, "weight ten", 20)
        _save(mem, "weight just under", 59.8)
        _save(mem, "weight thirty one", 62)
        assert mem.count_silent() == 2
        assert mem.count_silent(10) == 0
        assert mem.count_silent(31, as_of=NOW + timedelta(days=400)) == 3
        assert mem.count() == 3


def test_decay_deletes_nothing(tmp_path):
    with Memory(store=str(tmp_path / "s.db"), user=USER) as mem:
        _save(mem, "weight ten", 20)
        _save(mem, "weight forty", 80)
        with pytest.warns(DeprecationWarning, match="count_silent"):
            counts = mem.decay()
        assert counts == {"decayed": 0, "deleted": 0, "skipped": 1, "silent": 1}
        assert mem.count() == 2


def _old_store(path):
    """A store as the shipped module left it: no carried index, some rows deleted."""
    driver = SQLiteDriver(path)
    for text, p in [("quiet one the old decay deleted", 20), ("loud one a caller deleted", 80)]:
        driver.save(SignatureRecord(user=USER, compressed_fact=text, p_magnitude=p, is_deleted=True))
    driver.save(SignatureRecord(user=USER, compressed_fact="live twin", p_magnitude=20))
    driver.save(SignatureRecord(user=USER, compressed_fact="live twin", p_magnitude=20, is_deleted=True))
    driver._conn().execute("DROP INDEX idx_pdm_user_carried")
    driver._conn().commit()
    driver.close()


def test_upgrade_returns_quiet_deleted_rows(tmp_path, caplog):
    path = str(tmp_path / "old.db")
    _old_store(path)
    with caplog.at_level(logging.WARNING, logger="pdm_memory.storage.schema"):
        driver = SQLiteDriver(path)
    rows = {
        r["compressed_fact"]: r["is_deleted"]
        for r in driver._conn().execute("SELECT compressed_fact, is_deleted FROM pdm_signatures")
    }
    assert rows["quiet one the old decay deleted"] == 0
    assert rows["loud one a caller deleted"] == 1
    assert driver.count(USER) == 2  # the restored row and the live twin
    assert "restored 1 silent rows" in caplog.text
    assert "kept 2 deleted" in caplog.text
    driver.close()


def test_upgrade_runs_once(tmp_path, caplog):
    path = str(tmp_path / "old.db")
    _old_store(path)
    SQLiteDriver(path).close()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pdm_memory.storage.schema"):
        SQLiteDriver(path).close()
    assert "restored" not in caplog.text

