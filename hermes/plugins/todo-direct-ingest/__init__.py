"""Hermes-interpreted Telegram todo capture with deterministic side effects."""

from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import os
import re
import secrets
import threading
import time
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
TOOL_NAME = "todo_execute"
TOOLSET_NAME = "todo_capture"
SESSION_TTL_SECONDS = 10 * 60
MATCH_THRESHOLD = 0.42

_ISO_DATE_PATTERN = re.compile(r"(?<!\d)(?P<date>20\d{2}-\d{1,2}-\d{1,2})(?!\d)")
_LOCAL_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<month>1[0-2]|0?[1-9])(?:/|月)(?P<day>3[01]|[12]\d|0?[1-9])(?:日)?(?!\d)"
)
_MATCH_NOISE_PATTERN = re.compile(
    r"今天|明天|後天|待辦|任務|提醒我|記得|要記得|已經|已完成|完成了?|"
    r"我要|要做|去做|處理|詢問|有關|相關|申辦|申請的事情|的事情|事情|一下"
)
_PUNCTUATION_PATTERN = re.compile(r"[\s\-—_，。！？、：:；;,.!?「」『』（）()\[\]{}]+")

_SESSION_LOCK = threading.Lock()
_TELEGRAM_SESSIONS: dict[str, dict[str, str | float | None]] = {}


TODO_EXECUTE_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Create or complete a personal todo after interpreting the user's natural "
        "Traditional-Chinese request. Use only for an explicit todo action, never for "
        "todo queries, general chat, slash commands, or expense capture."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "complete"],
                "description": "Whether to create a todo or mark one completed.",
            },
            "item": {
                "type": "string",
                "description": (
                    "A short normalized task title. Remove conversational filler, "
                    "relative-date words, and phrases such as 有關/的事情 while preserving meaning."
                ),
            },
            "due_date": {
                "type": "string",
                "description": (
                    "Due date as YYYY-MM-DD in Asia/Taipei. Omit when no due date was stated."
                ),
            },
            "for_who": {
                "type": "string",
                "description": "Responsible person; use Myself when the user did not specify one.",
            },
            "source_text": {
                "type": "string",
                "description": "The user's original Telegram text, retained for review.",
            },
        },
        "required": ["action", "item", "source_text"],
    },
}


class TodoDataError(RuntimeError):
    """Raised when the append-only todo inbox cannot be trusted."""


def _allowed_chat_ids() -> set[str]:
    explicit = os.getenv("TODO_TELEGRAM_CHAT_ID", "").strip()
    if explicit:
        return {item.strip() for item in explicit.split(",") if item.strip()}

    try:
        try:
            from hermes_constants import get_hermes_home

            config_path = get_hermes_home() / "config.yaml"
        except ImportError:
            try:
                from hermes.constants import get_hermes_home

                config_path = get_hermes_home() / "config.yaml"
            except ImportError:
                configured_home = os.getenv("HERMES_HOME", "").strip()
                config_path = (
                    Path(configured_home) / "config.yaml"
                    if configured_home
                    else Path.home() / ".hermes" / "config.yaml"
                )
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
                    key = candidate.strip().split(":", 1)[0].strip().strip("\"'")
                    if key:
                        keys.add(key)
                return keys
    except Exception:
        logger.exception("Could not resolve the configured todo Telegram chat")
    return set()


def _remember_telegram_session(**kwargs: Any) -> dict[str, str] | None:
    """Give Hermes Todo guidance only inside the configured private Telegram chat."""

    platform = str(kwargs.get("platform") or "").strip().lower()
    sender_id = str(kwargs.get("sender_id") or "").strip()
    session_id = str(kwargs.get("session_id") or "").strip()
    if platform != "telegram" or not sender_id or sender_id not in _allowed_chat_ids():
        return None

    now = time.monotonic()
    with _SESSION_LOCK:
        expired = [
            key
            for key, value in _TELEGRAM_SESSIONS.items()
            if now - float(value.get("seen_at") or 0) > SESSION_TTL_SECONDS
        ]
        for key in expired:
            _TELEGRAM_SESSIONS.pop(key, None)
        if session_id:
            turn_id = str(kwargs.get("turn_id") or "").strip()
            _TELEGRAM_SESSIONS[session_id] = {
                "sender_id": sender_id,
                "source_text": str(kwargs.get("user_message") or "").strip(),
                "source_event_id": (
                    f"telegram:{sender_id}:{turn_id}" if turn_id else None
                ),
                "seen_at": now,
            }

    today = dt.datetime.now(TIMEZONE).date()
    tomorrow = today + dt.timedelta(days=1)
    return {
        "context": (
            "Telegram 私人待辦規則（本回合可用 todo_execute）：\n"
            "- 由你理解自然語言，不要把整句原文直接當成 item。item 要短、清楚並保留核心意思；"
            "例如「今天要詢問歐美亞有關護照申辦的事情」應整理成「歐美亞護照申請」。\n"
            f"- Asia/Taipei 今天是 {today.isoformat()}，明天是 {tomorrow.isoformat()}。"
            "期限轉成 YYYY-MM-DD；未提期限就省略 due_date。未提對象時 for_who=Myself。\n"
            "- 明確要新增、提醒、記住或完成待辦時，直接呼叫 todo_execute，一律不詢問確認。"
            "完成語句也要整理成核心項目，工具會自動找最合適的未完成項目。\n"
            "- 查詢型句子（例如今天要做什麼）、一般聊天、/指令或支出訊息不要呼叫此工具。\n"
            "- 禁止用 terminal、file、Notion 或其他工具自行寫待辦；Todo 只能由 todo_execute 寫入。\n"
            "- 工具回傳 response 後，最終只輸出 Telegram JSON contract，逐字採用 response 的 "
            "type/action/title/summary/data；confidence=1.0、actions=[]，不要補問或顯示內部欄位。"
        )
    }


def _authorized_context(session_id: str) -> dict[str, str | float | None] | None:
    if not session_id:
        return None
    with _SESSION_LOCK:
        context = _TELEGRAM_SESSIONS.get(session_id)
        if context is None:
            return None
        if time.monotonic() - float(context.get("seen_at") or 0) > SESSION_TTL_SECONDS:
            _TELEGRAM_SESSIONS.pop(session_id, None)
            return None
        return dict(context)


def _derive_due_date(text: str, today: dt.date | None = None) -> str | None:
    today = today or dt.datetime.now(TIMEZONE).date()
    if "後天" in text:
        return (today + dt.timedelta(days=2)).isoformat()
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


def _validated_due_date(value: Any, source_text: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return _derive_due_date(source_text)
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError as error:
        raise ValueError("期限格式無法辨識") from error


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


def _append_verified(record: dict[str, Any]) -> None:
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    with INBOX.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    verified = next((row for row in _read_inbox() if row.get("id") == record["id"]), None)
    if verified != record:
        raise TodoDataError("todo append verification failed")


def _new_record_id(prefix: str) -> tuple[str, str]:
    now = dt.datetime.now(TIMEZONE)
    record_id = prefix + now.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    return record_id, now.isoformat(timespec="seconds")


def _local_record(
    item: str,
    due_date: str | None,
    for_who: str,
    note: str | None,
    source_event_id: str | None,
    *,
    done: bool = False,
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
            and bool(record.get("done")) is done
        ):
            return record, False

    record_id, created_at = _new_record_id("HERMES-TELEGRAM-")
    record = {
        "type": "todo",
        "id": record_id,
        "created_at": created_at,
        "item": item,
        "due_date": due_date,
        "for_who": for_who,
        "done": done,
        "note": note,
        "source": "telegram",
        "source_event_id": source_event_id,
    }
    _append_verified(record)
    return record, True


def _local_completion_record(
    requested_item: str,
    matched: dict[str, Any] | None,
    note: str,
    source_event_id: str | None,
) -> tuple[dict[str, Any], bool]:
    records = _read_inbox()
    page_id = str((matched or {}).get("page_id") or "") or None
    if source_event_id:
        for record in reversed(records):
            if record.get("source_event_id") == source_event_id:
                return record, False
    for record in reversed(records):
        if (
            record.get("type") == "todo_completion"
            and record.get("requested_item") == requested_item
            and (
                not page_id
                or record.get("notion_page_id") in (None, page_id)
            )
        ):
            return record, False
    if page_id:
        for record in reversed(records):
            if record.get("type") == "todo_completion" and record.get("notion_page_id") == page_id:
                return record, False

    record_id, created_at = _new_record_id("HERMES-TODO-DONE-")
    record = {
        "type": "todo_completion",
        "id": record_id,
        "created_at": created_at,
        "item": requested_item,
        "matched_item": (matched or {}).get("item"),
        "requested_item": requested_item,
        "due_date": (matched or {}).get("due_date"),
        "for_who": (matched or {}).get("for_who") or DEFAULT_FOR_WHO,
        "done": True,
        "note": note,
        "source": "telegram",
        "source_event_id": source_event_id,
        "notion_page_id": page_id,
    }
    _append_verified(record)
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
    value = (page.get("properties") or {}).get(property_name)
    selected = value.get("select") if isinstance(value, dict) else None
    name = selected.get("name") if isinstance(selected, dict) else None
    return name if isinstance(name, str) else None


def _notion_date_start(page: dict[str, Any], property_name: str) -> str | None:
    value = (page.get("properties") or {}).get(property_name)
    date_value = value.get("date") if isinstance(value, dict) else None
    start = date_value.get("start") if isinstance(date_value, dict) else None
    return start[:10] if isinstance(start, str) else None


def _notion_checkbox(page: dict[str, Any], property_name: str) -> bool | None:
    value = (page.get("properties") or {}).get(property_name)
    checked = value.get("checkbox") if isinstance(value, dict) else None
    return checked if isinstance(checked, bool) else None


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


def _query_todos_by_item(item: str) -> list[dict[str, Any]]:
    response = _notion_request(
        "POST",
        f"/v1/data_sources/{DATA_SOURCE_ID}/query",
        {"filter": {"property": "Item", "title": {"equals": item}}, "page_size": 100},
    )
    results = response.get("results", []) if isinstance(response, dict) else []
    return [page for page in results if isinstance(page, dict)]


def _query_all_todos() -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(10):
        payload: dict[str, Any] = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        response = _notion_request(
            "POST", f"/v1/data_sources/{DATA_SOURCE_ID}/query", payload
        )
        if not isinstance(response, dict):
            break
        pages.extend(page for page in response.get("results", []) if isinstance(page, dict))
        if not response.get("has_more"):
            break
        next_cursor = response.get("next_cursor")
        cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
        if cursor is None:
            break
    return pages


def _query_matching_todo(record: dict[str, Any]) -> bool:
    expected_done = bool(record.get("done"))
    return any(
        _notion_select_name(page, "For Who") == record["for_who"]
        and _notion_date_start(page, "Due Date") == record.get("due_date")
        and bool(_notion_checkbox(page, "Done?")) is expected_done
        for page in _query_todos_by_item(record["item"])
    )


def _sync_to_notion(record: dict[str, Any]) -> str:
    if _query_matching_todo(record):
        return "already_exists"
    properties: dict[str, Any] = {
        "Item": {"title": [{"text": {"content": record["item"]}}]},
        "For Who": {"select": {"name": record["for_who"]}},
        "Done?": {"checkbox": bool(record.get("done"))},
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


def _todo_from_notion_page(page: dict[str, Any]) -> dict[str, Any] | None:
    item = _notion_title(page)
    page_id = page.get("id")
    if not item or not isinstance(page_id, str) or not page_id:
        return None
    return {
        "page_id": page_id,
        "item": item,
        "due_date": _notion_date_start(page, "Due Date"),
        "for_who": _notion_select_name(page, "For Who") or DEFAULT_FOR_WHO,
        "done": _notion_checkbox(page, "Done?") is True,
    }


def _matching_text(value: str) -> str:
    text = _PUNCTUATION_PATTERN.sub("", value.lower())
    return _MATCH_NOISE_PATTERN.sub("", text)


def _match_score(requested: str, candidate: str) -> float:
    raw_requested = _PUNCTUATION_PATTERN.sub("", requested.lower())
    raw_candidate = _PUNCTUATION_PATTERN.sub("", candidate.lower())
    semantic_requested = _matching_text(requested) or raw_requested
    semantic_candidate = _matching_text(candidate) or raw_candidate
    if semantic_requested == semantic_candidate:
        return 1.0
    sequence = difflib.SequenceMatcher(None, semantic_requested, semantic_candidate).ratio()
    requested_chars = set(semantic_requested)
    candidate_chars = set(semantic_candidate)
    union = requested_chars | candidate_chars
    overlap = len(requested_chars & candidate_chars) / len(union) if union else 0.0
    containment = (
        min(len(semantic_requested), len(semantic_candidate))
        / max(len(semantic_requested), len(semantic_candidate))
        if semantic_requested in semantic_candidate or semantic_candidate in semantic_requested
        else 0.0
    )
    return max(sequence, overlap, containment)


def _best_todo_match(
    requested_item: str, pages: list[dict[str, Any]], *, done: bool
) -> tuple[dict[str, Any] | None, float]:
    candidates = [candidate for page in pages if (candidate := _todo_from_notion_page(page))]
    candidates = [candidate for candidate in candidates if bool(candidate["done"]) is done]
    if not candidates:
        return None, 0.0
    scored = [(_match_score(requested_item, str(candidate["item"])), candidate) for candidate in candidates]
    score, best = max(scored, key=lambda pair: pair[0])
    return best, score


def _complete_notion_page(candidate: dict[str, Any]) -> None:
    _notion_request(
        "PATCH",
        f"/v1/pages/{candidate['page_id']}",
        {"properties": {"Done?": {"checkbox": True}}},
    )


def _due_label(due_date: str | None, today: dt.date | None = None) -> str:
    if not due_date:
        return "未指定"
    today = today or dt.datetime.now(TIMEZONE).date()
    if due_date == today.isoformat():
        return "今天"
    if due_date == (today + dt.timedelta(days=1)).isoformat():
        return "明天"
    return due_date


def _response(
    response_type: str,
    action: str,
    summary: str,
    data: dict[str, Any],
    *,
    title: str = "",
    status: str,
) -> str:
    return json.dumps(
        {
            "success": response_type != "error",
            "status": status,
            "response": {
                "type": response_type,
                "action": action,
                "title": title,
                "summary": summary,
                "data": data,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _success_data(record: dict[str, Any]) -> dict[str, str]:
    return {
        "item": str(record["item"]),
        "due_date": _due_label(record.get("due_date")),
        "for_who": str(record.get("for_who") or DEFAULT_FOR_WHO),
    }


def _local_failure(item: str, reason: str, status: str) -> str:
    return _response(
        "error",
        "todo_failed",
        f"待辦未記錄：{reason}",
        {"item": item},
        status=status,
    )


def _notion_warning(record: dict[str, Any], action: str, summary: str) -> str:
    return _response(
        "confirm",
        action,
        summary,
        _success_data(record),
        title="Notion 尚未同步",
        status="local_only",
    )


def _create_todo(
    item: str,
    due_date: str | None,
    for_who: str,
    source_text: str,
    source_event_id: str | None,
) -> str:
    try:
        record, _ = _local_record(
            item=item,
            due_date=due_date,
            for_who=for_who,
            note=source_text,
            source_event_id=source_event_id,
        )
    except TodoDataError:
        logger.exception("Todo inbox validation or append failed")
        return _local_failure(item, "Hermes 待辦資料需要修復，已停止寫入。", "local_invalid")
    except Exception:
        logger.exception("Todo local ingest failed")
        return _local_failure(item, "Hermes 暫時無法儲存，請稍後再試。", "local_failed")

    try:
        _sync_to_notion(record)
    except Exception:
        logger.exception("Todo persisted locally but Notion sync failed")
        return _notion_warning(
            record,
            "todo_created_local_only",
            "待辦已保留在 Hermes；稍後重試不會重複建立。",
        )
    return _response(
        "success",
        "todo_created",
        "待辦已記錄並同步到 Notion",
        _success_data(record),
        status="created",
    )


def _complete_todo(
    requested_item: str,
    source_text: str,
    source_event_id: str | None,
) -> str:
    try:
        _read_inbox()
    except TodoDataError:
        logger.exception("Todo inbox validation failed before completion")
        return _local_failure(
            requested_item, "Hermes 待辦資料需要修復，已停止寫入。", "local_invalid"
        )

    try:
        pages = _query_all_todos()
    except Exception:
        logger.exception("Could not query Notion todos for completion")
        try:
            pending, _ = _local_completion_record(
                requested_item, None, source_text, source_event_id
            )
        except Exception:
            logger.exception("Could not retain pending todo completion")
            return _local_failure(
                requested_item, "Hermes 與 Notion 目前都無法更新，請稍後再試。", "completion_failed"
            )
        return _notion_warning(
            pending,
            "todo_completion_local_only",
            "完成狀態已保留在 Hermes；稍後重試會繼續同步。",
        )

    open_match, open_score = _best_todo_match(requested_item, pages, done=False)
    if open_match is not None and open_score >= MATCH_THRESHOLD:
        try:
            completion, _ = _local_completion_record(
                requested_item, open_match, source_text, source_event_id
            )
        except Exception:
            logger.exception("Could not append todo completion event")
            return _local_failure(
                requested_item, "Hermes 暫時無法保存完成狀態，未變更 Notion。", "local_failed"
            )
        try:
            _complete_notion_page(open_match)
        except Exception:
            logger.exception("Todo completion retained locally but Notion update failed")
            return _notion_warning(
                completion,
                "todo_completion_local_only",
                "完成狀態已保留在 Hermes；稍後重試會繼續同步。",
            )
        return _response(
            "success",
            "todo_completed",
            "待辦已完成並更新到 Notion",
            _success_data(completion),
            status="completed",
        )

    done_match, done_score = _best_todo_match(requested_item, pages, done=True)
    if done_match is not None and done_score >= MATCH_THRESHOLD:
        try:
            completion, _ = _local_completion_record(
                requested_item, done_match, source_text, source_event_id
            )
        except Exception:
            logger.exception("Could not append already-completed todo event")
            completion = done_match
        return _response(
            "success",
            "todo_already_completed",
            "待辦已是完成狀態",
            _success_data(completion),
            status="already_completed",
        )

    due_date = _derive_due_date(source_text)
    try:
        record, _ = _local_record(
            item=requested_item,
            due_date=due_date,
            for_who=DEFAULT_FOR_WHO,
            note=source_text,
            source_event_id=source_event_id,
            done=True,
        )
    except Exception:
        logger.exception("Could not save unmatched completed todo")
        return _local_failure(
            requested_item, "Hermes 暫時無法保存完成紀錄，請稍後再試。", "local_failed"
        )
    try:
        _sync_to_notion(record)
    except Exception:
        logger.exception("Completed todo saved locally but Notion creation failed")
        return _notion_warning(
            record,
            "todo_completion_local_only",
            "完成紀錄已保留在 Hermes；稍後重試會繼續同步。",
        )
    return _response(
        "success",
        "todo_completed_created",
        "已建立完成紀錄並同步到 Notion",
        _success_data(record),
        status="completed_created",
    )


def _handle_todo_execute(args: dict[str, Any], **kwargs: Any) -> str:
    session_id = str(kwargs.get("session_id") or "").strip()
    context = _authorized_context(session_id)
    if context is None:
        logger.warning("Rejected todo_execute outside an authorized Telegram session")
        return _local_failure("待辦", "這個工具只接受指定 Telegram 私聊的請求。", "unauthorized")

    action = str(args.get("action") or "").strip().lower()
    item = str(args.get("item") or "").strip().strip("「」『』\"'")
    if action not in {"create", "complete"}:
        return _local_failure(item or "待辦", "無法辨識新增或完成動作。", "invalid_action")
    if not item:
        return _local_failure("待辦", "缺少待辦項目。", "missing_item")
    if len(item) > 200:
        return _local_failure(item[:200], "待辦項目過長。", "invalid_item")

    source_text = str(context.get("source_text") or args.get("source_text") or "").strip()
    source_event_id = str(context.get("source_event_id") or "").strip() or None
    if not source_text:
        source_text = str(args.get("source_text") or item).strip()

    if action == "complete":
        return _complete_todo(item, source_text, source_event_id)

    for_who = str(args.get("for_who") or DEFAULT_FOR_WHO).strip() or DEFAULT_FOR_WHO
    try:
        due_date = _validated_due_date(args.get("due_date"), source_text)
    except ValueError:
        return _local_failure(item, "期限格式無法辨識。", "invalid_due_date")
    return _create_todo(item, due_date, for_who, source_text, source_event_id)


def register(ctx: Any) -> None:
    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET_NAME,
        schema=TODO_EXECUTE_SCHEMA,
        handler=_handle_todo_execute,
        description=(
            "Hermes interprets a natural Telegram todo, then this tool performs the only "
            "allowed JSONL and Notion write."
        ),
        emoji="✅",
    )
    ctx.register_hook("pre_llm_call", _remember_telegram_session)
