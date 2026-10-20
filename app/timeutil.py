"""Zeit-Hilfen. Gespeichert wird immer UTC, gerechnet und angezeigt lokal."""
from datetime import date, datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 (DSM-eigenes Python)
    from backports.zoneinfo import ZoneInfo

from .config import settings


def tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_utc_str(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz())  # naive Eingaben gelten als Ortszeit
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def now_utc_str() -> str:
    return to_utc_str(now_utc())


def parse_utc(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(tz())


def local_iso(value: Optional[str]) -> Optional[str]:
    dt = parse_utc(value)
    return to_local(dt).isoformat() if dt else None


def local_date_of(value: str) -> str:
    """UTC-Zeitstempel → lokales Datum als YYYY-MM-DD."""
    return to_local(parse_utc(value)).date().isoformat()


def today_local() -> date:
    return to_local(now_utc()).date()


def local_hhmm(dt: datetime) -> str:
    return to_local(dt).strftime("%H:%M")


def day_start_utc(d: date) -> datetime:
    """Lokale Mitternacht des Tages d als UTC-Zeitpunkt (sommerzeitfest)."""
    return datetime(d.year, d.month, d.day, tzinfo=tz()).astimezone(timezone.utc)


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def fmt_duration(seconds: float) -> str:
    """Sekunden als [-]H:MM."""
    sign = "-" if seconds < 0 else ""
    minutes = int(round(abs(seconds) / 60))
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"
