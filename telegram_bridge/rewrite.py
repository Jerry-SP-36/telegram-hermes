"""Pure request-body rewriting used by the HTTP proxy and unit tests."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlencode

from .response_handler import render_telegram_response


def _content_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def rewrite_send_message_body(body: bytes, content_type: str | None) -> bytes:
    """Rewrite a JSON or form-encoded Telegram sendMessage payload.

    Telegram clients normally use one of these encodings for `sendMessage`.
    A body that cannot be safely decoded is left untouched so the upstream API
    can report its normal protocol error.
    """

    media_type = _content_type(content_type)
    if media_type == "application/json":
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            return body
        payload["text"] = render_telegram_response(payload["text"])
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    if media_type == "application/x-www-form-urlencoded":
        try:
            fields = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            return body
        rewritten = [
            (name, render_telegram_response(value) if name == "text" else value)
            for name, value in fields
        ]
        return urlencode(rewritten).encode("utf-8")

    return body
