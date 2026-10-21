"""Updates übers Paket-Zentrum: die App ist ihre eigene Paketquelle.

Ablauf:
1. Die App fragt (höchstens alle 30 Minuten) bei GitHub nach dem neuesten Release.
2. Hat es eine SPK (noarch, ältere Releases: x86_64), wird sie einmal in den Datenordner geladen (mit MD5 und Größe).
3. Das Paket-Zentrum fragt die Paketquelle  http://127.0.0.1:<port>/spk  ab und bekommt die
   Paketliste im Format, das DSM erwartet (wie bei SynoCommunity). Ist die Version neuer als die
   installierte, bietet DSM „Aktualisieren“ an. Heruntergeladen wird von der NAS selbst
   (/spk/download/<datei>), nicht direkt von GitHub.
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import __version__
from .config import settings

REPO = os.environ.get("ZE_UPDATE_REPO", "rinkelzz/zeiterfassung")
API_URL = "https://api.github.com/repos/{repo}/releases/latest"
RAW_URL = "https://raw.githubusercontent.com/{repo}/main/synology/{name}"
CHECK_EVERY = 30 * 60
# Ab 0.14.0 ein Paket für alle Prozessoren (noarch); bis 0.13.x nur x86_64
SPK_RE = re.compile(r"^zeiterfassung-(\d+\.\d+\.\d+(?:-\d+)?)-(noarch|x86_64)\.spk$")
MAX_SIZE = 200 * 1024 * 1024

_lock = threading.Lock()
_state: Dict = {"checked_at": 0.0, "release": None, "error": None}


def cache_dir() -> Path:
    return settings.data_dir / "updates"


def version_tuple(v: str) -> Tuple[int, ...]:
    """'0.8.1-0002' → (0, 8, 1, 2); Unbekanntes zählt als 0."""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")) or (0,)


def is_newer(candidate: str, installed: str = __version__) -> bool:
    c, i = version_tuple(candidate), version_tuple(installed)
    return c[:3] > i[:3]


def _http(url: str, timeout: float = 20):
    req = urllib.request.Request(url, headers={
        "User-Agent": f"zeiterfassung/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 – feste https-Adressen


def fetch_latest() -> Optional[Dict]:
    """Neuestes Release mit SPK von GitHub (oder None, wenn keins da ist). noarch hat Vorrang."""
    try:
        with _http(API_URL.format(repo=REPO)) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:  # noch kein Release veröffentlicht
            return None
        raise
    assets = sorted(data.get("assets", []), key=lambda a: "-noarch.spk" not in a.get("name", ""))
    for asset in assets:
        m = SPK_RE.match(asset.get("name", ""))
        if m:
            return {
                "tag": data.get("tag_name", ""),
                "version": m.group(1),  # z. B. 0.8.1-0001 (so steht es in INFO)
                "name": asset["name"],
                "url": asset["browser_download_url"],
                "size": int(asset.get("size") or 0),
                "notes": (data.get("body") or "").strip(),
                "published_at": data.get("published_at"),
                "html_url": data.get("html_url"),
            }
    return None


def _download(rel: Dict) -> Dict:
    """SPK in den Cache laden (falls noch nicht da) und MD5 bestimmen."""
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    target = d / rel["name"]
    meta = d / (rel["name"] + ".json")
    if target.exists() and meta.exists():
        info = json.loads(meta.read_text())
        if info.get("size") == target.stat().st_size and (not rel["size"] or rel["size"] == info["size"]):
            return info
    tmp = d / (rel["name"] + ".part")
    md5 = hashlib.md5()  # noqa: S324 – DSM verlangt MD5 als Prüfsumme
    size = 0
    with _http(rel["url"], timeout=120) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_SIZE:
                raise ValueError("Download zu groß")
            md5.update(chunk)
            f.write(chunk)
    if rel["size"] and size != rel["size"]:
        tmp.unlink()
        raise ValueError(f"Download unvollständig ({size} von {rel['size']} Bytes)")
    tmp.replace(target)
    info = {"name": rel["name"], "size": size, "md5": md5.hexdigest()}
    meta.write_text(json.dumps(info))
    # ältere SPKs aufräumen
    for old in d.glob("zeiterfassung-*.spk*"):
        if not old.name.startswith(rel["name"]):
            try:
                old.unlink()
            except OSError:
                pass
    return info


def check(force: bool = False) -> Dict:
    """Neuestes Release abfragen und SPK bereitlegen. Liefert den aktuellen Stand."""
    if not force and time.time() - _state["checked_at"] < CHECK_EVERY:
        return status()
    if not _lock.acquire(blocking=force):
        return status()  # läuft gerade schon
    try:
        rel = fetch_latest()
        old = _state["release"]
        if rel and old and old.get("name") == rel["name"] and old.get("md5"):
            rel.update(md5=old["md5"], size=old["size"])  # schon da
        _state.update(release=rel, error=None)  # auch ohne fertigen Download schon anbieten
        if rel and not rel.get("md5"):
            rel.update(_download(rel))
    except Exception as e:  # noqa: BLE001 – Netzwerkfehler sollen die App nie stören
        _state["error"] = f"{type(e).__name__}: {e}"
    finally:
        _state["checked_at"] = time.time()
        _lock.release()
    return status()


def check_in_background() -> None:
    if time.time() - _state["checked_at"] >= CHECK_EVERY and not _lock.locked():
        threading.Thread(target=check, name="update-check", daemon=True).start()


def status() -> Dict:
    rel = _state["release"]
    return {
        "installed": __version__,
        "repo": REPO,
        "latest": rel["version"].split("-")[0] if rel else None,
        "latest_release": {k: rel.get(k) for k in ("tag", "version", "name", "size", "published_at", "html_url")} if rel else None,
        "update_available": bool(rel and is_newer(rel["version"])),
        "ready": bool(rel and rel.get("md5")),
        "checked_at": _state["checked_at"] or None,
        "error": _state["error"],
        "source_url": f"http://127.0.0.1:{settings.port}{settings.root_path}/spk",
    }


def _plain_notes(md: str, limit: int = 1500) -> str:
    text = re.sub(r"[*_`#>]+", "", md or "")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def catalog(base_url: str) -> Dict:
    """Paketliste für das Paket-Zentrum (Format wie spkrepo/SynoCommunity, DSM 7)."""
    rel = _state["release"]
    if not rel:
        check_in_background()
        return {"packages": []}
    cached = bool(rel.get("md5")) and (cache_dir() / rel["name"]).exists()
    icon = RAW_URL.format(repo=REPO, name="PACKAGE_ICON.PNG")
    icon256 = RAW_URL.format(repo=REPO, name="PACKAGE_ICON_256.PNG")
    entry = {
        "package": "zeiterfassung",
        "version": rel["version"],
        "dname": "Zeiterfassung",
        "desc": "Kommen/Gehen-Zeiterfassung für mehrere Mitarbeiter – mit iPhone-Kurzbefehlen, NFC, "
                "Gleitzeit- und Überstundenkonto, Urlaub, Berichten als PDF/Excel und per Mail.",
        # liegt die SPK schon auf der NAS, von dort; sonst (noch beim Herunterladen) direkt von GitHub
        "link": f"{base_url}/spk/download/{rel['name']}" if cached else rel["url"],
        "thumbnail": [icon, icon256],
        "thumbnail_retina": [icon256, icon256],
        "snapshot": [],
        # Neuinstallation hat einen Assistenten (Admin-Passwort), Updates laufen ohne Rückfrage
        "qinst": False,
        "qupgrade": True,
        "qstart": False,
        "startable": "yes",
        "deppkgs": None,
        "conflictpkgs": None,
        "download_count": 0,
        "recent_download_count": 0,
        "size": rel["size"],
        "maintainer": "rinkelzz",
        "maintainer_url": f"https://github.com/{REPO}",
        "distributor": "rinkelzz",
        "distributor_url": f"https://github.com/{REPO}",
        "support_url": f"https://github.com/{REPO}/issues",
        "changelog": _plain_notes(rel.get("notes", "")),
    }
    if cached:
        entry["md5"] = rel["md5"]
    check_in_background()
    return {"packages": [entry]}


def cached_file(name: str) -> Optional[Path]:
    rel = _state["release"]
    if not rel or name != rel["name"] or not SPK_RE.match(name):
        return None
    p = cache_dir() / name
    return p if p.exists() else None
