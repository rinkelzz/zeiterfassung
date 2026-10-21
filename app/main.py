"""Einstiegspunkt der Zeiterfassung."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import settings
from .db import connect, init_db
from .dsm import write_ui_config
from .dsm_auth import proxy_info, publish_proxy_files
from .mailer import start_scheduler, stop_scheduler
from .routers import auth, backup, clock, closings, entries, importer, reports, updates, users


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    conn = connect()
    try:
        write_ui_config(conn)
        publish_proxy_files()
    finally:
        conn.close()
    if settings.mail_scheduler:
        start_scheduler()
    if settings.update_check:
        from . import updates
        updates.check_in_background()  # gleich beim Start, damit die Paketquelle sofort etwas liefert
    yield
    stop_scheduler()


app = FastAPI(
    title="Zeiterfassung",
    version=__version__,
    description="Kommen/Gehen-Zeiterfassung für die Synology NAS – Web, iPhone-Kurzbefehle, NFC, Kiosk.",
    root_path=settings.root_path,
    lifespan=lifespan,
)

for r in (auth.router, backup.router, clock.router, closings.router, entries.router, importer.router, reports.router, updates.router, users.router):
    app.include_router(r)


@app.middleware("http")
async def no_cache_for_api(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index(request: Request):
    # Versionsnummer an CSS/JS hängen, damit nach einem Update nichts Altes aus dem Cache kommt.
    # Im DSM-Fenster laufen alle Adressen über app.cgi?p=… (gleicher Ursprung wie DSM).
    proxied = proxy_info(request)
    base = (proxied["base"] + "?p=/") if proxied else ""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("{{v}}", __version__)
    html = html.replace('"static/', '"' + base + "static/")
    html = html.replace("<head>", "<head>\n<script>window.ZE_PROXY=" + ("\"" + proxied["base"] + "\"" if proxied else "null") + ";</script>", 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/setup", include_in_schema=False)
def setup_page():
    """Einrichtungsseite fürs iPhone (QR-Code). Das Token kommt per #t=… und bleibt im Browser."""
    html = (STATIC_DIR / "setup.html").read_text(encoding="utf-8").replace("{{v}}", __version__)
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/api/v1/health", tags=["System"])
def health():
    """Ohne Login – für DSM-Statusprüfung und Reverse-Proxy-Checks."""
    return {"status": "ok", "version": __version__}


def run() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    run()
