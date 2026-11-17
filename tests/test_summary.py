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
        assert c.get("/api/v1/summary-lists").status_code == 403


@pytest.fixture(scope="module")
def branches(admin, staff):
    """Filiale Nord: Sumo · Filiale Süd: Svenja."""
    out = {}
    for name, uid in (("Nord", staff["sumo"]), ("Süd", staff["sumkrank"])):
        r = admin.post("/api/v1/branches", json={"name": f"Sum-{name}", "user_ids": [uid]})
        assert r.status_code == 201, r.text
        out[name] = [b for b in r.json() if b["name"] == f"Sum-{name}"][0]["id"]
    return out


def test_summary_list_validation(admin, branches):
    base = {"name": "Test", "recipients": "lohn@steuer.de"}
    assert admin.post("/api/v1/summary-lists", json={**base, "recipients": "kaputt"}).status_code == 400
    assert admin.post("/api/v1/summary-lists", json={**base, "recipients": "", "auto": True}).status_code == 400
    assert admin.post("/api/v1/summary-lists", json={**base, "all_users": False}).status_code == 400
    assert admin.post("/api/v1/summary-lists", json={**base, "all_users": False, "branch_ids": [99999]}).status_code == 400
    r = admin.post("/api/v1/summary-lists", json={**base, "recipients": "lohn@steuer.de; chef@firma.de, lohn@steuer.de",
                                                   "all_users": False, "branch_ids": [branches["Nord"]]})
    assert r.status_code == 201, r.text
    lst = r.json()
    assert lst["recipients"] == "lohn@steuer.de, chef@firma.de" and lst["scope"] == "Filiale Sum-Nord"
    r = admin.put(f"/api/v1/summary-lists/{lst['id']}", json={**base, "all_users": True, "branch_ids": [branches["Nord"]]})
    assert r.status_code == 200 and r.json()["scope"] == "Alle Mitarbeiter" and r.json()["branch_ids"] == []
    assert admin.delete(f"/api/v1/summary-lists/{lst['id']}").status_code == 200
    assert admin.delete(f"/api/v1/summary-lists/{lst['id']}").status_code == 404


def _names(content: bytes):
    rows = list(load_workbook(io.BytesIO(content))["Übersicht"].iter_rows(values_only=True))
    return {r[0] for r in rows[4:] if r[0] and r[0] != "Summe"}


def test_summary_download_by_branch_and_list(admin, staff, branches):
    r = admin.get("/api/v1/reports/summary", params={"period": "2026-06", "file": "xlsx", "branch_id": branches["Süd"]})
    assert r.status_code == 200 and _names(r.content) == {"Svenja Krank"}
    assert "Monatszusammenfassung_2026-06_Sum-Sued.xlsx" in r.headers["content-disposition"]
    r = admin.post("/api/v1/summary-lists", json={"name": "Chef", "recipients": "", "all_users": False,
                                                  "branch_ids": [branches["Nord"]], "user_ids": [staff["sumkrank"]]})
    lst = r.json()
    assert lst["scope"] == "Filiale Sum-Nord + 1 weiterer Mitarbeiter"
    r = admin.get("/api/v1/reports/summary", params={"period": "2026-06", "file": "xlsx", "list_id": lst["id"]})
    assert _names(r.content) == {"Sumo Splitter", "Svenja Krank"}
    assert admin.get("/api/v1/reports/summary", params={"period": "2026-06", "list_id": 99999}).status_code == 404
    admin.delete(f"/api/v1/summary-lists/{lst['id']}")


def test_summary_mail_per_list_and_schedule(admin, staff, branches, smtp):
    for x in admin.get("/api/v1/summary-lists").json():
        admin.delete(f"/api/v1/summary-lists/{x['id']}")
    assert admin.post("/api/v1/reports/summary/mail", json={"period": "2026-06"}).status_code == 400  # kein Verteiler
    nord = admin.post("/api/v1/summary-lists", json={"name": "Steuerbüro Nord", "recipients": "nord@steuer.de",
                                                     "all_users": False, "branch_ids": [branches["Nord"]]}).json()
    sued = admin.post("/api/v1/summary-lists", json={"name": "Steuerbüro Süd", "recipients": "sued@steuer.de, chef@firma.de",
                                                     "all_users": False, "branch_ids": [branches["Süd"]],
                                                     "with_reports": False}).json()
    smtp.inbox.clear()
    r = admin.post("/api/v1/reports/summary/mail", json={"period": "2026-06"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert sorted(m["msg"]["To"] for m in smtp.inbox) == ["chef@firma.de", "nord@steuer.de", "sued@steuer.de"]
    by_to = {m["msg"]["To"]: m["msg"] for m in smtp.inbox}
    nmsg = by_to["nord@steuer.de"]
    assert nmsg["Subject"].startswith("Monatszusammenfassung Juni 2026 (Filiale Sum-Nord)")
    files = {p.get_filename(): p.get_payload(decode=True) for p in nmsg.walk() if p.get_filename()}
    assert sorted(files) == ["Arbeitszeitnachweise_2026-06.pdf", "Monatszusammenfassung_2026-06.pdf",
                             "Monatszusammenfassung_2026-06.xlsx"]
    assert _names(files["Monatszusammenfassung_2026-06.xlsx"]) == {"Sumo Splitter"}
    smsg = by_to["sued@steuer.de"]
    sfiles = {p.get_filename(): p.get_payload(decode=True) for p in smsg.walk() if p.get_filename()}
    assert len(sfiles) == 2 and _names(sfiles["Monatszusammenfassung_2026-06.xlsx"]) == {"Svenja Krank"}
    body = [p for p in smsg.walk() if p.get_content_type() == "text/plain"][0].get_content()
    assert "noch nicht abgeschlossen" in body and "Svenja Krank" in body and "Sumo" not in body

    # nur ein Verteiler
    smtp.inbox.clear()
    r = admin.post("/api/v1/reports/summary/mail", json={"period": "2026-06", "list_id": nord["id"]})
    assert r.status_code == 200 and [m["msg"]["To"] for m in smtp.inbox] == ["nord@steuer.de"]

    # Zeitplan: nur Verteiler mit „automatisch“, je Verteiler einmal
    with connect() as conn:
        assert summary.run_due(conn, datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)) == []
    admin.put(f"/api/v1/summary-lists/{sued['id']}", json={"name": "Steuerbüro Süd", "recipients": "sued@steuer.de",
                                                           "all_users": False, "branch_ids": [branches["Süd"]],
                                                           "auto": True})
    smtp.inbox.clear()
    with connect() as conn:
        # Juni wurde oben schon von Hand an „Süd“ geschickt → kein zweites Mal
        assert summary.run_due(conn, datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)) == []
        assert summary.run_due(conn, datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc)) == []  # 5 Uhr Ortszeit
        r = summary.run_due(conn, datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc))
        assert r == [{"period": "2026-07", "list": "Steuerbüro Süd", "ok": True}]
        assert summary.run_due(conn, datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)) == []  # schon verschickt
    assert [m["msg"]["To"] for m in smtp.inbox] == ["sued@steuer.de"]
    log = admin.get("/api/v1/reports/mail-log").json()
    assert any(x.get("summary") and x["display_name"] == "Monatszusammenfassung (Steuerbüro Süd)" for x in log)
    for x in (nord, sued):
        admin.delete(f"/api/v1/summary-lists/{x['id']}")


def test_migration_from_single_recipient_setting(tmp_path):
    """0.31/0.32: eine Adressliste in den Einstellungen → wird zum Verteiler „Alle Mitarbeiter“."""
    import sqlite3
    from app import db
    path = tmp_path / "alt.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.executescript(db.SCHEMA)
    conn.execute("CREATE TABLE summary_mails (period TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL "
                 "DEFAULT 0, last_attempt TEXT, sent_at TEXT, recipient TEXT, error TEXT)")
    conn.execute("INSERT INTO summary_mails VALUES ('2026-10', 'sent', 1, '2026-11-01T06:00:00Z', "
                 "'2026-11-01T06:00:00Z', 'lohn@steuer.de', '')")
    for k, v in (("summary_recipients", "lohn@steuer.de"), ("summary_mail", "1"), ("summary_with_reports", "0")):
        conn.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES (?, ?)", (k, v))
    conn.execute("PRAGMA user_version = 20")
    db.migrate(conn)
    lst = [dict(r) for r in conn.execute("SELECT * FROM summary_lists")]
    assert len(lst) == 1 and lst[0]["recipients"] == "lohn@steuer.de" and lst[0]["auto"] == 1
    assert lst[0]["with_reports"] == 0 and lst[0]["all_users"] == 1
    log = conn.execute("SELECT * FROM summary_mail_log").fetchone()
    assert log["period"] == "2026-10" and log["list_id"] == lst[0]["id"] and log["status"] == "sent"
    assert not conn.execute("SELECT 1 FROM app_settings WHERE key LIKE 'summary_%'").fetchone()
