"""SQLite-Datenbank: Schema, Migrationen und Verbindung pro Request."""
import secrets
import sqlite3
from typing import Iterator

from .config import settings
from .timeutil import local_date_of, now_utc_str

SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    password_hash TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    weekly_hours  REAL NOT NULL DEFAULT 40,
    workdays      TEXT NOT NULL DEFAULT '1,2,3,4,5',   -- ISO-Wochentage, 1 = Montag
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   TEXT NOT NULL UNIQUE,
    label        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    start_utc   TEXT NOT NULL,
    end_utc     TEXT,
    source      TEXT NOT NULL DEFAULT 'web',   -- web, shortcut, kiosk, nfc, manual
    note        TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT,
    updated_by  INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_entries_user_start ON entries(user_id, start_utc);
-- höchstens eine offene Buchung pro Nutzer
CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_one_open ON entries(user_id) WHERE end_utc IS NULL;

CREATE TABLE IF NOT EXISTS absences (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day      TEXT NOT NULL,                    -- YYYY-MM-DD (lokal)
    kind     TEXT NOT NULL,                    -- urlaub, krank, feiertag, frei, sonstiges
    note     TEXT,
    UNIQUE(user_id, day)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor_id  INTEGER,
    action    TEXT NOT NULL,
    target    TEXT,
    details   TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    # Automatischer Pausenabzug nach ArbZG (>6h: 30 min, >9h: 45 min)
    "auto_break": "0",  # veraltet (bis 0.2.x), wird nach break_mode migriert
    # Pausenabzug: none | arbzg | fixed
    "break_mode": "none",
    "break_minutes": "30",
    # Öffentliche Adresse (Reverse Proxy) für QR-Code und DSM-Fenster; leer = automatisch
    "public_url": "",
    # Wegezeit: beim Kommen früher, beim Gehen später stempeln (Minuten) – nur für diese Stempelwege
    "travel_in_minutes": "0",
    "travel_out_minutes": "0",
    "travel_sources": "nfc",
    # Anmeldung mit DSM-Konten
    "dsm_login": "1",        # Login-Formular prüft auch DSM-Passwörter
    "dsm_auto_create": "1",  # unbekannte DSM-Benutzer beim ersten Login anlegen
    "dsm_admins": "1",       # DSM-Administratoren werden Admins der Zeiterfassung
    "dsm_url": "http://127.0.0.1:5000",
    # Mailversand der Berichte (SMTP)
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_security": "starttls",   # starttls | ssl | none
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_from_name": "Zeiterfassung",
    "mail_monthly": "0",           # am 1. des Monats ab 6 Uhr: Bericht für den Vormonat
    "mail_yearly": "0",            # am 1. Januar ab 6 Uhr: Jahresbericht fürs Vorjahr
    "mail_formats": "pdf,xlsx",    # Anhänge
    # Datensicherung: jede Nacht, lokal + optional in einen freigegebenen Ordner
    "backup_enabled": "1",
    "backup_hour": "2",
    "backup_keep_days": "30",
    "backup_dir": "",              # z. B. /volume1/backup/zeiterfassung
    # Bundesland für die Feiertage (Kürzel wie NW, BY, …)
    "bundesland": "NW",
    # Betriebsfreie Tage, jedes Jahr gleich: "12-24, 12-31"
    "company_free_days": "",
}


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    """Bringt ältere Datenbanken auf den aktuellen Stand (Daten bleiben erhalten)."""
    from .timeutil import local_date_of

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 2:
        cols = _columns(conn, "users")
        if "tracking_start" not in cols:
            # Ab wann Soll und Saldo zählen (lokales Datum YYYY-MM-DD)
            conn.execute("ALTER TABLE users ADD COLUMN tracking_start TEXT")
        if "carryover_minutes" not in cols:
            # Saldo-Übertrag aus dem alten System, in Minuten (+/-)
            conn.execute("ALTER TABLE users ADD COLUMN carryover_minutes INTEGER NOT NULL DEFAULT 0")
        for row in conn.execute("SELECT id, created_at FROM users WHERE tracking_start IS NULL").fetchall():
            conn.execute(
                "UPDATE users SET tracking_start = ? WHERE id = ?",
                (local_date_of(row["created_at"]), row["id"]),
            )
    if version < 3:
        cols = _columns(conn, "users")
        for name, ddl in (
            ("vacation_days_per_year", "REAL NOT NULL DEFAULT 30"),
            ("carryover_vacation", "REAL NOT NULL DEFAULT 0"),
            ("carryover_overtime_minutes", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        if "half_day" not in _columns(conn, "absences"):
            conn.execute("ALTER TABLE absences ADD COLUMN half_day INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS day_flags ("
            " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
            " day TEXT NOT NULL, overtime INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (user_id, day))"
        )
        # „Freizeitausgleich“ (0.1/0.2) entspricht einem Gleittag
        conn.execute("UPDATE absences SET kind = 'gleittag' WHERE kind = 'frei'")
        # alter Schalter auto_break → neuer Pausenmodus
        old = conn.execute("SELECT value FROM app_settings WHERE key = 'auto_break'").fetchone()
        if old and old["value"] == "1":
            conn.execute(
                "INSERT INTO app_settings(key, value) VALUES ('break_mode', 'arbzg') "
                "ON CONFLICT(key) DO UPDATE SET value = 'arbzg'"
            )
    if version < 4:
        cols = _columns(conn, "entries")
        # Wegezeit, die beim Stempeln dazugerechnet wurde (nachvollziehbar pro Buchung)
        if "travel_in_min" not in cols:
            conn.execute("ALTER TABLE entries ADD COLUMN travel_in_min INTEGER NOT NULL DEFAULT 0")
        if "travel_out_min" not in cols:
            conn.execute("ALTER TABLE entries ADD COLUMN travel_out_min INTEGER NOT NULL DEFAULT 0")
    if version < 5:
        # Wochenarbeitszeit mit „gilt ab“-Datum. 1970-01-01 = „von Anfang an“.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS work_schedules ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
            " valid_from TEXT NOT NULL, weekly_hours REAL NOT NULL, workdays TEXT NOT NULL,"
            " created_at TEXT NOT NULL, UNIQUE(user_id, valid_from))"
        )
        conn.execute(
            "INSERT OR IGNORE INTO work_schedules(user_id, valid_from, weekly_hours, workdays, created_at) "
            "SELECT id, '1970-01-01', weekly_hours, workdays, ? FROM users",
            (now_utc_str(),),
        )
    if version < 6:
        cols = _columns(conn, "users")
        if "email" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        if "report_mail" not in cols:
            # Berichte automatisch per Mail an diese Person (wenn Adresse hinterlegt)
            conn.execute("ALTER TABLE users ADD COLUMN report_mail INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS report_mails ("
            " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
            " period TEXT NOT NULL,"            # 2026-09 oder 2026
            " status TEXT NOT NULL,"            # sent | error
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " last_attempt TEXT, sent_at TEXT, recipient TEXT, error TEXT,"
            " PRIMARY KEY (user_id, period))"
        )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def connect() -> sqlite3.Connection:
    # Autocommit: jede Änderung ist sofort gespeichert. Wichtig, weil FastAPI das
    # Aufräumen von get_db() erst NACH dem Senden der Antwort ausführt – sonst
    # fragt der Browser direkt nach dem Login /me ab, bevor die Session in der
    # Datenbank steht (auf langsamen NAS-Platten reproduzierbar).
    # check_same_thread=False: FastAPI öffnet die Verbindung und führt den Endpunkt
    # u. U. in verschiedenen Threads aus. Jede Verbindung gehört nur einem Request.
    conn = sqlite3.connect(
        str(settings.db_path), timeout=10, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI-Dependency: eine Verbindung pro Request (Autocommit)."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Legt Datenordner, Schema und (beim ersten Start) den Admin an."""
    from .security import hash_password  # vermeidet Import-Zyklus

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES (?, ?)", (key, value)
            )

        has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if not has_users:
            password = settings.initial_admin_password or secrets.token_urlsafe(12)
            now = now_utc_str()
            conn.execute(
                "INSERT INTO users(username, display_name, password_hash, is_admin, created_at, tracking_start) "
                "VALUES ('admin', 'Administrator', ?, 1, ?, ?)",
                (hash_password(password), now, local_date_of(now)),
            )
            if not settings.initial_admin_password:
                cred_file = settings.data_dir / "ERSTES_ADMIN_PASSWORT.txt"
                cred_file.write_text(
                    "Benutzer: admin\nPasswort: " + password + "\n\n"
                    "Bitte nach dem ersten Login ändern und diese Datei löschen.\n",
                    encoding="utf-8",
                )
                print(f"[zeiterfassung] Admin angelegt, Passwort steht in {cred_file}")
        conn.commit()
    finally:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def audit(conn: sqlite3.Connection, actor_id, action: str, target: str = "", details: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, actor_id, action, target, details) VALUES (?, ?, ?, ?, ?)",
        (now_utc_str(), actor_id, action, target, details),
    )
