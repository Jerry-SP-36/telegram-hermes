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


def render_telegram_response(raw_response: str) -> str:
    """Convert a contract JSON string to a Telegram message.

    Invalid content deliberately becomes a generic error.  This prevents a
    model's free-form text or malformed JSON from being displayed to Telegram.
    """

    try:
        payload = json.loads(raw_response)
        response = validate_response(payload)
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
