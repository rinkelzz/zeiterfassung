"""Datensicherung der SQLite-Datenbank.

- Jede Nacht (Standard 2 Uhr) eine Sicherung, geprüft mit PRAGMA integrity_check, gzip-komprimiert.
- Immer im Paketordner (data_dir/backups, die letzten LOCAL_KEEP Stück) und – wenn eingestellt –
  zusätzlich in einem freigegebenen Ordner (z. B. /volume1/backup/zeiterfassung), dort
  backup_keep_days Tage lang. Den freigegebenen Ordner kann Hyper Backup mitsichern.
- Gesichert wird über die Online-Backup-API von SQLite: konsistent, auch während jemand stempelt.
- Wiederherstellen schreibt die Sicherung über dieselbe API in die laufende Datenbank zurück
  (vorher wird automatisch der aktuelle Stand gesichert) und bringt das Schema auf den neuesten Stand.
"""
import gzip
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .config import settings
from .db import audit, connect, get_setting, migrate
from .timeutil import now_utc, to_local

LOCAL_KEEP = 7
NAME_RE = re.compile(r"^zeiterfassung_(\d{4}-\d{2}-\d{2})_(\d{4})(?:_([a-z-]+))?\.db\.gz$")
_lock = threading.Lock()


class BackupError(Exception):
    pass


def local_dir() -> Path:
    return settings.data_dir / "backups"


def shared_dir(conn: sqlite3.Connection) -> Optional[Path]:
    p = (get_setting(conn, "backup_dir") or "").strip()
    return Path(p) if p else None


def check_dir(path: str) -> Path:
    """Prüft, ob die App in den Ordner schreiben darf (legt ihn bei Bedarf an)."""
    p = Path(path.strip())
    if not p.is_absolute() or ".." in p.parts:
        raise BackupError("Bitte einen vollständigen Pfad angeben, z. B. /volume1/backup/zeiterfassung")
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".schreibtest"
        probe.write_text("ok")
        probe.unlink()
    except PermissionError:
        raise BackupError(
            f"Keine Schreibrechte für {p}. In DSM: Systemsteuerung → Freigegebener Ordner → Ordner bearbeiten → "
            "Berechtigungen → oben „System-interner Benutzer“ wählen → „zeiterfassung“ Lesen/Schreiben geben.")
    except OSError as e:
        raise BackupError(f"Ordner nicht nutzbar: {e}")
    return p


def _snapshot(dest_gz: Path) -> int:
    """Konsistente Kopie der laufenden DB, prüfen, gzip schreiben. Gibt die Größe zurück."""
    with tempfile.TemporaryDirectory(dir=str(settings.data_dir)) as tmp:
        raw = Path(tmp) / "snap.db"
        src = connect()
        dst = sqlite3.connect(str(raw))
        try:
            src.backup(dst)
        finally:
            src.close()
        try:
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
        if ok != "ok":
            raise BackupError(f"Integritätsprüfung fehlgeschlagen: {ok}")
        part = dest_gz.with_name(dest_gz.name + ".part")
        with open(raw, "rb") as f, gzip.open(part, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g)
        os.chmod(part, 0o640)
        part.replace(dest_gz)
    return dest_gz.stat().st_size


def _prune(folder: Path, keep_count: Optional[int] = None, keep_days: Optional[int] = None) -> None:
    files = sorted((f for f in folder.glob("zeiterfassung_*.db.gz") if NAME_RE.match(f.name)), key=lambda f: f.name)
    regular = [f for f in files if not NAME_RE.match(f.name).group(3)]  # Sonder-Sicherungen nicht zählen
    doomed = []
    if keep_count is not None and len(regular) > keep_count:
        doomed += regular[:-keep_count]
    if keep_days is not None:
        limit = (to_local(now_utc()) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        doomed += [f for f in files if NAME_RE.match(f.name).group(1) < limit]
    for f in set(doomed):
        try:
            f.unlink()
        except OSError:
            pass


def create(reason: str = "", actor_id: Optional[int] = None) -> Dict:
    """Sicherung jetzt erstellen. reason z. B. 'manuell' oder 'vor-wiederherstellung'."""
    with _lock:
        stamp = to_local(now_utc()).strftime("%Y-%m-%d_%H%M")
        name = f"zeiterfassung_{stamp}{'_' + reason if reason else ''}.db.gz"
        ld = local_dir()
        ld.mkdir(parents=True, exist_ok=True)
        size = _snapshot(ld / name)
        _prune(ld, keep_count=LOCAL_KEEP)
        result = {"name": name, "size": size, "local": True, "shared": None, "shared_error": None}
        conn = connect()
        try:
            sd = shared_dir(conn)
            keep = int(get_setting(conn, "backup_keep_days") or 30)
            if sd:
                try:
                    target = check_dir(str(sd))
                    shutil.copyfile(ld / name, target / (name + ".part"))
                    (target / (name + ".part")).replace(target / name)
                    _prune(target, keep_days=keep)
                    result["shared"] = str(target / name)
                except (BackupError, OSError) as e:
                    result["shared_error"] = str(e)
            status = "ok" if not result["shared_error"] else "teilweise"
            for key, value in (("backup_last", to_local(now_utc()).isoformat(timespec="minutes")),
                               ("backup_last_status", status if not result["shared_error"] else result["shared_error"][:300])):
                conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            audit(conn, actor_id, "backup_created", name, result["shared"] or result["shared_error"] or "nur lokal")
        finally:
            conn.close()
        return result


def list_backups(conn: sqlite3.Connection) -> List[Dict]:
    out = {}
    folders = [("lokal", local_dir())]
    sd = shared_dir(conn)
    if sd:
        folders.append(("ordner", sd))
    for where, folder in folders:
        try:
            files = list(folder.glob("zeiterfassung_*.db.gz"))
        except OSError:
            continue
        for f in files:
            m = NAME_RE.match(f.name)
            if not m:
                continue
            e = out.setdefault(f.name, {"name": f.name, "date": m.group(1), "time": m.group(2)[:2] + ":" + m.group(2)[2:],
                                        "reason": m.group(3) or "", "size": f.stat().st_size, "where": []})
            e["where"].append(where)
    return sorted(out.values(), key=lambda e: e["name"], reverse=True)


def find(conn: sqlite3.Connection, name: str) -> Optional[Path]:
    if not NAME_RE.match(name or ""):
        return None
    for folder in (local_dir(), shared_dir(conn)):
        if folder and (folder / name).is_file():
            return folder / name
    return None


def restore(name: str, actor_id: Optional[int] = None) -> Dict:
    """Sicherung zurückspielen. Vorher wird der aktuelle Stand gesichert."""
    conn = connect()
    try:
        path = find(conn, name)
    finally:
        conn.close()
    if not path:
        raise BackupError("Sicherung nicht gefunden")
    with tempfile.TemporaryDirectory(dir=str(settings.data_dir)) as tmp:
        raw = Path(tmp) / "restore.db"
        try:
            with gzip.open(path, "rb") as g, open(raw, "wb") as f:
                shutil.copyfileobj(g, f)
            check = sqlite3.connect(str(raw))
            try:
                ok = check.execute("PRAGMA integrity_check").fetchone()[0]
                check.execute("SELECT COUNT(*) FROM users").fetchone()
            finally:
                check.close()
        except (OSError, sqlite3.DatabaseError) as e:
            raise BackupError(f"Sicherung ist beschädigt: {e}")
        if ok != "ok":
            raise BackupError(f"Sicherung ist beschädigt: {ok}")
        before = create("vor-wiederherstellung", actor_id)
        with _lock:
            src = sqlite3.connect(str(raw))
            live = connect()
            try:
                src.backup(live)
                live.row_factory = sqlite3.Row
                migrate(live)  # ältere Sicherung → aktuelles Schema
                audit(live, actor_id, "backup_restored", name, f"vorher gesichert als {before['name']}")
            finally:
                src.close()
                live.close()
    return {"restored": name, "safety_backup": before["name"]}


def due(conn: sqlite3.Connection, now_local: datetime) -> bool:
    if get_setting(conn, "backup_enabled") != "1":
        return False
    hour = int(get_setting(conn, "backup_hour") or 2)
    if now_local.hour < hour:
        return False
    last = get_setting(conn, "backup_last_auto") or ""
    return last[:10] != now_local.strftime("%Y-%m-%d")


def run_due() -> Optional[Dict]:
    conn = connect()
    try:
        if not due(conn, to_local(now_utc())):
            return None
    finally:
        conn.close()
    result = create()
    conn = connect()
    try:
        conn.execute("INSERT INTO app_settings(key, value) VALUES ('backup_last_auto', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (to_local(now_utc()).isoformat(timespec="minutes"),))
    finally:
        conn.close()
    return result
