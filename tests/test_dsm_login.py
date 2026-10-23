"""Anmeldung mit DSM-Konten: Single Sign-on über app.cgi und Passwortprüfung bei DSM."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient

from app import dsm_auth
from app.main import app
from app.routers import auth as auth_router


def proxy_headers(user=None, admin=False, secret=None):
    h = {"X-ZE-Proxy-Secret": secret or dsm_auth.proxy_secret(), "X-ZE-Proxy-Base": "app.cgi"}
    if user:
        h["X-DSM-User"] = user
        h["X-DSM-Admin"] = "1" if admin else "0"
    return h


@pytest.fixture()
def admin():
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]})
        yield c
        c.patch("/api/v1/settings", json={"dsm_login": True, "dsm_auto_create": True, "dsm_admins": True})


# --- Single Sign-on ----------------------------------------------------------

def test_sso_creates_user_and_maps_admin(admin):
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        me = c.get("/api/v1/me", headers=proxy_headers("sso_hans"))
        assert me.status_code == 200 and me.json()["via_dsm"] and not me.json()["is_admin"]
        assert me.json()["has_password"] is False
        boss = c.get("/api/v1/me", headers=proxy_headers("sso_chefin", admin=True)).json()
        assert boss["is_admin"]
        # stempeln geht direkt, ohne Login
        assert c.post("/api/v1/clock/toggle", headers=proxy_headers("sso_hans")).json()["status"] == "in"
    log = admin.get("/api/v1/audit").json()
    assert any(a["action"] == "user_created_dsm" and "sso_hans" in (a["details"] or "") for a in log)


def test_sso_rejects_wrong_secret_and_missing_user():
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        # gefälschter Benutzer ohne gültigen Schlüssel wird ignoriert → nicht angemeldet
        assert c.get("/api/v1/me", headers=proxy_headers("admin", True, secret="falsch" * 8)).status_code == 401
        assert c.get("/api/v1/me", headers={"X-DSM-User": "admin", "X-DSM-Admin": "1"}).status_code == 401
        # Proxy ohne DSM-Benutzer (nicht in DSM angemeldet) → normales Login nötig
        assert c.get("/api/v1/me", headers=proxy_headers()).status_code == 401


def test_sso_respects_settings_and_inactive(admin):
    admin.patch("/api/v1/settings", json={"dsm_auto_create": False, "dsm_admins": False})
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        r = c.get("/api/v1/me", headers=proxy_headers("sso_fremd"))
        assert r.status_code == 403 and "beim Admin" in r.json()["detail"]
        assert not c.get("/api/v1/me", headers=proxy_headers("sso_hans", admin=True)).json()["is_admin"]
    uid = [u for u in admin.get("/api/v1/users").json() if u["username"] == "sso_hans"][0]["id"]
    admin.patch(f"/api/v1/users/{uid}", json={"active": False})
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        assert c.get("/api/v1/me", headers=proxy_headers("sso_hans")).status_code == 403
    admin.patch(f"/api/v1/users/{uid}", json={"active": True})


def test_index_uses_proxy_base():
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        html = c.get("/", headers=proxy_headers()).text
        assert 'window.ZE_PROXY="app.cgi"' in html and '"app.cgi?p=/static/app.js' in html
        plain = c.get("/").text
        assert "window.ZE_PROXY=null" in plain and '"static/app.js' in plain


# --- Passwortprüfung bei DSM ------------------------------------------------

def test_login_with_dsm_password(admin, monkeypatch):
    calls = []

    def fake_check(conn, username, password, otp=""):
        calls.append((username, otp))
        if password != "dsm-pass":
            return "wrong"
        if username == "tfa_user" and otp != "123456":
            return "otp"
        return "ok"

    monkeypatch.setattr(auth_router, "dsm_password_check", fake_check)
    monkeypatch.setattr(auth_router, "is_dsm_admin", lambda u: u == "dsm_admin")
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        r = c.post("/api/v1/auth/login", json={"username": "dsm_lisa", "password": "dsm-pass"})
        assert r.status_code == 200 and r.json()["username"] == "dsm_lisa" and not r.json()["is_admin"]
        assert c.get("/api/v1/me").status_code == 200
        r = c.post("/api/v1/auth/login", json={"username": "tfa_user", "password": "dsm-pass"})
        assert r.status_code == 401 and r.json()["detail"] == "otp_required"
        r = c.post("/api/v1/auth/login", json={"username": "tfa_user", "password": "dsm-pass", "otp_code": "123456"})
        assert r.status_code == 200
        assert c.post("/api/v1/auth/login", json={"username": "dsm_admin", "password": "dsm-pass"}).json()["is_admin"]
        assert c.post("/api/v1/auth/login", json={"username": "dsm_lisa", "password": "falsch"}).status_code == 401
        # lokales Passwort hat Vorrang und fragt DSM gar nicht erst
        n = len(calls)
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        assert len(calls) == n
        # Passwort ändern geht für DSM-Konten nicht
        c.post("/api/v1/auth/login", json={"username": "dsm_lisa", "password": "dsm-pass"})
        r = c.post("/api/v1/me/password", json={"old_password": "x", "new_password": "neuesPasswort1"})
        assert r.status_code == 400 and "DSM" in r.json()["detail"]
    admin.patch("/api/v1/settings", json={"dsm_login": False})
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        assert c.post("/api/v1/auth/login", json={"username": "dsm_lisa", "password": "dsm-pass"}).status_code == 401


def test_login_rate_limit(monkeypatch):
    monkeypatch.setattr(auth_router, "dsm_password_check", lambda *a, **k: "wrong")
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        for _ in range(5):
            assert c.post("/api/v1/auth/login", json={"username": "brute", "password": "x"}).status_code == 401
        assert c.post("/api/v1/auth/login", json={"username": "brute", "password": "x"}).status_code == 429
    dsm_auth.clear_failures("brute|testclient")


class FakeDSM(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        FakeDSM.seen.append(form)
        if form.get("account") == "tim" and form.get("passwd") == "richtig":
            if form.get("otp_code") not in (None, "654321"):
                return self._reply({"success": False, "error": {"code": 404}})
            return self._reply({"success": True, "data": {"sid": "abc"}})
        if form.get("account") == "zwei" and "otp_code" not in form:
            return self._reply({"success": False, "error": {"code": 403}})
        self._reply({"success": False, "error": {"code": 400}})

    def do_GET(self):
        FakeDSM.seen.append({"logout": self.path})
        self._reply({"success": True})


def test_dsm_password_check_against_fake_dsm(admin):
    srv = HTTPServer(("127.0.0.1", 0), FakeDSM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        admin.patch("/api/v1/settings", json={"dsm_url": f"http://127.0.0.1:{srv.server_port}"})
        from app.db import connect
        conn = connect()
        try:
            assert dsm_auth.dsm_password_check(conn, "tim", "richtig") == "ok"
            assert FakeDSM.seen[0]["api"] == "SYNO.API.Auth" and FakeDSM.seen[0]["passwd"] == "richtig"
            assert "logout" in FakeDSM.seen[1] and "_sid=abc" in FakeDSM.seen[1]["logout"]
            assert dsm_auth.dsm_password_check(conn, "tim", "falsch") == "wrong"
            assert dsm_auth.dsm_password_check(conn, "zwei", "egal") == "otp"
            assert dsm_auth.dsm_password_check(conn, "tim", "richtig", "000000") == "otp_wrong"
        finally:
            conn.close()
        admin.patch("/api/v1/settings", json={"dsm_url": "http://127.0.0.1:1"})
        conn = connect()
        try:
            assert dsm_auth.dsm_password_check(conn, "tim", "richtig") == "unreachable"
        finally:
            conn.close()
    finally:
        srv.shutdown()
        admin.patch("/api/v1/settings", json={"dsm_url": "http://127.0.0.1:5000"})
