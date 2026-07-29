from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "hermes"
    / "plugins"
    / "todo-direct-ingest"
    / "__init__.py"
)
SPEC = importlib.util.spec_from_file_location("todo_direct_ingest", PLUGIN_PATH)
assert SPEC is not None and SPEC.loader is not None
todo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(todo)


class Platform(Enum):
    TELEGRAM = "telegram"


class FakeAdapter:
    def __init__(self, success: bool = True) -> None:
        self.messages: list[tuple[str, str]] = []
        self.success = success

    async def send(self, chat_id: str, text: str) -> SimpleNamespace:
        self.messages.append((chat_id, text))
        return SimpleNamespace(success=self.success, error="send failed")


class NotionStub:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.items: set[tuple[str, str | None, str]] = set()
        self.created = 0

    def __call__(self, method: str, path: str, payload=None):
        if self.fail:
            raise RuntimeError("notion unavailable")
        if path.endswith("/query"):
            item = payload["filter"]["title"]["equals"]
            results = []
            for stored_item, due_date, for_who in self.items:
                if stored_item != item:
                    continue
                results.append(
                    {
                        "id": "existing",
                        "properties": {
                            "For Who": {"select": {"name": for_who}},
                            "Due Date": {
                                "date": None if due_date is None else {"start": due_date}
                            },
                        },
                    }
                )
            return {"results": results}
        if path == "/v1/pages":
            properties = payload["properties"]
            item = properties["Item"]["title"][0]["text"]["content"]
            due_property = properties.get("Due Date", {}).get("date")
            due_date = due_property.get("start") if due_property else None
            for_who = properties["For Who"]["select"]["name"]
            self.items.add((item, due_date, for_who))
            self.created += 1
            return {"url": "https://www.notion.so/test"}
        raise AssertionError(path)


class TodoDirectIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_inbox = todo.INBOX
        todo.INBOX = Path(self.temp_dir.name) / "todo-inbox.jsonl"
        self.env = patch.dict(
            os.environ,
            {"TODO_TELEGRAM_CHAT_ID": "42", "NOTION_API_KEY": "test-token"},
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        todo.INBOX = self.original_inbox
        self.temp_dir.cleanup()

    @staticmethod
    def event(text: str, message_id: str = "1", chat_id: str = "42") -> SimpleNamespace:
        source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id=chat_id, user_id=chat_id)
        return SimpleNamespace(text=text, message_id=message_id, source=source)

    async def run_hook(self, text: str, notion: NotionStub, message_id: str = "1"):
        adapter = FakeAdapter()
        event = self.event(text, message_id)
        gateway = SimpleNamespace(adapters={event.source.platform: adapter})
        with patch.object(todo, "_notion_request", side_effect=notion):
            result = todo._intercept_todo(event, gateway)
            await asyncio.sleep(0)
        return result, adapter

    def records(self) -> list[dict]:
        if not todo.INBOX.exists():
            return []
        return [json.loads(line) for line in todo.INBOX.read_text(encoding="utf-8").splitlines()]

    def test_clear_todo_prefix_records_without_confirmation(self) -> None:
        notion = NotionStub()
        result, adapter = asyncio.run(self.run_hook("待辦：拿大頭照", notion))
        self.assertEqual(result, {"action": "skip", "reason": "todo-direct-ingest"})
        self.assertEqual([row["item"] for row in self.records()], ["拿大頭照"])
        self.assertIn("✅ 待辦已記錄並同步到 Notion", adapter.messages[0][1])
        self.assertNotIn("確認", adapter.messages[0][1])

    def test_remember_prefix_records(self) -> None:
        notion = NotionStub()
        asyncio.run(self.run_hook("要記得拿大頭照", notion))
        self.assertEqual(self.records()[0]["item"], "拿大頭照")

    def test_today_action_keeps_meaningful_text_and_sets_due_date(self) -> None:
        notion = NotionStub()
        text = "今天要詢問歐美亞有關護照申辦的事情"
        _, adapter = asyncio.run(self.run_hook(text, notion))
        record = self.records()[0]
        self.assertEqual(record["item"], text)
        self.assertEqual(record["due_date"], todo.dt.datetime.now(todo.TIMEZONE).date().isoformat())
        self.assertIn("期限：今天", adapter.messages[0][1])

    def test_tomorrow_action_sets_due_date(self) -> None:
        notion = NotionStub()
        asyncio.run(self.run_hook("明天要做 Ansys 簡報", notion))
        expected = (todo.dt.datetime.now(todo.TIMEZONE).date() + todo.dt.timedelta(days=1)).isoformat()
        self.assertEqual(self.records()[0]["due_date"], expected)

    def test_todo_query_falls_through(self) -> None:
        notion = NotionStub()
        result, adapter = asyncio.run(self.run_hook("今天要做什麼？", notion))
        self.assertIsNone(result)
        self.assertEqual(self.records(), [])
        self.assertEqual(adapter.messages, [])

    def test_slash_command_and_expense_fall_through(self) -> None:
        notion = NotionStub()
        for index, text in enumerate(("/reset", "午餐 120 元"), 1):
            result, _ = asyncio.run(self.run_hook(text, notion, str(index)))
            self.assertIsNone(result)
        self.assertEqual(self.records(), [])

    def test_explicit_todo_wins_even_when_text_has_amount(self) -> None:
        notion = NotionStub()
        result, _ = asyncio.run(self.run_hook("待辦：買文具 120 元", notion))
        self.assertEqual(result["reason"], "todo-direct-ingest")
        self.assertEqual(self.records()[0]["item"], "買文具 120 元")

    def test_same_event_is_idempotent_locally_and_in_notion(self) -> None:
        notion = NotionStub()
        asyncio.run(self.run_hook("待辦：拿大頭照", notion, "99"))
        asyncio.run(self.run_hook("待辦：拿大頭照", notion, "99"))
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(notion.created, 1)
        self.assertEqual(self.records()[0]["source_event_id"], "telegram:42:99")

    def test_same_semantic_todo_with_new_message_id_is_idempotent(self) -> None:
        notion = NotionStub()
        asyncio.run(self.run_hook("待辦：拿大頭照", notion, "100"))
        asyncio.run(self.run_hook("待辦：拿大頭照", notion, "101"))
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(notion.created, 1)

    def test_same_item_with_different_due_date_is_not_a_duplicate(self) -> None:
        notion = NotionStub()
        first, first_added = todo._local_record(
            "確認 Ansys 簡報",
            "2026-07-30",
            "Myself",
            None,
            "telegram:42:300",
        )
        second, second_added = todo._local_record(
            "確認 Ansys 簡報",
            "2026-07-31",
            "Myself",
            None,
            "telegram:42:301",
        )
        with patch.object(todo, "_notion_request", side_effect=notion):
            self.assertEqual(todo._sync_to_notion(first), "created")
            self.assertEqual(todo._sync_to_notion(second), "created")
        self.assertTrue(first_added)
        self.assertTrue(second_added)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(notion.created, 2)

    def test_invalid_jsonl_fails_closed(self) -> None:
        todo.INBOX.write_text('{"broken"\n', encoding="utf-8")
        notion = NotionStub()
        result, adapter = asyncio.run(self.run_hook("待辦：拿大頭照", notion))
        self.assertEqual(result["reason"], "todo-direct-ingest-local-failed")
        self.assertIn("已停止寫入", adapter.messages[0][1])
        self.assertEqual(todo.INBOX.read_text(encoding="utf-8"), '{"broken"\n')

    def test_notion_failure_keeps_local_then_retry_syncs_without_duplicate(self) -> None:
        failing = NotionStub(fail=True)
        result, adapter = asyncio.run(self.run_hook("待辦：拿大頭照", failing, "200"))
        self.assertEqual(result["reason"], "todo-direct-ingest")
        self.assertIn("尚未同步到 Notion", adapter.messages[0][1])
        self.assertEqual(len(self.records()), 1)

        recovered = NotionStub()
        _, retry_adapter = asyncio.run(self.run_hook("待辦：拿大頭照", recovered, "201"))
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(recovered.created, 1)
        self.assertIn("✅", retry_adapter.messages[0][1])

    def test_missing_item_gets_one_direct_question_without_write(self) -> None:
        notion = NotionStub()
        result, adapter = asyncio.run(self.run_hook("待辦：", notion))
        self.assertEqual(result["reason"], "todo-direct-ingest-missing-item")
        self.assertEqual(self.records(), [])
        self.assertEqual(adapter.messages[0][1], "❓ 請告訴我要記錄的待辦內容。")

    def test_unconfigured_chat_fails_closed(self) -> None:
        notion = NotionStub()
        with patch.object(todo, "_allowed_chat_ids", return_value=set()):
            result, adapter = asyncio.run(
                self.run_hook("待辦：拿大頭照", notion, message_id="1")
            )
        self.assertIsNone(result)
        self.assertEqual(adapter.messages, [])

    def test_registers_only_the_pre_dispatch_hook(self) -> None:
        calls = []
        todo.register(SimpleNamespace(register_hook=lambda name, callback: calls.append((name, callback))))
        self.assertEqual(calls, [("pre_gateway_dispatch", todo._intercept_todo)])


if __name__ == "__main__":
    unittest.main()
