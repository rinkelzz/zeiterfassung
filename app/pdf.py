"""Kleiner PDF-Writer ohne Abhängigkeiten (Helvetica, A4, Umlaute über WinAnsi).

Reicht für tabellarische Berichte: Text (links/rechts/zentriert), Linien, Flächen.
Koordinaten von oben links in Punkt (1 pt = 1/72 Zoll).
"""
import zlib
from typing import List, Tuple

A4_W, A4_H = 595.28, 841.89

# Zeichenbreiten (1/1000 Schriftgröße) für Zeichen 32–255 in WinAnsi (cp1252), aus den Adobe-AFM-Dateien
HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 350, 556, 350, 222, 556, 333, 1000, 556, 556, 333, 1000, 667, 333, 1000, 350, 611, 350, 350, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944, 350, 500, 667, 278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611, 667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278, 278, 556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500]
HELV_B = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584, 350, 556, 350, 278, 556, 500, 1000, 556, 556, 333, 1000, 667, 333, 1000, 350, 611, 350, 350, 278, 278, 500, 500, 350, 556, 1000, 333, 1000, 556, 333, 944, 350, 500, 667, 278, 333, 556, 556, 556, 556, 280, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834, 834, 611, 722, 722, 722, 722, 722, 722, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 556, 556, 556, 556, 556, 278, 278, 278, 278, 611, 611, 611, 611, 611, 611, 611, 584, 611, 611, 611, 611, 611, 556, 611, 556]


def _enc(s: str) -> bytes:
    return str(s).encode("cp1252", "replace")


def text_width(s: str, size: float, bold: bool = False) -> float:
    table = HELV_B if bold else HELV
    return sum(table[b - 32] if 32 <= b <= 255 else 500 for b in _enc(s)) * size / 1000


def fit(s: str, width: float, size: float, bold: bool = False) -> str:
    """Text auf eine Breite kürzen (mit …)."""
    s = str(s or "")
    if text_width(s, size, bold) <= width:
        return s
    while s and text_width(s + "…", size, bold) > width:
        s = s[:-1]
    return s + "…"


def _esc(b: bytes) -> bytes:
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _rgb(c: Tuple[float, float, float]) -> str:
    return " ".join(f"{v:.3f}" for v in c)


class PDF:
    def __init__(self, title: str = "", author: str = "Zeiterfassung"):
        self.title, self.author = title, author
        self.pages: List[List[str]] = []
        self.ops: List[str] = []
        self.new_page()

    def new_page(self) -> None:
        self.ops = []
        self.pages.append(self.ops)

    # Zeichnen -------------------------------------------------------------
    def text(self, x: float, y: float, s: str, size: float = 9, bold: bool = False,
             align: str = "left", color=(0, 0, 0)) -> None:
        """y = Grundlinie, von oben gemessen."""
        if s is None or s == "":
            return
        if align == "right":
            x -= text_width(s, size, bold)
        elif align == "center":
            x -= text_width(s, size, bold) / 2
        data = _esc(_enc(s)).decode("latin-1")
        self.ops.append(f"BT /{'F2' if bold else 'F1'} {size:g} Tf {_rgb(color)} rg "
                        f"{x:.2f} {A4_H - y:.2f} Td ({data}) Tj ET")

    def rect(self, x: float, y: float, w: float, h: float, fill=(0.95, 0.95, 0.95)) -> None:
        self.ops.append(f"{_rgb(fill)} rg {x:.2f} {A4_H - y - h:.2f} {w:.2f} {h:.2f} re f")

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.5, color=(0.6, 0.6, 0.6)) -> None:
        self.ops.append(f"{width:g} w {_rgb(color)} RG {x1:.2f} {A4_H - y1:.2f} m {x2:.2f} {A4_H - y2:.2f} l S")

    # Ausgabe --------------------------------------------------------------
    def output(self) -> bytes:
        objs: List[bytes] = []

        def add(body: bytes) -> int:
            objs.append(body)
            return len(objs)

        def pdf_str(s: str) -> bytes:
            # Info-Texte als UTF-16 mit BOM → Umlaute im Dokumenttitel
            return b"<FEFF" + s.encode("utf-16-be").hex().upper().encode() + b">"

        catalog = add(b"")  # Platzhalter
        pages_id = add(b"")
        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        kids = []
        for ops in self.pages:
            raw = "\n".join(ops).encode("latin-1")
            comp = zlib.compress(raw)
            content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(comp) + comp + b"\nendstream")
            kids.append(add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Contents %d 0 R "
                b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>" % (pages_id, A4_W, A4_H, content, f1, f2)))
        objs[pages_id - 1] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (
            len(kids), b" ".join(b"%d 0 R" % k for k in kids))
        objs[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
        info = add(b"<< /Title " + pdf_str(self.title) + b" /Author " + pdf_str(self.author) +
                   b" /Producer (Zeiterfassung) >>")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        for off in offsets:
            out += b"%010d 00000 n \n" % off
        out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(objs) + 1, catalog, info, xref)
        return bytes(out)
