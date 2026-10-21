"""Vorgetäuschte Absenderadressen (X-Forwarded-For) und Login-Sperre pro Benutzername."""
import os

import pytest
from fastapi.testclient import TestClient

from app import dsm_auth
from app.main import app

PROXY = ("127.0.0.1", 40000)       # DSM-Reverse-Proxy auf der NAS
INTERNET = ("8.8.8.8", 40000)


@pytest.fixture(autouse=True)
def no_dsm_login(monkeypatch):
    # Kein echtes DSM im Test: Login prüft nur lokale Passwörter
    monkeypatch.setattr("app.routers.auth.get_setting", lambda conn, key: "0" if key == "dsm_login" else "")
    dsm_auth._fails.clear()
    yield
    dsm_auth._fails.clear()


def test_package_source_spoofing():
    # über den Reverse Proxy: echte Adresse aus dem Internet → gesperrt
    with TestClient(app, client=PROXY) as c:
        assert c.get("/spk", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 403
        # vorgetäuschte Heimnetz-Adresse vorne in der Kette hilft nicht – zählt die vom Proxy angehängte
        assert c.get("/spk", headers={"X-Forwarded-For": "192.168.1.5, 8.8.8.8"}).status_code == 403
        # echtes Heimnetz über den Proxy → erlaubt
        assert c.get("/spk", headers={"X-Forwarded-For": "192.168.1.5"}).status_code == 200
        # NAS selbst (Paket-Zentrum) → erlaubt
        assert c.get("/spk").status_code == 200
    # direkt aus dem Internet mit erfundenem Header → Header wird ignoriert
    with TestClient(app, client=INTERNET) as c:
        assert c.get("/spk", headers={"X-Forwarded-For": "192.168.1.5"}).status_code == 403


def test_forwarded_proto_only_from_proxy():
    with TestClient(app, client=PROXY) as c:
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]},
                   headers={"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"})
        assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()
    with TestClient(app, client=INTERNET) as c:
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]},
                   headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200 and "secure" not in r.headers["set-cookie"].lower()


def test_lockout_cannot_be_bypassed_with_fake_addresses():
    with TestClient(app, client=PROXY) as c:
        codes = []
        for i in range(12):
            r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "falsch"},
                       headers={"X-Forwarded-For": f"10.0.{i}.1, 198.51.100.7"})
            codes.append(r.status_code)
        assert codes[:5] == [401] * 5 and codes[5] == 429   # gleiche echte Adresse → nach 5 gesperrt

    dsm_auth._fails.clear()
    with TestClient(app, client=PROXY) as c:
        codes = []
        for i in range(12):   # jedes Mal wirklich eine andere Adresse (verteilter Angriff)
            r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "falsch"},
                       headers={"X-Forwarded-For": f"198.51.100.{i + 1}"})
            codes.append(r.status_code)
        assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]   # Sperre pro Name greift
        # andere Benutzernamen sind nicht betroffen
        r = c.post("/api/v1/auth/login", json={"username": "jemand", "password": "falsch"},
                   headers={"X-Forwarded-For": "198.51.100.99"})
        assert r.status_code == 401
