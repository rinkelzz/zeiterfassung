"""Datensicherung: erstellen, Ordner, Aufräumen, Zeitplan, Download, Wiederherstellen."""
import gzip
import os
import sqlite3
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import backup
from app.db import connect
from app.main import app
from app.timeutil import tz


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        yield c


def test_create_local_and_shared(admin, tmp_path):
    shared = tmp_path / "share" / "zeiterfassung"
    r = admin.patch("/api/v1/backups/settings", json={"dir": str(shared), "keep_days": 30})
    assert r.status_code == 200 and r.json()["dir"] == str(shared) and shared.is_dir()
    r = admin.post("/api/v1/backups").json()
    assert r["shared_error"] is None and r["name"].endswith("_manuell.db.gz")
    assert (backup.local_dir() / r["name"]).exists() and (shared / r["name"]).exists()
    # Inhalt ist eine gültige SQLite-Datenbank mit den Nutzern
    raw = tmp_path / "x.db"
    raw.write_bytes(gzip.decompress((shared / r["name"]).read_bytes()))
    db = sqlite3.connect(str(raw))
    assert db.execute("SELECT username FROM users WHERE id = 1").fetchone()[0] == "admin"
    db.close()
    listed = {b["name"]: b for b in admin.get("/api/v1/backups").json()["backups"]}
    assert set(listed[r["name"]]["where"]) == {"lokal", "ordner"}


def test_dir_validation(admin, tmp_path):
    assert admin.patch("/api/v1/backups/settings", json={"dir": "relativ/pfad"}).status_code == 400
    locked = tmp_path / "gesperrt"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        if os.geteuid() != 0:  # root darf überall schreiben
            r = admin.patch("/api/v1/backups/settings", json={"dir": str(locked / "sub")})
            assert r.status_code == 400 and "System-interner Benutzer" in r.json()["detail"]
    finally:
        locked.chmod(0o700)
    assert admin.patch("/api/v1/backups/settings", json={"dir": ""}).json()["dir"] == ""


def test_prune(tmp_path):
    for d in ("2026-01-01", "2026-09-30", "2026-10-20", "2026-10-21"):
        (tmp_path / f"zeiterfassung_{d}_0200.db.gz").write_bytes(b"x")
    (tmp_path / "zeiterfassung_2026-01-02_0900_vor-wiederherstellung.db.gz").write_bytes(b"x")
    (tmp_path / "fremd.txt").write_text("bleibt")
    backup._prune(tmp_path, keep_count=2)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert "zeiterfassung_2026-01-01_0200.db.gz" not in left and "zeiterfassung_2026-09-30_0200.db.gz" not in left
    assert "zeiterfassung_2026-01-02_0900_vor-wiederherstellung.db.gz" in left and "fremd.txt" in left


def test_due():
    with connect() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = 'backup_last_auto'")
        assert not backup.due(conn, datetime(2026, 10, 21, 1, 30, tzinfo=tz()))   # vor 2 Uhr
        assert backup.due(conn, datetime(2026, 10, 21, 2, 5, tzinfo=tz()))
        conn.execute("INSERT INTO app_settings(key, value) VALUES ('backup_last_auto', '2026-10-21T02:05+02:00') "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
        assert not backup.due(conn, datetime(2026, 10, 21, 23, 0, tzinfo=tz()))  # heute schon
        assert backup.due(conn, datetime(2026, 10, 22, 3, 0, tzinfo=tz()))


def test_restore(admin):
    r = admin.post("/api/v1/users", json={"username": "vorher", "display_name": "Vorher", "password": "geheim123"})
    assert r.status_code == 201
    snap = admin.post("/api/v1/backups").json()["name"]
    admin.post("/api/v1/users", json={"username": "nachher", "display_name": "Nachher", "password": "geheim123"})
    assert admin.post(f"/api/v1/backups/{snap}/restore", json={"confirm": "falsch"}).status_code == 400
    r = admin.post(f"/api/v1/backups/{snap}/restore", json={"confirm": snap})
    assert r.status_code == 200, r.text
    safety = r.json()["safety_backup"]
    assert safety.endswith("_vor-wiederherstellung.db.gz")
    with connect() as conn:
        names = {row[0] for row in conn.execute("SELECT username FROM users")}
        assert "vorher" in names and "nachher" not in names
        assert conn.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0] == "backup_restored"
    # die Sicherheitskopie enthält „nachher“ noch → zurück geht es auch
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]})
        assert c.post(f"/api/v1/backups/{safety}/restore", json={"confirm": safety}).status_code == 200
    with connect() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE username = 'nachher'").fetchone()


def test_download_and_rights(admin):
    name = admin.post("/api/v1/backups").json()["name"]
    r = admin.get(f"/api/v1/backups/{name}/download")
    assert r.status_code == 200 and gzip.decompress(r.content)[:15] == b"SQLite format 3"
    assert admin.get("/api/v1/backups/..%2Fzeiterfassung.db/download").status_code == 404
    assert admin.post("/api/v1/backups/fremd.db.gz/restore", json={"confirm": "fremd.db.gz"}).status_code == 400
    with TestClient(app) as c:
        admin.post("/api/v1/users", json={"username": "bert", "display_name": "Bert", "password": "geheim123"})
        c.post("/api/v1/auth/login", json={"username": "bert", "password": "geheim123"})
        assert c.get("/api/v1/backups").status_code == 403
        assert c.get(f"/api/v1/backups/{name}/download").status_code == 403
