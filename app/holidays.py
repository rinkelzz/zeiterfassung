"""Gesetzliche Feiertage in Deutschland nach Bundesland.

Portiert aus rinkelzz/gleitzeit (includes/holidays.php), ergänzt um:
- regionale Feiertage (Fronleichnam in Teilen von Sachsen/Thüringen, Mariä Himmelfahrt in Bayern,
  Augsburger Friedensfest) als Auswahl statt landesweit
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


# Feiertage, die nur in einem Teil des Landes gelten. Landesweit rechnet die App sie nicht;
# wer in einer solchen Gemeinde arbeitet, schaltet sie unter Verwaltung → Einstellungen ein.
#   Schlüssel: (Bundesland, Name, Erklärung, standardmäßig an)
REGIONAL = {
    "fronleichnam_sn": ("SN", "Fronleichnam", "nur in einzelnen Gemeinden im sorbischen Siedlungsgebiet, Landkreis Bautzen", False),
    "fronleichnam_th": ("TH", "Fronleichnam", "nur im Eichsfeld und einzelnen Gemeinden im Unstrut-Hainich- und Wartburgkreis", False),
    "mariae_himmelfahrt_by": ("BY", "Mariä Himmelfahrt", "nur in Gemeinden mit überwiegend katholischer Bevölkerung (die meisten)", True),
    "friedensfest_by": ("BY", "Augsburger Friedensfest", "nur in der Stadt Augsburg", False),
}


def regional_options(bl: str) -> Dict[str, Dict]:
    return {k: {"name": n, "info": i, "default": d} for k, (land, n, i, d) in REGIONAL.items() if land == bl.upper()}


def regional_all() -> Dict[str, Dict[str, Dict]]:
    """Alle regionalen Feiertage nach Bundesland (für die Einstellungsseite)."""
    out: Dict[str, Dict[str, Dict]] = {}
    for k, (land, n, i, d) in REGIONAL.items():
        out.setdefault(land, {})[k] = {"name": n, "info": i, "default": d}
    return out


def parse_regional(raw: str, bl: str) -> frozenset:
    """Gespeicherte Auswahl ('default' = Voreinstellung des Bundeslands) → aktive Schlüssel."""
    opts = regional_options(bl)
    if (raw or "default") == "default":
        return frozenset(k for k, o in opts.items() if o["default"])
    return frozenset(k.strip() for k in raw.split(",") if k.strip() in opts)


@lru_cache(maxsize=256)
def _public_holidays(year: int, bl: str, regional: frozenset = frozenset()) -> Dict[date, str]:
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
    if bl in ("BW", "BY", "HE", "NW", "RP", "SL") or regional & {"fronleichnam_sn", "fronleichnam_th"}:
        add(e + timedelta(days=60), "Fronleichnam")
    if bl == "SL" or "mariae_himmelfahrt_by" in regional:
        add(date(year, 8, 15), "Mariä Himmelfahrt")
    if "friedensfest_by" in regional:
        add(date(year, 8, 8), "Augsburger Friedensfest")
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


def holidays_for_year(year: int, bundesland: str, company_free_days: str = "", regional: str = "default") -> Dict[date, str]:
    """Feiertage plus betriebsfreie Tage eines Jahres: {Datum: Name}.
    regional: gespeicherte Auswahl regionaler Feiertage (siehe REGIONAL), 'default' = Voreinstellung."""
    bl = (bundesland or "").upper()
    days = dict(_public_holidays(year, bl, parse_regional(regional, bl)))
    for mm, dd in parse_company_free_days(company_free_days):
        try:
            d = date(year, mm, dd)
        except ValueError:  # 29.02. in Nicht-Schaltjahren
            continue
        days.setdefault(d, "Betriebsfrei")
    return dict(sorted(days.items()))
