"""Gesetzliche Feiertage in Deutschland nach Bundesland.

Portiert aus rinkelzz/gleitzeit (includes/holidays.php), ergänzt um:
- korrekten Buß- und Bettag (Mittwoch VOR dem 23.11., also 16.–22.11.)
- Jahresregeln für neuere Feiertage (Frauentag BE/MV, Weltkindertag TH,
  Reformationstag in den Nordländern, Reformationsjubiläum 2017)
"""
from datetime import date, timedelta
from functools import lru_cache
from typing import Dict

BUNDESLAENDER = {
    "BW": "Baden-Württemberg",
    "BY": "Bayern",
    "BE": "Berlin",
    "BB": "Brandenburg",
    "HB": "Bremen",
    "HH": "Hamburg",
    "HE": "Hessen",
    "MV": "Mecklenburg-Vorpommern",
    "NI": "Niedersachsen",
    "NW": "Nordrhein-Westfalen",
    "RP": "Rheinland-Pfalz",
    "SL": "Saarland",
    "SN": "Sachsen",
    "ST": "Sachsen-Anhalt",
    "SH": "Schleswig-Holstein",
    "TH": "Thüringen",
}


def easter_sunday(year: int) -> date:
    """Ostersonntag nach der Gaußschen Osterformel (gregorianisch)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def buss_und_bettag(year: int) -> date:
    """Mittwoch vor dem 23. November."""
    nov22 = date(year, 11, 22)
    return nov22 - timedelta(days=(nov22.weekday() - 2) % 7)


@lru_cache(maxsize=256)
def _public_holidays(year: int, bl: str) -> Dict[date, str]:
    bl = bl.upper()
    e = easter_sunday(year)
    days: Dict[date, str] = {}

    def add(d: date, name: str) -> None:
        days[d] = name

    # Bundesweit
    add(date(year, 1, 1), "Neujahr")
    add(e - timedelta(days=2), "Karfreitag")
    add(e + timedelta(days=1), "Ostermontag")
    add(date(year, 5, 1), "Tag der Arbeit")
    add(e + timedelta(days=39), "Christi Himmelfahrt")
    add(e + timedelta(days=50), "Pfingstmontag")
    add(date(year, 10, 3), "Tag der Deutschen Einheit")
    add(date(year, 12, 25), "1. Weihnachtstag")
    add(date(year, 12, 26), "2. Weihnachtstag")

    # Landesfeiertage
    if bl in ("BW", "BY", "ST"):
        add(date(year, 1, 6), "Heilige Drei Könige")
    if (bl == "BE" and year >= 2019) or (bl == "MV" and year >= 2023):
        add(date(year, 3, 8), "Internationaler Frauentag")
    if bl == "BB":
        add(e, "Ostersonntag")
        add(e + timedelta(days=49), "Pfingstsonntag")
    if bl in ("BW", "BY", "HE", "NW", "RP", "SL", "SN", "TH"):
        add(e + timedelta(days=60), "Fronleichnam")
    if bl in ("BY", "SL"):
        add(date(year, 8, 15), "Mariä Himmelfahrt")
    if bl == "TH" and year >= 2019:
        add(date(year, 9, 20), "Weltkindertag")
    reformation = {"BB", "MV", "SN", "ST", "TH"}
    if year >= 2018:
        reformation |= {"HB", "HH", "NI", "SH"}
    if bl in reformation or year == 2017:
        add(date(year, 10, 31), "Reformationstag")
    if bl in ("BW", "BY", "NW", "RP", "SL"):
        add(date(year, 11, 1), "Allerheiligen")
    if bl == "SN":
        add(buss_und_bettag(year), "Buß- und Bettag")
    return days


def parse_company_free_days(raw: str) -> list:
    """'12-24, 12-31' → [(12, 24), (12, 31)]; ungültige Einträge werden ignoriert."""
    result = []
    for part in (raw or "").split(","):
        part = part.strip()
        try:
            mm, dd = part.split("-")
            date(2024, int(mm), int(dd))  # Schaltjahr, damit 02-29 gültig ist
            result.append((int(mm), int(dd)))
        except ValueError:
            continue
    return result


def holidays_for_year(year: int, bundesland: str, company_free_days: str = "") -> Dict[date, str]:
    """Feiertage plus betriebsfreie Tage eines Jahres: {Datum: Name}."""
    days = dict(_public_holidays(year, bundesland))
    for mm, dd in parse_company_free_days(company_free_days):
        try:
            d = date(year, mm, dd)
        except ValueError:  # 29.02. in Nicht-Schaltjahren
            continue
        days.setdefault(d, "Betriebsfrei")
    return dict(sorted(days.items()))
