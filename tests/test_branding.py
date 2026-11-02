"""Firmenname und -logo: Upload, PNG-Dekoder, PDF, Excel, Kiosk."""
import base64
import io
import os
import struct
import zipfile
import zlib

import pytest
from fastapi.testclient import TestClient

from app import logo
from app.main import app

JPEG = ("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUo"
        "KSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCAAEAAgDASIAAhEBAxEB"
        "/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2Jy"
        "ggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLD"
        "xMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3"
        "AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6"
        "goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDhKKKK"
        "+PP14//Z")


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def make_png(w=6, h=5, ctype=6) -> tuple:
    """PNG mit allen fünf Zeilenfiltern; liefert (Datei, erwartete Pixel)."""
    ch = {0: 1, 2: 3, 6: 4}[ctype]
    rows, raw = [], b""
    prev = bytes(w * ch)
    for y in range(h):
        line = bytes((x * 40 + y * 30 + c * 70) % 256 for x in range(w) for c in range(ch))
        rows.append(line)
        f = y % 5
        enc = bytearray(line)
        for i in range(len(line)):
            a = line[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            if f == 1:
                enc[i] = (line[i] - a) & 255
            elif f == 2:
                enc[i] = (line[i] - b) & 255
            elif f == 3:
                enc[i] = (line[i] - ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                enc[i] = (line[i] - (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        raw += bytes([f]) + bytes(enc)
        prev = line
    png = (logo.PNG_SIG + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b""))
    return png, b"".join(rows)


def test_png_decoder():
    png, px = make_png(ctype=6)
    w, h, color, alpha = logo.decode_png(png)
    assert (w, h) == (6, 5)
    assert color == b"".join(px[i:i + 3] for i in range(0, len(px), 4))
    assert alpha == bytes(px[i + 3] for i in range(0, len(px), 4))
    png, px = make_png(ctype=2)
    assert logo.decode_png(png)[2] == px and logo.decode_png(png)[3] is None
    png, px = make_png(ctype=0)
    assert logo.decode_png(png)[2] == px


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]})
        yield c
        c.delete("/api/v1/branding/logo")
        c.patch("/api/v1/branding", json={"company_name": ""})


def _streams(pdf: bytes):
    """Inhalte aller Flate-Streams, die sich als Text dekodieren lassen (Seiteninhalte)."""
    import re
    for m in re.finditer(rb"stream\n(.*?)\nendstream", pdf, re.S):
        try:
            yield zlib.decompress(m.group(1))
        except zlib.error:
            pass


def data_url(raw: bytes, mime="image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def test_upload_and_public(admin):
    with TestClient(app) as anon:
        assert anon.get("/api/v1/branding").json() == {"company_name": "", "logo": None}
        assert anon.get("/api/v1/branding/logo").status_code == 404
        assert anon.put("/api/v1/branding/logo", json={"data_url": data_url(make_png()[0])}).status_code == 401
    assert admin.put("/api/v1/branding/logo", json={"data_url": "data:text/plain;base64,SGFsbG8="}).status_code == 400
    assert admin.put("/api/v1/branding/logo", json={"data_url": "kaputt"}).status_code == 400
    r = admin.put("/api/v1/branding/logo", json={"data_url": data_url(base64.b64decode(JPEG), "image/jpeg")})
    assert r.status_code == 200 and r.json()["logo"]["format"] == "jpeg" and r.json()["logo"]["width"] == 8
    png = make_png(60, 20)[0]
    r = admin.put("/api/v1/branding/logo", json={"data_url": data_url(png)})
    assert r.json()["logo"]["format"] == "png"
    assert admin.patch("/api/v1/branding", json={"company_name": "  Muster GmbH "}).json()["company_name"] == "Muster GmbH"
    with TestClient(app) as anon:
        b = anon.get("/api/v1/branding").json()
        assert b["company_name"] == "Muster GmbH" and b["logo"]["height"] == 20
        r = anon.get("/api/v1/branding/logo")
        assert r.status_code == 200 and r.content == png and r.headers["content-type"] == "image/png"


def test_reports_with_logo(admin):
    admin.put("/api/v1/branding/logo", json={"data_url": data_url(make_png(60, 20)[0])})
    pdf = admin.get("/api/v1/reports/download", params={"period": "2026-10", "format": "pdf"}).content
    assert b"/Subtype /Image" in pdf and b"/SMask" in pdf and b"/XObject << /Im1" in pdf
    text = b"".join(_streams(pdf))
    assert b"/Im1 Do" in text and b"Muster GmbH" in text
    xlsx = admin.get("/api/v1/reports/download", params={"period": "2026-10", "format": "xlsx"}).content
    names = zipfile.ZipFile(io.BytesIO(xlsx)).namelist()
    assert any(n.startswith("xl/media/image") for n in names)
    assert admin.get("/api/v1/reports/download", params={"period": "2026", "format": "pdf"}).status_code == 200
    assert admin.get("/api/v1/reports/download", params={"period": "2026", "format": "xlsx"}).status_code == 200
    # JPEG ebenfalls
    admin.put("/api/v1/branding/logo", json={"data_url": data_url(base64.b64decode(JPEG), "image/jpeg")})
    pdf = admin.get("/api/v1/reports/download", params={"period": "2026-10", "format": "pdf"}).content
    assert b"/DCTDecode" in pdf


def test_kiosk_and_backup(admin):
    k = admin.post("/api/v1/kiosks", json={"name": "Logo-Tablet"}).json()
    with TestClient(app) as t:
        t.headers["X-Kiosk-Token"] = k["url"].split("#k=")[1]
        r = t.get("/api/v1/kiosk/users").json()
        assert r["company_name"] == "Muster GmbH" and r["logo"]
    # Logo liegt in der Datenbank → steckt in jeder Sicherung
    from app.db import connect
    with connect() as conn:
        assert conn.execute("SELECT length(value) FROM app_settings WHERE key = 'logo_data'").fetchone()[0] > 100
    assert admin.delete("/api/v1/branding/logo").json()["logo"] is None
    assert b"/Subtype /Image" not in admin.get("/api/v1/reports/download", params={"period": "2026-10", "format": "pdf"}).content
