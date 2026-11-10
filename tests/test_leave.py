"""Urlaubsanträge mit Freigabe durch den Vorgesetzten, Admins anlegen."""
import os

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app

PW = "geheim123"


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        yield c


def login(name):
    c = TestClient(app)
    c.__enter__()
    user = name if name.startswith("admin") else "lv_" + name
    assert c.post("/api/v1/auth/login", json={"username": user, "password": PW}).status_code == 200, name
    return c


@pytest.fixture(scope="module")
def team(admin):
    def make(name, **kw):
        r = admin.post("/api/v1/users", json={"username": "lv_" + name, "display_name": name.title(), "password": PW,
                                              "tracking_start": "2026-01-01", "email": f"{name}@example.de", **kw})
        assert r.status_code == 201, r.text
        return r.json()["id"]
    boss_n = make("leiternord")
    boss_s = make("leitersued")
    ids = {"leiternord": boss_n, "leitersued": boss_s,
           "anton": make("anton", manager_id=boss_n), "berta": make("berta", manager_id=boss_s), "carl": make("carl")}
    return ids


@pytest.fixture()
def mails(monkeypatch):
    sent = []
    from app.routers import leave
    monkeypatch.setattr(leave, "_mail", lambda conn, to, subject, body: sent.append((list(to), subject, body)))
    return sent


def test_manager_assignment(admin, team):
    users = {u["username"]: u for u in admin.get("/api/v1/users").json()}
    assert users["lv_anton"]["manager_id"] == team["leiternord"] and users["lv_carl"]["manager_id"] is None
    assert admin.patch(f"/api/v1/users/{team['anton']}", json={"manager_id": team["anton"]}).status_code == 400
    assert admin.patch(f"/api/v1/users/{team['anton']}", json={"manager_id": 99999}).status_code == 400
    assert admin.patch(f"/api/v1/users/{team['carl']}", json={"manager_id": team["leitersued"]}).status_code == 200
    assert admin.patch(f"/api/v1/users/{team['carl']}", json={"manager_id": None}).status_code == 200
    assert login("anton").get("/api/v1/me").json()["manager_id"] == team["leiternord"]


def test_create_admin_in_ui(admin):
    r = admin.post("/api/v1/users", json={"username": "admin2", "display_name": "Zweit-Admin", "password": PW, "is_admin": True})
    assert r.status_code == 201 and r.json()["is_admin"] is True
    c = login("admin2")
    assert c.get("/api/v1/users").status_code == 200
    # Admin-Rechte wieder entziehen
    assert admin.patch(f"/api/v1/users/{r.json()['id']}", json={"is_admin": False}).status_code == 200
    assert c.get("/api/v1/users").status_code == 403


def test_request_and_approve_by_manager(admin, team, mails):
    anton, nord, sued = login("anton"), login("leiternord"), login("leitersued")
    # Mo 07.12. bis Fr 11.12.2026 = 5 Arbeitstage
    r = anton.post("/api/v1/leave", json={"kind": "urlaub", "from_day": "2026-12-07", "to_day": "2026-12-13", "note": "Familie"})
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["days"] == 5 and req["status"] == "offen" and req["manager_name"] == "Leiternord"
    # Mail an den Filialleiter, nicht an die Admins
    to, subject, body = mails[-1]
    assert to == ["leiternord@example.de"] and "Urlaubsantrag von Anton" in subject and "Resturlaub 2026 danach: 25" in body
    # Zähler
    assert nord.get("/api/v1/leave/count").json() == {"decide": 1, "mine": 0, "can_decide": True, "can_calendar": True}
    assert sued.get("/api/v1/leave/count").json()["decide"] == 0
    assert anton.get("/api/v1/leave/count").json()["can_decide"] is False
    # falscher Filialleiter und der Mitarbeiter selbst dürfen nicht
    assert sued.post(f"/api/v1/leave/{req['id']}/approve", json={}).status_code == 403
    assert anton.post(f"/api/v1/leave/{req['id']}/approve", json={}).status_code == 403
    assert sued.get("/api/v1/leave", params={"scope": "decide"}).json() == []
    todo = nord.get("/api/v1/leave", params={"scope": "decide"}).json()
    assert todo[0]["can_decide"] and todo[0]["vacation_remaining"] == 30
    # Filialleiter ohne Admin-Rechte kommt sonst an nichts
    assert nord.get("/api/v1/users").status_code == 403
    r = nord.post(f"/api/v1/leave/{req['id']}/approve", json={"note": "Schönen Urlaub"})
    assert r.status_code == 200 and r.json()["status"] == "genehmigt"
    to, subject, body = mails[-1]
    assert to == ["anton@example.de"] and "genehmigt" in subject and "Schönen Urlaub" in body
    with connect() as conn:
        days = [r[0] for r in conn.execute("SELECT day FROM absences WHERE user_id = ? AND kind = 'urlaub' ORDER BY day",
                                           (team["anton"],))]
    assert days == ["2026-12-07", "2026-12-08", "2026-12-09", "2026-12-10", "2026-12-11"]   # Wochenende nicht
    assert nord.post(f"/api/v1/leave/{req['id']}/reject", json={"note": "x"}).status_code == 409
    # schon eingetragen → neuer Antrag im selben Zeitraum geht nicht
    assert anton.post("/api/v1/leave", json={"from_day": "2026-12-09"}).status_code == 409


def test_reject_withdraw_and_rules(admin, team, mails):
    berta, sued = login("berta"), login("leitersued")
    assert berta.post("/api/v1/leave", json={"from_day": "2026-12-12", "to_day": "2026-12-13"}).status_code == 400  # nur Wochenende
    assert berta.post("/api/v1/leave", json={"from_day": "2026-12-14", "to_day": "2026-12-15", "half_day": True}).status_code == 400
    assert berta.post("/api/v1/leave", json={"kind": "krank", "from_day": "2026-12-14"}).status_code == 400
    r = berta.post("/api/v1/leave", json={"kind": "gleittag", "from_day": "2026-12-14", "half_day": True}).json()
    assert r["days"] == 0.5 and r["kind_label"] == "Gleittag"
    assert berta.post("/api/v1/leave", json={"from_day": "2026-12-14"}).status_code == 409        # überschneidet sich
    assert sued.post(f"/api/v1/leave/{r['id']}/reject", json={}).status_code == 400               # Begründung fehlt
    assert sued.post(f"/api/v1/leave/{r['id']}/reject", json={"note": "Inventur"}).json()["status"] == "abgelehnt"
    assert "abgelehnt" in mails[-1][1] and "Inventur" in mails[-1][2]
    # zurückziehen nur eigene und nur offene
    r2 = berta.post("/api/v1/leave", json={"from_day": "2026-12-16"}).json()
    assert sued.post(f"/api/v1/leave/{r2['id']}/withdraw").status_code == 403
    assert berta.post(f"/api/v1/leave/{r2['id']}/withdraw").json()["status"] == "zurueckgezogen"
    assert berta.post(f"/api/v1/leave/{r2['id']}/withdraw").status_code == 409
    mine = berta.get("/api/v1/leave").json()
    assert {x["status"] for x in mine} == {"abgelehnt", "zurueckgezogen"}


def test_without_manager_admins_decide(admin, team, mails):
    carl = login("carl")
    r = carl.post("/api/v1/leave", json={"from_day": "2026-12-17"}).json()
    to = mails[-1][0]
    assert "leiternord@example.de" not in to                                 # keine Vorgesetzten → Admins
    assert admin.get("/api/v1/leave/count").json()["decide"] >= 1
    assert admin.post(f"/api/v1/leave/{r['id']}/approve", json={}).json()["status"] == "genehmigt"
    log = admin.get("/api/v1/audit", params={"user_id": team["carl"], "category": "antraege"}).json()
    assert {e["action"] for e in log} >= {"leave_requested", "leave_approved"}


def test_closed_month_blocks(admin, team):
    anton = login("anton")
    admin.post("/api/v1/closings", json={"user_id": team["anton"], "month": "2026-01"})
    r = anton.post("/api/v1/leave", json={"from_day": "2026-01-14"})
    assert r.status_code in (409, 423), r.text


def test_berufsschule_and_elternzeit(admin):
    uid = admin.post("/api/v1/users", json={"username": "lv_azubi", "display_name": "Azubi", "password": PW,
                                            "tracking_start": "2026-01-01"}).json()["id"]
    assert admin.get("/api/v1/settings").json()["absence_kinds"]["berufsschule"] == "Berufsschule"
    # Mo 02.03. Berufsschule, 09.–13.03. Elternzeit, Di 03.03. normal gearbeitet
    assert admin.post("/api/v1/absences", json={"user_id": uid, "from_day": "2026-03-02", "kind": "berufsschule"}).status_code == 201
    r = admin.post("/api/v1/absences", json={"user_id": uid, "from_day": "2026-03-09", "to_day": "2026-03-15", "kind": "elternzeit"})
    assert r.json()["days"] == 5
    admin.post("/api/v1/entries", json={"user_id": uid, "start": "2026-03-03T08:00", "end": "2026-03-03T16:30"})
    rep = admin.get("/api/v1/reports/user", params={"user_id": uid, "from": "2026-03-02", "to": "2026-03-15"}).json()
    days = {d["date"]: d for d in rep["days"]}
    assert days["2026-03-02"]["absence_label"] == "Berufsschule" and days["2026-03-02"]["target"] == "0:00"
    assert days["2026-03-02"]["diff"] == "0:00"
    assert all(days[f"2026-03-{d:02d}"]["target"] == "0:00" for d in range(9, 14))
    t = rep["totals"]
    assert t["absence_days"]["berufsschule"] == 1 and t["absence_days"]["elternzeit"] == 5
    assert t["vacation_days"] == 0
    # Urlaub wird nicht verbraucht
    with connect() as conn:
        from app.reports import vacation_account
        user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        assert vacation_account(conn, user, 2026)["taken"] == 0
