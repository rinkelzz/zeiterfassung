"""Konfiguration über Umgebungsvariablen.

Alle Werte haben sinnvolle Defaults, damit die App lokal ohne weitere
Einrichtung startet. Im Synology-Paket werden sie vom Start-Skript gesetzt.
"""
import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "ja", "on")


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("ZE_DATA_DIR", "./data")).resolve()
        self.db_path = self.data_dir / "zeiterfassung.db"
        # Zeitzone für Tagesgrenzen und Anzeige
        self.timezone = os.environ.get("ZE_TIMEZONE", "Europe/Berlin")
        # Öffentliche Adresse (Reverse Proxy) – landet im Einrichtungs-QR-Code
        # Leer = Adresse wird aus dem Aufruf abgeleitet (z. B. http://nas:8765)
        self.public_url = os.environ.get("ZE_PUBLIC_URL", "").strip().rstrip("/")
        # Unterpfad, falls die App hinter einem Proxy unter /zeiterfassung läuft
        self.root_path = os.environ.get("ZE_ROOT_PATH", "").rstrip("/")
        self.host = os.environ.get("ZE_HOST", "0.0.0.0")
        self.port = int(os.environ.get("ZE_PORT", "8765"))
        # Session-Cookie nur über HTTPS senden (hinter Reverse Proxy mit TLS: an)
        self.cookie_secure = _bool("ZE_COOKIE_SECURE", False)
        self.session_days = int(os.environ.get("ZE_SESSION_DAYS", "30"))
        # Ordner der DSM-Oberfläche (ui/), dort liegt app.json fürs DSM-Fenster
        self.ui_dir = os.environ.get("ZE_UI_DIR", "")
        # Automatischer Berichtsversand per Mail (Hintergrund-Thread); in Tests aus
        self.mail_scheduler = _bool("ZE_MAIL_SCHEDULER", True)
        # Optional: Startpasswort für den ersten Admin
        self.initial_admin_password = os.environ.get("ZE_ADMIN_PASSWORD")


settings = Settings()
