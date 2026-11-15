"""Monatsbericht mit allen Stempelblöcken und Monatszusammenfassung aller Mitarbeiter (Steuerbüro)."""
import io
import os
import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import summary
from app.db import connect
from app.main import app
from app.report_files import build_data, month_pdf
from app.summary import absence_periods, period_text
from tests.test_reports_mail import FakeSMTP


@pytest.fixture(scope="module")
def smtp():
    srv = FakeSMTP()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


@pytest.fixture(scope="module")
def admin(smtp):
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        r = c.patch("/api/v1/settings", json={
            "smtp_host": "127.0.0.1", "smtp_port": smtp.server_address[1], "smtp_security": "none",
            "smtp_user": "zeit@example.de", "smtp_password": "richtig", "smtp_from": "zeit@example.de",
            "mail_transport": "smtp", "break_mode": "none"})
        assert r.status_code == 200, r.text
        yield c


@pytest.fixture(scope="module")
def staff(admin):
    ids = {}
    for name, disp in (("sumo", "Sumo Splitter"), ("sumkrank", "Svenja Krank")):
        r = admin.post("/api/v1/users", json={"username": name, "display_name": disp, "password": "geheim123",
                                             "tracking_start": "2026-01-01", "weekly_hours": 40})
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]
    u = ids["sumo"]
    # Di 02.06.2026: drei Blöcke
    for a, b in (("07:00", "09:00"), ("09:30", "12:00"), ("12:30", "16:00")):
        admin.post("/api/v1/entries", json={"user_id": u, "start": f"2026-06-02T{a}", "end": f"2026-06-02T{b}"})
    admin.post("/api/v1/entries", json={"user_id": u, "start": "2026-06-03T08:00", "end": "2026-06-03T16:00"})
    admin.post("/api/v1/absences", json={"user_id": u, "from_day": "2026-06-08", "to_day": "2026-06-09", "kind": "urlaub"})
    k = ids["sumkrank"]
    # Krank Do 11.06. – Mo 15.06. (über das Wochenende) → ein Zeitraum mit 3 Arbeitstagen
    admin.post("/api/v1/absences", json={"user_id": k, "from_day": "2026-06-11", "to_day": "2026-06-15", "kind": "krank"})
    admin.post("/api/v1/absences", json={"user_id": k, "from_day": "2026-06-22", "to_day": "2026-06-22", "kind": "kind_krank"})
    return ids


def test_blocks_in_report_data(admin, staff):
    r = admin.get("/api/v1/reports/user", params={"user_id": staff["sumo"], "from": "2026-06-02", "to": "2026-06-03"})
    assert r.status_code == 200, r.text
    d = r.json()["days"]
    assert d[0]["blocks"] == [{"in": "07:00", "out": "09:00"}, {"in": "09:30", "out": "12:00"},
                              {"in": "12:30", "out": "16:00"}]
    assert d[1]["blocks"] == [{"in": "08:00", "out": "16:00"}]


def test_month_xlsx_lists_every_block(admin, staff):
    r = admin.get("/api/v1/reports/download", params={"user_id": staff["sumo"], "period": "2026-06", "format": "xlsx"})
    assert r.status_code == 200
    rows = list(load_workbook(io.BytesIO(r.content)).active.iter_rows(values_only=True))
    i = next(n for n, row in enumerate(rows) if hasattr(row[0], "strftime") and row[0].strftime("%d.%m.") == "02.06.")
    assert rows[i][2:4] == ("07:00", "09:00") and rows[i][5] == 8
    assert rows[i + 1][:4] == (None, None, "09:30", "12:00") and rows[i + 1][5] is None
    assert rows[i + 2][2:4] == ("12:30", "16:00")
    assert rows[i + 3][0].strftime("%d.%m.") == "03.06."
    # Summenformel umfasst weiterhin alle Zeilen
    total = [row for row in rows if row[0] == "Summe"][0]
    assert str(total[5]).startswith("=SUM(F")


def test_month_pdf_breaks_onto_second_page():
    with connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE username = 'sumo'").fetchone()
        data = build_data(conn, u, "2026-06")
    # künstlich viele Blöcke: jeder Tag 6 Stempelpaare → passt nicht mehr auf eine Seite
    for d in data["report"]["days"]:
        d["blocks"] = [{"in": "08:00", "out": "09:00"}] * 6
    pdf = month_pdf(data)
    assert pdf.count(b"/Type /Page ") >= 2 and b"/Count 1 " not in pdf


def test_absence_periods_join_over_weekend():
    days = []
    for day, wd, kind in (("2026-06-11", True, "krank"), ("2026-06-12", True, "krank"), ("2026-06-13", False, "krank"),
                          ("2026-06-14", False, "krank"), ("2026-06-15", True, "krank"), ("2026-06-16", True, None),
                          ("2026-06-17", True, "krank")):
        days.append({"date": day, "absence": kind, "half_day": False, "workday": wd, "holiday": None,
                     "before_start": False})
    p = absence_periods(days)
    assert [(x["from"], x["to"], x["days"]) for x in p] == [("2026-06-11", "2026-06-15", 3), ("2026-06-17", "2026-06-17", 1)]
    assert period_text(p[0]) == "Krank 11.06.–15.06. (3 Tage)"


def test_summary_downloads(admin, staff):
    r = admin.get("/api/v1/reports/summary", params={"period": "2026-06", "file": "xlsx"})
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    assert 'filename="Monatszusammenfassung_2026-06.xlsx"' in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    ws = wb["Übersicht"]
    rows = list(ws.iter_rows(values_only=True))
    head = rows[3]
    sumo = [x for x in rows if x[0] == "Sumo Splitter"][0]
    sv = [x for x in rows if x[0] == "Svenja Krank"][0]
    col = {h: i for i, h in enumerate(head)}
    assert sumo[col["Ist (h)"]] == 16 and sumo[col["Urlaub im Monat (Tage)"]] == 2
    assert sv[col["Krank (Tage)"]] == 3 and sv[col["Kind krank (Tage)"]] == 1
    assert sv[col["Monat abgeschlossen"]] == "nein"
    assert "Krank 11.06.–15.06. (3 Tage)" in sv[col["Abwesenheiten"]]
    ab = [x for x in wb["Abwesenheiten"].iter_rows(values_only=True) if x[0] == "Svenja Krank"]
    assert [(x[1], x[4]) for x in ab] == [("Krank", 3), ("Kind krank", 1)]

    r = admin.get("/api/v1/reports/summary", params={"period": "2026-06", "file": "pdf"})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    r = admin.get("/api/v1/reports/summary", params={"period": "2026-06", "file": "reports"})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    with connect() as conn:
        n_users = len(summary.summary_users(conn, *summary.parse_period("2026-06")[1:]))
    assert r.content.count(b"/Type /Page ") >= n_users  # jeder Mitarbeiter mindestens eine Seite

    assert admin.get("/api/v1/reports/summary", params={"period": "2026", "file": "pdf"}).status_code == 400
    assert admin.get("/api/v1/reports/summary", params={"period": "2026-06", "file": "doc"}).status_code == 400


def test_summary_admin_only(admin, staff):
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "sumo", "password": "geheim123"})
        assert c.get("/api/v1/reports/summary", params={"period": "2026-06"}).status_code == 403
        assert c.post("/api/v1/reports/summary/mail", json={"period": "2026-06"}).status_code == 403
        assert "summary_recipients" not in c.get("/api/v1/settings").json()


def test_summary_settings_validation(admin):
    assert admin.patch("/api/v1/settings", json={"summary_recipients": "kaputt"}).status_code == 400
    assert admin.patch("/api/v1/settings", json={"summary_recipients": "", "summary_mail": True}).status_code == 400
    r = admin.patch("/api/v1/settings", json={"summary_recipients": "lohn@steuer.de; chef@firma.de, lohn@steuer.de"})
    assert r.status_code == 200
    s = admin.get("/api/v1/settings").json()
    assert s["summary_recipients"] == "lohn@steuer.de, chef@firma.de" and s["summary_with_reports"] is True


def test_summary_mail_now_and_schedule(admin, staff, smtp):
    admin.patch("/api/v1/settings", json={"summary_recipients": "lohn@steuer.de, chef@firma.de",
                                          "summary_with_reports": True})
    smtp.inbox.clear()
    r = admin.post("/api/v1/reports/summary/mail", json={"period": "2026-06"})
    assert r.status_code == 200, r.text
    assert sorted(m["msg"]["To"] for m in smtp.inbox) == ["chef@firma.de", "lohn@steuer.de"]
    msg = smtp.inbox[0]["msg"]
    assert msg["Subject"].startswith("Monatszusammenfassung Juni 2026")
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert names == ["Monatszusammenfassung_2026-06.pdf", "Monatszusammenfassung_2026-06.xlsx",
                     "Arbeitszeitnachweise_2026-06.pdf"]
    body = [p for p in msg.walk() if p.get_content_type() == "text/plain"][0].get_content()
    assert "noch nicht abgeschlossen" in body and "Svenja Krank" in body

    # ohne Nachweise
    admin.patch("/api/v1/settings", json={"summary_with_reports": False})
    smtp.inbox.clear()
    assert admin.post("/api/v1/reports/summary/mail", json={"period": "2026-06"}).status_code == 200
    assert len([p for p in smtp.inbox[0]["msg"].walk() if p.get_filename()]) == 2

    # Zeitplan: aus → nichts; an → am 1. ab 6 Uhr genau einmal
    with connect() as conn:
        assert summary.run_due(conn, datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)) is None
    admin.patch("/api/v1/settings", json={"summary_mail": True})
    smtp.inbox.clear()
    with connect() as conn:
        assert summary.run_due(conn, datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)) is None  # 5 Uhr Ortszeit
        r = summary.run_due(conn, datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc))
        assert r == {"period": "2026-06", "ok": True}
        assert summary.run_due(conn, datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc)) is None  # schon verschickt
        assert summary.run_due(conn, datetime(2026, 7, 9, 8, 0, tzinfo=timezone.utc)) is None  # nach dem 7.
    assert len(smtp.inbox) == 2
    log = admin.get("/api/v1/reports/mail-log").json()
    assert any(x.get("summary") and x["period"] == "2026-06" and x["status"] == "sent" for x in log)
    admin.patch("/api/v1/settings", json={"summary_mail": False})
