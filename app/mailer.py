"""Berichte per Mail: SMTP-Versand, Versandprotokoll und automatischer Zeitplan.

Zeitplan (wenn in den Einstellungen eingeschaltet):
- Monatsbericht: am 1. eines Monats ab 6 Uhr für den Vormonat
- Jahresbericht: am 1. Januar ab 6 Uhr für das Vorjahr
War die NAS zu dem Zeitpunkt aus, wird bis zum 7. des Monats nachgeholt.
Jeder Bericht geht pro Person nur einmal raus (Tabelle report_mails).
Fehlversuche werden höchstens 5-mal wiederholt, frühestens nach einer Stunde.
"""
import re
import smtplib
import sqlite3
import ssl
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Dict, List, Optional, Tuple

from .db import audit, connect, get_setting
from .report_files import build_data, parse_period, previous_month, render_data
from .timeutil import fmt_duration, now_utc, now_utc_str, parse_utc, to_local, to_utc_str

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
SEND_HOUR = 6
CATCH_UP_DAYS = 7
MAX_ATTEMPTS = 5
RETRY_AFTER = timedelta(hours=1)
FORMATS = ("pdf", "xlsx")


class MailError(Exception):
    pass


def valid_email(addr: str) -> bool:
    return bool(addr) and len(addr) <= 200 and bool(EMAIL_RE.match(addr))


def smtp_config(conn: sqlite3.Connection) -> Dict:
    port = get_setting(conn, "smtp_port") or "587"
    return {
        "host": get_setting(conn, "smtp_host").strip(),
        "port": int(port) if port.isdigit() else 587,
        "security": get_setting(conn, "smtp_security") or "starttls",
        "user": get_setting(conn, "smtp_user"),
        "password": get_setting(conn, "smtp_password"),
        "from": get_setting(conn, "smtp_from").strip() or get_setting(conn, "smtp_user").strip(),
        "from_name": get_setting(conn, "smtp_from_name") or "Zeiterfassung",
    }


def mail_formats(conn: sqlite3.Connection) -> List[str]:
    fm = [x for x in (get_setting(conn, "mail_formats") or "").split(",") if x in FORMATS]
    return fm or ["pdf"]


def send_mail(conn: sqlite3.Connection, to: str, subject: str, body: str,
              attachments: Optional[List[Tuple[str, bytes, str]]] = None) -> None:
    cfg = smtp_config(conn)
    if not cfg["host"]:
        raise MailError("Kein Mailserver eingetragen (Verwaltung → Einstellungen → Mailversand)")
    if not valid_email(cfg["from"]):
        raise MailError("Absenderadresse fehlt oder ist ungültig")
    if not valid_email(to):
        raise MailError(f"Ungültige Empfängeradresse: {to}")
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["from_name"], cfg["from"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=cfg["from"].split("@")[-1])
    msg.set_content(body)
    for name, data, mime in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    try:
        ctx = ssl.create_default_context()
        if cfg["security"] == "ssl":
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30, context=ctx)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        with server:
            server.ehlo()
            if cfg["security"] == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            if cfg["user"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise MailError("Anmeldung am Mailserver fehlgeschlagen – Benutzer/Passwort prüfen")
    except ssl.SSLError as e:
        raise MailError(f"Verschlüsselung fehlgeschlagen ({e.reason or e}) – Port und Verschlüsselung prüfen")
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"Mailversand fehlgeschlagen: {e}")


# --- Berichte -----------------------------------------------------------------

def _report_text(data: Dict) -> Tuple[str, str]:
    u = data["user"]
    t = data["report"]["totals"]
    eb = data["end_balance"]
    what = "Arbeitszeitnachweis" if data["kind"] == "month" else "Jahresübersicht"
    label = data["title"] if data["kind"] == "month" else str(data["from"].year)
    subject = f"{what} {label} – {u['display_name']}"
    stand = "heute" if data["end_is_today"] else f"{data['to']:%d.%m.%Y}"

    def s(v):
        return ("+" if v > 0 else "") + fmt_duration(v)

    lines = [
        f"Hallo {u['display_name']},",
        "",
        f"im Anhang findest du deine {what} für {label}.",
        "",
        f"Ist:        {t['worked']} h",
        f"Soll:       {t['target']} h",
        f"Differenz:  {s(t['diff_s'])} h",
        "",
        f"Gleitzeit-Konto (Stand {stand}):    {s(eb['gleitzeit_s'])} h",
        f"Überstunden-Konto (Stand {stand}):  {s(eb['ueberstunden_s'])} h",
    ]
    vac = data["vacation"]
    if vac:
        lines.append(f"Resturlaub {vac['year']}:  {str(vac['remaining']).replace('.0', '').replace('.', ',')} Tage")
    lines += ["", "Bitte prüfen – Korrekturen bitte an die Verwaltung.", "",
              "Diese Mail wurde automatisch von der Zeiterfassung verschickt."]
    return subject, "\n".join(lines)


def send_report(conn: sqlite3.Connection, user: sqlite3.Row, period: str,
                to: Optional[str] = None, actor_id: Optional[int] = None) -> str:
    """Bericht erzeugen und verschicken. Gibt die Empfängeradresse zurück."""
    parse_period(period)
    to = (to or user["email"] or "").strip()
    if not to:
        raise MailError(f"Für {user['display_name']} ist keine E-Mail-Adresse hinterlegt")
    data = build_data(conn, user, period)
    files = [render_data(data, fm) for fm in mail_formats(conn)]
    subject, body = _report_text(data)
    send_mail(conn, to, subject, body, [(name, content, mime) for content, name, mime in files])
    audit(conn, actor_id, "report_mailed", f"user:{user['id']}", f"{period} → {to}")
    return to


def _log(conn, user_id: int, period: str, ok: bool, to: str, error: str = "",
         when: Optional[datetime] = None) -> None:
    now = to_utc_str(when) if when else now_utc_str()
    conn.execute(
        "INSERT INTO report_mails(user_id, period, status, attempts, last_attempt, sent_at, recipient, error) "
        "VALUES (?, ?, ?, 1, ?, ?, ?, ?) ON CONFLICT(user_id, period) DO UPDATE SET "
        "status = excluded.status, attempts = report_mails.attempts + 1, last_attempt = excluded.last_attempt, "
        "sent_at = COALESCE(excluded.sent_at, report_mails.sent_at), recipient = excluded.recipient, error = excluded.error",
        (user_id, period, "sent" if ok else "error", now, now if ok else None, to, error[:500]),
    )


def due_periods(conn: sqlite3.Connection, now_local: datetime) -> List[str]:
    """Welche Berichte sind gerade fällig?"""
    if now_local.day > CATCH_UP_DAYS or (now_local.day == 1 and now_local.hour < SEND_HOUR):
        return []
    out = []
    if get_setting(conn, "mail_monthly") == "1":
        out.append(previous_month(now_local.date()))
    if get_setting(conn, "mail_yearly") == "1" and now_local.month == 1:
        out.append(str(now_local.year - 1))
    return out


def run_due(conn: sqlite3.Connection, now: Optional[datetime] = None) -> List[Dict]:
    """Fällige Berichte an alle Mitarbeiter mit Mailadresse schicken (einmal pro Bericht)."""
    now = now or now_utc()
    periods = due_periods(conn, to_local(now))
    if not periods or not smtp_config(conn)["host"]:
        return []
    results = []
    users = conn.execute(
        "SELECT * FROM users WHERE active = 1 AND report_mail = 1 AND email IS NOT NULL AND email != ''"
    ).fetchall()
    for period in periods:
        _, d_from, d_to = parse_period(period)
        for u in users:
            ts = u["tracking_start"]
            if ts and ts > d_to.isoformat():
                continue  # Zeitraum liegt komplett vor dem Erfassungsbeginn
            log = conn.execute("SELECT * FROM report_mails WHERE user_id = ? AND period = ?", (u["id"], period)).fetchone()
            if log and (log["status"] == "sent" or log["attempts"] >= MAX_ATTEMPTS):
                continue
            if log and log["last_attempt"] and parse_utc(log["last_attempt"]) > now - RETRY_AFTER:
                continue
            try:
                to = send_report(conn, u, period)
                _log(conn, u["id"], period, True, to, when=now)
                results.append({"user_id": u["id"], "period": period, "ok": True})
            except Exception as e:  # noqa: BLE001 – ein Fehler darf die anderen nicht aufhalten
                _log(conn, u["id"], period, False, u["email"], str(e), when=now)
                results.append({"user_id": u["id"], "period": period, "ok": False, "error": str(e)})
    return results


# --- Hintergrund-Thread -------------------------------------------------------------

_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _loop(interval: float) -> None:
    if _stop.wait(60):  # nach dem Start kurz warten
        return
    while not _stop.is_set():
        try:
            conn = connect()
            try:
                for r in run_due(conn):
                    status = "verschickt" if r["ok"] else f"FEHLER: {r.get('error')}"
                    print(f"[zeiterfassung] Bericht {r['period']} für Nutzer {r['user_id']}: {status}", flush=True)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            print(f"[zeiterfassung] Mail-Zeitplan: {e}", flush=True)
        _stop.wait(interval)


def start_scheduler(interval: float = 600) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(interval,), name="report-mails", daemon=True)
    _thread.start()


def stop_scheduler() -> None:
    _stop.set()
