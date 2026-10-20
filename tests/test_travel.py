"""Wegezeit: Kommen per NFC früher, Gehen später – nur für eingestellte Stempelwege."""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def ctx():
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]})
        uid = c.post("/api/v1/users", json={"username": f"weg{datetime.now().microsecond}", "display_name": "Weg",
                                            "password": "geheim123", "tracking_start": "2026-01-01"}).json()["id"]
        token = c.post(f"/api/v1/users/{uid}/tokens", json={"label": "nfc"}).json()["token"]
        yield c, uid, {"Authorization": f"Bearer {token}"}
        c.patch("/api/v1/settings", json={"travel_in_minutes": 0, "travel_out_minutes": 0, "travel_sources": ["nfc"]})


def entries(c, uid):
    today = datetime.now().date()
    return c.get("/api/v1/entries", params={"user_id": uid, "from": str(today - timedelta(days=1)), "to": str(today + timedelta(days=1))}).json()


def minutes_from_now(iso):
    return (datetime.fromisoformat(iso) - datetime.now(timezone.utc)).total_seconds() / 60


def test_nfc_adds_travel_time(ctx):
    c, uid, h = ctx
    r = c.patch("/api/v1/settings", json={"travel_in_minutes": 10, "travel_out_minutes": 15, "travel_sources": ["nfc"]})
    assert r.json()["travel_in_minutes"] == 10 and r.json()["travel_sources"] == ["nfc"]
    r = c.post("/api/v1/clock/in?source=nfc", headers=h).json()
    assert r["changed"] and "10 min Wegezeit" in r["message"]
    e = entries(c, uid)[-1]
    assert e["source"] == "nfc" and e["travel_in_min"] == 10
    assert -10.5 < minutes_from_now(e["start"]) < -9.5
    r = c.post("/api/v1/clock/out?source=nfc", headers=h).json()
    assert r["status"] == "out" and "15 min Wegezeit" in r["message"]
    e = entries(c, uid)[-1]
    assert e["travel_out_min"] == 15 and 14.5 < minutes_from_now(e["end"]) < 15.5
    # Auswertung weist die Wegezeit aus (Tagesgrenze um Mitternacht beachten)
    today = datetime.now().date()
    rep = c.get("/api/v1/reports/user", params={"user_id": uid, "from": str(today - timedelta(days=1)), "to": str(today + timedelta(days=1))}).json()
    assert sum(d["travel_min"] for d in rep["days"]) == 25
    csv = c.get("/api/v1/reports/export.csv", params={"user_id": uid, "from": str(today - timedelta(days=1)), "to": str(today + timedelta(days=1))})
    assert "davon Wegezeit" in csv.content.decode("utf-8-sig")


def test_other_sources_without_travel(ctx):
    c, uid, h = ctx
    c.patch("/api/v1/settings", json={"travel_in_minutes": 10, "travel_out_minutes": 10, "travel_sources": ["nfc"]})
    r = c.post("/api/v1/clock/in?source=shortcut", headers=h).json()
    assert "Wegezeit" not in r["message"]
    e = entries(c, uid)[-1]
    assert e["source"] == "shortcut" and e["travel_in_min"] == 0 and abs(minutes_from_now(e["start"])) < 0.5
    # Body statt Query geht auch
    c.post("/api/v1/clock/out", headers=h, json={"source": "nfc"})
    assert entries(c, uid)[-1]["travel_out_min"] == 10


def test_travel_never_overlaps_previous_entry(ctx):
    c, uid, h = ctx
    c.patch("/api/v1/settings", json={"travel_in_minutes": 10, "travel_out_minutes": 10, "travel_sources": ["nfc"]})
    c.post("/api/v1/clock/in?source=web", headers=h)
    c.post("/api/v1/clock/out?source=web", headers=h)          # Ende jetzt
    r = c.post("/api/v1/clock/in?source=nfc", headers=h).json()  # gleich wieder rein
    assert r["changed"]
    es = entries(c, uid)
    assert es[-1]["start"] >= es[-2]["end"]                     # keine Überschneidung
    assert es[-1]["travel_in_min"] == 0


def test_settings_validation(ctx):
    c, _, _ = ctx
    assert c.patch("/api/v1/settings", json={"travel_in_minutes": 500}).status_code == 422
    assert c.patch("/api/v1/settings", json={"travel_sources": ["teleport"]}).status_code == 400
