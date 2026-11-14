"""Loopback-only authenticated FastAPI interface. No cloud services or CDN assets."""

import hmac
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from batya.downloads import DownloadManager
from batya.system import select_cookie_file, select_folder


class DownloadInput(BaseModel):
    url: str = Field(min_length=1, max_length=8192)
    quality: Literal["1080", "720", "best", "audio"] = "1080"


class SettingsInput(BaseModel):
    folder: str = Field(min_length=1, max_length=4096)
    quality: Literal["1080", "720", "best", "audio"] = "1080"
    browser: Literal["none", "chrome", "firefox", "edge", "brave", "safari", "file"] | None = None
    cookies_file: str | None = Field(default=None, max_length=4096)


def create_app(manager: DownloadManager, origin: str, token: str | None = None) -> FastAPI:
    secret = token or secrets.token_urlsafe(32)
    host = urlsplit(origin).netloc
    cookie = f"batya_{urlsplit(origin).port}"
    static = Path(__file__).parent / "static"

    @asynccontextmanager
    async def lifespan(app):
        manager.start()
        yield
        manager.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = secret
    app.state.shutdown = lambda: None

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.headers.get("host", "") != host:
            return PlainTextResponse("Недопустимый адрес приложения.", status_code=400)
        if request.url.path.startswith("/api/"):
            supplied = request.cookies.get(cookie, "")
            if not hmac.compare_digest(supplied.encode(), secret.encode()):
                return JSONResponse({"detail": "Запустите приложение двойным кликом."}, status_code=401)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Запрос с другого сайта запрещён."}, status_code=403)
            if request.method != "GET" and request.headers.get("origin", origin) != origin:
                return JSONResponse({"detail": "Запрос с другого сайта запрещён."}, status_code=403)
        response = await call_next(request)
        response.headers.update(
            {
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
            }
        )
        return response

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(OSError)
    async def os_error(request: Request, exc: OSError):
        return JSONResponse({"detail": f"Не удалось выполнить действие: {exc}"}, status_code=400)

    @app.get("/")
    def index(request: Request):
        key = request.query_params.get("key", "")
        if key and hmac.compare_digest(key.encode(), secret.encode()):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(cookie, secret, httponly=True, samesite="strict")
            return response
        supplied = request.cookies.get(cookie, "")
        if not hmac.compare_digest(supplied.encode(), secret.encode()):
            return PlainTextResponse("Откройте приложение двойным кликом, чтобы войти.", status_code=401)
        return FileResponse(static / "index.html")

    @app.get("/api/state")
    def state():
        return manager.snapshot()

    @app.post("/api/jobs", status_code=201)
    def download(body: DownloadInput):
        return manager.enqueue(body.url, body.quality)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        manager.cancel(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/open")
    def play(job_id: str):
        manager.open_media(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/folder")
    def job_folder(job_id: str):
        manager.open_folder(job_id)
        return {"ok": True}

    @app.post("/api/folder/open")
    def folder_open():
        manager.open_folder()
        return {"ok": True}

    @app.post("/api/settings")
    def settings(body: SettingsInput):
        return manager.save_settings(body.folder, body.quality, body.browser, body.cookies_file)

    @app.post("/api/cookies/pick")
    def cookies_pick():
        return {"file": select_cookie_file()}

    @app.post("/api/folder/pick")
    def folder_pick():
        folder = select_folder(manager.snapshot()["settings"]["folder"])
        return {"folder": folder}

    @app.post("/api/update")
    def update():
        manager.begin_update()
        return {"ok": True}

    @app.post("/api/quit")
    def quit_app():
        threading.Timer(0.25, app.state.shutdown).start()
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=static), name="static")
    return app
