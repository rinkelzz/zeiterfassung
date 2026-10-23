"""Schutz vor Cross-Site-Request-Forgery (CSRF).

Der Login-Cookie hat über HTTPS SameSite=None (damit er im DSM-Fenster/iframe mitgeht). Ohne weitere Prüfung
könnte eine fremde Webseite deshalb im Namen eines angemeldeten Nutzers stempeln oder Admin-Aktionen auslösen.

Regel für ändernde Anfragen (POST/PUT/PATCH/DELETE) an /api/:
- Mit eigenem Header (Bearer, X-Session-Token, X-Kiosk-Token, Proxy-Schlüssel) ist alles gut – solche Header
  kann eine fremde Seite ohne CORS-Freigabe gar nicht setzen.
- Sonst (nur Cookie): Moderne Browser melden mit Sec-Fetch-Site, woher die Anfrage kommt – „cross-site“ wird
  abgelehnt, „same-origin“ erlaubt. Sonst muss Origin (oder Referer) zur eigenen Adresse passen.
"""
from typing import Optional, Set
from urllib.parse import urlsplit

from .config import settings

UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
OWN_AUTH_HEADERS = ("authorization", "x-session-token", "x-kiosk-token", "x-ze-proxy-secret")


def _netloc(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:
        return ""


def _own_hosts(headers) -> Set[str]:
    hosts = {h.strip().lower() for h in (headers.get("host"), headers.get("x-forwarded-host")) if h}
    for h in list(hosts):  # Standard-Ports weglassen dürfen
        for port in (":80", ":443"):
            if h.endswith(port):
                hosts.add(h[: -len(port)])
    if settings.public_url:
        hosts.add(_netloc(settings.public_url))
    hosts.discard("")
    return hosts


def blocked(method: str, path: str, headers) -> Optional[str]:
    """Liefert einen Grund, wenn die Anfrage als CSRF abgelehnt werden soll, sonst None."""
    if method.upper() not in UNSAFE or "/api/" not in path:
        return None
    if any(headers.get(h) for h in OWN_AUTH_HEADERS):
        return None
    site = (headers.get("sec-fetch-site") or "").lower()
    if site == "cross-site":
        return "Anfrage von einer fremden Webseite abgelehnt"
    if site in ("same-origin", "none"):
        return None
    # „same-site“ (andere Subdomain derselben Domain) und ältere Browser: Origin/Referer entscheiden
    origin = headers.get("origin")
    if origin is not None:
        if origin == "null" or _netloc(origin) not in _own_hosts(headers):
            return "Anfrage von einer fremden Webseite abgelehnt"
        return None
    referer = headers.get("referer")
    if referer and _netloc(referer) not in _own_hosts(headers):
        return "Anfrage von einer fremden Webseite abgelehnt"
    return None  # alte Clients/Werkzeuge ohne Origin und Referer (z. B. curl)
