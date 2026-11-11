"""Mailversand über Microsoft 365 (Microsoft Graph, sendMail) – ohne Zusatzbibliotheken.

Anmeldung als Anwendung (Client-Credentials) an einer Entra-App-Registrierung mit der Anwendungsberechtigung
Mail.Send – entweder mit Client-Secret oder mit Zertifikat. Für das Zertifikat wird die Anmeldung (JWT, RS256)
hier selbst signiert; dafür reicht Pythons eingebautes pow(), eine Krypto-Bibliothek ist auf der NAS nicht nötig.

Erwartet wird ein PEM-Text mit Zertifikat (BEGIN CERTIFICATE) und unverschlüsseltem RSA-Schlüssel
(BEGIN PRIVATE KEY oder BEGIN RSA PRIVATE KEY). Aus einer .pfx-Datei z. B. mit
  openssl pkcs12 -in zert.pfx -nodes -out zert.pem
"""
import base64
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Dict, List, Optional, Tuple

LOGIN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
SEND = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
SCOPE = "https://graph.microsoft.com/.default"
MAX_BODY = 3_500_000  # Graph nimmt Anfragen bis 4 MB an (Anhänge base64)


class GraphError(Exception):
    pass


# --- PEM / DER --------------------------------------------------------------------------------

def pem_blocks(text: str) -> List[Tuple[str, bytes]]:
    out = []
    for kind, body in re.findall(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", text or "", re.S):
        try:
            out.append((kind, base64.b64decode("".join(body.split()))))
        except ValueError:
            raise GraphError(f"PEM-Block „{kind}“ ist beschädigt")
    return out


def _der(data: bytes, pos: int = 0) -> Tuple[int, bytes, int]:
    """Ein DER-Element lesen → (Tag, Inhalt, Position danach)."""
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    return tag, data[pos:pos + length], pos + length


def _seq(data: bytes) -> List[Tuple[int, bytes]]:
    items, pos = [], 0
    while pos < len(data):
        tag, val, pos = _der(data, pos)
        items.append((tag, val))
    return items


RSA_OID = bytes.fromhex("2a864886f70d010101")


def rsa_key(der: bytes, pkcs8: bool) -> Tuple[int, int, int]:
    """→ (n, e, d) aus PKCS#8 (PRIVATE KEY) oder PKCS#1 (RSA PRIVATE KEY)."""
    try:
        tag, body, _ = _der(der)
        parts = _seq(body)
        if pkcs8:
            alg = _seq(parts[1][1])
            if alg[0][1] != RSA_OID:
                raise GraphError("Nur RSA-Schlüssel werden unterstützt")
            _, body, _ = _der(parts[2][1])
            parts = _seq(body)
        n, e, d = (int.from_bytes(parts[i][1], "big") for i in (1, 2, 3))
    except GraphError:
        raise
    except (IndexError, ValueError):
        raise GraphError("Privater Schlüssel konnte nicht gelesen werden")
    if n.bit_length() < 2048:
        raise GraphError("Der Schlüssel ist zu kurz (mindestens 2048 Bit)")
    return n, e, d


def load_credentials(pem: str) -> Dict:
    """Zertifikat + Schlüssel aus einem PEM-Text → {cert_der, key, thumbprint}."""
    blocks = pem_blocks(pem)
    if any(k == "ENCRYPTED PRIVATE KEY" for k, _ in blocks):
        raise GraphError("Der Schlüssel ist mit Passwort geschützt – bitte ohne Passwort exportieren (openssl … -nodes)")
    cert = next((d for k, d in blocks if k == "CERTIFICATE"), None)
    key = next(((d, k == "PRIVATE KEY") for k, d in blocks if k in ("PRIVATE KEY", "RSA PRIVATE KEY")), None)
    if not cert:
        raise GraphError("Im Text fehlt das Zertifikat (-----BEGIN CERTIFICATE-----)")
    if not key:
        raise GraphError("Im Text fehlt der private Schlüssel (-----BEGIN PRIVATE KEY-----)")
    n, e, d = rsa_key(*key)
    return {"cert_der": cert, "key": (n, e, d), "thumbprint": hashlib.sha1(cert).hexdigest().upper()}


# --- JWT (RS256) ------------------------------------------------------------------------------

SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def rs256(key: Tuple[int, int, int], data: bytes) -> bytes:
    n, _, d = key
    k = (n.bit_length() + 7) // 8
    t = SHA256_PREFIX + hashlib.sha256(data).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def client_assertion(tenant: str, client_id: str, creds: Dict, now: Optional[int] = None) -> str:
    now = int(now or time.time())
    header = {"alg": "RS256", "typ": "JWT", "x5t": _b64u(hashlib.sha1(creds["cert_der"]).digest())}
    claims = {"aud": LOGIN.format(tenant=tenant), "iss": client_id, "sub": client_id, "jti": str(uuid.uuid4()),
              "nbf": now - 60, "iat": now, "exp": now + 600}
    signing = _b64u(json.dumps(header, separators=(",", ":")).encode()) + "." + \
        _b64u(json.dumps(claims, separators=(",", ":")).encode())
    return signing + "." + _b64u(rs256(creds["key"], signing.encode()))


# --- Token und Versand ------------------------------------------------------------------------

_cache: Dict[tuple, Tuple[str, float]] = {}
_lock = threading.Lock()


def _http(req: urllib.request.Request, timeout: int = 30) -> Tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise GraphError(f"Microsoft nicht erreichbar: {getattr(e, 'reason', e)}")


def _ms_error(body: bytes) -> str:
    try:
        j = json.loads(body.decode() or "{}")
    except ValueError:
        return body[:200].decode(errors="replace")
    if "error_description" in j:
        return j["error_description"].split("\r\n")[0].split(" Trace ID")[0]
    err = j.get("error")
    if isinstance(err, dict):
        return f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
    return str(err or j)[:200]


def token(cfg: Dict) -> str:
    key = (cfg["tenant"], cfg["client_id"], cfg.get("secret") or "", cfg.get("cert_thumbprint") or "")
    with _lock:
        hit = _cache.get(key)
        if hit and hit[1] > time.time() + 60:
            return hit[0]
    form = {"client_id": cfg["client_id"], "scope": SCOPE, "grant_type": "client_credentials"}
    if cfg.get("creds"):
        form["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        form["client_assertion"] = client_assertion(cfg["tenant"], cfg["client_id"], cfg["creds"])
    elif cfg.get("secret"):
        form["client_secret"] = cfg["secret"]
    else:
        raise GraphError("Für Microsoft 365 fehlt das Client-Secret oder das Zertifikat")
    req = urllib.request.Request(LOGIN.format(tenant=urllib.parse.quote(cfg["tenant"], safe="")),
                                 data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    status, body = _http(req)
    if status != 200:
        raise GraphError(f"Anmeldung bei Microsoft fehlgeschlagen: {_ms_error(body)}")
    j = json.loads(body.decode())
    with _lock:
        _cache[key] = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
    return j["access_token"]


def send(cfg: Dict, sender: str, sender_name: str, to: str, subject: str, body: str,
         attachments: List[Tuple[str, bytes, str]]) -> None:
    message = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": to}}],
        "from": {"emailAddress": {"address": sender, "name": sender_name}},
        "attachments": [{"@odata.type": "#microsoft.graph.fileAttachment", "name": name, "contentType": mime,
                         "contentBytes": base64.b64encode(data).decode()} for name, data, mime in attachments],
    }
    payload = json.dumps({"message": message, "saveToSentItems": bool(cfg.get("save_sent"))}).encode()
    if len(payload) > MAX_BODY:
        raise GraphError("Die Mail ist für Microsoft Graph zu groß (höchstens etwa 3 MB Anhänge)")
    req = urllib.request.Request(SEND.format(sender=urllib.parse.quote(sender, safe="@")), data=payload, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token(cfg)})
    status, resp = _http(req, timeout=60)
    if status == 401:  # Token abgelaufen/zurückgezogen → einmal neu holen
        with _lock:
            _cache.clear()
        req.add_header("Authorization", "Bearer " + token(cfg))
        status, resp = _http(req, timeout=60)
    if status not in (200, 202):
        hint = ""
        if status == 403:
            hint = " – hat die App die Anwendungsberechtigung Mail.Send mit Administratorzustimmung?"
        elif status == 404:
            hint = " – gibt es das Postfach der Absenderadresse?"
        raise GraphError(f"Microsoft hat die Mail abgelehnt ({status}): {_ms_error(resp)}{hint}")
