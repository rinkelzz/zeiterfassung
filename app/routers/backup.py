"""Datensicherung: Einstellungen, Liste, Sicherung jetzt, Download, Wiederherstellen, Löschen (nur Admins)."""
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import backup
from ..db import audit, get_db, get_setting
from ..security import admin_user

router = APIRouter(prefix="/api/v1/backups", tags=["Datensicherung (Admin)"])


def _set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _overview(conn) -> dict:
    return {
        "enabled": get_setting(conn, "backup_enabled") == "1",
        "hour": int(get_setting(conn, "backup_hour") or 2),
        "keep_days": int(get_setting(conn, "backup_keep_days") or 30),
        "dir": get_setting(conn, "backup_dir"),
        "local_dir": str(backup.local_dir()),
        "local_keep": backup.LOCAL_KEEP,
        "last": get_setting(conn, "backup_last") or None,
        "last_status": get_setting(conn, "backup_last_status") or None,
        "backups": backup.list_backups(conn),
    }


@router.get("")
def overview(admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    return _overview(conn)


class BackupSettingsIn(BaseModel):
    enabled: Optional[bool] = None
    hour: Optional[int] = Field(None, ge=0, le=23)
    keep_days: Optional[int] = Field(None, ge=1, le=3650)
    dir: Optional[str] = Field(None, max_length=300, description="Freigegebener Ordner, leer = nur lokal")


@router.patch("/settings")
def update_settings(data: BackupSettingsIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    changes = data.model_dump(exclude_unset=True)
    if "dir" in changes:
        d = (data.dir or "").strip().rstrip("/")
        if d:
            try:
                d = str(backup.check_dir(d))
            except backup.BackupError as e:
                raise HTTPException(400, str(e))
        _set(conn, "backup_dir", d)
    if data.enabled is not None:
        _set(conn, "backup_enabled", "1" if data.enabled else "0")
    if data.hour is not None:
        _set(conn, "backup_hour", str(data.hour))
    if data.keep_days is not None:
        _set(conn, "backup_keep_days", str(data.keep_days))
    if changes:
        audit(conn, admin["id"], "settings_updated", "backup", str(changes))
    return _overview(conn)


@router.post("")
def create_now(admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    try:
        r = backup.create("manuell", admin["id"])
    except (backup.BackupError, OSError, sqlite3.Error) as e:
        raise HTTPException(500, f"Sicherung fehlgeschlagen: {e}")
    return {**r, **_overview(conn)}


@router.get("/{name}/download")
def download(name: str, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    path = backup.find(conn, name)
    if not path:
        raise HTTPException(404, "Sicherung nicht gefunden")
    audit(conn, admin["id"], "backup_download", name)
    return FileResponse(str(path), media_type="application/gzip", filename=name)


class RestoreIn(BaseModel):
    confirm: str = Field(..., description="Zur Sicherheit noch einmal den Namen der Sicherung")


@router.post("/{name}/restore")
def restore(name: str, data: RestoreIn, admin=Depends(admin_user)):
    if data.confirm != name:
        raise HTTPException(400, "Bestätigung stimmt nicht mit dem Namen der Sicherung überein")
    try:
        return backup.restore(name, admin["id"])
    except backup.BackupError as e:
        raise HTTPException(400, str(e))


class DeleteIn(BaseModel):
    names: List[str] = Field(..., min_length=1, max_length=500, description="Dateinamen der Sicherungen")


@router.post("/delete")
def delete_backups(data: DeleteIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """Eine oder mehrere Sicherungen löschen (lokal und im freigegebenen Ordner). Die neueste bleibt geschützt."""
    with backup._lock:
        result = backup.delete(conn, data.names, admin["id"])
    if not result["deleted"] and result["errors"]:
        raise HTTPException(400, result["errors"][0]["error"])
    return {**result, **_overview(conn)}
