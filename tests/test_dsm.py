"""Öffentliche Adresse, Header-Login fürs DSM-Fenster, ui/app.json."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def admin():
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]})
        assert r.status_code == 200
        yield c
        c.patch("/api/v1/settings", json={"public_url": ""})


def test_public_url_setting_drives_qr(admin):
    assert admin.patch("/api/v1/settings", json={"public_url": "zeit.example.de"}).status_code == 400
    r = admin.patch("/api/v1/settings", json={"public_url": "https://zeit.meinedomain.de/"})
    assert r.status_code == 200 and r.json()["public_url"] == "https://zeit.meinedomain.de"
    qr = admin.post("/api/v1/me/tokens", json={"label": "t"}).json()
    assert qr["setup"]["server"] == "https://zeit.meinedomain.de"
    # leer → zurück auf config.env (in den Tests: ZE_PUBLIC_URL)
    admin.patch("/api/v1/settings", json={"public_url": ""})
    assert admin.post("/api/v1/me/tokens", json={"label": "t"}).json()["setup"]["server"] == "https://zeit.example.de"


def test_session_header_login_without_cookie():
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]})
        token = r.json()["session_token"]
        assert token and "password_hash" not in r.json()
    with TestClient(app) as fresh:  # kein Cookie, nur Header
        assert fresh.get("/api/v1/me").status_code == 401
        assert fresh.get("/api/v1/me", headers={"X-Session-Token": token}).status_code == 200
        fresh.post("/api/v1/auth/logout", headers={"X-Session-Token": token})
        assert fresh.get("/api/v1/me", headers={"X-Session-Token": token}).status_code == 401


def test_https_cookie_is_samesite_none():
    with TestClient(app, base_url="https://testserver") as c:
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]})
        cookie = r.headers["set-cookie"].lower()
        assert "samesite=none" in cookie and "secure" in cookie
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]})
        assert "samesite=lax" in r.headers["set-cookie"].lower()


def test_ui_app_json_written(admin, tmp_path):
    old = settings.ui_dir
    settings.ui_dir = str(tmp_path)
    try:
        admin.patch("/api/v1/settings", json={"public_url": "https://zeit.meinedomain.de"})
        data = json.loads((tmp_path / "app.json").read_text())
        assert data == {"public_url": "https://zeit.meinedomain.de", "port": settings.port, "root_path": ""}
    finally:
        settings.ui_dir = old


def test_qr_is_setup_link(admin):
    r = admin.post("/api/v1/me/tokens", json={"label": "iPhone"}).json()
    assert r["setup_url"] == "https://zeit.example.de/setup#t=" + r["token"]
    assert r["server_is_public"] is True
    page = admin.get("/setup")
    assert page.status_code == 200 and "Zeiterfassung einrichten" in page.text
    assert page.headers["referrer-policy"] == "no-referrer" and "no-store" in page.headers["cache-control"]


def test_qr_in_dsm_window_uses_browser_host(monkeypatch):
    from app import dsm_auth
    monkeypatch.setattr(settings, "public_url", "")
    h = {"X-ZE-Proxy-Secret": dsm_auth.proxy_secret(), "X-ZE-Proxy-Base": "app.cgi", "X-DSM-User": "qr_tim", "X-DSM-Admin": "0"}
    with TestClient(app) as c:
        r = c.post("/api/v1/me/tokens", json={"label": "iPhone", "host_hint": "192.168.178.20"}, headers=h).json()
        assert r["setup"]["server"] == f"http://192.168.178.20:{settings.port}"
        assert r["setup_url"].startswith(f"http://192.168.178.20:{settings.port}/setup#t=ze_")
        assert r["server_is_public"] is False
        bad = c.post("/api/v1/me/tokens", json={"label": "x", "host_hint": "evil.com/<script>"}, headers=h)
        assert bad.status_code == 422
