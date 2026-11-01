"""Designs: eigenes Design, Standard der Firma, Kiosk."""
import os

from fastapi.testclient import TestClient

from app.main import app


def test_themes():
    with TestClient(app) as admin:
        admin.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]})
        s = admin.get("/api/v1/settings").json()
        assert set(s["themes"]) == {"classic", "dsm", "dark", "warm"} and s["theme_default"] == "classic"
        uid = admin.post("/api/v1/users", json={"username": "designer", "display_name": "Designer", "password": "geheim123"}).json()["id"]
        admin.put(f"/api/v1/users/{uid}/pin", json={"pin": "2468"})
        with TestClient(app) as u:
            u.post("/api/v1/auth/login", json={"username": "designer", "password": "geheim123"})
            me = u.get("/api/v1/me").json()
            assert me["theme"] is None and me["theme_effective"] == "classic"
            assert u.patch("/api/v1/me", json={"theme": "rosa"}).status_code == 400
            me = u.patch("/api/v1/me", json={"theme": "warm"}).json()
            assert me["theme"] == "warm" and me["theme_effective"] == "warm"
            # Firmen-Standard ändern: eigenes Design bleibt, ohne eigenes gilt der Standard
            assert admin.patch("/api/v1/settings", json={"theme_default": "dsm"}).status_code == 200
            assert u.get("/api/v1/me").json()["theme_effective"] == "warm"
            assert u.patch("/api/v1/me", json={"theme": None}).json()["theme_effective"] == "dsm"
            assert u.patch("/api/v1/settings", json={"theme_default": "dark"}).status_code == 403
        assert admin.patch("/api/v1/settings", json={"theme_default": "bunt"}).status_code == 400
        # Kiosk-Tablet nutzt den Firmen-Standard
        k = admin.post("/api/v1/kiosks", json={"name": "Design-Tablet"}).json()
        with TestClient(app) as t:
            t.headers["X-Kiosk-Token"] = k["url"].split("#k=")[1]
            assert t.get("/api/v1/kiosk/users").json()["theme"] == "dsm"
        admin.patch("/api/v1/settings", json={"theme_default": "classic"})
        for page in ("/", "/kiosk"):
            assert "static/theme.js" in admin.get(page).text
        assert admin.get("/static/theme.js").status_code == 200
