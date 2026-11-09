"""Härtung nach dem zweiten Sicherheitsreview: Proxy-Schlüssel 0600, Sitzungsdauer, Sicherungen ohne Sitzungen."""
import gzip
import os
import sqlite3
import stat

import pytest
from fastapi.testclient import TestClient

from app import dsm_auth
from app.config import Settings, settings
from app.main import app


def test_session_default_is_seven_days(monkeypatch):
    monkeypatch.delenv("ZE_SESSION_DAYS", raising=False)
    assert Settings().session_days == 7


def test_proxy_secret_only_for_package_user(tmp_path, monkeypatch):
    ui = tmp_path / "ui"
    ui.mkdir()
    monkeypatch.setattr(settings, "ui_dir", str(ui))
    dsm_auth.publish_proxy_files()
    assert stat.S_IMODE((tmp_path / ".proxy_secret").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / ".port").stat().st_mode) == 0o644


def test_backup_has_no_sessions_and_restore_keeps_login(tmp_path):
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        name = c.post("/api/v1/backups").json()["name"]
        from app import backup
        raw = tmp_path / "x.db"
        raw.write_bytes(gzip.decompress((backup.local_dir() / name).read_bytes()))
        db = sqlite3.connect(str(raw))
        try:
            assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        finally:
            db.close()
        assert c.post(f"/api/v1/backups/{name}/restore", json={"confirm": name}).status_code == 200
        # nach dem Wiederherstellen ist man weiterhin angemeldet
        assert c.get("/api/v1/me").status_code == 200
