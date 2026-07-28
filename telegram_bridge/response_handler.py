"""Validate Hermes final-response JSON and render concise Telegram text."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


FORMAT_ERROR_MESSAGE = "❌ 回覆格式錯誤，請再試一次。"
ALLOWED_TYPES = frozenset({"success", "question", "confirm", "error", "progress"})
REQUIRED_FIELDS = frozenset(
    {"version", "type", "action", "title", "summary", "confidence", "data", "actions"}
)


class ContractError(ValueError):
    """The response is not a valid Hermes Telegram JSON contract."""


def _require_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    return value


def validate_response(payload: Any) -> dict[str, Any]:
    """Return a validated response or raise ContractError without exposing it."""

    if not isinstance(payload, dict):
        raise ContractError("response must be an object")
    if set(payload) != REQUIRED_FIELDS:
        raise ContractError("response fields do not match the contract")
    if payload["version"] != "1.0":
        raise ContractError("unsupported contract version")
    response_type = _require_string(payload, "type")
    if response_type not in ALLOWED_TYPES:
        raise ContractError("unsupported response type")

    _require_string(payload, "action")
    title = _require_string(payload, "title")
    summary = _require_string(payload, "summary")
    if not summary.strip():
        raise ContractError("summary must not be empty")
    if response_type == "confirm" and not title.strip():
        raise ContractError("confirm title must not be empty")

    confidence = payload["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= confidence <= 1.0
    ):
        raise ContractError("confidence must be a number from 0.0 to 1.0")
    if not isinstance(payload["data"], dict):
        raise ContractError("data must be an object")
    if not isinstance(payload["actions"], list):
        raise ContractError("actions must be an array")
    return payload


def normalize_response(payload: Any) -> dict[str, Any]:
    """Return the canonical contract, repairing a safe legacy subset.

    Hermes is instructed to emit every contract field.  In practice, an LLM can
    occasionally omit fields whose values are not shown in Telegram (for
    example ``action`` or ``data``).  We accept only the safe JSON subset of
    ``type`` and ``summary`` with no unknown fields, fill the fixed defaults,
    and validate the resulting canonical object.  Free text, malformed JSON,
    unknown fields, and invalid field types remain fail-closed.
    """

    if not isinstance(payload, dict):
        raise ContractError("response must be an object")
    if not set(payload).issubset(REQUIRED_FIELDS):
        raise ContractError("response contains unknown fields")
    if "type" not in payload or "summary" not in payload:
        raise ContractError("response must include type and summary")

    response_type = _require_string(payload, "type")
    summary = _require_string(payload, "summary")
    if response_type not in ALLOWED_TYPES or not summary.strip():
        raise ContractError("response type or summary is invalid")

    version = payload.get("version", "1.0")
    action = payload.get("action", "")
    title = payload.get("title", "需要確認" if response_type == "confirm" else "")
    confidence = payload.get("confidence", 0.0)
    data = payload.get("data", {})
    actions = payload.get("actions", [])

    response = {
        "version": version,
        "type": response_type,
        "action": action,
        "title": title,
        "summary": summary,
        "confidence": confidence,
        "data": data,
        "actions": actions,
    }
    return validate_response(response)


def _decode_response_object(raw_response: str) -> Any:
    """Decode the first JSON object without ever forwarding surrounding text.

    The desired Hermes output is a bare JSON object.  This defensive decoder
    also accepts a JSON object wrapped in a Markdown fence or a short model
    preamble, but only the parsed object reaches the normalizer.  A response
    without a valid JSON object is still rejected.
    """

    content = raw_response.strip()
    if content.startswith("```"):
        first_newline = content.find("\n")
        if first_newline != -1:
            content = content[first_newline + 1 :]
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3].rstrip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(content):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        return payload
    raise ContractError("response does not contain a JSON object")


def response_shape(raw_response: str) -> str:
    """Return non-content diagnostics suitable for a private service log."""

    content = raw_response.strip()
    try:
        payload = _decode_response_object(raw_response)
    except ContractError:
        return (
            "non_json"
            f" chars={len(content)}"
            f" fence={content.startswith('```')}"
            f" brace={'{' in content}"
        )
    if isinstance(payload, dict):
        return f"json keys={','.join(sorted(str(key) for key in payload))}"
    return f"json_{type(payload).__name__}"


def render_telegram_response(raw_response: str) -> str:
    """Convert a contract JSON string to a Telegram message.

    Invalid content deliberately becomes a generic error.  This prevents a
    model's free-form text or malformed JSON from being displayed to Telegram.
    """

    try:
        payload = _decode_response_object(raw_response)
        response = normalize_response(payload)
    except (json.JSONDecodeError, ContractError, TypeError):
        return FORMAT_ERROR_MESSAGE

    response_type = response["type"]
    summary = response["summary"].strip()
    if response_type == "success":
        return f"✅ {summary}"
    if response_type == "question":
        return f"❓ {summary}"
    if response_type == "confirm":
        return f"⚠️ {response['title'].strip()}\n\n{summary}"
    if response_type == "progress":
        return f"⏳ {summary}"
    return f"❌ {summary}"
