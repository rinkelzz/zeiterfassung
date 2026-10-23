import json
import os
import tempfile


import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-passwort"})
        assert r.status_code == 200, r.text
        yield c


@pytest.fixture(scope="module")
def tim(admin):
    r = admin.post("/api/v1/users", json={
        "username": "tim", "display_name": "Tim", "password": "geheim123",
        "weekly_hours": 40, "workdays": [1, 2, 3, 4, 5], "tracking_start": "2026-01-01",
    })
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    r = admin.post(f"/api/v1/users/{uid}/tokens", json={"label": "iPhone"})
    assert r.status_code == 200
    body = r.json()
    return {"id": uid, "token": body["token"], "qr": body["qr_svg"], "setup": body["setup"]}


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def test_health(admin):
    assert admin.get("/api/v1/health").json()["status"] == "ok"


def test_unauthenticated():
    with TestClient(app) as c:
        assert c.get("/api/v1/me").status_code == 401
        assert c.post("/api/v1/clock/in", headers=bearer("ze_falsch")).status_code == 401


def test_wrong_password():
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "nein"})
        assert r.status_code == 401


def test_qr_setup(tim):
    assert tim["qr"].startswith("<?xml") or "<svg" in tim["qr"]
    assert tim["setup"]["server"] == "https://zeit.example.de"
    assert tim["setup"]["token"] == tim["token"]


def test_shortcut_clock_flow(tim):
    with TestClient(app) as c:
        h = bearer(tim["token"])
        r = c.post("/api/v1/clock/in", headers=h)  # ohne Body, wie ein schlichter Kurzbefehl
        assert r.status_code == 200 and r.json()["status"] == "in" and r.json()["changed"]
        r = c.post("/api/v1/clock/in", headers=h, json={"source": "shortcut"})
        assert r.json()["changed"] is False and "Bereits" in r.json()["message"]
        assert c.get("/api/v1/clock/status", headers=h).json()["status"] == "in"
        r = c.post("/api/v1/clock/out", headers=h, json={"source": "shortcut"})
        assert r.json()["status"] == "out" and r.json()["changed"]
        r = c.post("/api/v1/clock/out", headers=h)
        assert r.json()["changed"] is False
        r = c.post("/api/v1/clock/toggle", headers=h, json={"source": "nfc"})
        assert r.json()["status"] == "in"
        r = c.post("/api/v1/clock/toggle", headers=h, json={"source": "nfc"})
        assert r.json()["status"] == "out"


def test_employee_cannot_admin(tim):
    with TestClient(app) as c:
        h = bearer(tim["token"])
        assert c.get("/api/v1/users", headers=h).status_code == 403
        assert c.get("/api/v1/reports/user", params={"user_id": 1}, headers=h).status_code == 403
    with TestClient(app) as c:  # mit Login (nicht Stempel-Schlüssel): eigener Bericht ja, fremder nein
        assert c.post("/api/v1/auth/login", json={"username": "tim", "password": "geheim123"}).status_code == 200
        assert c.get("/api/v1/reports/user").status_code == 200
        assert c.get("/api/v1/reports/user", params={"user_id": 1}).status_code == 403
        assert c.get("/api/v1/users").status_code == 403


def test_manual_entries_and_report(admin, tim):
    uid = tim["id"]
    # Mo 05.10.2026: 08:00–12:00 und 12:30–17:00 → 8:30 Ist, Soll 8:00
    for s, e in [("2026-10-05T08:00", "2026-10-05T12:00"), ("2026-10-05T12:30", "2026-10-05T17:00")]:
        r = admin.post("/api/v1/entries", json={"user_id": uid, "start": s, "end": e})
        assert r.status_code == 201, r.text
    # 24-h-Dienst über Mitternacht: Di 07:00 → Mi 07:00
    r = admin.post("/api/v1/entries", json={"user_id": uid, "start": "2026-10-06T07:00", "end": "2026-10-07T07:00"})
    assert r.status_code == 201
    # Überschneidung wird abgelehnt
    r = admin.post("/api/v1/entries", json={"user_id": uid, "start": "2026-10-05T11:00", "end": "2026-10-05T13:00"})
    assert r.status_code == 409
    # Urlaub am Do/Fr
    r = admin.post("/api/v1/absences", json={"user_id": uid, "from_day": "2026-10-08", "to_day": "2026-10-09", "kind": "urlaub"})
    assert r.json()["days"] == 2

    rep = admin.get("/api/v1/reports/user", params={"user_id": uid, "from": "2026-10-05", "to": "2026-10-11"}).json()
    days = {d["date"]: d for d in rep["days"]}
    assert days["2026-10-05"]["worked"] == "8:30" and days["2026-10-05"]["diff"] == "0:30"
    assert days["2026-10-06"]["worked"] == "17:00"   # 07:00–24:00
    assert days["2026-10-06"]["last_out"] == "24:00" and days["2026-10-07"]["first_in"] == "00:00"
    assert days["2026-10-07"]["worked"] == "7:00"    # 00:00–07:00
    assert days["2026-10-08"]["target"] == "0:00" and days["2026-10-08"]["absence"] == "urlaub"
    assert days["2026-10-10"]["target"] == "0:00"    # Samstag
    t = rep["totals"]
    assert t["worked"] == "32:30" and t["target"] == "24:00" and t["diff"] == "8:30"
    assert t["absence_days"]["urlaub"] == 2


def test_auto_break(admin, tim):
    admin.patch("/api/v1/settings", json={"auto_break": True})
    uid = tim["id"]
    # Mo 12.10.2026: 7h am Stück → 30 min Abzug
    admin.post("/api/v1/entries", json={"user_id": uid, "start": "2026-10-12T08:00", "end": "2026-10-12T15:00"})
    rep = admin.get("/api/v1/reports/user", params={"user_id": uid, "from": "2026-10-12", "to": "2026-10-12"}).json()
    assert rep["days"][0]["worked"] == "6:30"
    # 05.10. hatte schon 30 min echte Pause → kein Abzug
    rep = admin.get("/api/v1/reports/user", params={"user_id": uid, "from": "2026-10-05", "to": "2026-10-05"}).json()
    assert rep["days"][0]["worked"] == "8:30"
    admin.patch("/api/v1/settings", json={"auto_break": False})


def test_dst_day(admin, tim):
    # 29.03.2026 Sommerzeit-Umstellung: der Tag hat nur 23 Stunden
    uid = tim["id"]
    r = admin.post("/api/v1/entries", json={"user_id": uid, "start": "2026-03-28T22:00", "end": "2026-03-30T00:00"})
    assert r.status_code == 201, r.text
    rep = admin.get("/api/v1/reports/user", params={"user_id": uid, "from": "2026-03-28", "to": "2026-03-29"}).json()
    assert rep["days"][0]["worked"] == "2:00"
    assert rep["days"][1]["worked"] == "23:00"


def test_future_entry_rejected(admin, tim):
    r = admin.post("/api/v1/entries", json={"user_id": tim["id"], "start": "2099-01-01T08:00", "end": "2099-01-01T09:00"})
    assert r.status_code == 400


def test_csv_export(admin, tim):
    r = admin.get("/api/v1/reports/export.csv", params={"user_id": tim["id"], "from": "2026-10-05", "to": "2026-10-11"})
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    assert "05.10.2026;Mo;08:00;17:00;;8:30;8:00;0:30;" in text
    assert "Urlaub" in text


def test_overview_and_audit(admin, tim):
    ov = admin.get("/api/v1/reports/overview", params={"from": "2026-10-05", "to": "2026-10-11"}).json()
    assert any(u["username"] == "tim" for u in ov["users"])
    log = admin.get("/api/v1/audit").json()
    assert any(a["action"] == "entry_created" for a in log)


def test_last_admin_protected(admin):
    r = admin.patch("/api/v1/users/1", json={"is_admin": False})
    assert r.status_code == 400


def test_token_revoke(admin, tim):
    tokens = admin.get(f"/api/v1/users/{tim['id']}/tokens").json()
    assert tokens and tokens[0]["last_used_at"]
    admin.delete(f"/api/v1/users/{tim['id']}/tokens/{tokens[0]['id']}")
    with TestClient(app) as c:
        assert c.get("/api/v1/me", headers=bearer(tim["token"])).status_code == 401
