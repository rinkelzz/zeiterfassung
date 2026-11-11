"""Mailwege neben SMTP: Microsoft 365 (Graph, Secret oder Zertifikat) und eigener Mailweg (Programm/Unix-Socket)."""
import base64
import email
import email.policy
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

import pytest
from fastapi.testclient import TestClient

from app import mail_graph, mailer
from app.config import settings
from app.db import connect
from app.main import app

needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl fehlt")


@pytest.fixture(scope="module")
def pem():
    """Selbst signiertes Zertifikat + Schlüssel (PKCS#8) und derselbe Schlüssel als PKCS#1."""
    if not shutil.which("openssl"):
        pytest.skip("openssl fehlt")
    d = tempfile.mkdtemp()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=zeiterfassung-test",
                    "-days", "2", "-keyout", f"{d}/k.pem", "-out", f"{d}/c.pem"], check=True, capture_output=True)
    subprocess.run(["openssl", "rsa", "-in", f"{d}/k.pem", "-traditional", "-out", f"{d}/k1.pem"], check=True, capture_output=True)
    cert, key, key1 = (open(f"{d}/{n}").read() for n in ("c.pem", "k.pem", "k1.pem"))
    return {"pkcs8": cert + key, "pkcs1": key1 + cert, "dir": d}


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ZE_ADMIN_PASSWORD"]})
        yield c
        c.patch("/api/v1/settings", json={"mail_transport": "smtp", "graph_clear": "secret"})
        c.patch("/api/v1/settings", json={"graph_clear": "cert"})


# --- Zertifikat / JWT ---------------------------------------------------------------------------

@needs_openssl
@pytest.mark.parametrize("variant", ["pkcs8", "pkcs1"])
def test_jwt_signature_verifies(pem, variant):
    creds = mail_graph.load_credentials(pem[variant])
    jwt = mail_graph.client_assertion("tenant-x", "client-y", creds, now=1_800_000_000)
    head, claims, sig = jwt.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    h = json.loads(base64.urlsafe_b64decode(pad(head)))
    c = json.loads(base64.urlsafe_b64decode(pad(claims)))
    assert h["alg"] == "RS256" and h["x5t"] == base64.urlsafe_b64encode(hashlib.sha1(creds["cert_der"]).digest()).rstrip(b"=").decode()
    assert c["aud"] == "https://login.microsoftonline.com/tenant-x/oauth2/v2.0/token"
    assert c["iss"] == c["sub"] == "client-y" and c["exp"] - c["iat"] == 600
    # mit openssl gegenprüfen
    d = pem["dir"]
    open(f"{d}/data", "wb").write(f"{head}.{claims}".encode())
    open(f"{d}/sig", "wb").write(base64.urlsafe_b64decode(pad(sig)))
    subprocess.run(["openssl", "x509", "-in", f"{d}/c.pem", "-pubkey", "-noout", "-out", f"{d}/pub.pem"], check=True)
    r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", f"{d}/pub.pem", "-signature", f"{d}/sig", f"{d}/data"],
                       capture_output=True, text=True)
    assert "Verified OK" in r.stdout, r.stdout + r.stderr


def test_bad_pem_messages():
    with pytest.raises(mail_graph.GraphError, match="Zertifikat"):
        mail_graph.load_credentials("nix")
    with pytest.raises(mail_graph.GraphError, match="Passwort"):
        mail_graph.load_credentials("-----BEGIN ENCRYPTED PRIVATE KEY-----\nAAAA\n-----END ENCRYPTED PRIVATE KEY-----")


# --- Graph-Versand mit nachgebautem Microsoft ----------------------------------------------------

class FakeMS:
    def __init__(self, send_status=202):
        self.calls = []
        self.send_status = send_status

    def __call__(self, req, timeout=30):
        body = req.data.decode() if req.data else ""
        self.calls.append((req.full_url, dict(req.header_items()), body))
        if "login.microsoftonline.com" in req.full_url:
            return 200, json.dumps({"access_token": "tok-123", "expires_in": 3600}).encode()
        return self.send_status, (b"" if self.send_status == 202 else
                                  json.dumps({"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}}).encode())


def _graph_settings(admin, **extra):
    body = {"mail_transport": "graph", "graph_tenant": "contoso.onmicrosoft.com",
            "graph_client_id": "11111111-2222-3333-4444-555555555555", "smtp_from": "zeit@contoso.de",
            "smtp_from_name": "Zeiterfassung", **extra}
    r = admin.patch("/api/v1/settings", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_graph_with_secret(admin, monkeypatch):
    mail_graph._cache.clear()
    fake = FakeMS()
    monkeypatch.setattr(mail_graph, "_http", fake)
    s = _graph_settings(admin, graph_secret="geheim-xyz")
    assert s["mail_transport"] == "graph" and s["graph_secret_set"] and "graph_secret" not in s
    r = admin.post("/api/v1/settings/mail-test", json={"to": "chef@contoso.de"})
    assert r.status_code == 200, r.text
    (turl, _, tbody), (surl, sh, sbody) = fake.calls
    assert turl == "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token"
    assert "client_secret=geheim-xyz" in tbody and "grant_type=client_credentials" in tbody
    assert surl == "https://graph.microsoft.com/v1.0/users/zeit@contoso.de/sendMail"
    assert sh["Authorization"] == "Bearer tok-123"
    msg = json.loads(sbody)["message"]
    assert msg["toRecipients"][0]["emailAddress"]["address"] == "chef@contoso.de" and "Testmail" in msg["subject"]
    # Fehler von Microsoft wird verständlich gemeldet
    monkeypatch.setattr(mail_graph, "_http", FakeMS(send_status=403))
    r = admin.post("/api/v1/settings/mail-test", json={"to": "chef@contoso.de"})
    assert r.status_code == 502 and "Mail.Send" in r.json()["detail"]


@needs_openssl
def test_graph_with_certificate_and_report_attachment(admin, pem, monkeypatch):
    mail_graph._cache.clear()
    fake = FakeMS()
    monkeypatch.setattr(mail_graph, "_http", fake)
    assert admin.patch("/api/v1/settings", json={"graph_cert_pem": "kaputt"}).status_code == 400
    s = _graph_settings(admin, graph_cert_pem=pem["pkcs8"])
    assert len(s["graph_cert_thumbprint"]) == 40
    uid = admin.post("/api/v1/users", json={"username": "graphrep", "display_name": "Graph Rep", "password": "geheim123",
                                            "email": "rep@contoso.de", "tracking_start": "2026-01-01"}).json()["id"]
    r = admin.post(f"/api/v1/reports/mail", json={"user_id": uid, "period": "2026-10"})
    assert r.status_code == 200, r.text
    token_body = fake.calls[0][2]
    assert "client_assertion_type=urn%3Aietf%3Aparams%3Aoauth%3Aclient-assertion-type%3Ajwt-bearer" in token_body
    assert "client_secret" not in token_body       # Zertifikat hat Vorrang
    att = json.loads(fake.calls[1][2])["message"]["attachments"]
    assert att and att[0]["name"].endswith(".pdf") and base64.b64decode(att[0]["contentBytes"]).startswith(b"%PDF")


def test_secrets_not_in_settings_or_backup(admin):
    from app.backup import SECRET_SETTINGS
    assert {"graph_secret", "graph_cert_pem"} <= set(SECRET_SETTINGS)
    s = admin.get("/api/v1/settings").json()
    assert not any(k in s for k in ("graph_secret", "graph_cert_pem", "smtp_password"))


def test_scheduler_uses_graph_without_smtp_host(admin, monkeypatch):
    with connect() as conn:
        conn.execute("UPDATE app_settings SET value = '' WHERE key = 'smtp_host'")
        conn.execute("INSERT INTO app_settings(key, value) VALUES ('graph_secret', 'x') "
                     "ON CONFLICT(key) DO UPDATE SET value = 'x'")
        conn.execute("UPDATE app_settings SET value = 'graph' WHERE key = 'mail_transport'")
        assert mailer.mail_ready(conn)


# --- Eigener Mailweg (config.env) ---------------------------------------------------------------

def test_external_requires_config_env(admin, monkeypatch):
    monkeypatch.setattr(settings, "mail_command", "")
    monkeypatch.setattr(settings, "mail_socket", "")
    r = admin.patch("/api/v1/settings", json={"mail_transport": "external"})
    assert r.status_code == 400 and "config.env" in r.json()["detail"]


def test_external_command(admin, monkeypatch, tmp_path):
    out = tmp_path / "mail.eml"
    script = tmp_path / "send.py"
    script.write_text("import os, sys\nopen(sys.argv[1], 'wb').write(sys.stdin.buffer.read())\n"
                      "open(sys.argv[1] + '.to', 'w').write(os.environ['ZE_MAIL_TO'])\n")
    monkeypatch.setattr(settings, "mail_command", f"{sys.executable} {script} {out}")
    monkeypatch.setattr(settings, "mail_socket", "")
    assert admin.patch("/api/v1/settings", json={"mail_transport": "external", "smtp_from": "zeit@contoso.de"}).status_code == 200
    assert admin.get("/api/v1/settings").json()["mail_external"].startswith("Programm: ")
    assert admin.post("/api/v1/settings/mail-test", json={"to": "a@b.de"}).status_code == 200
    msg = email.message_from_bytes(out.read_bytes(), policy=email.policy.default)
    assert msg["To"] == "a@b.de" and "Testmail" in msg["Subject"] and (tmp_path / "mail.eml.to").read_text() == "a@b.de"
    # Programm mit Fehler
    monkeypatch.setattr(settings, "mail_command", f"{sys.executable} -c \"import sys; sys.stderr.write('kaputt'); sys.exit(3)\"")
    r = admin.post("/api/v1/settings/mail-test", json={"to": "a@b.de"})
    assert r.status_code == 502 and "kaputt" in r.json()["detail"]


def test_external_socket(admin, monkeypatch):
    path = tempfile.mktemp(suffix=".sock", dir="/tmp")
    got = {}
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(2)

    def serve():
        for reply in (b"OK queued\n", b"ERR Postfach voll\n"):
            c, _ = srv.accept()
            data = b""
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    break
                data += chunk
            got.setdefault("raw", data)
            c.sendall(reply)
            c.close()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        monkeypatch.setattr(settings, "mail_command", "")
        monkeypatch.setattr(settings, "mail_socket", path)
        assert admin.patch("/api/v1/settings", json={"mail_transport": "external"}).status_code == 200
        assert admin.post("/api/v1/settings/mail-test", json={"to": "x@y.de"}).status_code == 200
        msg = email.message_from_bytes(got["raw"], policy=email.policy.default)
        assert msg["To"] == "x@y.de"
        r = admin.post("/api/v1/settings/mail-test", json={"to": "x@y.de"})
        assert r.status_code == 502 and "Postfach voll" in r.json()["detail"]
    finally:
        srv.close()
        os.unlink(path)
        admin.patch("/api/v1/settings", json={"mail_transport": "smtp"})
