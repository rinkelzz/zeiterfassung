"""Auswertungen, Konten, CSV-Export und App-Einstellungen."""
import csv
import io
import re
import sqlite3
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..config import settings as app_settings
from ..db import audit, get_db, get_setting
from ..mailer import FORMATS, MailError, send_mail, send_report, smtp_config, valid_email
from ..report_files import parse_period, render
from ..dsm import write_ui_config
from ..holidays import BUNDESLAENDER, holidays_for_year, parse_company_free_days
from ..reports import (
    ABSENCE_KINDS,
    BREAK_MODES,
    WEEKDAYS_DE,
    account_balance,
    break_rule,
    compute_report,
    vacation_account,
)
from ..security import admin_user, current_user
from ..timeutil import today_local
from .entries import default_range, resolve_user_id

router = APIRouter(prefix="/api/v1", tags=["Auswertungen"])


def _load_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Nutzer nicht gefunden")
    return row


@router.get("/reports/user")
def user_report(
    user_id: Optional[int] = None,
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    user=Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Tagesgenaue Auswertung eines Nutzers (Standard: ich selbst, aktueller Monat)
    plus Gleitzeit-, Überstunden- und Urlaubskonto."""
    uid = resolve_user_id(user, user_id)
    d_from, d_to = default_range(from_, to)
    target = _load_user(conn, uid)
    rep = compute_report(conn, target, d_from, d_to)
    rep["account"] = account_balance(conn, target)
    rep["vacation"] = vacation_account(conn, target, d_to.year)
    return rep


@router.get("/reports/year")
def year_report(
    user_id: Optional[int] = None,
    year: Optional[int] = Query(None, ge=1990, le=2100),
    user=Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Kompakte Jahresansicht für den Kalender."""
    uid = resolve_user_id(user, user_id)
    year = year or today_local().year
    target = _load_user(conn, uid)
    rep = compute_report(conn, target, date(year, 1, 1), date(year, 12, 31))
    keep = ("date", "absence", "absence_id", "absence_label", "half_day", "holiday", "workday",
            "worked_s", "worked", "before_start", "overtime_day")
    return {
        "year": year,
        "user": rep["user"],
        "days": [{k: d[k] for k in keep} for d in rep["days"]],
        "absence_days": rep["totals"]["absence_days"],
        "vacation": vacation_account(conn, target, year),
    }


@router.get("/holidays", tags=["Einstellungen"])
def list_holidays(
    year: Optional[int] = Query(None, ge=1990, le=2100),
    user=Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Feiertage und betriebsfreie Tage eines Jahres für das eingestellte Bundesland."""
    year = year or today_local().year
    bl = get_setting(conn, "bundesland")
    days = holidays_for_year(year, bl, get_setting(conn, "company_free_days"))
    return {
        "year": year,
        "bundesland": bl,
        "bundesland_name": BUNDESLAENDER.get(bl, bl),
        "days": [{"date": d.isoformat(), "name": n, "weekday": WEEKDAYS_DE[d.weekday()]} for d, n in days.items()],
    }


@router.get("/reports/overview")
def overview(
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    admin=Depends(admin_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Summen aller aktiven Mitarbeiter im Zeitraum – plus Kontostände und wer gerade da ist."""
    d_from, d_to = default_range(from_, to)
    present = {r["user_id"] for r in conn.execute("SELECT user_id FROM entries WHERE end_utc IS NULL")}
    rows = []
    for u in conn.execute("SELECT * FROM users WHERE active = 1 ORDER BY display_name").fetchall():
        rep = compute_report(conn, u, d_from, d_to)
        acc = account_balance(conn, u)
        vac = vacation_account(conn, u, today_local().year)
        rows.append({
            **rep["user"], "present": u["id"] in present, **rep["totals"],
            "balance": acc["balance"], "balance_s": acc["balance_s"],
            "account_gleitzeit": acc["gleitzeit"], "account_gleitzeit_s": acc["gleitzeit_s"],
            "account_ueberstunden": acc["ueberstunden"], "account_ueberstunden_s": acc["ueberstunden_s"],
            "vacation_remaining": vac["remaining"] if vac else None,
        })
    return {"from": d_from.isoformat(), "to": d_to.isoformat(), "users": rows}


def _num(v: float) -> str:
    return (f"{v:.1f}" if v % 1 else f"{int(v)}").replace(".", ",")


@router.get("/reports/export.csv")
def export_csv(
    user_id: Optional[int] = None,
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    user=Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """CSV mit Semikolon und BOM – öffnet sich in deutschem Excel direkt richtig."""
    uid = resolve_user_id(user, user_id)
    d_from, d_to = default_range(from_, to)
    target = _load_user(conn, uid)
    rep = compute_report(conn, target, d_from, d_to)
    acc = account_balance(conn, target)
    vac = vacation_account(conn, target, d_to.year)

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Mitarbeiter", target["display_name"]])
    w.writerow(["Zeitraum", f"{d_from:%d.%m.%Y} – {d_to:%d.%m.%Y}"])
    w.writerow([])
    w.writerow(["Datum", "Tag", "Kommen", "Gehen", "Pausenabzug", "Ist", "Soll", "Differenz", "Konto", "Abwesenheit", "davon Wegezeit"])
    for d in rep["days"]:
        w.writerow([
            date.fromisoformat(d["date"]).strftime("%d.%m.%Y"),
            d["weekday"],
            d["first_in"] or "",
            ("läuft" if d["running"] else d["last_out"]) or "",
            f"{d['break_deducted_s'] // 60} min" if d["break_deducted_s"] else "",
            d["worked"],
            d["target"],
            d["diff"],
            ("Überstunden" if d["account"] == "ueberstunden" else "Gleitzeit") if d["diff_s"] else "",
            d["absence_label"] or "",
            f"{d['travel_min']} min" if d["travel_min"] else "",
        ])
    t = rep["totals"]
    w.writerow([])
    w.writerow(["Summe", "", "", "", "", t["worked"], t["target"], t["diff"], "", ""])
    w.writerow(["davon Gleitzeit", "", "", "", "", "", "", t["gleitzeit"], "", ""])
    w.writerow(["davon Überstunden", "", "", "", "", "", "", t["ueberstunden"], "", ""])
    w.writerow([])
    w.writerow([f"Kontostand heute (seit {date.fromisoformat(acc['since']):%d.%m.%Y}, inkl. Übertrag)"])
    w.writerow(["Gleitzeit-Konto", acc["gleitzeit"]])
    w.writerow(["Überstunden-Konto", acc["ueberstunden"]])
    if vac:
        w.writerow([f"Urlaub {vac['year']}", f"Anspruch {_num(vac['entitlement'])}", f"Übertrag {_num(vac['carryover'])}",
                    f"genommen {_num(vac['taken'])}", f"geplant {_num(vac['planned'])}", f"Rest {_num(vac['remaining'])}"])

    audit(conn, user["id"], "export_csv", f"user:{uid}", f"{d_from} – {d_to}")
    filename = f"zeiterfassung_{target['username']}_{d_from}_{d_to}.csv"
    return Response(
        content="﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Berichte als Datei und per Mail -----------------------------------------

@router.get("/reports/download")
def download_report(
    period: str = Query(..., description="JJJJ-MM für einen Monat, JJJJ für ein Jahr"),
    format: str = Query("pdf", description="pdf oder xlsx"),
    user_id: Optional[int] = None,
    user=Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Monats- oder Jahresbericht als PDF oder Excel."""
    uid = resolve_user_id(user, user_id)
    target = _load_user(conn, uid)
    if format not in FORMATS:
        raise HTTPException(400, "Format muss pdf oder xlsx sein")
    try:
        body, name, mime = render(conn, target, period, format)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(conn, user["id"], "report_download", f"user:{uid}", f"{period} {format}")
    return Response(content=body, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


class MailReportIn(BaseModel):
    period: str = Field(..., description="JJJJ-MM oder JJJJ")
    user_id: Optional[int] = None


@router.post("/reports/mail")
def mail_report(data: MailReportIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Bericht sofort an die hinterlegte Adresse der Person schicken."""
    uid = resolve_user_id(user, data.user_id)
    target = _load_user(conn, uid)
    try:
        parse_period(data.period)
        to = send_report(conn, target, data.period, actor_id=user["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    except MailError as e:
        raise HTTPException(502 if smtp_config(conn)["host"] else 400, str(e))
    return {"ok": True, "to": to, "message": f"Bericht an {to} verschickt"}


@router.get("/reports/mail-log", tags=["Einstellungen"])
def mail_log(limit: int = Query(100, le=500), admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """Automatisch verschickte Berichte (und Fehlversuche)."""
    rows = conn.execute(
        "SELECT m.*, u.display_name FROM report_mails m JOIN users u ON u.id = m.user_id "
        "ORDER BY m.last_attempt DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


class MailTestIn(BaseModel):
    to: str = Field(..., max_length=200)


@router.post("/settings/mail-test", tags=["Einstellungen"])
def mail_test(data: MailTestIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    try:
        send_mail(conn, data.to.strip(), "Zeiterfassung: Testmail",
                  "Hallo,\n\nder Mailversand der Zeiterfassung funktioniert.\n\n"
                  "Monats- und Jahresberichte kommen ab jetzt an die Adressen der Mitarbeiter.")
    except MailError as e:
        raise HTTPException(502 if smtp_config(conn)["host"] else 400, str(e))
    audit(conn, admin["id"], "mail_test", "", data.to)
    return {"ok": True, "message": f"Testmail an {data.to.strip()} verschickt"}


# --- App-Einstellungen ------------------------------------------------------

class SettingsIn(BaseModel):
    bundesland: Optional[str] = None
    company_free_days: Optional[str] = Field(None, max_length=300, description="z. B. 12-24, 12-31")
    break_mode: Optional[str] = Field(None, description="none, arbzg oder fixed")
    break_minutes: Optional[int] = Field(None, ge=0, le=120)
    auto_break: Optional[bool] = Field(None, description="veraltet – entspricht break_mode arbzg/none")
    public_url: Optional[str] = Field(None, max_length=200, description="z. B. https://zeit.meinedomain.de – leer = automatisch")
    dsm_login: Optional[bool] = None
    dsm_auto_create: Optional[bool] = None
    dsm_admins: Optional[bool] = None
    dsm_url: Optional[str] = Field(None, max_length=200)
    travel_in_minutes: Optional[int] = Field(None, ge=0, le=120, description="Wegezeit beim Kommen (früher stempeln)")
    travel_out_minutes: Optional[int] = Field(None, ge=0, le=120, description="Wegezeit beim Gehen (später stempeln)")
    travel_sources: Optional[List[str]] = Field(None, description="Stempelwege mit Wegezeit: nfc, shortcut, web, kiosk, api")
    smtp_host: Optional[str] = Field(None, max_length=200)
    smtp_port: Optional[int] = Field(None, ge=1, le=65535)
    smtp_security: Optional[str] = Field(None, description="starttls, ssl oder none")
    smtp_user: Optional[str] = Field(None, max_length=200)
    smtp_password: Optional[str] = Field(None, max_length=200, description="leer lassen = unverändert")
    smtp_from: Optional[str] = Field(None, max_length=200)
    smtp_from_name: Optional[str] = Field(None, max_length=100)
    mail_monthly: Optional[bool] = None
    mail_yearly: Optional[bool] = None
    mail_formats: Optional[List[str]] = None
    open_limit_hours: Optional[float] = Field(None, ge=0, le=48, description="Erinnern nach X Stunden, 0 = aus")
    open_auto_close: Optional[bool] = Field(None, description="Um Mitternacht nach der Grenze automatisch beenden")


def _settings_out(conn) -> dict:
    rule = break_rule(conn)
    return {
        "bundesland": get_setting(conn, "bundesland"),
        "company_free_days": get_setting(conn, "company_free_days"),
        "break_mode": rule["mode"],
        "break_minutes": rule["minutes"],
        "auto_break": rule["mode"] == "arbzg",
        "bundeslaender": BUNDESLAENDER,
        "break_modes": BREAK_MODES,
        "absence_kinds": ABSENCE_KINDS,
        "public_url": get_setting(conn, "public_url"),
        "public_url_env": app_settings.public_url,
        "app_port": app_settings.port,
        "dsm_login": get_setting(conn, "dsm_login") == "1",
        "dsm_auto_create": get_setting(conn, "dsm_auto_create") == "1",
        "dsm_admins": get_setting(conn, "dsm_admins") == "1",
        "dsm_url": get_setting(conn, "dsm_url"),
        "travel_in_minutes": int(get_setting(conn, "travel_in_minutes") or 0),
        "travel_out_minutes": int(get_setting(conn, "travel_out_minutes") or 0),
        "travel_sources": [x for x in (get_setting(conn, "travel_sources") or "").split(",") if x],
        "smtp_host": get_setting(conn, "smtp_host"),
        "smtp_port": int(get_setting(conn, "smtp_port") or 587),
        "smtp_security": get_setting(conn, "smtp_security") or "starttls",
        "smtp_user": get_setting(conn, "smtp_user"),
        "smtp_password_set": bool(get_setting(conn, "smtp_password")),
        "smtp_from": get_setting(conn, "smtp_from"),
        "smtp_from_name": get_setting(conn, "smtp_from_name"),
        "mail_monthly": get_setting(conn, "mail_monthly") == "1",
        "mail_yearly": get_setting(conn, "mail_yearly") == "1",
        "mail_formats": [x for x in (get_setting(conn, "mail_formats") or "").split(",") if x],
        "open_limit_hours": float(get_setting(conn, "open_limit_hours") or 0),
        "open_auto_close": get_setting(conn, "open_auto_close") == "1",
    }


def _set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


@router.get("/settings", tags=["Einstellungen"])
def get_settings(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    out = _settings_out(conn)
    if not user["is_admin"]:  # Mailserver-Zugang geht nur Admins etwas an
        for k in [k for k in out if k.startswith("smtp_")]:
            out.pop(k)
    return out


@router.patch("/settings", tags=["Einstellungen"])
def update_settings(data: SettingsIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    changes = data.model_dump(exclude_unset=True)
    if "bundesland" in changes:
        bl = (data.bundesland or "").upper()
        if bl not in BUNDESLAENDER:
            raise HTTPException(400, "Unbekanntes Bundesland")
        _set(conn, "bundesland", bl)
    if "company_free_days" in changes:
        raw = data.company_free_days or ""
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        valid = parse_company_free_days(raw)
        if len(valid) != len(parts):
            raise HTTPException(400, "Betriebsfreie Tage im Format MM-TT angeben, z. B. 12-24, 12-31")
        _set(conn, "company_free_days", ", ".join(f"{m:02d}-{d:02d}" for m, d in valid))
    if data.auto_break is not None and data.break_mode is None:
        _set(conn, "break_mode", "arbzg" if data.auto_break else "none")
    if data.break_mode is not None:
        if data.break_mode not in BREAK_MODES:
            raise HTTPException(400, "Pausenmodus muss none, arbzg oder fixed sein")
        _set(conn, "break_mode", data.break_mode)
    if data.break_minutes is not None:
        _set(conn, "break_minutes", str(data.break_minutes))
    for key in ("travel_in_minutes", "travel_out_minutes"):
        if changes.get(key) is not None:
            _set(conn, key, str(changes[key]))
    if data.travel_sources is not None:
        allowed = ("nfc", "shortcut", "web", "kiosk", "api")
        bad = [x for x in data.travel_sources if x not in allowed]
        if bad:
            raise HTTPException(400, f"Unbekannter Stempelweg: {', '.join(bad)}")
        _set(conn, "travel_sources", ",".join(dict.fromkeys(data.travel_sources)))
    for key in ("dsm_login", "dsm_auto_create", "dsm_admins"):
        if changes.get(key) is not None:
            _set(conn, key, "1" if changes[key] else "0")
    if changes.get("dsm_url"):
        url = data.dsm_url.strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+$", url):
            raise HTTPException(400, "DSM-Adresse z. B. http://127.0.0.1:5000")
        _set(conn, "dsm_url", url)
    for key in ("smtp_host", "smtp_user", "smtp_from_name"):
        if key in changes:
            _set(conn, key, (changes[key] or "").strip())
    if "smtp_from" in changes:
        addr = (data.smtp_from or "").strip()
        if addr and not valid_email(addr):
            raise HTTPException(400, "Absenderadresse ist ungültig")
        _set(conn, "smtp_from", addr)
    if changes.get("smtp_port") is not None:
        _set(conn, "smtp_port", str(data.smtp_port))
    if changes.get("smtp_security") is not None:
        if data.smtp_security not in ("starttls", "ssl", "none"):
            raise HTTPException(400, "Verschlüsselung muss starttls, ssl oder none sein")
        _set(conn, "smtp_security", data.smtp_security)
    if data.smtp_password:  # leer = unverändert
        _set(conn, "smtp_password", data.smtp_password)
    if changes.get("open_limit_hours") is not None:
        _set(conn, "open_limit_hours", f"{data.open_limit_hours:g}")
    if changes.get("open_auto_close") is not None:
        _set(conn, "open_auto_close", "1" if data.open_auto_close else "0")
    for key in ("mail_monthly", "mail_yearly"):
        if changes.get(key) is not None:
            _set(conn, key, "1" if changes[key] else "0")
    if data.mail_formats is not None:
        fm = [x for x in dict.fromkeys(data.mail_formats) if x in FORMATS]
        if not fm:
            raise HTTPException(400, "Mindestens ein Anhang (pdf oder xlsx) auswählen")
        _set(conn, "mail_formats", ",".join(fm))
    if "public_url" in changes:
        url = (data.public_url or "").strip().rstrip("/")
        if url and not re.match(r"^https?://[^\s/]+(/[^\s]*)?$", url):
            raise HTTPException(400, "Adresse muss mit http:// oder https:// beginnen, z. B. https://zeit.meinedomain.de")
        _set(conn, "public_url", url)
        write_ui_config(conn)
    if changes:
        logged = {k: ("***" if k == "smtp_password" else v) for k, v in changes.items()}
        audit(conn, admin["id"], "settings_updated", ",".join(sorted(changes)), str(logged))
    return _settings_out(conn)


@router.get("/audit", tags=["Einstellungen"])
def audit_log(limit: int = Query(200, le=1000), admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """Protokoll aller Änderungen – wichtig für nachvollziehbare Arbeitszeitnachweise."""
    rows = conn.execute(
        "SELECT a.*, u.display_name AS actor FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id "
        "ORDER BY a.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
