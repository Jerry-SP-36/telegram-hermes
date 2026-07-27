from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:bridge-test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_bridge.app import app


def contract(summary: str) -> str:
    return json.dumps(
        {
            "version": "1.0",
            "type": "success",
            "action": "",
            "title": "",
            "summary": summary,
            "confidence": 1.0,
            "data": {},
            "actions": [],
        },
        ensure_ascii=False,
    )


class FakeTelegramClient:
    def __init__(self) -> None:
        self.request: httpx.Request | None = None

    def build_request(self, method: str, url: str, *, headers: dict[str, str], content: bytes) -> httpx.Request:
        self.request = httpx.Request(method, url, headers=headers, content=content)
        return self.request

    async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b'{"ok":true,"result":{"message_id":1}}'),
            request=request,
        )


class BridgeAppTests(unittest.TestCase):
    def test_send_message_is_rewritten_before_forwarding(self) -> None:
        fake = FakeTelegramClient()
        client = TestClient(app)
        client.__enter__()
        original_client = app.state.client
        app.state.client = fake
        try:
            response = client.post(
                "/bot123456:bridge-test-token/sendMessage",
                data={"chat_id": "99", "text": contract("測試完成")},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ok"], True)
            assert fake.request is not None
            self.assertEqual(str(fake.request.url), "https://api.telegram.org/bot123456:bridge-test-token/sendMessage")
            self.assertEqual(parse_qs(fake.request.content.decode())["text"], ["✅ 測試完成"])
        finally:
            app.state.client = original_client
            client.__exit__(None, None, None)

    def test_wrong_token_path_is_rejected(self) -> None:
        client = TestClient(app)
        client.__enter__()
        try:
            response = client.post("/botwrong-token/sendMessage", data={"text": "ignored"})
            self.assertEqual(response.status_code, 404)
        finally:
            client.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
