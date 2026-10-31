"""Filialen am Kiosk-Tablet und Kontostände nach der PIN."""
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
def setup(admin):
    ids = {}
    for name, pin in (("nora", "2468"), ("sven", "1357"), ("bea", "8642")):
        r = admin.post("/api/v1/users", json={"username": name, "display_name": name.title(), "password": "geheim123",
                                              "tracking_start": "2026-01-01", "carryover_minutes": 90})
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]
        assert admin.put(f"/api/v1/users/{ids[name]}/pin", json={"pin": pin}).status_code == 200
    nord = [b for b in admin.post("/api/v1/branches", json={"name": "Filiale Nord", "user_ids": [ids["nora"]]}).json()
            if b["name"] == "Filiale Nord"][0]
    sued = [b for b in admin.post("/api/v1/branches", json={"name": "Filiale Süd"}).json() if b["name"] == "Filiale Süd"][0]
    # bea arbeitet in beiden
    admin.patch(f"/api/v1/branches/{nord['id']}", json={"user_ids": [ids["nora"], ids["bea"]]})
    admin.patch(f"/api/v1/branches/{sued['id']}", json={"user_ids": [ids["sven"], ids["bea"]]})
    k = admin.post("/api/v1/kiosks", json={"name": "Tablet Nord", "branch_id": nord["id"]}).json()
    return {"ids": ids, "nord": nord["id"], "sued": sued["id"], "kiosk": k}


def tablet(k):
    c = TestClient(app)
    c.headers["X-Kiosk-Token"] = k["url"].split("#k=")[1]
    return c


def test_branch_crud(admin, setup):
    bs = {b["name"]: b for b in admin.get("/api/v1/branches").json()}
    assert sorted(bs["Filiale Nord"]["user_ids"]) == sorted([setup["ids"]["nora"], setup["ids"]["bea"]])
    assert admin.post("/api/v1/branches", json={"name": "Filiale Nord"}).status_code == 409
    assert admin.post("/api/v1/branches", json={"name": ""}).status_code in (400, 422)
    assert admin.patch("/api/v1/branches/9999", json={"name": "x"}).status_code == 404
    assert admin.post("/api/v1/branches", json={"name": "x", "user_ids": [99999]}).status_code == 400
    ks = {k["name"]: k for k in admin.get("/api/v1/kiosks").json()}
    assert ks["Tablet Nord"]["branch"] == "Filiale Nord"


def test_tablet_shows_only_its_branch(admin, setup):
    ids = setup["ids"]
    with tablet(setup["kiosk"]) as k:
        r = k.get("/api/v1/kiosk/users").json()
        assert r["branch"] == "Filiale Nord"
        names = {u["name"] for u in r["users"]}
        assert {"Nora", "Bea"} <= names and "Sven" not in names
        # Sven gehört nur zu Süd → am Nord-Tablet nicht stempeln, auch mit richtiger PIN
        assert k.post("/api/v1/kiosk/action", json={"user_id": ids["sven"], "pin": "1357"}).status_code == 403
        assert k.post("/api/v1/kiosk/action", json={"user_id": ids["bea"], "pin": "8642"}).status_code == 200
        # Tablet auf „alle“ umstellen → Sven erscheint
        admin.patch(f"/api/v1/kiosks/{setup['kiosk']['id']}", json={"branch_id": None})
        names = {u["name"] for u in k.get("/api/v1/kiosk/users").json()["users"]}
        assert "Sven" in names
        admin.patch(f"/api/v1/kiosks/{setup['kiosk']['id']}", json={"branch_id": setup["nord"]})
        assert admin.patch(f"/api/v1/kiosks/{setup['kiosk']['id']}", json={"branch_id": 9999}).status_code == 400


def test_users_without_branch_everywhere(admin, setup):
    r = admin.post("/api/v1/users", json={"username": "ohnefil", "display_name": "Ohne Filiale", "password": "geheim123"})
    uid = r.json()["id"]
    admin.put(f"/api/v1/users/{uid}/pin", json={"pin": "9753"})
    with tablet(setup["kiosk"]) as k:
        assert "Ohne Filiale" in {u["name"] for u in k.get("/api/v1/kiosk/users").json()["users"]}


def test_accounts_after_pin(admin, setup):
    ids = setup["ids"]
    with tablet(setup["kiosk"]) as k:
        r = k.post("/api/v1/kiosk/action", json={"user_id": ids["nora"], "pin": "2468", "action": "status"}).json()
        a = r["accounts"]
        assert set(a) >= {"today_worked", "today_target", "today_diff", "gleitzeit", "gleitzeit_s", "vacation_remaining"}
        assert a["ueberstunden"] is None                    # nutzt keine Ü-Tage → nicht anzeigen
        assert a["vacation_remaining"] == 30
        r = k.post("/api/v1/kiosk/action", json={"user_id": ids["nora"], "pin": "2468", "action": "in"}).json()
        assert r["status"] == "in" and "accounts" in r
        r = k.post("/api/v1/kiosk/action", json={"user_id": ids["nora"], "pin": "2468", "action": "out"}).json()
        assert r["status"] == "out" and r["accounts"]["today_worked"] == "0:00"
        # falsche PIN verrät nichts
        bad = k.post("/api/v1/kiosk/action", json={"user_id": ids["nora"], "pin": "0000", "action": "status"})
        assert bad.status_code == 403 and "accounts" not in bad.text
    # mit Überstunden-Übertrag wird das Ü-Konto gezeigt
    admin.patch(f"/api/v1/users/{ids['bea']}", json={"carryover_overtime_minutes": 120})
    with tablet(setup["kiosk"]) as k:
        a = k.post("/api/v1/kiosk/action", json={"user_id": ids["bea"], "pin": "8642"}).json()["accounts"]
        assert a["ueberstunden"] == "2:00"


def test_delete_branch_and_rights(admin, setup):
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "nora", "password": "geheim123"})
        assert c.get("/api/v1/branches").status_code == 403
        assert c.post("/api/v1/branches", json={"name": "x"}).status_code == 403
    admin.delete(f"/api/v1/branches/{setup['nord']}")
    ks = {k["name"]: k for k in admin.get("/api/v1/kiosks").json()}
    assert ks["Tablet Nord"]["branch_id"] is None
    with tablet(setup["kiosk"]) as k:  # Tablet zeigt wieder alle
        assert "Sven" in {u["name"] for u in k.get("/api/v1/kiosk/users").json()["users"]}
