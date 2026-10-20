"""Anbindung an das DSM-Fenster.

DSM lädt beim Klick auf das Menü-Icon die Seite ui/index.html (same origin wie DSM).
Die liest ui/app.json und leitet in die eigentliche App weiter. Diese Datei schreibt
die App beim Start und wenn die öffentliche Adresse geändert wird.
"""
import json
import sqlite3
from pathlib import Path

from .config import settings
from .db import get_setting


def write_ui_config(conn: sqlite3.Connection) -> bool:
    if not settings.ui_dir:
        return False
    data = {
        "public_url": (get_setting(conn, "public_url") or settings.public_url or "").rstrip("/"),
        "port": settings.port,
        "root_path": settings.root_path,
    }
    try:
        target = Path(settings.ui_dir) / "app.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(target)
        return True
    except OSError as e:  # nicht schreibbar → DSM-Fenster nutzt die Rückfalllösung
        print(f"[zeiterfassung] ui/app.json nicht schreibbar: {e}")
        return False
