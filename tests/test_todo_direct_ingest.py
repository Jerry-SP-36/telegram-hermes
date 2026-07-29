from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
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


class NotionStub:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.pages: dict[str, dict[str, object]] = {}
        self.created = 0
        self.updated = 0

    def add_page(
        self,
        item: str,
        due_date: str | None,
        for_who: str,
        done: bool = False,
        note: str | None = None,
    ) -> str:
        page_id = f"page-{len(self.pages) + 1}"
        self.pages[page_id] = {
            "item": item,
            "due_date": due_date,
            "for_who": for_who,
            "done": done,
            "note": note,
        }
        return page_id

    @staticmethod
    def _result(page_id: str, page: dict[str, object]) -> dict[str, object]:
        return {
            "id": page_id,
            "properties": {
                "Item": {
                    "title": [
                        {
                            "plain_text": page["item"],
                            "text": {"content": page["item"]},
                        }
                    ]
                },
                "For Who": {"select": {"name": page["for_who"]}},
                "Due Date": {
                    "date": (
                        None
                        if page["due_date"] is None
                        else {"start": page["due_date"]}
                    )
                },
                "Done?": {"checkbox": page["done"]},
            },
        }

    def __call__(self, method: str, path: str, payload=None):
        if self.fail:
            raise RuntimeError("notion unavailable")
        if path.endswith("/query"):
            exact_item = None
            if isinstance(payload, dict) and isinstance(payload.get("filter"), dict):
                exact_item = payload["filter"]["title"]["equals"]
            results = [
                self._result(page_id, page)
                for page_id, page in self.pages.items()
                if exact_item is None or page["item"] == exact_item
            ]
            return {"results": results, "has_more": False, "next_cursor": None}
        if path == "/v1/pages":
            properties = payload["properties"]
            item = properties["Item"]["title"][0]["text"]["content"]
            due_property = properties.get("Due Date", {}).get("date")
            due_date = due_property.get("start") if due_property else None
            for_who = properties["For Who"]["select"]["name"]
            done = properties["Done?"]["checkbox"]
            note_property = properties.get("備註", {}).get("rich_text")
            note = note_property[0]["text"]["content"] if note_property else None
            self.add_page(item, due_date, for_who, done=done, note=note)
            self.created += 1
            return {"url": "https://www.notion.so/test"}
        if method == "PATCH" and path.startswith("/v1/pages/"):
            page_id = path.rsplit("/", 1)[-1]
            self.pages[page_id]["done"] = payload["properties"]["Done?"]["checkbox"]
            self.updated += 1
            return {"id": page_id}
        raise AssertionError(path)


class FakePluginContext:
    def __init__(self) -> None:
        self.tools: list[dict[str, object]] = []
        self.hooks: list[tuple[str, object]] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_hook(self, name, callback) -> None:
        self.hooks.append((name, callback))


class TodoDirectIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_inbox = todo.INBOX
        todo.INBOX = Path(self.temp_dir.name) / "todo-inbox.jsonl"
        with todo._SESSION_LOCK:
            todo._TELEGRAM_SESSIONS.clear()
        self.env = patch.dict(
            os.environ,
            {"TODO_TELEGRAM_CHAT_ID": "42", "NOTION_API_KEY": "test-token"},
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        todo.INBOX = self.original_inbox
        with todo._SESSION_LOCK:
            todo._TELEGRAM_SESSIONS.clear()
        self.temp_dir.cleanup()

    def authorize(self, text: str, turn_id: str = "1", session_id: str = "session-1") -> dict:
        context = todo._remember_telegram_session(
            platform="telegram",
            sender_id="42",
            session_id=session_id,
            turn_id=turn_id,
            user_message=text,
        )
        self.assertIsNotNone(context)
        return context

    def execute(self, notion: NotionStub, args: dict, session_id: str = "session-1") -> dict:
        with patch.object(todo, "_notion_request", side_effect=notion):
            raw = todo._handle_todo_execute(args, session_id=session_id)
        return json.loads(raw)

    def records(self) -> list[dict]:
        if not todo.INBOX.exists():
            return []
        return [json.loads(line) for line in todo.INBOX.read_text(encoding="utf-8").splitlines()]

    def test_llm_normalized_today_todo_is_written_without_confirmation(self) -> None:
        original = "今天要詢問歐美亞有關護照申辦的事情"
        context = self.authorize(original)
        self.assertIn("歐美亞護照申請", context["context"])
        notion = NotionStub()
        result = self.execute(
            notion,
            {
                "action": "create",
                "item": "歐美亞護照申請",
                "due_date": todo.dt.datetime.now(todo.TIMEZONE).date().isoformat(),
                "for_who": "Myself",
                "source_text": original,
            },
        )
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["response"]["action"], "todo_created")
        self.assertEqual(result["response"]["data"]["item"], "歐美亞護照申請")
        self.assertEqual(result["response"]["data"]["due_date"], "今天")
        self.assertNotIn("確認", result["response"]["summary"])
        record = self.records()[0]
        self.assertEqual(record["item"], "歐美亞護照申請")
        self.assertEqual(record["note"], original)
        self.assertEqual(notion.pages["page-1"]["note"], original)

    def test_due_date_falls_back_to_original_text_when_model_omits_it(self) -> None:
        self.authorize("明天要做 Ansys 簡報")
        notion = NotionStub()
        result = self.execute(
            notion,
            {
                "action": "create",
                "item": "Ansys 演講簡報",
                "source_text": "明天要做 Ansys 簡報",
            },
        )
        self.assertEqual(result["response"]["data"]["due_date"], "明天")

    def test_pre_llm_hook_does_not_write_or_intercept_queries_chat_or_expense(self) -> None:
        for turn_id, text in enumerate(("今天要做什麼？", "你好", "午餐 120 元"), 1):
            context = self.authorize(text, str(turn_id))
            self.assertIn("不要呼叫此工具", context["context"])
        self.assertEqual(self.records(), [])

    def test_same_telegram_turn_and_same_semantic_todo_are_idempotent(self) -> None:
        notion = NotionStub()
        args = {"action": "create", "item": "拿大頭照", "source_text": "要記得拿大頭照"}
        self.authorize("要記得拿大頭照", "99")
        self.execute(notion, args)
        self.execute(notion, args)
        self.authorize("待辦：拿大頭照", "100")
        self.execute(notion, {**args, "source_text": "待辦：拿大頭照"})
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["source_event_id"], "telegram:42:99")
        self.assertEqual(notion.created, 1)

    def test_same_item_with_different_due_date_is_not_duplicate(self) -> None:
        notion = NotionStub()
        for turn_id, due in (("301", "2026-07-30"), ("302", "2026-07-31")):
            self.authorize(f"{due} 確認 Ansys 簡報", turn_id)
            self.execute(
                notion,
                {
                    "action": "create",
                    "item": "確認 Ansys 簡報",
                    "due_date": due,
                    "source_text": f"{due} 確認 Ansys 簡報",
                },
            )
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(notion.created, 2)

    def test_fuzzy_completion_uses_llm_title_and_updates_best_open_notion_page(self) -> None:
        notion = NotionStub()
        page_id = notion.add_page(
            "今天要詢問歐美亞有關護照申辦的事情",
            todo.dt.datetime.now(todo.TIMEZONE).date().isoformat(),
            "Myself",
        )
        notion.add_page("整理 Ansys 簡報", None, "Myself")
        self.authorize("已完成歐美亞護照事情", "400")
        result = self.execute(
            notion,
            {
                "action": "complete",
                "item": "歐美亞護照申請",
                "source_text": "已完成歐美亞護照事情",
            },
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(notion.pages[page_id]["done"])
        self.assertFalse(notion.pages["page-2"]["done"])
        self.assertEqual(notion.updated, 1)
        self.assertEqual(result["response"]["data"]["item"], "歐美亞護照申請")
        completion = self.records()[0]
        self.assertEqual(completion["type"], "todo_completion")
        self.assertEqual(completion["matched_item"], "今天要詢問歐美亞有關護照申辦的事情")

    def test_completion_never_asks_candidates_and_chooses_best_match(self) -> None:
        notion = NotionStub()
        first = notion.add_page("拿大頭照", None, "Myself")
        second = notion.add_page("拿大頭照給公司證件", None, "Myself")
        self.authorize("完成拿大頭照", "401")
        result = self.execute(
            notion,
            {"action": "complete", "item": "拿大頭照", "source_text": "完成拿大頭照"},
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(notion.pages[first]["done"])
        self.assertFalse(notion.pages[second]["done"])
        self.assertNotIn("候選", result["response"]["summary"])
        self.assertNotIn("確認", result["response"]["summary"])

    def test_unknown_completion_creates_an_already_completed_record(self) -> None:
        notion = NotionStub()
        notion.add_page("完全不相關的工作", None, "Myself")
        self.authorize("已完成買郵票", "402")
        result = self.execute(
            notion,
            {"action": "complete", "item": "買郵票", "source_text": "已完成買郵票"},
        )
        self.assertEqual(result["status"], "completed_created")
        self.assertEqual(notion.created, 1)
        created = notion.pages["page-2"]
        self.assertEqual(created["item"], "買郵票")
        self.assertTrue(created["done"])
        self.assertTrue(self.records()[0]["done"])

    def test_already_completed_is_idempotent(self) -> None:
        notion = NotionStub()
        page_id = notion.add_page("拿大頭照", None, "Myself", done=True)
        self.authorize("已完成拿大頭照", "403")
        result = self.execute(
            notion,
            {"action": "complete", "item": "拿大頭照", "source_text": "已完成拿大頭照"},
        )
        self.assertEqual(result["status"], "already_completed")
        self.assertTrue(notion.pages[page_id]["done"])
        self.assertEqual(notion.updated, 0)

    def test_notion_failure_keeps_local_then_retry_syncs_without_duplicate(self) -> None:
        args = {"action": "create", "item": "拿大頭照", "source_text": "待辦：拿大頭照"}
        self.authorize("待辦：拿大頭照", "500")
        failing_result = self.execute(NotionStub(fail=True), args)
        self.assertEqual(failing_result["status"], "local_only")
        self.assertEqual(len(self.records()), 1)

        recovered = NotionStub()
        self.authorize("待辦：拿大頭照", "501")
        retry_result = self.execute(recovered, args)
        self.assertEqual(retry_result["status"], "created")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(recovered.created, 1)

    def test_completion_notion_failure_retains_one_event_for_retry(self) -> None:
        failing = NotionStub(fail=True)
        self.authorize("完成拿大頭照", "510")
        args = {"action": "complete", "item": "拿大頭照", "source_text": "完成拿大頭照"}
        first = self.execute(failing, args)
        self.assertEqual(first["status"], "local_only")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["type"], "todo_completion")

        recovered = NotionStub()
        recovered.add_page("拿大頭照", None, "Myself")
        self.authorize("完成拿大頭照", "511")
        second = self.execute(recovered, args)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(recovered.updated, 1)

    def test_invalid_jsonl_fails_closed_before_notion_write(self) -> None:
        todo.INBOX.write_text('{"broken"\n', encoding="utf-8")
        notion = NotionStub()
        self.authorize("待辦：拿大頭照", "600")
        result = self.execute(
            notion,
            {"action": "create", "item": "拿大頭照", "source_text": "待辦：拿大頭照"},
        )
        self.assertEqual(result["status"], "local_invalid")
        self.assertEqual(notion.created, 0)
        self.assertEqual(todo.INBOX.read_text(encoding="utf-8"), '{"broken"\n')

    def test_tool_rejects_unapproved_or_non_telegram_sessions(self) -> None:
        notion = NotionStub()
        result = self.execute(
            notion,
            {"action": "create", "item": "拿大頭照", "source_text": "待辦：拿大頭照"},
            session_id="missing",
        )
        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(self.records(), [])
        self.assertIsNone(
            todo._remember_telegram_session(
                platform="discord",
                sender_id="42",
                session_id="discord-session",
                user_message="待辦：拿大頭照",
            )
        )

    def test_allowed_chat_ids_uses_active_hermes_home(self) -> None:
        config_path = Path(self.temp_dir.name) / "config.yaml"
        config_path.write_text(
            "platforms:\n"
            "  telegram:\n"
            "    extra:\n"
            "      channel_prompts:\n"
            "        '42': Jerry private chat\n",
            encoding="utf-8",
        )
        constants = ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path(self.temp_dir.name)
        with patch.dict(os.environ, {"TODO_TELEGRAM_CHAT_ID": ""}, clear=False):
            with patch.dict(sys.modules, {"hermes_constants": constants}):
                self.assertEqual(todo._allowed_chat_ids(), {"42"})

    def test_registers_one_native_tool_and_one_pre_llm_hook(self) -> None:
        context = FakePluginContext()
        todo.register(context)
        self.assertEqual(len(context.tools), 1)
        self.assertEqual(context.tools[0]["name"], "todo_execute")
        self.assertEqual(context.tools[0]["toolset"], "todo_capture")
        self.assertIs(context.tools[0]["handler"], todo._handle_todo_execute)
        self.assertEqual(context.hooks, [("pre_llm_call", todo._remember_telegram_session)])


if __name__ == "__main__":
    unittest.main()
