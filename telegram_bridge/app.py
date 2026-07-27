"""A restricted transparent proxy for the Telegram Bot API."""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse

from .rewrite import rewrite_send_message_body


DEFAULT_TELEGRAM_ORIGIN = "https://api.telegram.org"
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
METHOD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    telegram_origin: str

    @classmethod
    def from_environment(cls) -> "Settings":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        origin = os.environ.get("TELEGRAM_API_ORIGIN", DEFAULT_TELEGRAM_ORIGIN).rstrip("/")
        if not origin.startswith("https://"):
            raise RuntimeError("TELEGRAM_API_ORIGIN must be an HTTPS URL")
        return cls(bot_token=token, telegram_origin=origin)


@dataclass(frozen=True)
class TelegramTarget:
    upstream_path: str
    method_name: str | None


def resolve_telegram_target(path: str, token: str) -> TelegramTarget | None:
    """Allow only configured-token Bot API or Bot API file paths."""

    api_prefix = f"/bot{token}/"
    file_prefix = f"/file/bot{token}/"
    if path.startswith(api_prefix):
        method_name = path[len(api_prefix) :]
        if not METHOD_NAME.fullmatch(method_name):
            return None
        return TelegramTarget(upstream_path=path, method_name=method_name)
    if path.startswith(file_prefix) and path[len(file_prefix) :]:
        return TelegramTarget(upstream_path=path, method_name=None)
    return None


def _request_headers(request: Request) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS
    }


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_environment()
    timeout = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=30.0)
    app.state.settings = settings
    app.state.client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(title="Telegram Response Bridge", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.api_route("/{proxy_path:path}", methods=["GET", "POST"])
async def proxy(request: Request, proxy_path: str):
    settings: Settings = request.app.state.settings
    path = f"/{proxy_path}"
    target = resolve_telegram_target(path, settings.bot_token)
    if target is None:
        raise HTTPException(status_code=404, detail="not found")

    body = await request.body()
    if target.method_name == "sendMessage":
        body = rewrite_send_message_body(body, request.headers.get("content-type"))

    upstream_url = f"{settings.telegram_origin}{target.upstream_path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"
    upstream_request = request.app.state.client.build_request(
        request.method,
        upstream_url,
        headers=_request_headers(request),
        content=body,
    )
    upstream_response = await request.app.state.client.send(upstream_request, stream=True)
    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response),
        background=BackgroundTask(upstream_response.aclose),
    )
