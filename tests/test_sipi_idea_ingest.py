from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "hermes"
    / "plugins"
    / "sipi-idea-ingest"
    / "__init__.py"
)
SPEC = importlib.util.spec_from_file_location("sipi_idea_ingest", PLUGIN_PATH)
assert SPEC is not None and SPEC.loader is not None
sipi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sipi)


class FakePluginContext:
    def __init__(self) -> None:
        self.tools: list[dict[str, object]] = []
        self.hooks: list[tuple[str, object]] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_hook(self, name, callback) -> None:
        self.hooks.append((name, callback))


class SipiIdeaIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_inbox = sipi.INBOX
        sipi.INBOX = Path(self.temp_dir.name) / "sipi-idea-inbox.jsonl"
        with sipi._SESSION_LOCK:
            sipi._TELEGRAM_SESSIONS.clear()
        self.env = patch.dict(os.environ, {"SIPI_TELEGRAM_CHAT_ID": "42"}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        sipi.INBOX = self.original_inbox
        with sipi._SESSION_LOCK:
            sipi._TELEGRAM_SESSIONS.clear()
        self.temp_dir.cleanup()

    def authorize(self, text: str, turn_id: str = "1", session_id: str = "s-1") -> dict:
        context = sipi._remember_telegram_session(
            platform="telegram",
            sender_id="42",
            session_id=session_id,
            turn_id=turn_id,
            user_message=text,
        )
        self.assertIsNotNone(context)
        return context

    def execute(self, args: dict, session_id: str = "s-1") -> dict:
        return json.loads(sipi._handle_sipi_idea_execute(args, session_id=session_id))

    def records(self) -> list[dict]:
        if not sipi.INBOX.exists():
            return []
        return [json.loads(line) for line in sipi.INBOX.read_text(encoding="utf-8").splitlines()]

    def test_registers_one_tool_and_pre_llm_hook(self) -> None:
        context = FakePluginContext()
        sipi.register(context)
        self.assertEqual(context.tools[0]["name"], "sipi_idea_execute")
        self.assertEqual(context.hooks[0][0], "pre_llm_call")

    def test_llm_shortened_idea_preserves_original_and_pending_state(self) -> None:
        original = "我想到可以研究高速連接器的 via stub 對回損的影響，記成 SI idea"
        context = self.authorize(original)
        self.assertIn("精簡 title", context["context"])
        self.assertIn("不寫 Notion", context["context"])
        result = self.execute(
            {
                "action": "create",
                "title": "高速連接器 via stub 回損",
                "summary": "研究 via stub 對高速連接器回損的影響。",
                "source_text": original,
            }
        )
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["response"]["data"]["item"], "高速連接器 via stub 回損")
        self.assertEqual(result["response"]["data"]["note"], "待消化")
        self.assertEqual(self.records()[0]["source_text"], original)
        self.assertEqual(self.records()[0]["status"], "pending")

    def test_query_todo_expense_and_general_chat_are_only_guided_not_written(self) -> None:
        for turn_id, text in enumerate(
            (
                "SI 的 insertion loss 怎麼看？",
                "待辦：整理 SI 量測資料",
                "午餐 120 元",
                "你好",
            ),
            1,
        ):
            context = self.authorize(text, str(turn_id))
            self.assertIn("不要呼叫", context["context"])
        self.assertEqual(self.records(), [])

    def test_same_event_and_same_pending_title_are_idempotent(self) -> None:
        args = {
            "action": "create",
            "title": "PDN anti-resonance 測試",
            "source_text": "PI idea：PDN anti-resonance 測試",
        }
        self.authorize(args["source_text"], "9")
        self.execute(args)
        duplicate_event = self.execute(args)
        self.assertEqual(duplicate_event["status"], "already_exists")
        self.authorize("記錄 PI 想法：PDN anti resonance 測試", "10")
        duplicate_title = self.execute({**args, "source_text": "另一種說法"})
        self.assertEqual(duplicate_title["status"], "already_exists")
        self.assertEqual(len(self.records()), 1)

    def test_consume_selects_one_best_pending_idea_without_confirmation(self) -> None:
        self.authorize("SI idea：高速連接器 via stub 回損", "1")
        self.execute(
            {
                "action": "create",
                "title": "高速連接器 via stub 回損",
                "source_text": "SI idea：高速連接器 via stub 回損",
            }
        )
        self.authorize("PI idea：PDN anti-resonance", "2")
        self.execute(
            {
                "action": "create",
                "title": "PDN anti-resonance",
                "source_text": "PI idea：PDN anti-resonance",
            }
        )
        self.authorize("高速連接器那個 SI idea 已消化", "3")
        result = self.execute(
            {
                "action": "consume",
                "title": "高速連接器 via stub",
                "source_text": "高速連接器那個 SI idea 已消化",
            }
        )
        self.assertEqual(result["status"], "consumed")
        self.assertEqual(result["response"]["data"]["note"], "已消化")
        pending = sipi._pending_ideas(self.records())
        self.assertEqual([idea["title"] for idea in pending], ["PDN anti-resonance"])

    def test_consume_with_no_pending_ideas_returns_readable_error(self) -> None:
        self.authorize("SI idea 已消化：不存在", "1")
        result = self.execute(
            {"action": "consume", "title": "不存在", "source_text": "SI idea 已消化：不存在"}
        )
        self.assertEqual(result["status"], "not_found")
        self.assertIn("目前沒有待消化", result["response"]["summary"])

    def test_corrupt_jsonl_stops_before_append(self) -> None:
        sipi.INBOX.write_text("{broken\n", encoding="utf-8")
        self.authorize("SI idea：阻抗不連續", "1")
        result = self.execute(
            {"action": "create", "title": "阻抗不連續", "source_text": "SI idea：阻抗不連續"}
        )
        self.assertEqual(result["status"], "local_invalid")
        self.assertEqual(sipi.INBOX.read_text(encoding="utf-8"), "{broken\n")

    def test_rejects_non_telegram_and_wrong_chat(self) -> None:
        self.assertIsNone(
            sipi._remember_telegram_session(
                platform="telegram", sender_id="99", session_id="x", user_message="SI idea"
            )
        )
        result = json.loads(
            sipi._handle_sipi_idea_execute(
                {"action": "create", "title": "未授權", "source_text": "未授權"},
                session_id="x",
            )
        )
        self.assertEqual(result["status"], "unauthorized")


if __name__ == "__main__":
    unittest.main()
