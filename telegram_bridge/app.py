"""A restricted transparent proxy for the Telegram Bot API."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse

from .response_handler import render_telegram_response, response_shape
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
BOT_API_PATH = re.compile(
    r"/bot(?P<token>[0-9]+:[A-Za-z0-9_-]+)/(?P<method>[A-Za-z][A-Za-z0-9_]*)\Z"
)
BOT_FILE_PATH = re.compile(r"/file/bot(?P<token>[0-9]+:[A-Za-z0-9_-]+)/.+\Z")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    allowed_bot_token: str | None
    telegram_origin: str

    @classmethod
    def from_environment(cls) -> "Settings":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None
        origin = os.environ.get("TELEGRAM_API_ORIGIN", DEFAULT_TELEGRAM_ORIGIN).rstrip("/")
        if not origin.startswith("https://"):
            raise RuntimeError("TELEGRAM_API_ORIGIN must be an HTTPS URL")
        return cls(allowed_bot_token=token, telegram_origin=origin)


@dataclass(frozen=True)
class TelegramTarget:
    upstream_path: str
    method_name: str | None


def resolve_telegram_target(path: str, allowed_token: str | None) -> TelegramTarget | None:
    """Allow Telegram Bot API paths, optionally pinning them to one bot token.

    Hermes already includes its bot token in each Bot API path.  Keeping the
    bridge tokenless prevents duplicating that secret in a second service while
    the service remains reachable only through Zeabur private networking.
    """

    api_match = BOT_API_PATH.fullmatch(path)
    if api_match:
        if allowed_token is not None and api_match.group("token") != allowed_token:
            return None
        return TelegramTarget(upstream_path=path, method_name=api_match.group("method"))

    file_match = BOT_FILE_PATH.fullmatch(path)
    if file_match:
        if allowed_token is not None and file_match.group("token") != allowed_token:
            return None
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


def _send_message_diagnostic(body: bytes, content_type: str | None) -> tuple[str, str] | None:
    """Return a hashed chat ID and response shape without retaining content."""

    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    try:
        if media_type == "application/json":
            fields = json.loads(body.decode("utf-8"))
        elif media_type == "application/x-www-form-urlencoded":
            fields = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        else:
            return None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(fields, dict) or not isinstance(fields.get("text"), str):
        return None
    chat_id = str(fields.get("chat_id", ""))
    chat_hash = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:12]
    return chat_hash, response_shape(fields["text"])


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


@app.post("/render")
async def render_response(request: Request) -> JSONResponse:
    """Render one Hermes final response for the Telegram sender hook.

    The service is reachable only on Zeabur private networking. It accepts only
    response text, never persists it, and does not call Telegram itself.
    """

    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise HTTPException(status_code=400, detail="text must be a string")
    logger.info("Telegram response render=%s", response_shape(payload["text"]))
    return JSONResponse({"text": render_telegram_response(payload["text"])})


@app.api_route("/{proxy_path:path}", methods=["GET", "POST"])
async def proxy(request: Request, proxy_path: str):
    settings: Settings = request.app.state.settings
    path = f"/{proxy_path}"
    target = resolve_telegram_target(path, settings.allowed_bot_token)
    if target is None:
        raise HTTPException(status_code=404, detail="not found")

    body = await request.body()
    if target.method_name == "sendMessage":
        diagnostic = _send_message_diagnostic(body, request.headers.get("content-type"))
        if diagnostic is not None:
            chat_hash, shape = diagnostic
            logger.info("Telegram sendMessage chat=%s response=%s", chat_hash, shape)
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
