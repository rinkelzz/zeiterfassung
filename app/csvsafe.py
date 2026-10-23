"""Schutz vor Formel-Injektion in CSV- und Excel-Exporten.

Excel/LibreOffice werten Zellen, die mit = + - @ (oder Tab/CR) beginnen, als Formel aus.
Freitext (Begründungen, Namen, Notizen) wird deshalb mit einem Apostroph entschärft.
Echte Zahlen und Zeiten wie „-0:14“ oder „-1,5“ bleiben unverändert.
"""
import re
from typing import Any, Iterable, List

_DANGER = ("=", "+", "-", "@", "\t", "\r")
_NUMBER = re.compile(r"^[+-]?\d[\d:.,]*( ?(min|h))?$")


def cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_DANGER) and not _NUMBER.match(value):
        return "'" + value
    return value


def row(values: Iterable[Any]) -> List[Any]:
    return [cell(v) for v in values]


def no_formulas(ws) -> None:
    """Letzte Zeile eines openpyxl-Blatts: Text, der mit = beginnt, als Text speichern statt als Formel."""
    for c in ws[ws.max_row]:
        if c.data_type == "f":
            c.data_type = "s"
