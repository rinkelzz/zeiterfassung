"""Nutzerverwaltung für Admins."""
import sqlite3
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..db import audit, get_db
from ..security import admin_user, hash_password
from ..reports import FOREVER, schedule_on, schedules, sync_current_schedule
from ..timeutil import now_utc_str, today_local
from .auth import TokenIn, issue_token_with_qr, list_tokens, user_out

router = APIRouter(prefix="/api/v1/users", tags=["Nutzerverwaltung (Admin)"])


def _check_workdays(v):
    if v is None:
        return v
    if not v or any(d < 1 or d > 7 for d in v):
        raise ValueError("Arbeitstage als ISO-Wochentage 1 (Mo) bis 7 (So)")
    return sorted(set(v))


def _check_email(v):
    from ..mailer import valid_email
    v = (v or "").strip()
    if v and not valid_email(v):
        raise ValueError("Ungültige E-Mail-Adresse")
    return v or None


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8)
    is_admin: bool = False
    weekly_hours: float = Field(40, ge=0, le=80)
    workdays: List[int] = [1, 2, 3, 4, 5]
    tracking_start: Optional[date] = Field(None, description="Erfassungsbeginn, Standard: heute")
    carryover_minutes: int = Field(0, ge=-100000, le=100000, description="Übertrag Gleitzeit in Minuten")
    carryover_overtime_minutes: int = Field(0, ge=-100000, le=100000, description="Übertrag Überstunden in Minuten")
    vacation_days_per_year: float = Field(30, ge=0, le=60)
    carryover_vacation: float = Field(0, ge=-60, le=200, description="Resturlaub aus dem Vorjahr (Tage, ½ möglich)")
    email: Optional[str] = Field(None, max_length=200, description="für Monats-/Jahresberichte per Mail")
    report_mail: bool = True

    @field_validator("email")
    @classmethod
    def _em(cls, v):
        return _check_email(v)

    @field_validator("workdays")
    @classmethod
    def _wd(cls, v):
        return _check_workdays(v)


class UserUpdate(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=80)
    password: Optional[str] = Field(None, min_length=8)
    is_admin: Optional[bool] = None
    active: Optional[bool] = None
    weekly_hours: Optional[float] = Field(None, ge=0, le=80)
    workdays: Optional[List[int]] = None
    tracking_start: Optional[date] = None
    carryover_minutes: Optional[int] = Field(None, ge=-100000, le=100000)
    carryover_overtime_minutes: Optional[int] = Field(None, ge=-100000, le=100000)
    vacation_days_per_year: Optional[float] = Field(None, ge=0, le=60)
    carryover_vacation: Optional[float] = Field(None, ge=-60, le=200)
    email: Optional[str] = Field(None, max_length=200)
    report_mail: Optional[bool] = None

    @field_validator("email")
    @classmethod
    def _em(cls, v):
        return _check_email(v)

    @field_validator("workdays")
    @classmethod
    def _wd(cls, v):
        return _check_workdays(v)


def _half(v: float) -> float:
    """Urlaubstage auf halbe Tage runden."""
    return round(v * 2) / 2


def _get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Nutzer nicht gefunden")
    return row


def _active_admins(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1 AND active = 1").fetchone()[0]


@router.get("")
def list_users(include_inactive: bool = False, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    sql = "SELECT * FROM users" + ("" if include_inactive else " WHERE active = 1") + " ORDER BY display_name"
    return [user_out(r) for r in conn.execute(sql).fetchall()]


@router.post("", status_code=201)
def create_user(data: UserCreate, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    try:
        cur = conn.execute(
            "INSERT INTO users(username, display_name, password_hash, is_admin, weekly_hours, workdays, created_at, "
            "tracking_start, carryover_minutes, carryover_overtime_minutes, vacation_days_per_year, carryover_vacation, "
            "email, report_mail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.username,
                data.display_name,
                hash_password(data.password),
                int(data.is_admin),
                data.weekly_hours,
                ",".join(str(d) for d in data.workdays),
                now_utc_str(),
                (data.tracking_start or today_local()).isoformat(),
                data.carryover_minutes,
                data.carryover_overtime_minutes,
                data.vacation_days_per_year,
                _half(data.carryover_vacation),
                data.email,
                int(data.report_mail),
            ),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Benutzername ist schon vergeben")
    conn.execute(
        "INSERT INTO work_schedules(user_id, valid_from, weekly_hours, workdays, created_at) VALUES (?, ?, ?, ?, ?)",
        (cur.lastrowid, FOREVER, data.weekly_hours, ",".join(str(d) for d in data.workdays), now_utc_str()),
    )
    audit(conn, admin["id"], "user_created", f"user:{cur.lastrowid}", data.username)
    return user_out(_get_user(conn, cur.lastrowid))


@router.get("/{user_id}")
def get_user(user_id: int, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    return user_out(_get_user(conn, user_id))


@router.patch("/{user_id}")
def update_user(user_id: int, data: UserUpdate, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    user = _get_user(conn, user_id)
    changes = data.model_dump(exclude_unset=True)

    # Den letzten aktiven Admin nicht aussperren
    loses_admin = changes.get("is_admin") is False or changes.get("active") is False
    if loses_admin and user["is_admin"] and user["active"] and _active_admins(conn) <= 1:
        raise HTTPException(400, "Es muss mindestens ein aktiver Admin bleiben")

    # Wochenstunden/Arbeitstage ohne Datum ändern den heute gültigen Eintrag im Verlauf.
    # Für eine Änderung „ab Datum“ gibt es POST /users/{id}/schedules.
    if "weekly_hours" in changes or "workdays" in changes:
        cur_s = schedule_on(conn, user_id, today_local()) or conn.execute(
            "SELECT * FROM work_schedules WHERE user_id = ? ORDER BY valid_from LIMIT 1", (user_id,)).fetchone()
        hours = changes.get("weekly_hours", cur_s["weekly_hours"] if cur_s else user["weekly_hours"])
        days = ",".join(str(d) for d in changes["workdays"]) if "workdays" in changes else (
            cur_s["workdays"] if cur_s else user["workdays"])
        if cur_s:
            conn.execute("UPDATE work_schedules SET weekly_hours = ?, workdays = ? WHERE id = ?", (hours, days, cur_s["id"]))
        else:
            conn.execute("INSERT INTO work_schedules(user_id, valid_from, weekly_hours, workdays, created_at) "
                         "VALUES (?, ?, ?, ?, ?)", (user_id, FOREVER, hours, days, now_utc_str()))

    fields, values = [], []
    for key, value in changes.items():
        if key == "password":
            fields.append("password_hash = ?")
            values.append(hash_password(value))
        elif key == "workdays":
            fields.append("workdays = ?")
            values.append(",".join(str(d) for d in value))
        elif key == "carryover_vacation":
            fields.append("carryover_vacation = ?")
            values.append(_half(value))
        elif key == "tracking_start":
            if value is None:
                raise HTTPException(400, "Erfassungsbeginn darf nicht leer sein")
            fields.append("tracking_start = ?")
            values.append(value.isoformat())
        elif key in ("is_admin", "active", "report_mail"):
            fields.append(f"{key} = ?")
            values.append(int(value))
        else:
            fields.append(f"{key} = ?")
            values.append(value)
    if fields:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", (*values, user_id))
        if "password" in changes or changes.get("active") is False:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        audit(conn, admin["id"], "user_updated", f"user:{user_id}", ",".join(sorted(changes)))
    return user_out(_get_user(conn, user_id))


@router.delete("/{user_id}")
def deactivate_user(user_id: int, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """Deaktiviert statt löscht – Arbeitszeitnachweise müssen erhalten bleiben."""
    return update_user(user_id, UserUpdate(active=False), admin, conn)


@router.get("/{user_id}/tokens")
def user_tokens(user_id: int, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    _get_user(conn, user_id)
    return list_tokens(conn, user_id)


@router.post("/{user_id}/tokens")
def create_user_token(user_id: int, data: TokenIn, request: Request, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """Admin erstellt Token + QR-Code für einen Mitarbeiter."""
    user = _get_user(conn, user_id)
    return issue_token_with_qr(conn, user, data, admin["id"], request)


@router.delete("/{user_id}/tokens/{token_id}")
def delete_user_token(user_id: int, token_id: int, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    cur = conn.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, user_id))
    if cur.rowcount == 0:
        raise HTTPException(404, "Token nicht gefunden")
    audit(conn, admin["id"], "token_deleted", f"user:{user_id}", str(token_id))
    return {"ok": True}


# --- Wochenarbeitszeit mit „gilt ab“ ------------------------------------------

class ScheduleIn(BaseModel):
    valid_from: date = Field(description="gilt ab diesem Tag")
    weekly_hours: float = Field(ge=0, le=80)
    workdays: List[int] = [1, 2, 3, 4, 5]

    @field_validator("workdays")
    @classmethod
    def _wd(cls, v):
        return _check_workdays(v)


def _schedule_out(r: sqlite3.Row, current_id: Optional[int]) -> dict:
    return {
        "id": r["id"],
        "valid_from": None if r["valid_from"] == FOREVER else r["valid_from"],
        "weekly_hours": r["weekly_hours"],
        "workdays": [int(d) for d in r["workdays"].split(",") if d],
        "current": r["id"] == current_id,
    }


@router.get("/{user_id}/schedules")
def list_schedules(user_id: int, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    user = _get_user(conn, user_id)
    rows = schedules(conn, user_id)
    if not rows:  # z. B. automatisch angelegter DSM-Benutzer: Verlauf aus aktuellen Werten anlegen
        conn.execute("INSERT INTO work_schedules(user_id, valid_from, weekly_hours, workdays, created_at) VALUES (?, ?, ?, ?, ?)",
                     (user_id, FOREVER, user["weekly_hours"], user["workdays"], now_utc_str()))
        rows = schedules(conn, user_id)
    cur = schedule_on(conn, user_id, today_local())
    return [_schedule_out(r, cur["id"] if cur else rows[0]["id"]) for r in rows]


@router.post("/{user_id}/schedules", status_code=201)
def add_schedule(user_id: int, data: ScheduleIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """Neue Wochenarbeitszeit ab einem Datum. Gleiches Datum = Eintrag ersetzen."""
    list_schedules(user_id, admin, conn)  # stellt sicher, dass es einen Anfangseintrag gibt
    days = ",".join(str(d) for d in data.workdays)
    conn.execute(
        "INSERT INTO work_schedules(user_id, valid_from, weekly_hours, workdays, created_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, valid_from) DO UPDATE SET weekly_hours = excluded.weekly_hours, workdays = excluded.workdays",
        (user_id, data.valid_from.isoformat(), data.weekly_hours, days, now_utc_str()),
    )
    sync_current_schedule(conn, user_id)
    audit(conn, admin["id"], "schedule_set", f"user:{user_id}",
          f"ab {data.valid_from}: {data.weekly_hours} h, Tage {days}")
    return list_schedules(user_id, admin, conn)


@router.delete("/{user_id}/schedules/{schedule_id}")
def delete_schedule(user_id: int, schedule_id: int, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM work_schedules WHERE id = ? AND user_id = ?", (schedule_id, user_id)).fetchone()
    if not row:
        raise HTTPException(404, "Eintrag nicht gefunden")
    if row["valid_from"] == FOREVER:
        raise HTTPException(400, "Der Anfangseintrag kann nicht gelöscht werden – bitte stattdessen ändern")
    conn.execute("DELETE FROM work_schedules WHERE id = ?", (schedule_id,))
    sync_current_schedule(conn, user_id)
    audit(conn, admin["id"], "schedule_deleted", f"user:{user_id}", f"ab {row['valid_from']}: {row['weekly_hours']} h")
    return list_schedules(user_id, admin, conn)
