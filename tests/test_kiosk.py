"""Kiosk-Tablet: freischalten, PIN, Namen + Anwesenheit, Kommen/Gehen, Sperren."""
import os

import pytest
from fastapi.testclient import TestClient

from app import dsm_auth
from app.main import app


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        c.patch("/api/v1/settings", json={"travel_sources": ["nfc", "kiosk"], "travel_in_minutes": 5, "travel_out_minutes": 0})
        yield c
        c.patch("/api/v1/settings", json={"travel_sources": ["nfc"], "travel_in_minutes": 0})


@pytest.fixture(scope="module")
def emp(admin):
    uid = admin.post("/api/v1/users", json={"username": "kiki", "display_name": "Kiki Kiosk", "password": "geheim123"}).json()["id"]
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "kiki", "password": "geheim123"})
        yield {"c": c, "uid": uid}


@pytest.fixture(scope="module")
def tablet(admin):
    r = admin.post("/api/v1/kiosks", json={"name": "Tablet Wache"})
    assert r.status_code == 200, r.text
    k = r.json()
    assert k["url"].startswith("https://zeit.example.de/kiosk#k=zk_") and "<svg" in k["qr_svg"]
    token = k["url"].split("#k=")[1]
    with TestClient(app) as c:
        c.headers["X-Kiosk-Token"] = token
        yield {"c": c, "id": k["id"]}


def test_page_served():
    with TestClient(app) as c:
        r = c.get("/kiosk")
        assert r.status_code == 200 and "Kiosk" in r.text and r.headers["referrer-policy"] == "no-referrer"


def test_pin_rules(emp):
    c = emp["c"]
    assert c.put("/api/v1/me/pin", json={"pin": "12"}).status_code == 400
    assert c.put("/api/v1/me/pin", json={"pin": "abcd"}).status_code == 400
    assert c.put("/api/v1/me/pin", json={"pin": "1111"}).status_code == 400
    assert c.put("/api/v1/me/pin", json={"pin": "4711"}).json()["has_pin"] is True
    assert c.get("/api/v1/me").json()["has_pin"] is True


def test_tablet_flow(admin, emp, tablet):
    k, uid = tablet["c"], emp["uid"]
    users = k.get("/api/v1/kiosk/users").json()
    assert users["kiosk"] == "Tablet Wache"
    me = [u for u in users["users"] if u["id"] == uid][0]
    assert me == {"id": uid, "name": "Kiki Kiosk", "present": False}
    assert all(u["name"] != "Administrator" for u in users["users"])        # ohne PIN nicht sichtbar
    r = k.post("/api/v1/kiosk/action", json={"user_id": uid, "pin": "4711", "action": "status"}).json()
    assert r["status"] == "out" and r["name"] == "Kiki Kiosk"
    r = k.post("/api/v1/kiosk/action", json={"user_id": uid, "pin": "4711", "action": "in"}).json()
    assert r["status"] == "in" and "Eingestempelt" in r["message"] and "5 min Wegezeit" in r["message"]
    assert [u for u in k.get("/api/v1/kiosk/users").json()["users"] if u["id"] == uid][0]["present"] is True
    r = k.post("/api/v1/kiosk/action", json={"user_id": uid, "pin": "4711", "action": "out"}).json()
    assert r["status"] == "out" and "Ausgestempelt" in r["message"]
    entries = admin.get("/api/v1/entries", params={"user_id": uid}).json()
    assert entries[-1]["source"] == "kiosk"
    log = admin.get("/api/v1/audit", params={"user_id": uid, "category": "buchungen"}).json()
    assert any(r["action"] == "clock_in" and r["text"].startswith("am Kiosk") for r in log)


def test_wrong_pin_lockout(emp, tablet):
    k, uid = tablet["c"], emp["uid"]
    dsm_auth._fails.clear()
    try:
        codes = [k.post("/api/v1/kiosk/action", json={"user_id": uid, "pin": "0000"}).status_code for _ in range(6)]
        assert codes == [403] * 5 + [429]
        # auch die richtige PIN ist jetzt gesperrt
        assert k.post("/api/v1/kiosk/action", json={"user_id": uid, "pin": "4711"}).status_code == 429
    finally:
        dsm_auth._fails.clear()


def test_no_token_and_revoke(admin, emp, tablet):
    with TestClient(app) as c:
        assert c.get("/api/v1/kiosk/users").status_code == 401
        assert c.get("/api/v1/kiosk/users", headers={"X-Kiosk-Token": "zk_falsch"}).status_code == 401
        # Kiosk-Schlüssel ist kein Login für die normale App
        assert c.get("/api/v1/me", headers={"X-Kiosk-Token": tablet["c"].headers["X-Kiosk-Token"]}).status_code == 401
    # Mitarbeiter dürfen keine Tablets freischalten
    assert emp["c"].post("/api/v1/kiosks", json={"name": "x"}).status_code == 403
    assert [x["name"] for x in admin.get("/api/v1/kiosks").json()] == ["Tablet Wache"]
    # Admin setzt PIN zurück → Name verschwindet vom Tablet
    assert admin.put(f"/api/v1/users/{emp['uid']}/pin", json={"pin": None}).json()["has_pin"] is False
    assert all(u["id"] != emp["uid"] for u in tablet["c"].get("/api/v1/kiosk/users").json()["users"])
    assert admin.delete(f"/api/v1/kiosks/{tablet['id']}").status_code == 200
    assert tablet["c"].get("/api/v1/kiosk/users").status_code == 401
