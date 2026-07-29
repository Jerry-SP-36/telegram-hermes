"""Build one canonical Hermes daily review for Telegram and n8n/Gmail."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


logger = logging.getLogger("hermes.daily_review")

TIMEZONE = ZoneInfo("Asia/Taipei")
SIPI_INBOX = Path(
    os.getenv("SIPI_IDEA_INBOX_PATH", "/opt/data/workspace/sipi-idea-inbox.jsonl")
)
DELIVERY_OUTBOX = Path(
    os.getenv(
        "DAILY_REVIEW_DELIVERY_OUTBOX_PATH",
        "/opt/data/workspace/daily-review-delivery-outbox.jsonl",
    )
)
DATA_SOURCE_ID = os.getenv(
    "TODO_NOTION_DATA_SOURCE_ID", "3327c93b-34d9-8094-b5b4-000beb590261"
).strip()
NOTION_VERSION = "2026-03-11"
WEBHOOK_URL = os.getenv("HERMES_DAILY_REVIEW_WEBHOOK_URL", "").strip()
WEBHOOK_KEY = os.getenv("HERMES_DAILY_REVIEW_WEBHOOK_KEY", "").strip()


class ReviewDataError(RuntimeError):
    """The daily review source cannot be read without risking a false report."""


def _notion_request(payload: dict[str, Any]) -> dict[str, Any]:
    token = os.getenv("NOTION_API_KEY", "").strip()
    if not token:
        raise ReviewDataError("NOTION_API_KEY is not configured")
    request = urllib.request.Request(
        f"https://api.notion.com/v1/data_sources/{DATA_SOURCE_ID}/query",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as error:
        raise ReviewDataError("Notion todo query failed") from error
    if not isinstance(result, dict):
        raise ReviewDataError("Notion todo query returned invalid data")
    return result


def _notion_title(page: dict[str, Any], property_name: str = "Item") -> str | None:
    value = (page.get("properties") or {}).get(property_name)
    title = value.get("title") if isinstance(value, dict) else None
    if not isinstance(title, list):
        return None
    parts: list[str] = []
    for fragment in title:
        if not isinstance(fragment, dict):
            continue
        plain = fragment.get("plain_text")
        if isinstance(plain, str):
            parts.append(plain)
            continue
        content = (fragment.get("text") or {}).get("content")
        if isinstance(content, str):
            parts.append(content)
    result = "".join(parts).strip()
    return result or None


def _notion_date_start(page: dict[str, Any], property_name: str = "Due Date") -> str | None:
    value = (page.get("properties") or {}).get(property_name)
    date_value = value.get("date") if isinstance(value, dict) else None
    start = date_value.get("start") if isinstance(date_value, dict) else None
    return start[:10] if isinstance(start, str) and start else None


def query_open_todos() -> list[dict[str, str | None]]:
    todos: list[dict[str, str | None]] = []
    cursor: str | None = None
    for _ in range(20):
        payload: dict[str, Any] = {
            "filter": {"property": "Done?", "checkbox": {"equals": False}},
            "page_size": 100,
        }
        if cursor:
            payload["start_cursor"] = cursor
        response = _notion_request(payload)
        for page in response.get("results", []):
            if not isinstance(page, dict):
                continue
            item = _notion_title(page)
            if item:
                todos.append({"item": item, "due_date": _notion_date_start(page)})
        if not response.get("has_more"):
            break
        next_cursor = response.get("next_cursor")
        cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
        if cursor is None:
            break
    return sorted(
        todos,
        key=lambda todo: (
            todo.get("due_date") is None,
            str(todo.get("due_date") or "9999-12-31"),
            str(todo.get("item") or "").casefold(),
        ),
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ReviewDataError(f"{path.name} line {line_number} is invalid JSON") from error
        if not isinstance(record, dict):
            raise ReviewDataError(f"{path.name} line {line_number} is not an object")
        records.append(record)
    return records


def pending_sipi_ideas() -> list[dict[str, str]]:
    ideas: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    consumed: set[str] = set()
    for record in _read_jsonl(SIPI_INBOX):
        if record.get("type") == "sipi_idea" and isinstance(record.get("id"), str):
            idea_id = str(record["id"])
            ideas[idea_id] = record
            order.append(idea_id)
        elif (
            record.get("type") == "sipi_idea_status"
            and record.get("status") == "consumed"
            and isinstance(record.get("idea_id"), str)
        ):
            consumed.add(str(record["idea_id"]))
    result: list[dict[str, str]] = []
    for idea_id in order:
        idea = ideas.get(idea_id)
        if idea is None or idea_id in consumed:
            continue
        title = str(idea.get("title") or "").strip()
        if title:
            result.append({"id": idea_id, "title": title, "status": "pending"})
    return result


def build_telegram_report(
    report_date: dt.date,
    todos: list[dict[str, str | None]],
    ideas: list[dict[str, str]],
) -> str:
    lines = [f"🌙 每日盤點｜{report_date.isoformat()}", "", f"📋 未完成待辦（{len(todos)}）"]
    if todos:
        for index, todo in enumerate(todos, 1):
            item = str(todo.get("item") or "").strip()
            due_date = str(todo.get("due_date") or "未指定")
            lines.append(f"{index}. {item}｜{due_date}")
    else:
        lines.append("目前沒有未完成待辦。")

    lines.extend(("", f"💡 待消化 SI/PI idea（{len(ideas)}）"))
    if ideas:
        for index, idea in enumerate(ideas, 1):
            lines.append(f"{index}. {idea['title']}")
    else:
        lines.append("目前沒有待消化的 SI/PI idea。")
    return "\n".join(lines)


def build_payload(
    generated_at: dt.datetime,
    todos: list[dict[str, str | None]],
    ideas: list[dict[str, str]],
    telegram_text: str,
) -> dict[str, Any]:
    report_date = generated_at.date().isoformat()
    return {
        "version": "1.0",
        "report_id": f"hermes-daily-review:{report_date}",
        "date": report_date,
        "timezone": "Asia/Taipei",
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "subject": f"Hermes 每日盤點｜{report_date}",
        "todos": todos,
        "sipi_ideas": ideas,
        "telegram_text": telegram_text,
    }


def _post_to_n8n(payload: dict[str, Any]) -> bool:
    if not WEBHOOK_URL:
        logger.error("HERMES_DAILY_REVIEW_WEBHOOK_URL is not configured")
        return False
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_KEY:
        headers["X-Hermes-Daily-Review-Key"] = WEBHOOK_KEY
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(3):
        request = urllib.request.Request(
            WEBHOOK_URL, data=body, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if 200 <= response.status < 300:
                    return True
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            logger.exception("Daily review n8n delivery attempt %s failed", attempt + 1)
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return False


def _enqueue_delivery(payload: dict[str, Any]) -> None:
    try:
        existing = _read_jsonl(DELIVERY_OUTBOX)
        if any(record.get("report_id") == payload.get("report_id") for record in existing):
            return
        DELIVERY_OUTBOX.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "daily_review_delivery",
            "status": "pending",
            **payload,
        }
        with DELIVERY_OUTBOX.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        logger.exception("Could not retain failed daily review delivery")


def _append_delivery_status(report_id: str, status: str) -> None:
    DELIVERY_OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "daily_review_delivery_status",
        "report_id": report_id,
        "status": status,
        "updated_at": dt.datetime.now(TIMEZONE).isoformat(timespec="seconds"),
    }
    with DELIVERY_OUTBOX.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def retry_pending_deliveries() -> None:
    """Retry older webhook payloads; n8n deduplicates by report_id."""

    try:
        records = _read_jsonl(DELIVERY_OUTBOX)
    except Exception:
        logger.exception("Could not read daily review delivery outbox")
        return
    sent = {
        str(record["report_id"])
        for record in records
        if record.get("type") == "daily_review_delivery_status"
        and record.get("status") == "sent"
        and isinstance(record.get("report_id"), str)
    }
    pending: dict[str, dict[str, Any]] = {}
    for record in records:
        report_id = record.get("report_id")
        if (
            record.get("type") == "daily_review_delivery"
            and record.get("status") == "pending"
            and isinstance(report_id, str)
            and report_id not in sent
        ):
            pending[report_id] = {
                key: value
                for key, value in record.items()
                if key not in {"type", "status"}
            }
    for report_id, payload in pending.items():
        if _post_to_n8n(payload):
            try:
                _append_delivery_status(report_id, "sent")
            except Exception:
                logger.exception("Could not mark daily review delivery sent")


def run(now: dt.datetime | None = None) -> str:
    generated_at = now or dt.datetime.now(TIMEZONE)
    retry_pending_deliveries()
    todos = query_open_todos()
    ideas = pending_sipi_ideas()
    report = build_telegram_report(generated_at.date(), todos, ideas)
    payload = build_payload(generated_at, todos, ideas, report)
    if not _post_to_n8n(payload):
        _enqueue_delivery(payload)
    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        report = run()
    except ReviewDataError:
        logger.exception("Daily review stopped because source data is not trustworthy")
        print("❌ 每日盤點未完成\n\n暫時無法讀取完整的待辦或 SI/PI idea，未產生不完整盤點。")
        return 1
    except Exception:
        logger.exception("Daily review failed")
        print("❌ 每日盤點未完成\n\nHermes 暫時無法整理資料，請稍後再試。")
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
