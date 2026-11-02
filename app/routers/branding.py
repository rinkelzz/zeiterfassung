"""Firmenname und Firmenlogo – für Weboberfläche, Kiosk-Tablet, PDF und Excel."""
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import logo
from ..db import audit, get_db, get_setting
from ..security import admin_user

router = APIRouter(prefix="/api/v1/branding", tags=["Firmenlogo"])


def branding(conn) -> dict:
    return {"company_name": get_setting(conn, "company_name") or "", "logo": logo.info(conn)}


@router.get("")
def get_branding(conn: sqlite3.Connection = Depends(get_db)):
    """Ohne Login – die Anmeldeseite und das Kiosk-Tablet zeigen Name und Logo."""
    return branding(conn)


@router.get("/logo")
def get_logo(conn: sqlite3.Connection = Depends(get_db)):
    got = logo.load(conn)
    if not got:
        return Response(status_code=404)
    raw, meta = got
    return Response(raw, media_type="image/jpeg" if meta.get("format") == "jpeg" else "image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


class LogoIn(BaseModel):
    data_url: str = Field(..., max_length=1_500_000, description="data:image/png;base64,… oder image/jpeg")


@router.put("/logo")
def put_logo(data: LogoIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    try:
        info = logo.save_data_url(conn, data.data_url)
    except logo.LogoError as e:
        raise HTTPException(400, str(e))
    audit(conn, admin["id"], "settings_updated", "logo", f"Firmenlogo hochgeladen ({info['width']}×{info['height']})")
    return branding(conn)


@router.delete("/logo")
def delete_logo(admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    logo.remove(conn)
    audit(conn, admin["id"], "settings_updated", "logo", "Firmenlogo entfernt")
    return branding(conn)


class NameIn(BaseModel):
    company_name: Optional[str] = Field(None, max_length=80)


@router.patch("")
def set_name(data: NameIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    name = (data.company_name or "").strip()
    conn.execute("INSERT INTO app_settings(key, value) VALUES ('company_name', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (name,))
    audit(conn, admin["id"], "settings_updated", "branding", f"Firmenname: {name or '–'}")
    return branding(conn)
