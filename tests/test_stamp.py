"""Stempel-Link: Gerät koppeln (HttpOnly-Cookie), stempeln per Aufkleber-Adresse ohne Geheimnis."""
import os

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]})
        yield c


@pytest.fixture(scope="module")
def worker(admin):
    r = admin.post("/api/v1/users", json={"username": "st_link", "display_name": "Lina Link", "password": "passwort123",
                                          "weekly_hours": 40, "workdays": [1, 2, 3, 4, 5], "tracking_start": "2026-01-01"})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    tok = admin.post(f"/api/v1/users/{uid}/tokens", json={"label": "Stempel-Link"}).json()["token"]
    return {"id": uid, "token": tok}


def _phone():
    return TestClient(app)


def test_page_served_without_referrer():
    with _phone() as p:
        r = p.get("/stempeln")
    assert r.status_code == 200 and "stamp/pair" in r.text
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"


def test_not_paired_and_bad_token(worker):
    with _phone() as p:
        assert p.get("/api/v1/stamp/status").status_code == 401
        assert p.post("/api/v1/stamp/toggle?src=nfc").json()["detail"] == "not_paired"
        assert p.post("/api/v1/stamp/pair", json={"token": "ze_falsch_falsch_falsch"}).status_code == 401


def test_pair_sets_httponly_strict_cookie_and_stamps(worker):
    with _phone() as p:
        r = p.post("/api/v1/stamp/pair", json={"token": worker["token"]})
        assert r.status_code == 200 and r.json()["user"] == "Lina Link"
        sc = r.headers["set-cookie"].lower()
        assert "ze_stamp=" in sc and "httponly" in sc and "samesite=strict" in sc and "path=/api/v1/stamp" in sc
        # Aufkleber-Adresse ohne Schlüssel: Kommen per NFC
        r = p.post("/api/v1/stamp/in?src=nfc").json()
        assert r["changed"] and r["status"] == "in"
        with connect() as conn:
            src = conn.execute("SELECT source FROM entries WHERE user_id = ? ORDER BY id DESC", (worker["id"],)).fetchone()[0]
        assert src == "nfc"
        # nochmal Kommen ändert nichts
        assert p.post("/api/v1/stamp/in?src=nfc").json()["changed"] is False
        # Umschalten direkt danach: Schutz gegen Doppel-Scan
        r = p.post("/api/v1/stamp/toggle?src=nfc").json()
        assert r["changed"] is False and "Gerade erst gestempelt" in r["message"] and r["status"] == "in"
        # Gehen geht immer (eindeutige Absicht)
        r = p.post("/api/v1/stamp/out").json()
        assert r["changed"] and r["status"] == "out"
        with connect() as conn:
            src = conn.execute("SELECT source FROM entries WHERE user_id = ? ORDER BY id DESC", (worker["id"],)).fetchone()[0]
        assert src == "nfc"  # Quelle bleibt die vom Kommen
        # Abmelden: Cookie weg
        p.post("/api/v1/stamp/forget")
        assert p.get("/api/v1/stamp/status").status_code == 401


def test_toggle_after_guard(worker, monkeypatch):
    import app.routers.stamp as st
    monkeypatch.setattr(st, "TOGGLE_GUARD_SECONDS", 0)
    with _phone() as p:
        p.post("/api/v1/stamp/pair", json={"token": worker["token"]})
        a = p.post("/api/v1/stamp/toggle").json()
        b = p.post("/api/v1/stamp/toggle").json()
        assert a["changed"] and b["changed"] and a["status"] != b["status"]


def test_revoked_token_stops_paired_device(admin, worker):
    with _phone() as p:
        p.post("/api/v1/stamp/pair", json={"token": worker["token"]})
        assert p.get("/api/v1/stamp/status").status_code == 200
        for t in admin.get(f"/api/v1/users/{worker['id']}/tokens").json():
            admin.delete(f"/api/v1/users/{worker['id']}/tokens/{t['id']}")
        assert p.get("/api/v1/stamp/status").status_code == 401


def test_cross_site_post_blocked(worker):
    with _phone() as p:
        r = p.post("/api/v1/stamp/toggle", headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
