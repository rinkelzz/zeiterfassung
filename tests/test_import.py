"""Import aus rinkelzz/gleitzeit (CSV-Export + Tabellenexporte)."""
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app

# genau so, wie export.php der Gleitzeit-App schreibt (BOM, Semikolon, fputcsv-Quoting)
GLEITZEIT_EXPORT = (
    "﻿datum;checkin;checkout;dauer_h;notiz\n"
    "2026-03-02;\"2026-03-02 07:58\";\"2026-03-02 16:45\";8.78;\n"
    "2026-03-03;\"2026-03-03 08:01\";\"2026-03-03 17:30\";9.48;\"Einsatz, lange\"\n"
    "2026-03-04;\"2026-03-04 07:00\";;;\n"                       # offen → Fehler
    "2026-03-05;\"2026-03-05 08:00\";\"2026-03-05 12:00\";4;\n"
    "2026-03-05;\"2026-03-05 11:00\";\"2026-03-05 13:00\";2;\n"  # überschneidet Zeile davor
    "kaputt;nix;;;\n"
)
# phpMyAdmin-Export der Tabelle absences (mit Kopfzeile)
ABSENCES = (
    "id;date;type;half_day;note;created_at\n"
    "1;2026-03-09;vacation;0;NULL;2026-03-01 10:00:00\n"
    "2;2026-03-10;vacation;1;halber Tag;2026-03-01 10:00:00\n"
    "3;2026-03-11;gleitzeit;0;;2026-03-01 10:00:00\n"
    "4;2026-03-12;overtime_withdrawal;0;;2026-03-01 10:00:00\n"
    "5;2026-03-13;urlaubsreise;0;;2026-03-01 10:00:00\n"
)
DAYFLAGS = "date;overtime\n2026-03-03;1\n2026-03-06;0\n"


@pytest.fixture(scope="module")
def ctx():
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]})
        uid = c.post("/api/v1/users", json={"username": "importtim", "display_name": "Import Tim",
                                            "password": "geheim123", "tracking_start": "2026-03-01"}).json()["id"]
        yield c, uid


def run(c, uid, kind, text, commit):
    r = c.post("/api/v1/import", json={"user_id": uid, "kind": kind, "csv_text": text, "commit": commit})
    assert r.status_code == 200, r.text
    return r.json()


def test_entries_preview_and_commit(ctx):
    c, uid = ctx
    pre = run(c, uid, "entries", GLEITZEIT_EXPORT, False)
    assert pre["summary"] == {"neu": 3, "fehler": 2, "konflikt": 1}
    assert pre["rows"][1]["note"] == "Einsatz, lange" and pre["rows"][1]["hours"] == 9.48
    assert c.get("/api/v1/entries", params={"user_id": uid, "from": "2026-03-01", "to": "2026-03-31"}).json() == []
    done = run(c, uid, "entries", GLEITZEIT_EXPORT, True)
    assert done["imported"] == 3
    entries = c.get("/api/v1/entries", params={"user_id": uid, "from": "2026-03-01", "to": "2026-03-31"}).json()
    assert [e["start"][:16] for e in entries] == ["2026-03-02T07:58", "2026-03-03T08:01", "2026-03-05T08:00"]
    assert all(e["source"] == "import" for e in entries)
    # zweiter Import erkennt alles als doppelt
    again = run(c, uid, "entries", GLEITZEIT_EXPORT, True)
    assert again["imported"] == 0 and again["summary"]["doppelt"] == 3


def test_time_only_and_night_shift(ctx):
    c, uid = ctx
    text = "Datum;Kommen;Gehen;Notiz\n16.03.2026;19:00;07:00;Nachtdienst\n"
    pre = run(c, uid, "entries", text, False)
    assert pre["rows"][0]["status"] == "neu"
    assert pre["rows"][0]["start"] == "2026-03-16 19:00" and pre["rows"][0]["end"] == "2026-03-17 07:00"


def test_absences_and_dayflags(ctx):
    c, uid = ctx
    pre = run(c, uid, "absences", ABSENCES, False)
    assert pre["summary"] == {"neu": 4, "fehler": 1}
    assert [r["kind"] for r in pre["rows"][:4]] == ["urlaub", "urlaub", "gleittag", "mehrarbeit"]
    assert pre["rows"][1]["half_day"] is True and pre["rows"][0]["note"] is None
    assert run(c, uid, "absences", ABSENCES, True)["imported"] == 4
    assert run(c, uid, "dayflags", DAYFLAGS, True)["imported"] == 1
    rep = c.get("/api/v1/reports/user", params={"user_id": uid, "from": "2026-03-02", "to": "2026-03-13"}).json()
    days = {d["date"]: d for d in rep["days"]}
    assert days["2026-03-03"]["overtime_day"] and days["2026-03-03"]["account"] == "ueberstunden"
    assert days["2026-03-10"]["absence_label"] == "Urlaub (½)"
    assert days["2026-03-12"]["account"] == "ueberstunden"
    # vorhandene Abwesenheit mit anderer Art wird nicht überschrieben
    pre = run(c, uid, "absences", "date;type\n2026-03-09;sick\n", False)
    assert pre["rows"][0]["status"] == "konflikt"


def test_import_requires_admin(ctx):
    c, uid = ctx
    with TestClient(app) as other:
        other.post("/api/v1/auth/login", json={"username": "importtim", "password": "geheim123"})
        r = other.post("/api/v1/import", json={"user_id": uid, "kind": "entries", "csv_text": "x", "commit": False})
        assert r.status_code == 403
