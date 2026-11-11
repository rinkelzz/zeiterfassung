"""Abwesenheiten als Kalender-Abo (iCalendar/ICS) – z. B. für Nextcloud, iPhone, Outlook, Thunderbird.

Zwei Arten von Links:
- persönlich: Wer den Abwesenheitskalender sehen darf, legt sich unter „Abwesenheiten → Kalender-Abo“ einen Link an.
  Er zeigt genau das, was diese Person auch in der App sieht (Admins alles, Vorgesetzte ihr Team,
  freigegebene Mitarbeiter ihre Filiale nur als „abwesend“). Verliert die Person das Recht, liefert der Link nichts mehr.
- Filiale (nur Admins): ein Link für alle Mitarbeiter einer Filiale (oder alle), etwa für einen gemeinsamen
  Team-Kalender in Nextcloud. Ob die Art (Urlaub, Krank …) sichtbar ist, legt der Admin fest.

Bei beiden ist wählbar, ob offene Anträge als „(beantragt)“ erscheinen. Der Link enthält einen geheimen Schlüssel
(in der Datenbank nur als Hash) und lässt sich jederzeit löschen. Nur lesend – eingetragen wird in der App.
"""
import hashlib
import secrets
import sqlite3
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..db import audit, get_db, get_setting
from ..reports import holidays_between, target_fn
from ..security import current_user
from ..timeutil import daterange, local_iso, now_utc, now_utc_str
from .auth import server_url
from .leave import _cells, can_see_calendar, visible_users

router = APIRouter(prefix="/api/v1/calendar", tags=["Kalender-Abo"])

PAST_DAYS = 92      # gut drei Monate zurück
FUTURE_DAYS = 400   # gut ein Jahr voraus
TOKEN_PREFIX = "zecal_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class FeedIn(BaseModel):
    kind: str = Field("personal", pattern=r"^(personal|branch)$", description="personal oder branch (nur Admins)")
    branch_id: Optional[int] = Field(None, description="nur bei branch: Filiale; leer = alle Mitarbeiter")
    include_pending: bool = Field(False, description="offene Anträge als „(beantragt)“ zeigen")
    show_kinds: bool = Field(True, description="nur bei branch: Art zeigen (sonst nur „abwesend“)")
    label: Optional[str] = Field(None, max_length=60)
    host_hint: Optional[str] = Field(None, max_length=255, description="Hostname aus dem Browser (für die Adresse)")


def _feed_out(r, url: Optional[str] = None) -> dict:
    out = {"id": r["id"], "kind": r["kind"], "branch_id": r["branch_id"], "branch": r["branch"],
           "include_pending": bool(r["include_pending"]), "show_kinds": bool(r["show_kinds"]),
           "label": r["label"], "owner": r["owner"], "owner_id": r["user_id"],
           "created_at": local_iso(r["created_at"]), "last_used_at": local_iso(r["last_used_at"])}
    if url:
        out["url"] = url
    return out


FEED_SQL = ("SELECT f.*, b.name AS branch, u.display_name AS owner FROM calendar_feeds f "
            "LEFT JOIN branches b ON b.id = f.branch_id JOIN users u ON u.id = f.user_id")


@router.get("/feeds")
def list_feeds(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Eigene Links; Admins sehen zusätzlich alle Filial-Links."""
    if user["is_admin"]:
        rows = conn.execute(FEED_SQL + " WHERE f.user_id = ? OR f.kind = 'branch' ORDER BY f.id", (user["id"],))
    else:
        rows = conn.execute(FEED_SQL + " WHERE f.user_id = ? AND f.kind = 'personal' ORDER BY f.id", (user["id"],))
    return [_feed_out(r) for r in rows]


@router.post("/feeds", status_code=201)
def create_feed(data: FeedIn, request: Request, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Neuen Abo-Link anlegen. Die Adresse wird nur jetzt angezeigt."""
    if data.kind == "branch":
        if not user["is_admin"]:
            raise HTTPException(403, "Filial-Links legen nur Admins an")
        if data.branch_id is not None and not conn.execute("SELECT 1 FROM branches WHERE id = ?", (data.branch_id,)).fetchone():
            raise HTTPException(404, "Filiale nicht gefunden")
    elif not can_see_calendar(conn, user):
        raise HTTPException(403, "Den Abwesenheitskalender sehen Admins, Vorgesetzte und freigegebene Mitarbeiter")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO calendar_feeds(token_hash, user_id, kind, branch_id, include_pending, show_kinds, label, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_hash(token), user["id"], data.kind, data.branch_id if data.kind == "branch" else None,
         int(data.include_pending), int(data.show_kinds if data.kind == "branch" else True),
         (data.label or "").strip() or None, now_utc_str()))
    row = conn.execute(FEED_SQL + " WHERE f.id = ?", (cur.lastrowid,)).fetchone()
    audit(conn, user["id"], "calendar_feed_created", f"feed:{cur.lastrowid}",
          "Filiale " + (row["branch"] or "alle") if data.kind == "branch" else "persönlich")
    base = server_url(request, conn, data.host_hint)
    return _feed_out(row, f"{base}/api/v1/calendar/{token}.ics")


@router.delete("/feeds/{feed_id}")
def delete_feed(feed_id: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM calendar_feeds WHERE id = ?", (feed_id,)).fetchone()
    if not row or (row["user_id"] != user["id"] and not (user["is_admin"] and row["kind"] == "branch")):
        raise HTTPException(404, "Abo-Link nicht gefunden")
    conn.execute("DELETE FROM calendar_feeds WHERE id = ?", (feed_id,))
    audit(conn, user["id"], "calendar_feed_deleted", f"feed:{feed_id}", None)
    return {"ok": True}


# --- ICS ------------------------------------------------------------------------------------

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Zeilen nach RFC 5545 auf 75 Bytes falten."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode())
            cur = b""
        cur += b
    parts.append(cur.decode())
    return "\r\n ".join(parts)


def _people(conn, feed):
    """→ (Mitarbeiter, IDs mit Details) für diesen Link."""
    if feed["kind"] == "branch":
        if feed["branch_id"] is None:
            rows = conn.execute("SELECT * FROM users WHERE active = 1").fetchall()
        else:
            rows = conn.execute("SELECT u.* FROM users u JOIN user_branches ub ON ub.user_id = u.id "
                                "WHERE u.active = 1 AND ub.branch_id = ?", (feed["branch_id"],)).fetchall()
        return rows, ({r["id"] for r in rows} if feed["show_kinds"] else set())
    owner = conn.execute("SELECT * FROM users WHERE id = ? AND active = 1", (feed["user_id"],)).fetchone()
    if not owner:
        raise HTTPException(404, "Kalender nicht gefunden")
    return visible_users(conn, owner)


def _events(conn, people, full, include_pending: bool, d_from: date, d_to: date):
    """Zusammenhängende Abwesenheiten je Mitarbeiter und Art zu Terminen zusammenfassen. Lücken, an denen der
    Mitarbeiter ohnehin nicht arbeitet (Wochenende, Teilzeit-frei, Feiertag), unterbrechen einen Termin nicht."""
    cells = _cells(conn, [p["id"] for p in people], d_from, d_to)
    holidays = holidays_between(conn, d_from, d_to)
    events = []
    for p in people:
        days = cells.get(p["id"], {})
        if p["id"] not in full:
            days = {d: {**c, "kind": "abwesend", "label": "abwesend"} for d, c in days.items() if c["status"] != "beantragt"}
        elif not include_pending:
            days = {d: c for d, c in days.items() if c["status"] != "beantragt"}
        if not days:
            continue
        tgt = target_fn(conn, p)
        free = lambda d: d in holidays or tgt(d) <= 0  # noqa: E731
        run = None
        for d in daterange(d_from, d_to + timedelta(days=1)):
            c = days.get(d.isoformat()) if d <= d_to else None
            key = (c["kind"], c["status"], c["half"]) if c else None
            if run and key == run["key"]:
                run["end"] = d
                continue
            if run and key is None and free(d) and d <= d_to:
                run["gap"] = run.get("gap", 0) + 1   # merken, aber Termin nicht beenden
                continue
            if run:
                events.append(run)
                run = None
            if c:
                run = {"key": key, "start": d, "end": d, "user": p, "label": c["label"]}
        if run:
            events.append(run)
    return events


@router.get("/{token}.ics", include_in_schema=False)
def ics(token: str, conn: sqlite3.Connection = Depends(get_db)):
    feed = conn.execute("SELECT * FROM calendar_feeds WHERE token_hash = ?", (_hash(token),)).fetchone()
    if not feed:
        raise HTTPException(404, "Kalender nicht gefunden")
    try:
        people, full = _people(conn, feed)
    except HTTPException:
        raise HTTPException(404, "Kalender nicht gefunden")   # Recht verloren → nichts mehr liefern
    conn.execute("UPDATE calendar_feeds SET last_used_at = ? WHERE id = ?", (now_utc_str(), feed["id"]))
    today = now_utc().date()
    d_from, d_to = today - timedelta(days=PAST_DAYS), today + timedelta(days=FUTURE_DAYS)
    company = get_setting(conn, "company_name") or "Zeiterfassung"
    name = f"Abwesenheiten – {company}"
    if feed["kind"] == "branch" and feed["branch_id"]:
        b = conn.execute("SELECT name FROM branches WHERE id = ?", (feed["branch_id"],)).fetchone()
        if b:
            name += f" – {b[0]}"
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//rinkelzz//Zeiterfassung//DE", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_esc(name)}", "X-WR-TIMEZONE:Europe/Berlin",
             "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for ev in _events(conn, people, full, bool(feed["include_pending"]), d_from, d_to):
        kind, status, half = ev["key"]
        title = f"{ev['user']['display_name']} – {ev['label']}"
        if half:
            title += " (½ Tag)"
        if status == "beantragt":
            title += " (beantragt)"
        uid = f"ze-{feed['id']}-{ev['user']['id']}-{ev['start'].isoformat()}-{kind}-{status}-{int(half)}@zeiterfassung"
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{ev['start'].strftime('%Y%m%d')}",
                  f"DTEND;VALUE=DATE:{(ev['end'] + timedelta(days=1)).strftime('%Y%m%d')}",
                  f"SUMMARY:{_esc(title)}", "TRANSP:TRANSPARENT",
                  "STATUS:TENTATIVE" if status == "beantragt" else "STATUS:CONFIRMED", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    body = "\r\n".join(_fold(x) for x in lines) + "\r\n"
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "no-store", "Content-Disposition": 'inline; filename="abwesenheiten.ics"'})
