"""Gleitzeit-/Überstunden-Konto, Urlaubskonto, halbe Tage, Pausenmodus."""
import os
import tempfile


import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        c.patch("/api/v1/settings", json={"bundesland": "NW", "company_free_days": "", "break_mode": "none"})
        yield c


def new_user(admin, name, **extra):
    body = {"username": name, "display_name": name.title(), "password": "geheim123",
            "tracking_start": "2026-01-01", **extra}
    r = admin.post("/api/v1/users", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def report(admin, uid, f, t):
    return admin.get("/api/v1/reports/user", params={"user_id": uid, "from": f, "to": t}).json()


def entry(admin, uid, s, e):
    r = admin.post("/api/v1/entries", json={"user_id": uid, "start": s, "end": e})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_gleitzeit_vs_ueberstunden(admin):
    uid = new_user(admin, "carl", carryover_minutes=60, carryover_overtime_minutes=120)
    # Mo 02.03.2026: 10 h → +2 h Gleitzeit; Di 03.03.: 11 h, als Ü markiert → +3 h Überstunden
    entry(admin, uid, "2026-03-02T07:00", "2026-03-02T17:00")
    entry(admin, uid, "2026-03-03T07:00", "2026-03-03T18:00")
    assert admin.put("/api/v1/dayflags", json={"user_id": uid, "day": "2026-03-03", "overtime": True}).status_code == 200
    # Mi 04.03.: Gleittag → −8 h Gleitzeit; Do 05.03.: Mehrarbeit-Entnahme → −8 h Überstunden
    admin.post("/api/v1/absences", json={"user_id": uid, "from_day": "2026-03-04", "kind": "gleittag"})
    admin.post("/api/v1/absences", json={"user_id": uid, "from_day": "2026-03-05", "kind": "mehrarbeit"})
    t = report(admin, uid, "2026-03-02", "2026-03-05")["totals"]
    assert t["gleitzeit"] == "-6:00" and t["ueberstunden"] == "-5:00" and t["diff"] == "-11:00"
    days = report(admin, uid, "2026-03-02", "2026-03-05")["days"]
    assert [d["account"] for d in days] == ["gleitzeit", "ueberstunden", "gleitzeit", "ueberstunden"]
    # Markierung wieder entfernen
    admin.put("/api/v1/dayflags", json={"user_id": uid, "day": "2026-03-03", "overtime": False})
    t = report(admin, uid, "2026-03-02", "2026-03-05")["totals"]
    assert t["gleitzeit"] == "-3:00" and t["ueberstunden"] == "-8:00"
    # Kontostand enthält Überträge (1 h / 2 h)
    acc = report(admin, uid, "2026-03-02", "2026-03-02")["account"]
    assert acc["carryover"] == "1:00" and acc["carryover_overtime_s"] == 7200


def test_half_day_vacation_and_account(admin):
    uid = new_user(admin, "dora", vacation_days_per_year=28, carryover_vacation=2.5)
    # Mo 09.03.2026 halber Urlaub, 4 h gearbeitet → Soll 4 h, Saldo 0
    entry(admin, uid, "2026-03-09T08:00", "2026-03-09T12:00")
    admin.post("/api/v1/absences", json={"user_id": uid, "from_day": "2026-03-09", "kind": "urlaub", "half_day": True})
    d = report(admin, uid, "2026-03-09", "2026-03-09")["days"][0]
    assert d["target"] == "4:00" and d["diff"] == "0:00" and d["absence_label"] == "Urlaub (½)"
    # Urlaub über Ostern: Do 02.04. bis Di 07.04.2026 → Karfreitag, Sa, So, Ostermontag übersprungen
    r = admin.post("/api/v1/absences", json={"user_id": uid, "from_day": "2026-04-02", "to_day": "2026-04-07", "kind": "urlaub"})
    assert r.json()["days"] == 2 and r.json()["skipped"] == 4
    vac = report(admin, uid, "2026-01-01", "2026-12-31")["vacation"]
    assert vac["entitlement"] == 28 and vac["carryover"] == 2.5
    assert vac["taken"] + vac["planned"] == 2.5
    assert vac["remaining"] == 28.0
    # Folgejahr: Rest wird übertragen
    vac27 = admin.get("/api/v1/reports/year", params={"user_id": uid, "year": 2027}).json()["vacation"]
    assert vac27["carryover"] == 28.0 and vac27["remaining"] == 56.0


def test_absence_range_delete_and_single_day_on_weekend(admin):
    uid = new_user(admin, "emil")
    # Einzeltag am Samstag wird gesetzt (bewusste Auswahl), zählt aber nicht als Urlaubstag
    admin.post("/api/v1/absences", json={"user_id": uid, "from_day": "2026-03-14", "kind": "urlaub"})
    y = admin.get("/api/v1/reports/year", params={"user_id": uid, "year": 2026}).json()
    assert y["absence_days"]["urlaub"] == 0
    admin.post("/api/v1/absences", json={"user_id": uid, "from_day": "2026-03-16", "to_day": "2026-03-20", "kind": "krank"})
    r = admin.delete("/api/v1/absences", params={"user_id": uid, "from": "2026-03-14", "to": "2026-03-18"})
    assert r.json()["days"] == 4
    left = admin.get("/api/v1/absences", params={"user_id": uid, "from": "2026-03-01", "to": "2026-03-31"}).json()
    assert [a["day"] for a in left] == ["2026-03-19", "2026-03-20"]


def test_break_modes(admin):
    uid = new_user(admin, "fritz")
    entry(admin, uid, "2026-03-23T08:00", "2026-03-23T16:00")   # 8 h am Stück
    entry(admin, uid, "2026-03-24T08:00", "2026-03-24T12:00")   # 4 h
    get = lambda: [d["worked"] for d in report(admin, uid, "2026-03-23", "2026-03-24")["days"]]
    assert get() == ["8:00", "4:00"]
    admin.patch("/api/v1/settings", json={"break_mode": "fixed", "break_minutes": 30})
    assert get() == ["7:30", "3:30"]
    admin.patch("/api/v1/settings", json={"break_mode": "arbzg"})
    assert get() == ["7:30", "4:00"]
    assert admin.patch("/api/v1/settings", json={"break_mode": "quatsch"}).status_code == 400
    admin.patch("/api/v1/settings", json={"break_mode": "none"})


def test_csv_contains_accounts(admin):
    uid = new_user(admin, "gina", vacation_days_per_year=30)
    entry(admin, uid, "2026-03-02T07:00", "2026-03-02T17:00")
    text = admin.get("/api/v1/reports/export.csv", params={"user_id": uid, "from": "2026-03-02", "to": "2026-03-02"}).content.decode("utf-8-sig")
    assert "02.03.2026;Mo;07:00;17:00;;10:00;8:00;2:00;Gleitzeit;" in text
    assert "Gleitzeit-Konto;" in text and "Urlaub 2026;Anspruch 30" in text


def test_ue_day_only_moves_time_above_target(admin):
    """35-h-Woche → 7 h Tagessoll. Ü-Tag: nur das Plus geht in die Überstunden, ein Minus bleibt Gleitzeit."""
    uid = new_user(admin, "hanna", weekly_hours=35)
    flag = lambda d: admin.put("/api/v1/dayflags", json={"user_id": uid, "day": d, "overtime": True})
    entry(admin, uid, "2026-09-07T07:00", "2026-09-07T16:00")   # Mo 9 h, Ü → +2 Überstunden
    entry(admin, uid, "2026-09-08T08:00", "2026-09-08T13:00")   # Di 5 h, Ü → −2 Gleitzeit, 0 Überstunden
    entry(admin, uid, "2026-09-09T07:00", "2026-09-09T16:00")   # Mi 9 h, ohne Ü → +2 Gleitzeit
    entry(admin, uid, "2026-09-12T09:00", "2026-09-12T13:00")   # Sa 4 h, Ü → +4 Überstunden (kein Soll)
    for d in ("2026-09-07", "2026-09-08", "2026-09-12"):
        flag(d)
    rep = report(admin, uid, "2026-09-07", "2026-09-12")
    days = {d["date"]: d for d in rep["days"]}
    assert days["2026-09-07"]["target"] == "7:00"
    assert (days["2026-09-07"]["diff_gleitzeit_s"], days["2026-09-07"]["diff_ueberstunden_s"]) == (0, 2 * 3600)
    assert (days["2026-09-08"]["diff_gleitzeit_s"], days["2026-09-08"]["diff_ueberstunden_s"]) == (-2 * 3600, 0)
    assert (days["2026-09-09"]["diff_gleitzeit_s"], days["2026-09-09"]["diff_ueberstunden_s"]) == (2 * 3600, 0)
    assert days["2026-09-12"]["diff_ueberstunden_s"] == 4 * 3600
    # Do/Fr ohne Buchung: je −7 h Gleitzeit
    t = rep["totals"]
    assert t["ueberstunden"] == "6:00"
    assert t["gleitzeit"] == "-14:00"      # −2 + 2 − 7 − 7
    assert t["diff"] == "-8:00"


def test_weekly_hours_history(admin):
    """40 h bis Ende September, ab 01.10. 35 h – alte Monate bleiben unverändert."""
    uid = new_user(admin, "ida", weekly_hours=40)
    sched = admin.get(f"/api/v1/users/{uid}/schedules").json()
    assert len(sched) == 1 and sched[0]["valid_from"] is None and sched[0]["weekly_hours"] == 40
    sep = report(admin, uid, "2026-09-28", "2026-09-28")["days"][0]
    assert sep["target"] == "8:00"
    r = admin.post(f"/api/v1/users/{uid}/schedules", json={"valid_from": "2026-10-01", "weekly_hours": 35})
    assert r.status_code == 201 and len(r.json()) == 2
    assert report(admin, uid, "2026-09-28", "2026-09-28")["days"][0]["target"] == "8:00"   # alt bleibt
    assert report(admin, uid, "2026-10-01", "2026-10-01")["days"][0]["target"] == "7:00"   # neu ab 01.10.
    # Teilzeit an 4 Tagen ab 2026-10-12 (Mo–Do)
    admin.post(f"/api/v1/users/{uid}/schedules", json={"valid_from": "2026-10-12", "weekly_hours": 28, "workdays": [1, 2, 3, 4]})
    days = {d["date"]: d for d in report(admin, uid, "2026-10-12", "2026-10-16")["days"]}
    assert days["2026-10-12"]["target"] == "7:00" and days["2026-10-16"]["target"] == "0:00"   # Fr frei
    # aktueller Wert wird in der Mitarbeiterliste gespiegelt
    me = [u for u in admin.get("/api/v1/users").json() if u["id"] == uid][0]
    assert me["weekly_hours"] == 28 and me["workdays"] == [1, 2, 3, 4]
    # Urlaub über einen Freitag im Teilzeit-Zeitraum zählt den Freitag nicht
    r = admin.post("/api/v1/absences", json={"user_id": uid, "from_day": "2026-10-15", "to_day": "2026-10-19", "kind": "urlaub"})
    assert r.json()["days"] == 2   # Do 15. und Mo 19.
    # Eintrag löschen → wieder 35 h Mo–Fr
    sid = [s for s in admin.get(f"/api/v1/users/{uid}/schedules").json() if s["valid_from"] == "2026-10-12"][0]["id"]
    admin.delete(f"/api/v1/users/{uid}/schedules/{sid}")
    assert report(admin, uid, "2026-10-16", "2026-10-16")["days"][0]["target"] == "7:00"
    first = [s for s in admin.get(f"/api/v1/users/{uid}/schedules").json() if s["valid_from"] is None][0]["id"]
    assert admin.delete(f"/api/v1/users/{uid}/schedules/{first}").status_code == 400


def test_patch_weekly_hours_changes_current_entry_only(admin):
    uid = new_user(admin, "jan", weekly_hours=40)
    admin.post(f"/api/v1/users/{uid}/schedules", json={"valid_from": "2026-10-01", "weekly_hours": 30})
    admin.patch(f"/api/v1/users/{uid}", json={"weekly_hours": 32})   # ändert den heute gültigen (ab 01.10.)
    s = {x["valid_from"]: x["weekly_hours"] for x in admin.get(f"/api/v1/users/{uid}/schedules").json()}
    assert s == {None: 40, "2026-10-01": 32}
    assert report(admin, uid, "2026-09-28", "2026-09-28")["days"][0]["target"] == "8:00"
