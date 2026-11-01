"""Stunden und Pausenabzug je Wochentag, Mitarbeiter endgültig löschen."""
import os

import pytest
from fastapi.testclient import TestClient

from app import backup
from app.db import connect
from app.main import app

WEEK = ("2026-10-05", "2026-10-11")  # Mo–So


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        old = c.get("/api/v1/settings").json()["break_mode"]
        c.patch("/api/v1/settings", json={"break_mode": "arbzg"})
        yield c
        c.patch("/api/v1/settings", json={"break_mode": old})


def make_user(admin, name):
    r = admin.post("/api/v1/users", json={"username": name, "display_name": name.title(), "password": "geheim123",
                                          "weekly_hours": 40, "tracking_start": "2026-10-01"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def book(admin, uid, day, start, end):
    r = admin.post("/api/v1/entries", json={"user_id": uid, "start": f"{day}T{start}", "end": f"{day}T{end}"})
    assert r.status_code in (200, 201), r.text


def test_hours_and_breaks_per_weekday(admin):
    uid = make_user(admin, "tagi")
    r = admin.post(f"/api/v1/users/{uid}/schedules", json={
        "valid_from": "2026-10-01",
        "day_hours": [0, 8.5, 8.5, 8.5, 8.5, 4, 0],
        "day_breaks": ["", "", "45", "", "", "none", ""],
    })
    assert r.status_code == 201, r.text
    cur = [x for x in r.json() if x["valid_from"] == "2026-10-01"][0]
    assert cur["weekly_hours"] == 38 and cur["workdays"] == [2, 3, 4, 5, 6]
    assert cur["day_hours"] == [0, 8.5, 8.5, 8.5, 8.5, 4, 0] and cur["day_breaks"][5] == "none"

    book(admin, uid, "2026-10-06", "08:00", "17:00")   # Di: 9 h − 30 min (ArbZG) = 8:30 → ±0
    book(admin, uid, "2026-10-07", "08:00", "17:00")   # Mi: 9 h − 45 min (fest)  = 8:15 → −0:15
    book(admin, uid, "2026-10-10", "08:00", "12:30")   # Sa: 4:30, keine Pause    → +0:30
    rep = admin.get("/api/v1/reports/user", params={"user_id": uid, "from": WEEK[0], "to": WEEK[1]}).json()
    days = {d["date"]: d for d in rep["days"]}
    assert days["2026-10-05"]["target"] == "0:00"                                  # Montag frei
    assert days["2026-10-06"]["target"] == "8:30" and days["2026-10-06"]["diff"] == "0:00"
    assert days["2026-10-07"]["break_deducted_s"] == 45 * 60 and days["2026-10-07"]["diff"] == "-0:15"
    assert days["2026-10-10"]["target"] == "4:00" and days["2026-10-10"]["break_deducted_s"] == 0
    assert days["2026-10-10"]["diff"] == "0:30"
    # Do + Fr ohne Buchung: je −8:30
    assert rep["totals"]["target"] == "38:00"
    assert rep["totals"]["diff"] == "-16:45"

    # PDF/Excel-Kopf
    from app.report_files import build_data
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        data = build_data(conn, user, "2026-10")
    assert data["schedule"] == "38 Std./Woche (Di–Fr 8,5 h, Sa 4 h)"
    assert "Mi fest 45 min" in data["break_text"] and "Sa kein Abzug" in data["break_text"]

    # Liste der Mitarbeiter kennt die Tagesstunden
    me = [u for u in admin.get("/api/v1/users").json() if u["id"] == uid][0]
    assert me["day_hours"] == [0, 8.5, 8.5, 8.5, 8.5, 4, 0] and me["weekly_hours"] == 38

    # Wochenstunden ohne Tage ändern → wieder gleichmäßig verteilt, Pausen je Tag bleiben
    assert admin.patch(f"/api/v1/users/{uid}", json={"weekly_hours": 40, "workdays": [1, 2, 3, 4, 5]}).status_code == 200
    cur = [x for x in admin.get(f"/api/v1/users/{uid}/schedules").json() if x["current"]][0]
    assert cur["day_hours"] is None and cur["per_day_hours"][0] == 8 and cur["day_breaks"][5] == "none"


def test_schedule_validation(admin):
    uid = make_user(admin, "falschi")
    base = {"valid_from": "2026-10-01"}
    assert admin.post(f"/api/v1/users/{uid}/schedules", json={**base, "day_hours": [8, 8, 8]}).status_code == 422
    assert admin.post(f"/api/v1/users/{uid}/schedules", json={**base, "day_hours": [0] * 7}).status_code == 422
    assert admin.post(f"/api/v1/users/{uid}/schedules", json={**base, "day_hours": [25, 0, 0, 0, 0, 0, 0]}).status_code == 422
    assert admin.post(f"/api/v1/users/{uid}/schedules", json={**base, "weekly_hours": 40,
                                                              "day_breaks": ["x"] * 7}).status_code == 422
    assert admin.post(f"/api/v1/users/{uid}/schedules", json={**base, "weekly_hours": 40,
                                                              "day_breaks": ["500"] + [""] * 6}).status_code == 422
    # nur Pausen je Tag, Stunden gleichmäßig
    r = admin.post(f"/api/v1/users/{uid}/schedules", json={**base, "weekly_hours": 40, "workdays": [1, 2, 3, 4, 5],
                                                           "day_breaks": ["arbzg", "", "", "", "none", "", ""]})
    assert r.status_code == 201, r.text
    book(admin, uid, "2026-10-09", "08:00", "17:00")   # Fr: keine Pause → 9 h
    rep = admin.get("/api/v1/reports/user", params={"user_id": uid, "from": "2026-10-09", "to": "2026-10-09"}).json()
    assert rep["days"][0]["worked"] == "9:00" and rep["days"][0]["diff"] == "1:00"


def test_purge_user(admin):
    uid = make_user(admin, "weg")
    book(admin, uid, "2026-10-06", "08:00", "12:00")
    admin.post("/api/v1/absences", json={"user_id": uid, "from": "2026-10-07", "to": "2026-10-07", "kind": "urlaub"})
    # zweiter Admin, der eine Buchung eines anderen bearbeitet hat (updated_by → darf das Löschen nicht blockieren)
    boss = admin.post("/api/v1/users", json={"username": "chefin2", "display_name": "Chefin", "password": "geheim123",
                                             "is_admin": True}).json()["id"]
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "chefin2", "password": "geheim123"})
        eid = admin.get("/api/v1/entries", params={"user_id": uid, "from": "2026-10-01", "to": "2026-10-31"}).json()[0]["id"]
        assert c.patch(f"/api/v1/entries/{eid}", json={"note": "geprüft"}).status_code == 200
        # niemand löscht sich selbst
        assert c.post(f"/api/v1/users/{boss}/purge", json={"confirm": "chefin2"}).status_code == 400

    assert admin.post(f"/api/v1/users/{uid}/purge", json={"confirm": "falsch"}).status_code == 400
    r = admin.post(f"/api/v1/users/{uid}/purge", json={"confirm": "weg"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entries"] == 1 and body["safety_backup"].endswith("_vor-loeschen.db.gz")
    assert (backup.local_dir() / body["safety_backup"]).exists()
    with connect() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM entries WHERE user_id = ?", (uid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM absences WHERE user_id = ?", (uid,)).fetchone()[0] == 0
    assert admin.get(f"/api/v1/users/{uid}/schedules").status_code == 404

    # Protokoll behält den Namen
    log = admin.get("/api/v1/audit", params={"user_id": uid}).json()
    purged = [e for e in log if e["action"] == "user_purged"][0]
    assert purged["subject"] == "Weg (gelöscht)" and "1 Buchungen" in purged["text"]

    # der zweite Admin lässt sich löschen, obwohl er Buchungen anderer bearbeitet hat
    assert admin.post(f"/api/v1/users/{boss}/purge", json={"confirm": "chefin2"}).status_code == 200
    with connect() as conn:
        assert conn.execute("SELECT updated_by FROM entries WHERE updated_by = ?", (boss,)).fetchone() is None


def test_last_admin_and_rights(admin):
    with connect() as conn:
        me = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
    assert admin.post(f"/api/v1/users/{me}/purge", json={"confirm": "admin"}).status_code == 400
    uid = make_user(admin, "normalo")
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "normalo", "password": "geheim123"})
        assert c.post(f"/api/v1/users/{me}/purge", json={"confirm": "admin"}).status_code == 403
        assert c.post(f"/api/v1/users/{uid}/schedules", json={"valid_from": "2026-10-01", "weekly_hours": 1}).status_code == 403
