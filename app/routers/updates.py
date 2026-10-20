"""Paketquelle fürs Paket-Zentrum (/spk) und Update-Status für Admins."""
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import updates
from ..security import admin_user

router = APIRouter(tags=["Updates"])


def _local_only(request: Request) -> None:
    """Die Paketquelle ist nur für die NAS selbst und das Heimnetz gedacht, nicht fürs Internet."""
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return
    except ValueError:
        if host in ("localhost", "testclient"):
            return
    raise HTTPException(403, "Paketquelle nur im lokalen Netz erreichbar")


@router.api_route("/spk", methods=["GET", "POST"], include_in_schema=False)
def package_source(request: Request):
    """Paketliste im Format, das DSM für Paketquellen erwartet."""
    _local_only(request)
    base = str(request.base_url).rstrip("/")
    return JSONResponse(updates.catalog(base), headers={"Cache-Control": "no-store"})


@router.get("/spk/download/{name}", include_in_schema=False)
def package_download(name: str, request: Request):
    _local_only(request)
    path = updates.cached_file(name)
    if not path:
        raise HTTPException(404, "Paket nicht gefunden")
    return FileResponse(str(path), media_type="application/octet-stream", filename=name)


@router.get("/api/v1/updates")
def update_status(admin=Depends(admin_user)):
    """Installierte und neueste Version, Adresse der Paketquelle."""
    updates.check_in_background()
    return updates.status()


@router.post("/api/v1/updates/check")
def update_check(admin=Depends(admin_user)):
    """Sofort bei GitHub nachsehen (lädt eine neue SPK ggf. direkt herunter)."""
    return updates.check(force=True)
