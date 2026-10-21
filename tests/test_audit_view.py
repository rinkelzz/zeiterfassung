"""Änderungsprotokoll: betroffener Mitarbeiter, Filter, verständliche Texte, Nachladen, CSV."""
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        yield c


@pytest.fixture(scope="module")
def uid(admin):
    uid = admin.post("/api/v1/users", json={"username": "protokoll", "display_name": "Paula Protokoll",
                                           "password": "geheim123", "tracking_start": "2026-01-01"}).json()["id"]
    eid = admin.post("/api/v1/entries", json={"user_id": uid, "start": "2026-07-14T07:00", "end": "2026-07-14T15:00"}).json()["id"]
    admin.patch(f"/api/v1/entries/{eid}", json={"end": "2026-07-14T16:00"})
    admin.delete(f"/api/v1/entries/{eid}")
    admin.post("/api/v1/absences", json={"user_id": uid, "from_day": "2026-07-15", "kind": "krank"})
    return uid


def test_filter_by_employee(admin, uid):
    rows = admin.get("/api/v1/audit", params={"user_id": uid}).json()
    actions = [r["action"] for r in rows]
    # auch die gelöschte Buchung ist ihr zugeordnet
    for a in ("user_created", "entry_created", "entry_updated", "entry_deleted", "absence_set"):
        assert a in actions, a
    assert all(r["subject_user_id"] == uid or r["actor_id"] == uid for r in rows)
    upd = [r for r in rows if r["action"] == "entry_updated"][0]
    assert upd["label"] == "Buchung geändert" and upd["subject"] == "Paula Protokoll" and upd["actor"] == "Administrator"
    assert "14.07.2026 07:00" in upd["text"] and "+00:00" not in upd["text"]      # Ortszeit statt UTC
    deleted = [r for r in rows if r["action"] == "entry_deleted"][0]
    assert "Paula Protokoll" in deleted["text"]                                    # user:N → Name


def test_category_search_date_paging(admin, uid):
    cat = admin.get("/api/v1/audit", params={"user_id": uid, "category": "buchungen"}).json()
    assert cat and all(r["category"] == "buchungen" for r in cat)
    assert admin.get("/api/v1/audit", params={"category": "quatsch"}).status_code == 400
    hit = admin.get("/api/v1/audit", params={"q": "14.07.2026"}).json()
    assert any(r["action"] == "entry_created" and r["subject_user_id"] == uid for r in hit)
    assert admin.get("/api/v1/audit", params={"from": "2099-01-01"}).json() == []
    first = admin.get("/api/v1/audit", params={"limit": 2}).json()
    nxt = admin.get("/api/v1/audit", params={"limit": 2, "before_id": first[-1]["id"]}).json()
    assert len(first) == 2 and all(r["id"] < first[-1]["id"] for r in nxt)
    assert "buchungen" in admin.get("/api/v1/audit/categories").json()


def test_csv_and_rights(admin, uid):
    r = admin.get("/api/v1/audit/export.csv", params={"user_id": uid})
    assert r.status_code == 200 and r.text.startswith("﻿Zeitpunkt;Wer;Betrifft;Bereich;Aktion;Details")
    assert "Buchung gelöscht" in r.text and "Paula Protokoll" in r.text
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "protokoll", "password": "geheim123"})
        assert c.get("/api/v1/audit").status_code == 403
        assert c.get("/api/v1/audit/export.csv").status_code == 403


def test_migration_backfills_subject(tmp_path):
    import sqlite3
    from app import db as dbmod
    from app.config import settings
    saved = (settings.data_dir, settings.db_path)
    settings.data_dir, settings.db_path = tmp_path, tmp_path / "zeiterfassung.db"
    try:
        dbmod.init_db()
        conn = dbmod.connect()
        conn.execute("INSERT INTO audit_log(ts, actor_id, action, target, details) VALUES "
                     "('2026-01-01T00:00:00+00:00', 1, 'entry_deleted', 'entry:999', 'user:7 a – b')")
        conn.execute("UPDATE audit_log SET subject_user_id = NULL")
        conn.execute("PRAGMA user_version = 9")
        conn.close()
        dbmod.init_db()
        conn = dbmod.connect()
        row = conn.execute("SELECT subject_user_id FROM audit_log WHERE target = 'entry:999'").fetchone()
        assert row[0] == 7
        conn.close()
    finally:
        settings.data_dir, settings.db_path = saved
