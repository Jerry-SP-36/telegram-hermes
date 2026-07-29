"""Deterministic Telegram-to-todo bridge for Hermes Gateway."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


logger = logging.getLogger("hermes.plugin.todo_direct_ingest")

TIMEZONE = ZoneInfo("Asia/Taipei")
INBOX = Path(os.getenv("TODO_INBOX_PATH", "/opt/data/workspace/todo-inbox.jsonl"))
DATA_SOURCE_ID = os.getenv(
    "TODO_NOTION_DATA_SOURCE_ID", "3327c93b-34d9-8094-b5b4-000beb590261"
).strip()
NOTION_VERSION = "2026-03-11"
DEFAULT_FOR_WHO = "Myself"

_PREFIX_PATTERN = re.compile(
    r"^(?:待辦\s*[:：]|提醒我(?:要|去|一下)?|記住(?:要|去)?|要記得|加入任務\s*[:：]?)\s*(?P<item>.*)$"
)
_DATED_ACTION_PATTERN = re.compile(r"^(?:今天|明天)要\S.*$")
_QUERY_PATTERN = re.compile(
    r"(?:今天|明天)?要做什麼|(?:有|有哪些|查看|查詢|列出|顯示).{0,8}待辦|待辦.{0,8}(?:有什麼|有哪些|清單|列表|進度|狀態)"
)
_ISO_DATE_PATTERN = re.compile(r"(?<!\d)(?P<date>20\d{2}-\d{1,2}-\d{1,2})(?!\d)")
_LOCAL_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<month>1[0-2]|0?[1-9])(?:/|月)(?P<day>3[01]|[12]\d|0?[1-9])(?:日)?(?!\d)"
)


class TodoDataError(RuntimeError):
    """Raised when the append-only todo inbox cannot be trusted."""


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform)).lower()


def _allowed_chat_ids() -> set[str]:
    explicit = os.getenv("TODO_TELEGRAM_CHAT_ID", "").strip()
    if explicit:
        return {item.strip() for item in explicit.split(",") if item.strip()}

    try:
        try:
            from hermes.constants import get_hermes_home

            config_path = get_hermes_home() / "config.yaml"
        except ImportError:
            config_path = Path.home() / ".hermes" / "config.yaml"
        config_text = config_path.read_text(encoding="utf-8")
        try:
            import yaml

            config = yaml.safe_load(config_text) or {}
            prompts = (
                config.get("platforms", {})
                .get("telegram", {})
                .get("extra", {})
                .get("channel_prompts", {})
            )
            if isinstance(prompts, dict):
                return {str(key).strip() for key in prompts if str(key).strip()}
        except ImportError:
            lines = config_text.splitlines()
            for index, line in enumerate(lines):
                if line.strip() != "channel_prompts:":
                    continue
                base_indent = len(line) - len(line.lstrip())
                keys: set[str] = set()
                for candidate in lines[index + 1 :]:
                    if not candidate.strip() or candidate.lstrip().startswith("#"):
                        continue
                    indent = len(candidate) - len(candidate.lstrip())
                    if indent <= base_indent:
                        break
                    key = candidate.strip().split(":", 1)[0].strip().strip("\"")
                    if key:
                        keys.add(key)
                return keys
    except Exception:
        logger.exception("Could not resolve the configured todo Telegram chat")
    return set()


def _is_query(text: str) -> bool:
    return bool(_QUERY_PATTERN.search(text))


def _parse_todo_text(text: str) -> tuple[str, str | None] | None:
    normalized = (text or "").strip()
    if not normalized or normalized.startswith("/"):
        return None

    prefix_match = _PREFIX_PATTERN.fullmatch(normalized)
    if prefix_match is not None:
        item = prefix_match.group("item").strip()
        if item and not _is_query(item):
            return item, _derive_due_date(item)
        if not item:
            return "", None
        return None

    if _is_query(normalized) or "?" in normalized or "？" in normalized:
        return None
    if _DATED_ACTION_PATTERN.fullmatch(normalized):
        return normalized, _derive_due_date(normalized)
    return None


def _derive_due_date(text: str, today: dt.date | None = None) -> str | None:
    today = today or dt.datetime.now(TIMEZONE).date()
    if "明天" in text:
        return (today + dt.timedelta(days=1)).isoformat()
    if "今天" in text:
        return today.isoformat()

    iso_match = _ISO_DATE_PATTERN.search(text)
    if iso_match is not None:
        try:
            return dt.date.fromisoformat(iso_match.group("date")).isoformat()
        except ValueError:
            return None

    local_match = _LOCAL_DATE_PATTERN.search(text)
    if local_match is None:
        return None
    month = int(local_match.group("month"))
    day = int(local_match.group("day"))
    try:
        due = dt.date(today.year, month, day)
        if due < today:
            due = dt.date(today.year + 1, month, day)
        return due.isoformat()
    except ValueError:
        return None


def _source_event_id(event: Any) -> str | None:
    source = getattr(event, "source", None)
    chat_id = str(getattr(source, "chat_id", "") or "").strip()
    message_id = str(getattr(event, "message_id", "") or "").strip()
    if not chat_id or not message_id:
        return None
    return f"telegram:{chat_id}:{message_id}"


def _read_inbox() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not INBOX.exists():
        return records
    for line_number, raw in enumerate(INBOX.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as error:
            raise TodoDataError(f"todo-inbox.jsonl line {line_number} is invalid JSON") from error
        if not isinstance(record, dict):
            raise TodoDataError(f"todo-inbox.jsonl line {line_number} is not an object")
        records.append(record)
    return records


def _local_record(
    item: str,
    due_date: str | None,
    for_who: str,
    note: str | None,
    source_event_id: str | None,
) -> tuple[dict[str, Any], bool]:
    records = _read_inbox()
    if source_event_id:
        for record in reversed(records):
            if record.get("source_event_id") == source_event_id:
                return record, False
    for record in reversed(records):
        if (
            record.get("type") == "todo"
            and record.get("item") == item
            and record.get("due_date") == due_date
            and record.get("for_who") == for_who
        ):
            return record, False

    now = dt.datetime.now(TIMEZONE)
    record = {
        "type": "todo",
        "id": "HERMES-TELEGRAM-" + now.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2),
        "created_at": now.isoformat(timespec="seconds"),
        "item": item,
        "due_date": due_date,
        "for_who": for_who,
        "note": note,
        "source": "telegram",
        "source_event_id": source_event_id,
    }
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    with INBOX.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    verified = next((row for row in _read_inbox() if row.get("id") == record["id"]), None)
    if verified != record:
        raise TodoDataError("todo append verification failed")
    return record, True


def _notion_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    token = os.getenv("NOTION_API_KEY", "").strip()
    if not token:
        raise RuntimeError("NOTION_API_KEY is not configured")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "https://api.notion.com" + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8", "replace"))
            message = detail.get("message") or detail.get("code") or "Notion API error"
        except Exception:
            message = "Notion API error"
        raise RuntimeError(f"Notion API {error.code}: {message}") from error


def _notion_select_name(page: dict[str, Any], property_name: str) -> str | None:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return None
    value = properties.get(property_name)
    if not isinstance(value, dict):
        return None
    selected = value.get("select")
    if not isinstance(selected, dict):
        return None
    name = selected.get("name")
    return name if isinstance(name, str) else None


def _notion_date_start(page: dict[str, Any], property_name: str) -> str | None:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return None
    value = properties.get(property_name)
    if not isinstance(value, dict):
        return None
    date_value = value.get("date")
    if not isinstance(date_value, dict):
        return None
    start = date_value.get("start")
    return start[:10] if isinstance(start, str) else None


def _query_matching_todo(record: dict[str, Any]) -> bool:
    results = _notion_request(
        "POST",
        f"/v1/data_sources/{DATA_SOURCE_ID}/query",
        {
            "filter": {
                "property": "Item",
                "title": {"equals": record["item"]},
            },
            "page_size": 100,
        },
    ).get("results", [])
    return any(
        isinstance(page, dict)
        and _notion_select_name(page, "For Who") == record["for_who"]
        and _notion_date_start(page, "Due Date") == record.get("due_date")
        for page in results
    )


def _sync_to_notion(record: dict[str, Any]) -> str:
    if _query_matching_todo(record):
        return "already_exists"
    properties: dict[str, Any] = {
        "Item": {"title": [{"text": {"content": record["item"]}}]},
        "For Who": {"select": {"name": record["for_who"]}},
        "Done?": {"checkbox": False},
        "Date": {"date": {"start": record["created_at"][:10]}},
    }
    if record.get("due_date"):
        properties["Due Date"] = {"date": {"start": record["due_date"]}}
    if record.get("note"):
        properties["備註"] = {
            "rich_text": [{"text": {"content": str(record["note"])[:2000]}}]
        }
    _notion_request(
        "POST",
        "/v1/pages",
        {
            "parent": {"type": "data_source_id", "data_source_id": DATA_SOURCE_ID},
            "properties": properties,
        },
    )
    return "created"


def _due_label(due_date: str | None, today: dt.date | None = None) -> str:
    if not due_date:
        return "未指定"
    today = today or dt.datetime.now(TIMEZONE).date()
    if due_date == today.isoformat():
        return "今天"
    if due_date == (today + dt.timedelta(days=1)).isoformat():
        return "明天"
    return due_date


def _success_reply(record: dict[str, Any]) -> str:
    return (
        "✅ 待辦已記錄並同步到 Notion\n\n"
        f"項目：{record['item']}\n"
        f"期限：{_due_label(record.get('due_date'))}\n"
        f"對象：{record['for_who']}"
    )


def _notion_warning_reply(record: dict[str, Any]) -> str:
    return (
        "⚠️ 待辦已保留在 Hermes，但尚未同步到 Notion\n\n"
        f"項目：{record['item']}\n"
        "稍後重試不會重複建立待辦。"
    )


async def _send_reply(gateway: Any, event: Any, text: str) -> None:
    source = event.source
    adapter = gateway.adapters.get(source.platform)
    if adapter is None:
        logger.error("Telegram adapter unavailable after todo ingest")
        return
    result = await adapter.send(str(source.chat_id), text)
    if result is not None and getattr(result, "success", True) is False:
        logger.error(
            "Telegram todo acknowledgement failed: %s",
            getattr(result, "error", "unknown"),
        )


def _schedule_reply(gateway: Any, event: Any, reply: str) -> None:
    try:
        asyncio.get_running_loop().create_task(_send_reply(gateway, event, reply))
    except RuntimeError:
        logger.error("No running event loop available for Telegram todo acknowledgement")


def _intercept_todo(event: Any, gateway: Any, **kwargs: Any) -> dict[str, str] | None:
    del kwargs
    source = getattr(event, "source", None)
    if source is None or _platform_name(source) != "telegram":
        return None

    allowed_chats = _allowed_chat_ids()
    chat_id = str(getattr(source, "chat_id", "") or "")
    user_id = str(getattr(source, "user_id", "") or "")
    if not allowed_chats.intersection((chat_id, user_id)):
        return None

    parsed = _parse_todo_text(getattr(event, "text", ""))
    if parsed is None:
        return None
    item, due_date = parsed
    if not item:
        _schedule_reply(gateway, event, "❓ 請告訴我要記錄的待辦內容。")
        return {"action": "skip", "reason": "todo-direct-ingest-missing-item"}

    try:
        record, _ = _local_record(
            item=item,
            due_date=due_date,
            for_who=DEFAULT_FOR_WHO,
            note=None,
            source_event_id=_source_event_id(event),
        )
    except TodoDataError:
        logger.exception("Todo inbox validation or append failed")
        _schedule_reply(
            gateway,
            event,
            "❌ 待辦未記錄\n\n原因：Hermes 待辦資料需要修復，已停止寫入。",
        )
        return {"action": "skip", "reason": "todo-direct-ingest-local-failed"}
    except Exception:
        logger.exception("Deterministic todo local ingest failed")
        _schedule_reply(
            gateway,
            event,
            "❌ 待辦未記錄\n\n原因：Hermes 暫時無法儲存待辦，請稍後再試。",
        )
        return {"action": "skip", "reason": "todo-direct-ingest-local-failed"}

    try:
        _sync_to_notion(record)
        reply = _success_reply(record)
    except Exception:
        logger.exception("Todo persisted locally but Notion sync failed")
        reply = _notion_warning_reply(record)

    _schedule_reply(gateway, event, reply)
    return {"action": "skip", "reason": "todo-direct-ingest"}


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", _intercept_todo)
