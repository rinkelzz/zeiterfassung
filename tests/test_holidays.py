import os
import sqlite3
import tempfile
from datetime import date


import pytest
from fastapi.testclient import TestClient

from app.holidays import buss_und_bettag, easter_sunday, holidays_for_year
from app.main import app


# --- reine Feiertagslogik ------------------------------------------------

@pytest.mark.parametrize("year,expected", [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)),
                                           (2026, date(2026, 4, 5)), (2027, date(2027, 3, 28))])
def test_easter(year, expected):
    assert easter_sunday(year) == expected


def test_buss_und_bettag_never_on_23rd():
    # 2022: 23.11. war ein Mittwoch → Buß- und Bettag ist der 16.11.
    assert buss_und_bettag(2022) == date(2022, 11, 16)
    assert buss_und_bettag(2026) == date(2026, 11, 18)
    for y in range(2000, 2100):
        d = buss_und_bettag(y)
        assert d.weekday() == 2 and date(y, 11, 16) <= d <= date(y, 11, 22)


def test_state_specific():
    nw = holidays_for_year(2026, "NW")
    assert nw[date(2026, 6, 4)] == "Fronleichnam" and date(2026, 11, 1) in nw
    assert date(2026, 10, 31) not in nw
    by = holidays_for_year(2026, "BY")
    assert date(2026, 8, 15) in by and date(2026, 1, 6) in by
    assert date(2026, 3, 8) in holidays_for_year(2026, "MV")
    assert date(2022, 3, 8) not in holidays_for_year(2022, "MV")   # erst ab 2023
    assert date(2017, 10, 31) in holidays_for_year(2017, "NW")      # Reformationsjubiläum
    assert date(2026, 10, 31) in holidays_for_year(2026, "NI")
    assert len(holidays_for_year(2026, "NW")) == 11


def test_company_free_days():
    h = holidays_for_year(2026, "NW", "12-24, 12-31, 02-29")
    assert h[date(2026, 12, 24)] == "Betriebsfrei" and h[date(2026, 12, 31)] == "Betriebsfrei"
    assert date(2026, 2, 28) not in h  # 29.02. existiert 2026 nicht, wird übersprungen
    assert date(2028, 2, 29) in holidays_for_year(2028, "NW", "02-29")


# --- Integration in Auswertungen ----------------------------------------

@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        yield c


def test_settings_validation(admin):
    assert admin.patch("/api/v1/settings", json={"bundesland": "XX"}).status_code == 400
    assert admin.patch("/api/v1/settings", json={"company_free_days": "24.12."}).status_code == 400
    r = admin.patch("/api/v1/settings", json={"bundesland": "nw", "company_free_days": "12-24,12-31"})
    assert r.status_code == 200
    assert r.json()["bundesland"] == "NW" and r.json()["company_free_days"] == "12-24, 12-31"
    hol = admin.get("/api/v1/holidays", params={"year": 2026}).json()
    assert {"date": "2026-12-24", "name": "Betriebsfrei", "weekday": "Do"} in hol["days"]


def test_holiday_and_tracking_start_in_report(admin):
    r = admin.post("/api/v1/users", json={
        "username": "anna", "display_name": "Anna", "password": "geheim123",
        "tracking_start": "2026-06-03", "carryover_minutes": 210,
    })
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert r.json()["tracking_start"] == "2026-06-03" and r.json()["carryover_minutes"] == 210
    # Mo 01.06. bis Fr 05.06.2026: Erfassungsbeginn Mi 03.06., Do 04.06. Fronleichnam (NW)
    admin.post("/api/v1/entries", json={"user_id": uid, "start": "2026-06-03T08:00", "end": "2026-06-03T16:00"})
    admin.post("/api/v1/entries", json={"user_id": uid, "start": "2026-06-05T08:00", "end": "2026-06-05T17:00"})
    rep = admin.get("/api/v1/reports/user", params={"user_id": uid, "from": "2026-06-01", "to": "2026-06-05"}).json()
    days = {d["date"]: d for d in rep["days"]}
    assert days["2026-06-01"]["target"] == "0:00" and days["2026-06-01"]["before_start"]
    assert days["2026-06-03"]["target"] == "8:00"
    assert days["2026-06-04"]["absence_label"] == "Fronleichnam" and days["2026-06-04"]["target"] == "0:00"
    assert rep["totals"]["diff"] == "1:00"
    assert rep["totals"]["absence_days"]["feiertag"] == 1
    # Gesamtsaldo: Übertrag 3:30 + alles ab 03.06. bis heute
    assert rep["account"]["since"] == "2026-06-03" and rep["account"]["carryover"] == "3:30"
    # Erfassungsbeginn verschieben ändert das Soll
    admin.patch(f"/api/v1/users/{uid}", json={"tracking_start": "2026-06-01"})
    rep = admin.get("/api/v1/reports/user", params={"user_id": uid, "from": "2026-06-01", "to": "2026-06-05"}).json()
    assert rep["totals"]["target"] == "32:00"  # Mo, Di, Mi, Fr – Do ist Fronleichnam
    assert admin.patch(f"/api/v1/users/{uid}", json={"tracking_start": None}).status_code == 400


def test_manual_absence_beats_holiday(admin):
    uid = admin.post("/api/v1/users", json={"username": "ben", "display_name": "Ben", "password": "geheim123",
                                            "tracking_start": "2026-01-01"}).json()["id"]
    admin.post("/api/v1/absences", json={"user_id": uid, "from_day": "2026-12-24", "kind": "urlaub"})
    rep = admin.get("/api/v1/reports/user", params={"user_id": uid, "from": "2026-12-24", "to": "2026-12-25"}).json()
    assert rep["days"][0]["absence_label"] == "Urlaub"
    assert rep["days"][1]["absence_label"] == "1. Weihnachtstag"


def test_migration_from_v1(tmp_path):
    """Datenbank aus Version 0.1.x wird beim Start ohne Datenverlust erweitert."""
    from app import db as dbmod
    from app.config import settings

    old = sqlite3.connect(str(tmp_path / "zeiterfassung.db"))
    old.executescript(dbmod.SCHEMA)  # v1-Schema ohne neue Spalten
    old.execute("DROP TABLE users")
    old.execute("""CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        display_name TEXT NOT NULL, password_hash TEXT, is_admin INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
        weekly_hours REAL NOT NULL DEFAULT 40, workdays TEXT NOT NULL DEFAULT '1,2,3,4,5', created_at TEXT NOT NULL)""")
    old.execute("INSERT INTO users(username, display_name, created_at) VALUES ('alt', 'Alt', '2026-10-16T13:00:00+00:00')")
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()

    saved = (settings.data_dir, settings.db_path)
    settings.data_dir, settings.db_path = tmp_path, tmp_path / "zeiterfassung.db"
    try:
        dbmod.init_db()
        conn = dbmod.connect()
        row = conn.execute("SELECT * FROM users WHERE username = 'alt'").fetchone()
        assert row["tracking_start"] == "2026-10-16" and row["carryover_minutes"] == 0
        assert row["email"] is None and row["report_mail"] == 1  # v6: Berichte per Mail
        assert conn.execute("SELECT COUNT(*) FROM report_mails").fetchone()[0] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION
        assert dbmod.get_setting(conn, "bundesland") == "NW"
        conn.close()
        dbmod.init_db()  # zweiter Start ändert nichts
    finally:
        settings.data_dir, settings.db_path = saved
