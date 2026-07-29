from __future__ import annotations

import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "hermes" / "scripts" / "daily_review.py"
)
SPEC = importlib.util.spec_from_file_location("daily_review", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


def notion_page(item: str, due_date: str | None) -> dict:
    return {
        "properties": {
            "Item": {"title": [{"plain_text": item}]},
            "Due Date": {"date": None if due_date is None else {"start": due_date}},
            "Done?": {"checkbox": False},
        }
    }


class DailyReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_sipi_inbox = review.SIPI_INBOX
        self.original_outbox = review.DELIVERY_OUTBOX
        review.SIPI_INBOX = Path(self.temp_dir.name) / "sipi-idea-inbox.jsonl"
        review.DELIVERY_OUTBOX = Path(self.temp_dir.name) / "delivery-outbox.jsonl"

    def tearDown(self) -> None:
        review.SIPI_INBOX = self.original_sipi_inbox
        review.DELIVERY_OUTBOX = self.original_outbox
        self.temp_dir.cleanup()

    def write_ideas(self, records: list[dict]) -> None:
        review.SIPI_INBOX.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_queries_only_incomplete_notion_todos_and_sorts_dates(self) -> None:
        responses = [
            {
                "results": [
                    notion_page("沒有日期", None),
                    notion_page("明天事項", "2026-07-30"),
                    notion_page("今天事項", "2026-07-29"),
                ],
                "has_more": False,
            }
        ]
        with patch.object(review, "_notion_request", side_effect=responses) as request:
            todos = review.query_open_todos()
        self.assertEqual(
            todos,
            [
                {"item": "今天事項", "due_date": "2026-07-29"},
                {"item": "明天事項", "due_date": "2026-07-30"},
                {"item": "沒有日期", "due_date": None},
            ],
        )
        self.assertEqual(
            request.call_args.args[0]["filter"],
            {"property": "Done?", "checkbox": {"equals": False}},
        )

    def test_pending_ideas_excludes_consumed_status_events(self) -> None:
        self.write_ideas(
            [
                {"type": "sipi_idea", "id": "a", "title": "待消化 A", "status": "pending"},
                {"type": "sipi_idea", "id": "b", "title": "已消化 B", "status": "pending"},
                {"type": "sipi_idea_status", "id": "s1", "idea_id": "b", "status": "consumed"},
            ]
        )
        self.assertEqual(
            review.pending_sipi_ideas(),
            [{"id": "a", "title": "待消化 A", "status": "pending"}],
        )

    def test_report_lists_each_todo_name_and_date_only(self) -> None:
        report = review.build_telegram_report(
            dt.date(2026, 7, 29),
            [
                {"item": "整理 Ansys 簡報", "due_date": "2026-07-29"},
                {"item": "拿大頭照", "due_date": None},
            ],
            [{"id": "a", "title": "PDN anti-resonance", "status": "pending"}],
        )
        self.assertIn("1. 整理 Ansys 簡報｜2026-07-29", report)
        self.assertIn("2. 拿大頭照｜未指定", report)
        self.assertNotIn("Myself", report)
        self.assertNotIn("備註", report)
        self.assertIn("1. PDN anti-resonance", report)

    def test_one_canonical_payload_is_used_for_n8n_and_telegram(self) -> None:
        now = dt.datetime(2026, 7, 29, 21, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        todos = [{"item": "待辦 A", "due_date": "2026-07-30"}]
        ideas = [{"id": "i1", "title": "SI idea A", "status": "pending"}]
        report_text = review.build_telegram_report(now.date(), todos, ideas)
        payload = review.build_payload(now, todos, ideas, report_text)
        self.assertEqual(payload["report_id"], "hermes-daily-review:2026-07-29")
        self.assertEqual(payload["telegram_text"], report_text)
        self.assertEqual(payload["todos"], todos)
        self.assertEqual(payload["sipi_ideas"], ideas)

    def test_failed_n8n_delivery_is_spooled_once_without_hiding_telegram_report(self) -> None:
        now = dt.datetime(2026, 7, 29, 21, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        with (
            patch.object(
                review,
                "query_open_todos",
                return_value=[{"item": "待辦 A", "due_date": "2026-07-29"}],
            ),
            patch.object(
                review,
                "pending_sipi_ideas",
                return_value=[{"id": "i1", "title": "SI idea A", "status": "pending"}],
            ),
            patch.object(review, "_post_to_n8n", return_value=False),
        ):
            first = review.run(now)
            second = review.run(now)
        self.assertEqual(first, second)
        records = review._read_jsonl(review.DELIVERY_OUTBOX)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["report_id"], "hermes-daily-review:2026-07-29")

    def test_pending_n8n_delivery_retries_and_appends_sent_status(self) -> None:
        payload = {
            "version": "1.0",
            "report_id": "hermes-daily-review:2026-07-28",
            "date": "2026-07-28",
            "telegram_text": "older report",
        }
        review._enqueue_delivery(payload)
        with patch.object(review, "_post_to_n8n", return_value=True) as post:
            review.retry_pending_deliveries()
            review.retry_pending_deliveries()
        self.assertEqual(post.call_count, 1)
        records = review._read_jsonl(review.DELIVERY_OUTBOX)
        self.assertEqual(records[-1]["type"], "daily_review_delivery_status")
        self.assertEqual(records[-1]["status"], "sent")

    def test_corrupt_idea_inbox_stops_instead_of_reporting_false_empty_state(self) -> None:
        review.SIPI_INBOX.write_text("{broken\n", encoding="utf-8")
        with self.assertRaises(review.ReviewDataError):
            review.pending_sipi_ideas()


if __name__ == "__main__":
    unittest.main()
