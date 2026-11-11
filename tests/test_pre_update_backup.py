"""Vor einem Datenbank-Update (ältere Schema-Version) legt die App automatisch eine Sicherung an."""
import gzip
import sqlite3

import pytest

from app import db
from app.config import settings


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", tmp_path / "zeiterfassung.db")
    monkeypatch.setattr(settings, "initial_admin_password", "startpasswort123")
    return tmp_path


def _version(path, v=None):
    c = sqlite3.connect(str(path))
    try:
        if v is not None:
            c.execute(f"PRAGMA user_version = {v}")
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def test_backup_only_when_schema_is_older(fresh):
    db.init_db()                                     # Neuinstallation: keine Sicherung
    assert not list((fresh / "backups").glob("*vor-update*")) if (fresh / "backups").exists() else True
    db.init_db()                                     # gleicher Stand: keine Sicherung
    assert not (fresh / "backups").exists() or not list((fresh / "backups").glob("*vor-update*"))
    _version(settings.db_path, db.SCHEMA_VERSION - 1)
    db.init_db()                                     # Update: Sicherung + Protokoll
    files = list((fresh / "backups").glob("zeiterfassung_*_vor-update.db.gz"))
    assert len(files) == 1
    raw = gzip.decompress(files[0].read_bytes())
    snap = fresh / "snap.db"
    snap.write_bytes(raw)
    assert _version(snap) == db.SCHEMA_VERSION - 1          # Stand vor dem Update
    assert _version(settings.db_path) == db.SCHEMA_VERSION
    with db.connect() as conn:
        row = conn.execute("SELECT details FROM audit_log WHERE action = 'backup_created' ORDER BY id DESC").fetchone()
    assert "vor dem Update" in row[0]


def test_keeps_last_three(fresh):
    db.init_db()
    folder = fresh / "backups"
    folder.mkdir(exist_ok=True)
    for i in range(1, 6):
        (folder / f"zeiterfassung_2026-01-0{i}_0100_vor-update.db.gz").write_bytes(b"x")
    _version(settings.db_path, db.SCHEMA_VERSION - 1)
    db.init_db()
    left = sorted(f.name for f in folder.glob("*vor-update*"))
    assert len(left) == 3 and "zeiterfassung_2026-01-05_0100_vor-update.db.gz" in left


def test_failed_backup_stops_update(fresh, monkeypatch):
    db.init_db()
    _version(settings.db_path, db.SCHEMA_VERSION - 1)
    from app import backup

    def boom(_):
        raise OSError("Datenträger voll")
    monkeypatch.setattr(backup, "_snapshot", boom)
    with pytest.raises(RuntimeError, match="Update abgebrochen"):
        db.init_db()
    assert _version(settings.db_path) == db.SCHEMA_VERSION - 1     # nichts verändert
