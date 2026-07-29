"""Hermes-interpreted SI/PI idea capture with append-only local state."""

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
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


logger = logging.getLogger("hermes.plugin.sipi_idea_ingest")

TIMEZONE = ZoneInfo("Asia/Taipei")
INBOX = Path(
    os.getenv("SIPI_IDEA_INBOX_PATH", "/opt/data/workspace/sipi-idea-inbox.jsonl")
)
TOOL_NAME = "sipi_idea_execute"
TOOLSET_NAME = "sipi_idea_capture"
SESSION_TTL_SECONDS = 10 * 60

_SESSION_LOCK = threading.Lock()
_TELEGRAM_SESSIONS: dict[str, dict[str, str | float | None]] = {}
_PUNCTUATION_PATTERN = re.compile(r"[\s\-—_，。！？、：:；;,.!?「」『』（）()\[\]{}]+")


SIPI_IDEA_EXECUTE_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Save or consume one explicit signal-integrity or power-integrity idea. "
        "Hermes must shorten the idea before calling this tool. Do not use for "
        "todos, expenses, general SI/PI questions, or ordinary conversation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "consume"],
                "description": "Create a pending idea or mark one pending idea consumed.",
            },
            "title": {
                "type": "string",
                "description": (
                    "A concise Traditional-Chinese SI/PI topic title that preserves the "
                    "technical core and removes conversational filler."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "Optional one-sentence concise explanation. Do not invent facts that "
                    "were absent from the Telegram message."
                ),
            },
            "source_text": {
                "type": "string",
                "description": "The user's original Telegram text, retained for review.",
            },
        },
        "required": ["action", "title", "source_text"],
    },
}


class IdeaDataError(RuntimeError):
    """Raised when the append-only SI/PI inbox cannot be trusted."""


def _allowed_chat_ids() -> set[str]:
    explicit = os.getenv("SIPI_TELEGRAM_CHAT_ID", "").strip()
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
        logger.exception("Could not resolve the configured SI/PI Telegram chat")
    return set()


def _remember_telegram_session(**kwargs: Any) -> dict[str, str] | None:
    """Add SI/PI guidance only inside the configured private Telegram chat."""

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

    return {
        "context": (
            "Telegram 私人 SI/PI idea 規則（本回合可用 sipi_idea_execute）：\n"
            "- 只有使用者明確表示要保存、記錄或已消化一個 Signal Integrity / "
            "Power Integrity 想法時才呼叫。一般 SI/PI 問題、討論與查詢不要呼叫。\n"
            "- 待辦、提醒、工作事項或有期限的行動交給 Todo；支出交給 Expense。"
            "即使文字含有 SI/PI，只要核心是行動任務，就不要使用本工具。\n"
            "- create 時由你理解原文並精簡 title：保留技術主題，移除『我想到、可以研究、"
            "有關、這件事』等口語贅詞。summary 只整理原文，不可補造內容。\n"
            "- consume 時直接選擇一個最符合的待消化 idea，不詢問候選或二次確認；"
            "工具會用 title 對現有 pending ideas 做單一最佳匹配。\n"
            "- SI/PI idea 只寫 Hermes append-only JSONL，不寫 Notion。禁止用 terminal、"
            "file 或其他工具自行寫 idea。\n"
            "- 工具回傳 response 後，最終只輸出 Telegram JSON contract，逐字採用 response "
            "的 type/action/title/summary/data；confidence=1.0、actions=[]，不顯示內部欄位。"
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
            raise IdeaDataError(
                f"sipi-idea-inbox.jsonl line {line_number} is invalid JSON"
            ) from error
        if not isinstance(record, dict):
            raise IdeaDataError(
                f"sipi-idea-inbox.jsonl line {line_number} is not an object"
            )
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
        raise IdeaDataError("SI/PI idea append verification failed")


def _new_record_id(prefix: str) -> tuple[str, str]:
    now = dt.datetime.now(TIMEZONE)
    record_id = prefix + now.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    return record_id, now.isoformat(timespec="seconds")


def _normalized_title(value: str) -> str:
    return _PUNCTUATION_PATTERN.sub("", value).casefold()


def _pending_ideas(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ideas: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    consumed: set[str] = set()
    for record in records:
        record_type = record.get("type")
        if record_type == "sipi_idea" and isinstance(record.get("id"), str):
            idea_id = str(record["id"])
            ideas[idea_id] = record
            order.append(idea_id)
        elif (
            record_type == "sipi_idea_status"
            and record.get("status") == "consumed"
            and isinstance(record.get("idea_id"), str)
        ):
            consumed.add(str(record["idea_id"]))
    return [ideas[idea_id] for idea_id in order if idea_id in ideas and idea_id not in consumed]


def _source_event_record(
    records: list[dict[str, Any]], source_event_id: str | None
) -> dict[str, Any] | None:
    if not source_event_id:
        return None
    return next(
        (record for record in reversed(records) if record.get("source_event_id") == source_event_id),
        None,
    )


def _response(
    response_type: str,
    action: str,
    summary: str,
    data: dict[str, Any],
    *,
    status: str,
) -> str:
    return json.dumps(
        {
            "success": response_type != "error",
            "status": status,
            "response": {
                "type": response_type,
                "action": action,
                "title": "",
                "summary": summary,
                "data": data,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _failure(title: str, reason: str, status: str) -> str:
    return _response(
        "error",
        "sipi_idea_failed",
        f"SI/PI idea 未更新：{reason}",
        {"item": title},
        status=status,
    )


def _success_data(title: str, state: str) -> dict[str, str]:
    return {"item": title, "note": state}


def _create_idea(
    title: str,
    summary: str | None,
    source_text: str,
    source_event_id: str | None,
) -> str:
    try:
        records = _read_inbox()
        duplicate_event = _source_event_record(records, source_event_id)
        if duplicate_event is not None:
            existing_title = str(duplicate_event.get("title") or title)
            return _response(
                "success",
                "sipi_idea_already_saved",
                "SI/PI idea 已存在",
                _success_data(existing_title, "待消化"),
                status="already_exists",
            )

        normalized = _normalized_title(title)
        for idea in reversed(_pending_ideas(records)):
            if _normalized_title(str(idea.get("title") or "")) == normalized:
                return _response(
                    "success",
                    "sipi_idea_already_saved",
                    "SI/PI idea 已存在",
                    _success_data(str(idea["title"]), "待消化"),
                    status="already_exists",
                )

        record_id, created_at = _new_record_id("HERMES-SIPI-")
        record = {
            "type": "sipi_idea",
            "id": record_id,
            "created_at": created_at,
            "title": title,
            "summary": summary,
            "source_text": source_text,
            "status": "pending",
            "source": "telegram",
            "source_event_id": source_event_id,
        }
        _append_verified(record)
    except IdeaDataError:
        logger.exception("SI/PI idea inbox validation or append failed")
        return _failure(title, "Hermes idea 資料需要修復，已停止寫入。", "local_invalid")
    except Exception:
        logger.exception("SI/PI idea local ingest failed")
        return _failure(title, "Hermes 暫時無法儲存，請稍後再試。", "local_failed")

    return _response(
        "success",
        "sipi_idea_created",
        "SI/PI idea 已保存",
        _success_data(title, "待消化"),
        status="created",
    )


def _match_score(requested: str, candidate: str) -> float:
    left = _normalized_title(requested)
    right = _normalized_title(candidate)
    if left == right:
        return 1.0
    sequence = difflib.SequenceMatcher(None, left, right).ratio()
    left_chars = set(left)
    right_chars = set(right)
    union = left_chars | right_chars
    overlap = len(left_chars & right_chars) / len(union) if union else 0.0
    containment = (
        min(len(left), len(right)) / max(len(left), len(right))
        if left and right and (left in right or right in left)
        else 0.0
    )
    return max(sequence, overlap, containment)


def _consume_idea(
    requested_title: str,
    source_text: str,
    source_event_id: str | None,
) -> str:
    try:
        records = _read_inbox()
        duplicate_event = _source_event_record(records, source_event_id)
        if duplicate_event is not None:
            existing_title = str(duplicate_event.get("title") or requested_title)
            state = "已消化" if duplicate_event.get("status") == "consumed" else "待消化"
            return _response(
                "success",
                "sipi_idea_already_updated",
                "SI/PI idea 狀態已更新",
                _success_data(existing_title, state),
                status="already_updated",
            )

        pending = _pending_ideas(records)
        if not pending:
            return _failure(requested_title, "目前沒有待消化的 SI/PI idea。", "not_found")

        matched = max(
            pending,
            key=lambda idea: _match_score(requested_title, str(idea.get("title") or "")),
        )
        record_id, created_at = _new_record_id("HERMES-SIPI-STATE-")
        status_record = {
            "type": "sipi_idea_status",
            "id": record_id,
            "created_at": created_at,
            "idea_id": matched["id"],
            "title": matched["title"],
            "status": "consumed",
            "source_text": source_text,
            "source": "telegram",
            "source_event_id": source_event_id,
        }
        _append_verified(status_record)
    except IdeaDataError:
        logger.exception("SI/PI idea inbox validation failed")
        return _failure(
            requested_title, "Hermes idea 資料需要修復，已停止更新。", "local_invalid"
        )
    except Exception:
        logger.exception("SI/PI idea state update failed")
        return _failure(
            requested_title, "Hermes 暫時無法更新，請稍後再試。", "local_failed"
        )

    return _response(
        "success",
        "sipi_idea_consumed",
        "SI/PI idea 已標記為已消化",
        _success_data(str(matched["title"]), "已消化"),
        status="consumed",
    )


def _handle_sipi_idea_execute(args: dict[str, Any], **kwargs: Any) -> str:
    session_id = str(kwargs.get("session_id") or "").strip()
    context = _authorized_context(session_id)
    if context is None:
        logger.warning("Rejected sipi_idea_execute outside an authorized Telegram session")
        return _failure("SI/PI idea", "這個工具只接受指定 Telegram 私聊的請求。", "unauthorized")

    action = str(args.get("action") or "").strip().lower()
    title = str(args.get("title") or "").strip().strip("「」『』\"'")
    if action not in {"create", "consume"}:
        return _failure(title or "SI/PI idea", "無法辨識新增或消化動作。", "invalid_action")
    if not title:
        return _failure("SI/PI idea", "缺少 idea 主題。", "missing_title")
    if len(title) > 160:
        return _failure(title[:160], "idea 主題過長。", "invalid_title")

    source_text = str(context.get("source_text") or args.get("source_text") or "").strip()
    source_event_id = str(context.get("source_event_id") or "").strip() or None
    if not source_text:
        source_text = str(args.get("source_text") or title).strip()

    if action == "consume":
        return _consume_idea(title, source_text, source_event_id)

    summary = str(args.get("summary") or "").strip() or None
    if summary and len(summary) > 500:
        summary = summary[:500].rstrip()
    return _create_idea(title, summary, source_text, source_event_id)


def register(ctx: Any) -> None:
    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET_NAME,
        schema=SIPI_IDEA_EXECUTE_SCHEMA,
        handler=_handle_sipi_idea_execute,
        description=(
            "Hermes shortens one explicit SI/PI idea, then this tool performs the only "
            "allowed append-only idea write or consumed-state update."
        ),
        emoji="💡",
    )
    ctx.register_hook("pre_llm_call", _remember_telegram_session)
