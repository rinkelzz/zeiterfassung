"""Firmenlogo: speichern, prüfen und für PDF/Excel aufbereiten – ohne Pillow (läuft so auf jeder NAS).

Erlaubt sind PNG und JPEG bis 1 MB. Für das PDF werden JPEGs direkt eingebettet (DCTDecode), PNGs hier
selbst dekodiert (8 Bit, Graustufen/RGB/Palette, mit Transparenz) und als Flate-Bild mit Alpha-Maske eingebettet.
"""
import base64
import json
import struct
import time
import zlib
from typing import Dict, Optional, Tuple


MAX_BYTES = 1024 * 1024
PNG_SIG = b"\x89PNG\r\n\x1a\n"


class LogoError(ValueError):
    pass


# Gespeichert in der Datenbank (app_settings) – so ist das Logo automatisch in jeder Datensicherung.

def _get(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def _set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def load(conn) -> Optional[Tuple[bytes, Dict]]:
    """(Bilddaten, {format, width, height, version}) oder None."""
    raw = _get(conn, "logo_data")
    if not raw:
        return None
    try:
        return base64.b64decode(raw), json.loads(_get(conn, "logo_meta") or "{}")
    except (ValueError, base64.binascii.Error):
        return None


def info(conn) -> Optional[Dict]:
    got = load(conn)
    return got[1] if got else None


# --- Prüfen -----------------------------------------------------------------------

def _png_size(data: bytes) -> Tuple[int, int]:
    if data[12:16] != b"IHDR":
        raise LogoError("PNG-Datei ist beschädigt")
    return struct.unpack(">II", data[16:24])


def _jpeg_size(data: bytes) -> Tuple[int, int, int]:
    """Breite, Höhe, Farbkanäle aus dem SOF-Block."""
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h, data[i + 9]
        i += 2 + length
    raise LogoError("JPEG-Datei ist beschädigt")


def save(conn, raw: bytes) -> Dict:
    if len(raw) > MAX_BYTES:
        raise LogoError("Das Logo ist zu groß (höchstens 1 MB)")
    if raw.startswith(PNG_SIG):
        w, h = _png_size(raw)
        decode_png(raw)  # früh prüfen, ob wir es fürs PDF lesen können
        fmt = "png"
    elif raw[:3] == b"\xff\xd8\xff":
        w, h, comps = _jpeg_size(raw)
        if comps not in (1, 3):
            raise LogoError("Bitte ein JPEG in RGB oder Graustufen verwenden (kein CMYK)")
        fmt = "jpeg"
    else:
        raise LogoError("Bitte ein PNG- oder JPEG-Bild hochladen")
    if not (1 <= w <= 4000 and 1 <= h <= 4000):
        raise LogoError("Das Bild ist zu groß (höchstens 4000 × 4000 Pixel)")
    meta = {"format": fmt, "width": w, "height": h, "version": int(time.time())}
    _set(conn, "logo_data", base64.b64encode(raw).decode())
    _set(conn, "logo_meta", json.dumps(meta))
    return meta


def save_data_url(conn, data_url: str) -> Dict:
    try:
        _, b64 = data_url.split(",", 1)
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, base64.binascii.Error):
        raise LogoError("Bild konnte nicht gelesen werden")
    return save(conn, raw)


def remove(conn) -> None:
    _set(conn, "logo_data", "")
    _set(conn, "logo_meta", "")


# --- PNG dekodieren (für das PDF) -------------------------------------------------

def decode_png(data: bytes) -> Tuple[int, int, bytes, Optional[bytes]]:
    """→ (Breite, Höhe, RGB- oder Grau-Pixel, Alpha oder None). 8-Bit-PNGs, nicht interlaced."""
    pos, idat, plte, trns = 8, b"", None, None
    w = h = depth = ctype = interlace = None
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"PLTE":
            plte = chunk
        elif kind == b"tRNS":
            trns = chunk
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if w is None:
        raise LogoError("PNG-Datei ist beschädigt")
    if interlace:
        raise LogoError("Bitte das PNG ohne „Interlacing“ speichern")
    if depth != 8 or ctype not in (0, 2, 3, 4, 6):
        raise LogoError("Bitte ein PNG mit 8 Bit Farbtiefe verwenden")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    stride = w * channels
    try:
        raw = zlib.decompress(idat)
    except zlib.error:
        raise LogoError("PNG-Datei ist beschädigt")
    if len(raw) < h * (stride + 1):
        raise LogoError("PNG-Datei ist unvollständig")
    out = bytearray(h * stride)
    prev = bytearray(stride)
    bpp = channels
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[i] = (line[i] + pred) & 0xFF
        elif f != 0:
            raise LogoError("PNG-Datei ist beschädigt")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    px = bytes(out)
    if ctype == 3:  # Palette → RGB, tRNS → Alpha
        if not plte:
            raise LogoError("PNG-Datei ist beschädigt")
        rgb = bytearray()
        alpha = bytearray() if trns else None
        for idx in px:
            rgb += plte[idx * 3:idx * 3 + 3]
            if alpha is not None:
                alpha.append(trns[idx] if idx < len(trns) else 255)
        return w, h, bytes(rgb), bytes(alpha) if alpha is not None else None
    if ctype in (4, 6):
        n = channels
        color = bytearray()
        alpha = bytearray()
        for i in range(0, len(px), n):
            color += px[i:i + n - 1]
            alpha.append(px[i + n - 1])
        return w, h, bytes(color), bytes(alpha)
    return w, h, px, None


def pdf_image(conn) -> Optional[Dict]:
    """Bilddaten fürs PDF: {width, height, filter, colorspace, data, alpha}."""
    got = load(conn)
    if not got:
        return None
    raw, meta = got
    if meta.get("format") == "jpeg":
        w, h, comps = _jpeg_size(raw)
        return {"width": w, "height": h, "filter": "DCTDecode", "colorspace": "DeviceGray" if comps == 1 else "DeviceRGB",
                "data": raw, "alpha": None}
    w, h, px, alpha = decode_png(raw)
    gray = len(px) == w * h
    return {"width": w, "height": h, "filter": "FlateDecode", "colorspace": "DeviceGray" if gray else "DeviceRGB",
            "data": zlib.compress(px), "alpha": zlib.compress(alpha) if alpha and min(alpha) < 255 else None}


def fit_box(w: int, h: int, max_w: float, max_h: float) -> Tuple[float, float]:
    s = min(max_w / w, max_h / h)
    return w * s, h * s
