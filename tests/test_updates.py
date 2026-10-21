"""Paketquelle fürs Paket-Zentrum: GitHub-Release → Cache → Paketliste → Download."""
import hashlib
import io
import json
import os

import pytest
from fastapi.testclient import TestClient

from app import __version__, updates
from app.main import app

SPK = b"SPK-INHALT" * 1000
NAME = "zeiterfassung-99.1.0-0001-x86_64.spk"
RELEASE = {
    "tag_name": "v99.1.0",
    "body": "### Neu\n- **Updates** über das [Paket-Zentrum](https://example.org)",
    "published_at": "2026-10-21T08:00:00Z",
    "html_url": "https://github.com/rinkelzz/zeiterfassung/releases/tag/v99.1.0",
    "assets": [
        {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.org/sums", "size": 10},
        {"name": NAME, "browser_download_url": "https://example.org/spk", "size": len(SPK)},
    ],
}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@pytest.fixture()
def fake_github(monkeypatch):
    calls = []

    def fake_http(url, timeout=20):
        calls.append(url)
        if url.startswith("https://api.github.com/"):
            return _Resp(json.dumps(RELEASE).encode())
        if url == "https://example.org/spk":
            return _Resp(SPK)
        raise OSError("unerwartet: " + url)

    monkeypatch.setattr(updates, "_http", fake_http)
    monkeypatch.setitem(updates._state, "checked_at", 0.0)
    monkeypatch.setitem(updates._state, "release", None)
    monkeypatch.setitem(updates._state, "error", None)
    yield calls


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]}).status_code == 200
        yield c


def test_version_compare():
    assert updates.is_newer("0.8.1-0001", "0.8.0")
    assert not updates.is_newer("0.8.0-0002", "0.8.0")
    assert updates.is_newer("0.10.0-0001", "0.9.9")


def test_catalog_and_download(admin, fake_github):
    # vor der ersten Prüfung: leere Liste (Prüfung startet im Hintergrund)
    updates._state["checked_at"] = 9e12  # Hintergrundprüfung hier unterdrücken
    assert admin.get("/spk").json() == {"packages": []}
    updates._state["checked_at"] = 0.0

    st = admin.post("/api/v1/updates/check").json()
    assert st["latest"] == "99.1.0" and st["update_available"] and st["ready"] and st["error"] is None
    assert st["installed"] == __version__ and st["source_url"].endswith(":8765/spk")

    cat = admin.post("/spk", data={"arch": "apollolake", "build": "72806", "language": "ger"}).json()
    (pkg,) = cat["packages"]
    assert pkg["package"] == "zeiterfassung" and pkg["version"] == "99.1.0-0001"
    assert pkg["md5"] == hashlib.md5(SPK).hexdigest() and pkg["size"] == len(SPK)
    assert pkg["link"] == f"http://testserver/spk/download/{NAME}"
    assert pkg["qupgrade"] is True and pkg["qinst"] is False
    assert "Updates über das Paket-Zentrum" in pkg["changelog"] and "**" not in pkg["changelog"]
    assert pkg["thumbnail"][0].startswith("https://raw.githubusercontent.com/")

    r = admin.get(f"/spk/download/{NAME}")
    assert r.status_code == 200 and r.content == SPK
    assert admin.get("/spk/download/zeiterfassung-0.0.1-0001-x86_64.spk").status_code == 404
    assert admin.get("/spk/download/..%2F..%2Fzeiterfassung.db").status_code == 404

    # zweite Prüfung lädt nicht erneut herunter
    n = len(fake_github)
    admin.post("/api/v1/updates/check")
    assert fake_github[n:] == ["https://api.github.com/repos/rinkelzz/zeiterfassung/releases/latest"]


def test_github_error_is_reported(admin, monkeypatch):
    def broken(url, timeout=20):
        raise OSError("Netzwerk weg")

    monkeypatch.setattr(updates, "_http", broken)
    monkeypatch.setitem(updates._state, "release", None)
    st = admin.post("/api/v1/updates/check").json()
    assert "Netzwerk weg" in st["error"] and st["latest"] is None
    assert admin.get("/spk").json() == {"packages": []}


def test_only_local_network(fake_github):
    with TestClient(app, client=("8.8.8.8", 5000)) as c:
        assert c.get("/spk").status_code == 403
    with TestClient(app, client=("192.168.1.20", 5000)) as c:
        assert c.get("/spk").status_code == 200


def test_status_admin_only():
    with TestClient(app) as c:
        assert c.get("/api/v1/updates").status_code == 401


def test_prefers_noarch(monkeypatch):
    rel = dict(RELEASE, assets=[
        {"name": "zeiterfassung-99.2.0-0001-x86_64.spk", "browser_download_url": "https://example.org/x", "size": 1},
        {"name": "zeiterfassung-99.2.0-0001-noarch.spk", "browser_download_url": "https://example.org/n", "size": 1},
    ])
    monkeypatch.setattr(updates, "_http", lambda url, timeout=20: _Resp(json.dumps(rel).encode()))
    r = updates.fetch_latest()
    assert r["name"].endswith("-noarch.spk") and r["version"] == "99.2.0-0001"
    # ältere Releases (nur x86_64) werden weiter erkannt
    rel["assets"] = rel["assets"][:1]
    assert updates.fetch_latest()["name"].endswith("-x86_64.spk")
