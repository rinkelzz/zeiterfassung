"""Abwesenheitskalender (Admins alle, Vorgesetzte ihr Team) und Überschneidungs-Hinweis beim Genehmigen."""
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app

PW = "geheim123"


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        yield c


def login(user):
    c = TestClient(app)
    c.__enter__()
    assert c.post("/api/v1/auth/login", json={"username": user, "password": PW}).status_code == 200, user
    return c


@pytest.fixture(scope="module")
def team(admin):
    def make(name, **kw):
        r = admin.post("/api/v1/users", json={"username": "tc_" + name, "display_name": name.title(), "password": PW,
                                              "weekly_hours": 40, "workdays": [1, 2, 3, 4, 5],
                                              "tracking_start": "2026-01-01", **kw})
        assert r.status_code == 201, r.text
        return r.json()["id"]
    boss = make("chefin")
    ids = {"chefin": boss, "ida": make("ida", manager_id=boss), "jan": make("jan", manager_id=boss),
           "kai": make("kai", manager_id=boss), "fremd": make("fremd")}
    br = admin.post("/api/v1/branches", json={"name": "Kalender-Wache", "user_ids": [ids["ida"], ids["jan"], ids["kai"]]})
    assert br.status_code in (200, 201), br.text
    # Jan: Urlaub eingetragen (genehmigt), Kai: krank, Fremd: Urlaub (gehört nicht zum Team)
    for uid, kind, a, b in ((ids["jan"], "urlaub", "2027-03-08", "2027-03-10"),
                            (ids["kai"], "krank", "2027-03-09", "2027-03-09"), (ids["fremd"], "urlaub", "2027-03-09", "2027-03-09")):
        r = admin.post("/api/v1/absences", json={"user_id": uid, "from_day": a, "to_day": b, "kind": kind})
        assert r.status_code == 201, r.text
    return ids


def test_calendar_admin_sees_all(admin, team):
    d = admin.get("/api/v1/leave/calendar", params={"month": "2027-03"}).json()
    assert len(d["days"]) == 31 and d["days"][0]["date"] == "2027-03-01" and d["days"][0]["weekday"] == 0
    names = {u["name"] for u in d["users"]}
    assert {"Ida", "Jan", "Kai", "Fremd", "Chefin"} <= names
    assert d["cells"][str(team["jan"])]["2027-03-08"] == {"kind": "urlaub", "label": "Urlaub", "half": False, "status": "eingetragen"}
    assert d["cells"][str(team["kai"])]["2027-03-09"]["kind"] == "krank"
    jan = next(u for u in d["users"] if u["id"] == team["jan"])
    assert "2027-03-06" in jan["off"] and "2027-03-08" not in jan["off"] and jan["branches"] == ["Kalender-Wache"]
    assert any(b["name"] == "Kalender-Wache" for b in d["branches"])
    # Filter Filiale und Mitarbeiter
    bid = next(b["id"] for b in d["branches"] if b["name"] == "Kalender-Wache")
    f = admin.get("/api/v1/leave/calendar", params={"month": "2027-03", "branch_id": bid}).json()
    assert {u["name"] for u in f["users"]} == {"Ida", "Jan", "Kai"}
    f = admin.get("/api/v1/leave/calendar", params={"month": "2027-03", "users": f"{team['ida']},{team['kai']}"}).json()
    assert [u["id"] for u in f["users"]] == [team["ida"], team["kai"]]
    assert admin.get("/api/v1/leave/calendar", params={"month": "2027-13"}).status_code == 422


def test_calendar_manager_only_team_and_others_forbidden(team):
    boss = login("tc_chefin")
    d = boss.get("/api/v1/leave/calendar", params={"month": "2027-03"}).json()
    assert {u["name"] for u in d["users"]} == {"Chefin", "Ida", "Jan", "Kai"}
    assert str(team["fremd"]) not in d["cells"]
    ida = login("tc_ida")
    assert ida.get("/api/v1/leave/calendar", params={"month": "2027-03"}).status_code == 403


def test_pending_request_and_overlap_hint(team):
    ida = login("tc_ida")
    r = ida.post("/api/v1/leave", json={"kind": "urlaub", "from_day": "2027-03-08", "to_day": "2027-03-12"})
    assert r.status_code == 201, r.text
    boss = login("tc_chefin")
    d = boss.get("/api/v1/leave/calendar", params={"month": "2027-03"}).json()
    cell = d["cells"][str(team["ida"])]["2027-03-10"]
    assert cell["status"] == "beantragt" and cell["request_id"] == r.json()["id"]
    todo = [x for x in boss.get("/api/v1/leave", params={"scope": "decide"}).json() if x["id"] == r.json()["id"]][0]
    ov = todo["overlap"]
    assert ov["group"] == "Filiale Kalender-Wache" and ov["size"] == 3
    absent = {a["name"]: a for a in ov["absent"]}
    assert set(absent) == {"Jan", "Kai"}            # „Fremd“ ist nicht in der Filiale
    assert absent["Jan"]["span"] == "08.03.2027 – 10.03.2027" and absent["Jan"]["kinds"] == "Urlaub"
    assert absent["Kai"]["kinds"] == "Krank" and absent["Kai"]["pending"] is False


def test_calendar_access_for_employee_neutral(admin, team):
    """Freigegebene Mitarbeiter sehen ihre Filiale – Kollegen nur als „abwesend“, keine offenen Anträge."""
    lisa = admin.post("/api/v1/users", json={"username": "tc_lisa", "display_name": "Lisa", "password": PW,
                                             "weekly_hours": 40, "workdays": [1, 2, 3, 4, 5], "tracking_start": "2026-01-01"}).json()["id"]
    bid = next(b["id"] for b in admin.get("/api/v1/branches").json() if b["name"] == "Kalender-Wache")
    members = next(b for b in admin.get("/api/v1/branches").json() if b["id"] == bid)["user_ids"]
    assert admin.patch(f"/api/v1/branches/{bid}", json={"user_ids": sorted(set(members) | {lisa})}).status_code == 200
    admin.post("/api/v1/absences", json={"user_id": lisa, "from_day": "2027-03-15", "to_day": "2027-03-15", "kind": "krank"})
    c = login("tc_lisa")
    assert c.get("/api/v1/leave/count").json()["can_calendar"] is False
    assert c.get("/api/v1/leave/calendar", params={"month": "2027-03"}).status_code == 403
    assert admin.patch(f"/api/v1/users/{lisa}", json={"calendar_access": True}).json()["calendar_access"] is True
    assert c.get("/api/v1/leave/count").json()["can_calendar"] is True
    d = c.get("/api/v1/leave/calendar", params={"month": "2027-03"}).json()
    ids = {u["id"] for u in d["users"]}
    assert {team["ida"], team["jan"], team["kai"], lisa} <= ids and team["fremd"] not in ids
    assert d["detail"] == "mixed"
    assert d["cells"][str(team["kai"])]["2027-03-09"] == {"kind": "abwesend", "label": "abwesend", "half": False, "status": "eingetragen"}
    assert d["cells"][str(team["jan"])]["2027-03-08"]["kind"] == "abwesend"
    # offener Antrag von Ida (aus dem vorigen Test) bleibt unsichtbar, eigene Daten mit Art
    assert all(v["status"] != "beantragt" for v in d["cells"].get(str(team["ida"]), {}).values())
    assert d["cells"][str(lisa)]["2027-03-15"]["kind"] == "krank"
    admin.patch(f"/api/v1/users/{lisa}", json={"calendar_access": False})


def test_company_workdays_setting(admin):
    assert admin.get("/api/v1/settings").json()["company_workdays"] == [1, 2, 3, 4, 5]
    assert admin.patch("/api/v1/settings", json={"company_workdays": [2, 3, 4, 5, 6]}).status_code == 200
    assert admin.get("/api/v1/leave/calendar", params={"month": "2027-03"}).json()["workdays"] == [2, 3, 4, 5, 6]
    assert admin.patch("/api/v1/settings", json={"company_workdays": []}).status_code == 400
    assert admin.patch("/api/v1/settings", json={"company_workdays": [0, 8]}).status_code == 400
    admin.patch("/api/v1/settings", json={"company_workdays": [1, 2, 3, 4, 5]})
