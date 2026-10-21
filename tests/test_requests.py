"""Korrekturanträge: stellen, genehmigen (wird übernommen), ablehnen, zurückziehen, Mails, Sperren."""
import os
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from test_reports_mail import FakeSMTP


@pytest.fixture(scope="module")
def smtp():
    srv = FakeSMTP()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture(scope="module")
def admin(smtp):
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        c.patch("/api/v1/settings", json={
            "smtp_host": "127.0.0.1", "smtp_port": smtp.server_address[1], "smtp_security": "none",
            "smtp_user": "zeit@example.de", "smtp_password": "richtig", "smtp_from": "zeit@example.de", "break_mode": "none"})
        c.patch("/api/v1/users/1", json={"email": "chef@example.de"})
        yield c
        c.patch("/api/v1/users/1", json={"email": None})


@pytest.fixture(scope="module")
def emp(admin):
    r = admin.post("/api/v1/users", json={"username": "antrag", "display_name": "Anton Antrag", "password": "geheim123",
                                         "tracking_start": "2026-05-01", "email": "anton@example.de"})
    uid = r.json()["id"]
    eid = admin.post("/api/v1/entries", json={"user_id": uid, "start": "2026-05-04T07:00", "end": "2026-05-04T15:00"}).json()["id"]
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "antrag", "password": "geheim123"}).status_code == 200
        yield {"c": c, "uid": uid, "eid": eid}


def day(admin, uid, d):
    return admin.get("/api/v1/reports/user", params={"user_id": uid, "from": d, "to": d}).json()["days"][0]


def test_add_and_approve(admin, emp, smtp):
    c, uid = emp["c"], emp["uid"]
    smtp.inbox.clear()
    r = c.post("/api/v1/requests", json={"kind": "add", "start": "2026-05-05T07:10", "end": "2026-05-05T15:40",
                                         "reason": "Stempeln vergessen"})
    assert r.status_code == 201, r.text
    rq = r.json()
    assert rq["status"] == "offen" and rq["new_text"] == "05.05.2026 07:10 – 15:40" and rq["day"] == "2026-05-05"
    assert [m["msg"]["To"] for m in smtp.inbox] == ["chef@example.de"]
    assert "Anton Antrag" in smtp.inbox[0]["msg"]["Subject"]
    assert day(admin, uid, "2026-05-05")["worked"] == "0:00"            # noch nichts übernommen
    assert c.get("/api/v1/requests/count").json() == {"mine": 1, "all": None}
    assert admin.get("/api/v1/requests/count").json()["all"] == 1
    r = admin.post(f"/api/v1/requests/{rq['id']}/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "genehmigt"
    assert day(admin, uid, "2026-05-05")["worked"] == "8:30"
    assert smtp.inbox[-1]["msg"]["To"] == "anton@example.de" and "genehmigt" in smtp.inbox[-1]["msg"]["Subject"]
    e = admin.get("/api/v1/entries", params={"user_id": uid, "from": "2026-05-05", "to": "2026-05-05"}).json()[0]
    assert e["note"] == "Korrekturantrag: Stempeln vergessen" and e["edited"]
    assert admin.post(f"/api/v1/requests/{rq['id']}/approve", json={}).status_code == 409   # nicht doppelt


def test_change_reject_withdraw(admin, emp, smtp):
    c, uid, eid = emp["c"], emp["uid"], emp["eid"]
    r = c.post("/api/v1/requests", json={"kind": "change", "entry_id": eid, "start": "2026-05-04T07:00",
                                         "end": "2026-05-04T16:30", "reason": "Einsatz länger"})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert r.json()["old_text"] == "04.05.2026 07:00 – 15:00"
    # zweiter Antrag zur selben Buchung geht nicht
    assert c.post("/api/v1/requests", json={"kind": "delete", "entry_id": eid, "reason": "doch weg"}).status_code == 409
    assert admin.post(f"/api/v1/requests/{rid}/reject", json={"note": ""}).status_code == 400
    r = admin.post(f"/api/v1/requests/{rid}/reject", json={"note": "Einsatz endete laut Bericht 15:00"})
    assert r.status_code == 200 and r.json()["status"] == "abgelehnt"
    assert "abgelehnt" in smtp.inbox[-1]["msg"]["Subject"]
    assert day(admin, uid, "2026-05-04")["last_out"] == "15:00"        # unverändert
    # zurückziehen
    rid = c.post("/api/v1/requests", json={"kind": "delete", "entry_id": eid, "reason": "doppelt gebucht"}).json()["id"]
    assert c.post(f"/api/v1/requests/{rid}/withdraw").json()["status"] == "zurueckgezogen"
    mine = {x["id"]: x["status"] for x in c.get("/api/v1/requests").json()}
    assert mine[rid] == "zurueckgezogen"


def test_change_approve_and_stale(admin, emp):
    c, uid, eid = emp["c"], emp["uid"], emp["eid"]
    rid = c.post("/api/v1/requests", json={"kind": "change", "entry_id": eid, "start": "2026-05-04T06:45",
                                           "end": "2026-05-04T15:00", "reason": "früher angefangen"}).json()["id"]
    assert admin.post(f"/api/v1/requests/{rid}/approve", json={}).status_code == 200
    assert day(admin, uid, "2026-05-04")["first_in"] == "06:45"
    # Buchung wird zwischen Antrag und Entscheidung vom Admin geändert → Genehmigen verweigert
    rid = c.post("/api/v1/requests", json={"kind": "delete", "entry_id": eid, "reason": "falscher Tag"}).json()["id"]
    admin.patch(f"/api/v1/entries/{eid}", json={"end": "2026-05-04T14:00"})
    r = admin.post(f"/api/v1/requests/{rid}/approve", json={})
    assert r.status_code == 409 and "inzwischen geändert" in r.json()["detail"]
    admin.post(f"/api/v1/requests/{rid}/reject", json={"note": "bitte neu beantragen"})


def test_validation_and_rights(admin, emp):
    c, uid, eid = emp["c"], emp["uid"], emp["eid"]
    assert c.post("/api/v1/requests", json={"kind": "add", "start": "2026-05-06T15:00", "end": "2026-05-06T07:00",
                                            "reason": "xyz"}).status_code == 400
    assert c.post("/api/v1/requests", json={"kind": "add", "start": "2099-05-06T07:00", "end": "2099-05-06T15:00",
                                            "reason": "Zukunft"}).status_code == 400
    assert c.post("/api/v1/requests", json={"kind": "add", "start": "2026-05-04T10:00", "end": "2026-05-04T12:00",
                                            "reason": "überlappt"}).status_code == 409
    assert c.post("/api/v1/requests", json={"kind": "add", "start": "2026-05-06T07:00", "end": "2026-05-06T15:00",
                                            "reason": "x"}).status_code == 422          # Begründung zu kurz
    # fremde Buchung
    other = admin.post("/api/v1/entries", json={"user_id": 1, "start": "2026-05-04T07:00", "end": "2026-05-04T08:00"}).json()["id"]
    assert c.post("/api/v1/requests", json={"kind": "delete", "entry_id": other, "reason": "nicht meins"}).status_code == 404
    admin.delete(f"/api/v1/entries/{other}")
    # Mitarbeiter darf nicht entscheiden und keine fremden Anträge sehen
    rid = c.post("/api/v1/requests", json={"kind": "add", "start": "2026-05-07T07:00", "end": "2026-05-07T09:00",
                                           "reason": "Fortbildung"}).json()["id"]
    assert c.post(f"/api/v1/requests/{rid}/approve", json={}).status_code == 403
    assert c.get("/api/v1/requests", params={"all_users": True}).status_code == 403
    assert c.get("/api/v1/requests", params={"user_id": 1}).status_code == 403
    assert admin.post(f"/api/v1/requests/{rid}/withdraw").status_code == 403       # nur der Antragsteller
    assert any(r["id"] == rid for r in admin.get("/api/v1/requests", params={"all_users": True, "status": "offen"}).json())
    admin.post(f"/api/v1/requests/{rid}/reject", json={"note": "Test"})


def test_closed_month(admin, emp):
    c, uid = emp["c"], emp["uid"]
    rid = c.post("/api/v1/requests", json={"kind": "add", "start": "2026-05-08T07:00", "end": "2026-05-08T09:00",
                                           "reason": "Nachtrag"}).json()["id"]
    assert admin.post("/api/v1/closings", json={"user_id": uid, "month": "2026-05"}).status_code == 200
    r = admin.post(f"/api/v1/requests/{rid}/approve", json={})
    assert r.status_code == 409 and "abgeschlossen" in r.json()["detail"]
    r = c.post("/api/v1/requests", json={"kind": "add", "start": "2026-05-11T07:00", "end": "2026-05-11T09:00", "reason": "noch einer"})
    assert r.status_code == 409
    admin.post(f"/api/v1/requests/{rid}/reject", json={"note": "Monat ist abgeschlossen"})
    admin.post("/api/v1/closings/reopen", json={"user_id": uid, "month": "2026-05", "reason": "Test aufräumen"})
