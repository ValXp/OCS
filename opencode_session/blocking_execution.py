import json
import uuid

from opencode_session.api_client import OpenCodeApiError
from opencode_session.capabilities import (
    LEGACY_REPLY_PATH,
    LEGACY_RUN_PATH,
    SESSION_MESSAGE_PATH,
    blocking_message_supported,
    legacy_run_reply_supported,
)
from opencode_session.formatting import compact_value as _compact_value
from opencode_session.status import short_status


DEFAULT_BLOCKING_EXECUTION_TIMEOUT_SECONDS = 120


class BlockingProviderFailure(Exception):
    def __init__(self, message, *, prompt_id=None):
        super().__init__(message)
        self.prompt_id = prompt_id


def blocking_execution_capabilities(doc):
    blocking_message_available = blocking_message_supported(doc)
    legacy_fallback_available = legacy_run_reply_supported(doc)
    return {
        "route_availability": {
            "blocking_message": _execution_route(SESSION_MESSAGE_PATH, "POST", blocking_message_available),
            "legacy_run": _execution_route(LEGACY_RUN_PATH, "POST", legacy_fallback_available),
            "legacy_reply": _execution_route(LEGACY_REPLY_PATH, "POST", legacy_fallback_available),
        },
        "blocking_message_available": blocking_message_available,
        "blocking_execution_available": blocking_message_available or legacy_fallback_available,
        "legacy_fallback_available": legacy_fallback_available,
    }


def blocking_execution_strategy(capabilities):
    routes = capabilities.get("route_availability") or {}
    blocking_message = routes.get("blocking_message") or {}
    if capabilities.get("blocking_message_available") or blocking_message.get("available"):
        return "session_message"
    if capabilities.get("legacy_fallback_available"):
        return "legacy_run_reply"
    return None


def unsupported_blocking_execution_message():
    return (
        "unsupported route behavior: missing blocking execution: POST /session/{sessionID}/message or legacy "
        "POST /session/{sessionID}/run + POST /session/{sessionID}/reply; v2 prompt admission is not execution"
    )


def execute_blocking_prompt(
    client,
    session_id,
    prompt,
    capabilities,
    *,
    timeout=DEFAULT_BLOCKING_EXECUTION_TIMEOUT_SECONDS,
):
    strategy = blocking_execution_strategy(capabilities)
    if strategy == "session_message":
        return _execute_session_message_prompt(client, session_id, prompt, capabilities, timeout)
    if strategy == "legacy_run_reply":
        return _execute_legacy_run_reply_prompt(client, session_id, prompt, timeout)
    raise OpenCodeApiError(unsupported_blocking_execution_message())


def legacy_run_reply_result(session_id, run_message, reply_message):
    raw_status = message_value(reply_message, "status") or "completed"
    status = short_status(raw_status)
    return {
        "session_id": session_id,
        "message_ids": {
            "user": message_value(run_message, "id", "messageID", "messageId"),
            "assistant": message_value(reply_message, "id", "messageID", "messageId"),
        },
        "status": status,
        "raw_status": raw_status,
        "terminal_state": status,
        "api_path": {"run": LEGACY_RUN_PATH, "reply": LEGACY_REPLY_PATH},
        "execution_strategy": "legacy_run_reply",
        "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
        "cost": message_value(reply_message, "cost"),
        "tokens": message_tokens(reply_message),
        "text": message_text(reply_message),
    }


def skipped_blocking_execution_result(session_id, capabilities, *, reason="no-live-model"):
    routes = capabilities["route_availability"]
    return {
        "session_id": session_id,
        "status": "skipped",
        "reason": reason,
        "raw_status": "skipped",
        "terminal_state": "skipped",
        "api_path": {"run": routes["legacy_run"]["path"], "reply": routes["legacy_reply"]["path"]},
        "fallback": {
            "available": capabilities["legacy_fallback_available"],
            "strategy": "legacy_run_reply",
            "used": False,
        },
    }


def format_blocking_execution_compact(result):
    fields = [
        ("session", result["session_id"]),
        ("status", result["status"]),
        ("user", result["message_ids"]["user"]),
        ("assistant", result["message_ids"]["assistant"]),
        ("cost", result["cost"]),
        ("tokens", tokens_total(result["tokens"])),
        ("text", result["text"]),
    ]
    return "run_blocking " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def message_value(message, *names):
    message = message if isinstance(message, dict) else {}
    for name in names:
        value = message.get(name)
        if value is not None:
            return value
    info = message.get("info")
    if isinstance(info, dict):
        for name in names:
            value = info.get(name)
            if value is not None:
                return value
    return None


def message_tokens(message):
    return message_value(message, "tokens", "usage")


def tokens_total(tokens):
    if isinstance(tokens, dict):
        if tokens.get("total") is not None:
            return tokens["total"]
        return sum(value for value in tokens.values() if isinstance(value, int))
    return tokens


def message_text(message):
    message = message if isinstance(message, dict) else {}
    text = message_value(message, "text", "content")
    if text is not None:
        return text
    parts = message.get("parts")
    if isinstance(parts, list):
        return "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def provider_failure(message):
    status = str(message_value(message, "status") or "").lower()
    error = message_value(message, "error", "reason", "message")
    if status not in {"failed", "error", "errored"}:
        if not status and error:
            if isinstance(error, dict):
                return error.get("message") or json.dumps(error, sort_keys=True)
            return str(error)
        return None
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error, sort_keys=True)
    return error or status


def _execute_session_message_prompt(client, session_id, prompt, capabilities, timeout):
    message_id = f"msg_{uuid.uuid4().hex}"
    response = client.message_session_response(
        session_id,
        prompt,
        message_id=message_id,
        timeout=_request_timeout(client, timeout),
    )
    error = provider_failure(response.data)
    if error:
        raise BlockingProviderFailure(error, prompt_id=message_id)
    return _session_message_result(session_id, message_id, response.data, capabilities)


def _execute_legacy_run_reply_prompt(client, session_id, prompt, timeout):
    request_timeout = _request_timeout(client, timeout)
    run_response = client.run_session_response(session_id, prompt, timeout=request_timeout)
    error = provider_failure(run_response.data)
    if error:
        raise BlockingProviderFailure(
            error,
            prompt_id=message_value(run_response.data, "id", "messageID", "messageId"),
        )
    reply_response = client.reply_session_response(session_id, timeout=request_timeout)
    error = provider_failure(reply_response.data)
    if error:
        raise BlockingProviderFailure(
            error,
            prompt_id=message_value(run_response.data, "id", "messageID", "messageId"),
        )
    return legacy_run_reply_result(session_id, run_response.data, reply_response.data)


def _request_timeout(client, timeout):
    default_timeout = getattr(client, "timeout", None)
    if timeout is None:
        return default_timeout
    if default_timeout is None:
        return timeout
    return max(default_timeout, timeout)


def _session_message_result(session_id, prompt_message_id, assistant_message, capabilities):
    raw_status = message_value(assistant_message, "status") or "completed"
    status = short_status(raw_status)
    return {
        "session_id": session_id,
        "message_ids": {
            "user": prompt_message_id,
            "assistant": message_value(assistant_message, "id", "messageID", "messageId"),
        },
        "status": status,
        "raw_status": raw_status,
        "terminal_state": status,
        "api_path": {"message": SESSION_MESSAGE_PATH},
        "execution_strategy": "session_message",
        "fallback": {
            "available": capabilities.get("legacy_fallback_available", False),
            "strategy": "legacy_run_reply",
            "used": False,
        },
        "cost": message_value(assistant_message, "cost"),
        "tokens": message_tokens(assistant_message),
        "text": message_text(assistant_message),
    }


def _execution_route(path, method, available):
    return {"path": path, "method": method, "available": available}
