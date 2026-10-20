"""Weiterleitung DSM-Fenster → Zeiterfassung (nur Standardbibliothek, Python 3.6+).

Wird von ui/app.cgi aufgerufen, also vom Webserver von DSM. Dadurch kommt die
Zeiterfassung aus Sicht des Browsers von DSM selbst:
- DSM blockiert den Inhalt im Fenster nicht mehr (gleicher Ursprung)
- kein http/https-Problem
- der angemeldete DSM-Benutzer wird hier über authenticate.cgi ermittelt und mit
  einem Geheimnis an die App weitergegeben → Anmeldung ohne zweiten Login.

Aufruf: app.cgi?SynoToken=…&p=/pfad?query
"""
import http.client
import os
import re
import subprocess
import sys
from urllib.parse import parse_qs, quote

PKG = "zeiterfassung"
TARGET = os.environ.get("ZE_PKG_TARGET", "/var/packages/%s/target" % PKG)
VAR = os.environ.get("ZE_PKG_VAR", "/var/packages/%s/var" % PKG)
AUTH_CGI = os.environ.get("ZE_AUTH_CGI", "/usr/syno/synoman/webman/modules/authenticate.cgi")
ID_CMD = os.environ.get("ZE_ID_CMD", "id")
USER_RE = re.compile(r"^[A-Za-z0-9._@\-\\ ]{1,64}$")
PASS_HEADERS = ("CONTENT_TYPE", "HTTP_ACCEPT", "HTTP_ACCEPT_LANGUAGE", "HTTP_X_SESSION_TOKEN")
RETURN_HEADERS = ("content-type", "content-disposition", "cache-control", "etag", "last-modified")


def out(status, headers, body=b""):
    w = sys.stdout.buffer
    w.write(("Status: %s\r\n" % status).encode())
    for k, v in headers:
        w.write(("%s: %s\r\n" % (k, v)).encode("latin-1", "replace"))
    w.write(b"\r\n")
    w.write(body)
    w.flush()


def fail(status, text):
    out(status, [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store")], text.encode("utf-8"))


def read_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def port():
    m = re.search(r"^ZE_PORT=\"?(\d+)", read_file(os.path.join(VAR, "config.env")), re.M)
    return int(m.group(1)) if m else int(read_file(os.path.join(TARGET, ".port")) or 8765)


def dsm_user(token):
    """Angemeldeten DSM-Benutzer über Synologys authenticate.cgi ermitteln."""
    env = dict(os.environ)
    if token:
        env["QUERY_STRING"] = "SynoToken=" + quote(token)
        env["HTTP_X_SYNO_TOKEN"] = token
    try:
        res = subprocess.run([AUTH_CGI], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    name = res.stdout.decode("utf-8", "replace").strip().splitlines()
    name = name[-1].strip() if name else ""
    return name if USER_RE.match(name) else ""


def is_admin(user):
    try:
        res = subprocess.run([ID_CMD, "-Gn", user], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        groups = res.stdout.decode("utf-8", "replace").split()
        if "administrators" in groups:
            return True
        res = subprocess.run([ID_CMD, "-G", user], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        return "101" in res.stdout.decode().split()
    except (OSError, subprocess.SubprocessError):
        return False


def main():
    qs = parse_qs(os.environ.get("QUERY_STRING", ""), keep_blank_values=True)
    path = (qs.get("p") or ["/"])[0] or "/"
    if not path.startswith("/") or path.startswith("//") or "\r" in path or "\n" in path:
        return fail("400 Bad Request", "Ungültiger Pfad")
    token = (qs.get("SynoToken") or [""])[0] or os.environ.get("HTTP_X_SYNO_TOKEN", "")

    secret = read_file(os.path.join(TARGET, ".proxy_secret"))
    if not secret:
        return fail("503 Service Unavailable", "Zeiterfassung ist noch nicht gestartet (kein Proxy-Schlüssel). Bitte das Paket starten.")

    user = dsm_user(token)
    headers = {"X-ZE-Proxy-Secret": secret, "X-ZE-Proxy-Base": "app.cgi"}
    if user:
        headers["X-DSM-User"] = quote(user)
        headers["X-DSM-Admin"] = "1" if is_admin(user) else "0"
    for key in PASS_HEADERS:
        if os.environ.get(key):
            name = "Content-Type" if key == "CONTENT_TYPE" else key[5:].replace("_", "-").title()
            headers[name] = os.environ[key]

    method = os.environ.get("REQUEST_METHOD", "GET").upper()
    # Der Webserver von DSM reicht nur GET/POST zuverlässig an CGIs durch.
    # PATCH/PUT/DELETE kommen deshalb als POST mit X-HTTP-Method-Override.
    override = os.environ.get("HTTP_X_HTTP_METHOD_OVERRIDE", "").upper()
    if method == "POST" and override in ("PATCH", "PUT", "DELETE"):
        method = override
    body = None
    length = int(os.environ.get("CONTENT_LENGTH") or 0)
    if length > 0:
        if length > 6 * 1024 * 1024:
            return fail("413 Payload Too Large", "Anfrage zu groß")
        body = sys.stdin.buffer.read(length)

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port(), timeout=60)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
    except OSError as e:
        return fail("502 Bad Gateway", "Zeiterfassung nicht erreichbar – läuft das Paket? (%s)" % e)
    hdrs = [(k, v) for k, v in resp.getheaders() if k.lower() in RETURN_HEADERS and k.lower() != "cache-control"]
    if path.startswith("/api/"):  # API-Antworten nie zwischenspeichern
        hdrs += [("Cache-Control", "no-store, no-cache, must-revalidate"), ("Pragma", "no-cache"), ("Expires", "0")]
    else:
        hdrs += [(k, v) for k, v in resp.getheaders() if k.lower() == "cache-control"]
    out("%d %s" % (resp.status, resp.reason), hdrs, data)


if __name__ == "__main__":
    main()
