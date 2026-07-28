"""Hermes Telegram sender hook for the private Response Bridge.

Install this file as ``sitecustomize.py`` in the Hermes runtime Python path.
It changes only replies to a user message: Hermes' final text first goes to the
private ``/render`` endpoint, then the existing Telegram sender delivers the
short rendered result.
"""

import asyncio
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from telegram import Bot

try:
    from telegram.ext import ExtBot
except ImportError:  # pragma: no cover - depends on installed Telegram SDK
    ExtBot = None


RENDER_URL = "http://telegram-hermes.zeabur.internal:8080/render"
FORMAT_ERROR = "❌ 回覆格式錯誤，請再試一次。"


def _render_for_telegram(raw_text: str) -> str:
    request = Request(
        RENDER_URL,
        data=json.dumps({"text": raw_text}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            rendered = json.load(response).get("text")
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
        return FORMAT_ERROR
    return rendered if isinstance(rendered, str) else FORMAT_ERROR


def _is_user_reply(kwargs: dict) -> bool:
    return kwargs.get("reply_to_message_id") is not None or kwargs.get("reply_parameters") is not None


def _wrap_send_message(original_send_message):
    async def send_message_with_response_handler(self, *args, **kwargs):
        if not _is_user_reply(kwargs):
            return await original_send_message(self, *args, **kwargs)

        if isinstance(kwargs.get("text"), str):
            updated_kwargs = dict(kwargs)
            updated_kwargs["text"] = await asyncio.to_thread(_render_for_telegram, kwargs["text"])
            return await original_send_message(self, *args, **updated_kwargs)

        if len(args) >= 2 and isinstance(args[1], str):
            updated_args = list(args)
            updated_args[1] = await asyncio.to_thread(_render_for_telegram, args[1])
            return await original_send_message(self, *updated_args, **kwargs)

        return await original_send_message(self, *args, **kwargs)

    return send_message_with_response_handler


# PTB's ExtBot can delegate to Bot.send_message.  Wrapping both would render a
# valid JSON reply twice, turning the first rendered message into invalid input
# on the second pass.  Patch only the effective implementation.
if ExtBot is not None and ExtBot.send_message is not Bot.send_message:
    ExtBot.send_message = _wrap_send_message(ExtBot.send_message)
else:
    Bot.send_message = _wrap_send_message(Bot.send_message)
