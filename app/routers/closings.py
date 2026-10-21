"""Monatsabschluss: abschließen (einzeln oder für alle bis zu einem Monat), wieder öffnen, Übersicht."""
import re
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import closing
from ..db import get_db
from ..security import admin_user, current_user
from ..timeutil import today_local
from .entries import resolve_user_id

router = APIRouter(prefix="/api/v1/closings", tags=["Monatsabschluss"])
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"


def _user(conn, uid: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        raise HTTPException(404, "Nutzer nicht gefunden")
    return row


def _status(conn, user) -> dict:
    last = closing.last_closing(conn, user["id"])
    return {
        "user_id": user["id"],
        "display_name": user["display_name"],
        "last_closed": closing.closing_out(last) if last else None,
        "next_month": closing.first_open_month(conn, user),
    }


@router.get("")
def month_status(
    user_id: Optional[int] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    user=Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Ist der Monat abgeschlossen? (für Monatsansicht) – plus Stand des Mitarbeiters."""
    uid = resolve_user_id(user, user_id)
    target = _user(conn, uid)
    out = _status(conn, target)
    if month:
        row = conn.execute("SELECT * FROM month_closings WHERE user_id = ? AND month = ?", (uid, month)).fetchone()
        out["month"] = month
        out["closed"] = closing.closing_out(row) if row else None
        if row and row["closed_by"]:
            by = conn.execute("SELECT display_name FROM users WHERE id = ?", (row["closed_by"],)).fetchone()
            out["closed"]["closed_by_name"] = by["display_name"] if by else None
        out["can_close"] = out["next_month"] == month and closing.month_end(month) < today_local()
        out["can_reopen"] = bool(row) and out["last_closed"]["month"] == month
    return out


@router.get("/overview")
def overview(admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute("SELECT * FROM users WHERE active = 1 ORDER BY display_name").fetchall()
    return [_status(conn, u) for u in rows]


class CloseIn(BaseModel):
    user_id: int
    month: str = Field(..., pattern=MONTH_RE)


@router.post("")
def close(data: CloseIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    return closing.close_month(conn, _user(conn, data.user_id), data.month, admin["id"])


class CloseAllIn(BaseModel):
    until: str = Field(..., pattern=MONTH_RE, description="alle offenen Monate bis einschließlich")


@router.post("/all")
def close_all(data: CloseAllIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """Für alle aktiven Mitarbeiter alle offenen Monate bis einschließlich `until` der Reihe nach abschließen."""
    results = []
    for u in conn.execute("SELECT * FROM users WHERE active = 1 ORDER BY display_name").fetchall():
        closed, error = [], None
        while True:
            nxt = closing.first_open_month(conn, u)
            if not nxt or nxt > data.until:
                break
            try:
                closing.close_month(conn, u, nxt, admin["id"])
                closed.append(nxt)
            except HTTPException as e:
                error = e.detail
                break
        results.append({"user_id": u["id"], "display_name": u["display_name"], "closed": closed, "error": error})
    return {"results": results}


class ReopenIn(BaseModel):
    user_id: int
    month: str = Field(..., pattern=MONTH_RE)
    reason: str = Field(..., max_length=300)


@router.post("/reopen")
def reopen(data: ReopenIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    closing.reopen_month(conn, _user(conn, data.user_id), data.month, data.reason, admin["id"])
    return {"ok": True}
