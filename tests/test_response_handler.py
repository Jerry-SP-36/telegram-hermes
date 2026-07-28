from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_bridge.response_handler import FORMAT_ERROR_MESSAGE, render_telegram_response, response_shape
from telegram_bridge.rewrite import rewrite_send_message_body
from telegram_bridge.app import resolve_telegram_target


def contract(response_type: str, summary: str, **overrides: object) -> str:
    payload: dict[str, object] = {
        "version": "1.0",
        "type": response_type,
        "action": "",
        "title": "需要確認" if response_type == "confirm" else "",
        "summary": summary,
        "confidence": 0.9,
        "data": {},
        "actions": [],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class ResponseHandlerTests(unittest.TestCase):
    def test_success(self) -> None:
        self.assertEqual(render_telegram_response(contract("success", "已完成整理。")), "✅ 已完成整理。")

    def test_expense_success_renders_user_facing_details(self) -> None:
        self.assertEqual(
            render_telegram_response(
                contract(
                    "success",
                    "已記錄支出",
                    action="expense_created",
                    data={
                        "amount": 120,
                        "currency": "TWD",
                        "category": "餐飲",
                        "payment_method": "信用卡",
                        "ledger_id": "internal-id-must-not-leak",
                        "duplicate": False,
                    },
                )
            ),
            "✅ 已記錄支出\n\n金額：NT$120\n分類：餐飲\n付款方式：信用卡",
        )

    def test_todo_success_skips_empty_and_internal_fields(self) -> None:
        self.assertEqual(
            render_telegram_response(
                contract(
                    "success",
                    "已記錄待辦",
                    data={
                        "item": "要記得拿大頭照",
                        "due_date": None,
                        "event_appended": True,
                    },
                )
            ),
            "✅ 已記錄待辦\n\n項目：要記得拿大頭照",
        )

    def test_question(self) -> None:
        self.assertEqual(render_telegram_response(contract("question", "要套用到哪個專案？")), "❓ 要套用到哪個專案？")

    def test_error(self) -> None:
        self.assertEqual(render_telegram_response(contract("error", "找不到指定資料。")), "❌ 找不到指定資料。")

    def test_confirm(self) -> None:
        self.assertEqual(
            render_telegram_response(contract("confirm", "是否繼續部署？", title="部署確認")),
            "⚠️ 部署確認\n\n是否繼續部署？",
        )

    def test_plain_text_passes_through_but_broken_json_stays_fail_closed(self) -> None:
        self.assertEqual(render_telegram_response("這是自由文字"), "這是自由文字")
        self.assertEqual(render_telegram_response("✅ 已經整理完成"), "✅ 已經整理完成")
        self.assertEqual(render_telegram_response('{"type":"success"}'), FORMAT_ERROR_MESSAGE)
        self.assertEqual(render_telegram_response("{broken"), FORMAT_ERROR_MESSAGE)

    def test_self_improvement_notification_is_rendered_in_readable_chinese(self) -> None:
        self.assertEqual(
            render_telegram_response(
                "💾 Self-improvement review: Patched SKILL.md in skill "
                "'expense-capture' (1 replacement)."
            ),
            "🧠 Hermes 自我改善\n\n已更新技能：expense-capture\n變更：1 處",
        )
        self.assertEqual(
            render_telegram_response(
                "💾 Self-improvement review: Staged for approval "
                "(skills.write_approval is on). Pending id: skill-123."
            ),
            "🛡️ 技能修改等待審核\n\nHermes 尚未套用這次變更。\n"
            "輸入 /skills pending 查看。",
        )
        self.assertEqual(
            render_telegram_response("💾 Self-improvement review: Memory updated"),
            "🧠 Hermes 記憶已更新",
        )

    def test_plain_text_is_sanitized_and_bounded_for_telegram(self) -> None:
        self.assertEqual(render_telegram_response("A\x00B"), "AB")
        self.assertEqual(render_telegram_response("x" * 4001), "x" * 3999 + "…")

    def test_partial_json_is_normalized_before_rendering(self) -> None:
        self.assertEqual(
            render_telegram_response('{"type":"success","summary":"成功測試"}'),
            "✅ 成功測試",
        )
        self.assertEqual(
            render_telegram_response('{"type":"confirm","summary":"是否繼續？"}'),
            "⚠️ 需要確認\n\n是否繼續？",
        )
        self.assertEqual(
            render_telegram_response('{"type":"success","summary":"完成","unexpected":true}'),
            FORMAT_ERROR_MESSAGE,
        )

    def test_json_wrapped_by_model_text_is_safely_extracted(self) -> None:
        partial = '{"type":"success","summary":"成功測試"}'
        self.assertEqual(
            render_telegram_response(f"```json\n{partial}\n```"),
            "✅ 成功測試",
        )
        self.assertEqual(
            render_telegram_response(f"以下是結果：\n{partial}\n請查收。"),
            "✅ 成功測試",
        )

    def test_real_telegram_contract_repairs_optional_metadata_and_stays_readable(self) -> None:
        payload = {
            "version": "1.0",
            "type": "success",
            "action": "telegram_bridge_status",
            "title": "",
            "summary": "Telegram 橋接已連線",
            "confidence": "1.0",
            "data": {},
            "actions": [],
        }
        self.assertEqual(
            render_telegram_response(json.dumps(payload, ensure_ascii=False)),
            "✅ Telegram 橋接已連線",
        )
        self.assertEqual(
            render_telegram_response(
                "以下是結果：\n" + json.dumps(payload, ensure_ascii=False)
            ),
            "✅ Telegram 橋接已連線",
        )

    def test_python_mapping_repr_from_telegram_adapter_is_safely_normalized(self) -> None:
        payload = {
            "version": "1.0",
            "type": "success",
            "action": "",
            "title": "Telegram 收件內容",
            "summary": "已記錄 Telegram 測試內容。",
            "confidence": 1.0,
            "data": {
                "memory_saved": True,
                "event_appended": True,
                "notion_synced": False,
                "duplicate": False,
            },
            "actions": [],
        }
        self.assertEqual(
            render_telegram_response(repr(payload)),
            "✅ 已記錄 Telegram 測試內容。",
        )

    def test_response_shape_contains_no_message_content(self) -> None:
        self.assertEqual(response_shape('{"type":"success","summary":"成功測試"}'), "json keys=summary,type")
        self.assertEqual(response_shape("自由文字"), "plain_text chars=4 fence=False brace=False")

    def test_form_send_message_is_rewritten(self) -> None:
        body = f"chat_id=123&text={contract('success', '完成')}&disable_notification=true".encode()
        rewritten = rewrite_send_message_body(body, "application/x-www-form-urlencoded")
        parsed = parse_qs(rewritten.decode())
        self.assertEqual(parsed["text"], ["✅ 完成"])
        self.assertEqual(parsed["chat_id"], ["123"])

    def test_json_send_message_is_rewritten(self) -> None:
        body = json.dumps({"chat_id": 123, "text": contract("error", "失敗")}, ensure_ascii=False).encode()
        rewritten = rewrite_send_message_body(body, "application/json")
        self.assertEqual(json.loads(rewritten)["text"], "❌ 失敗")

    def test_tokenless_bridge_accepts_only_valid_bot_api_paths(self) -> None:
        target = resolve_telegram_target("/bot123456:bridge-test-token/sendMessage", None)
        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.method_name, "sendMessage")
        self.assertIsNone(resolve_telegram_target("/botnot-a-token/sendMessage", None))


if __name__ == "__main__":
    unittest.main()
