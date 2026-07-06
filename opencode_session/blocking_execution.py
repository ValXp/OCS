import json
import uuid

from opencode_session.api_client import OpenCodeApiError
from opencode_session.capabilities import (
    LEGACY_REPLY_PATH,
    LEGACY_RUN_PATH,
    SESSION_MESSAGE_PATH,
)
from opencode_session.formatting import compact_value as _compact_value
from opencode_session.schema_common import tokens_total
from opencode_session.schema_message_adapter import (
    LEGACY_MESSAGE_ROUTE,
    SESSION_MESSAGE_ROUTE,
    normalize_message_record,
)
from opencode_session.status import short_status


DEFAULT_BLOCKING_EXECUTION_TIMEOUT_SECONDS = 120


class BlockingProviderFailure(Exception):
    def __init__(self, message, *, prompt_id=None):
        super().__init__(message)
        self.prompt_id = prompt_id


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
    deadline=None,
):
    strategy = blocking_execution_strategy(capabilities)
    if strategy == "session_message":
        return _execute_session_message_prompt(client, session_id, prompt, capabilities, timeout, deadline)
    if strategy == "legacy_run_reply":
        return _execute_legacy_run_reply_prompt(client, session_id, prompt, capabilities, timeout, deadline)
    raise OpenCodeApiError(unsupported_blocking_execution_message())


def legacy_run_reply_result(session_id, run_message, reply_message, *, api_path=None):
    run_record = normalize_message_record(run_message, route=LEGACY_MESSAGE_ROUTE)
    reply_record = normalize_message_record(reply_message, route=LEGACY_MESSAGE_ROUTE)
    raw_status = _message_raw_status(reply_record, default="completed")
    status = short_status(raw_status)
    return {
        "session_id": session_id,
        "message_ids": {
            "user": run_record.get("id"),
            "assistant": reply_record.get("id"),
        },
        "status": status,
        "raw_status": raw_status,
        "terminal_state": status,
        "api_path": api_path or {"run": LEGACY_RUN_PATH, "reply": LEGACY_REPLY_PATH},
        "execution_strategy": "legacy_run_reply",
        "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
        "cost": reply_record.get("cost"),
        "tokens": reply_record.get("tokens"),
        "text": reply_record.get("text"),
    }


def skipped_blocking_execution_result(session_id, capabilities, *, reason="no-live-model"):
    return {
        "session_id": session_id,
        "status": "skipped",
        "reason": reason,
        "raw_status": "skipped",
        "terminal_state": "skipped",
        "api_path": _legacy_api_path(capabilities),
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


def provider_failure(message, *, route=None):
    record = normalize_message_record(message, route=route)
    status = str(_message_raw_status(record, default="") or "").lower()
    error = record.get("error")
    if status not in {"failed", "error", "errored"}:
        if not status and error:
            if isinstance(error, dict):
                return error.get("message") or json.dumps(error, sort_keys=True)
            return str(error)
        return None
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error, sort_keys=True)
    return error or status


def _execute_session_message_prompt(client, session_id, prompt, capabilities, timeout, deadline):
    message_id = f"msg_{uuid.uuid4().hex}"
    kwargs = {"message_id": message_id, "timeout": _request_timeout(client, timeout, deadline)}
    if deadline is not None:
        kwargs["deadline"] = deadline
    response = client.message_session_response(session_id, prompt, **kwargs)
    error = provider_failure(response.data, route=SESSION_MESSAGE_ROUTE)
    if error:
        raise BlockingProviderFailure(error, prompt_id=message_id)
    return _session_message_result(session_id, message_id, response.data, capabilities)


def _execute_legacy_run_reply_prompt(client, session_id, prompt, capabilities, timeout, deadline):
    run_kwargs = {"timeout": _request_timeout(client, timeout, deadline)}
    if deadline is not None:
        run_kwargs["deadline"] = deadline
    run_response = client.run_session_response(session_id, prompt, **run_kwargs)
    error = provider_failure(run_response.data, route=LEGACY_MESSAGE_ROUTE)
    if error:
        raise BlockingProviderFailure(
            error,
            prompt_id=normalize_message_record(run_response.data, route=LEGACY_MESSAGE_ROUTE).get("id"),
        )
    reply_kwargs = {"timeout": _request_timeout(client, timeout, deadline)}
    if deadline is not None:
        reply_kwargs["deadline"] = deadline
    reply_response = client.reply_session_response(session_id, **reply_kwargs)
    error = provider_failure(reply_response.data, route=LEGACY_MESSAGE_ROUTE)
    if error:
        raise BlockingProviderFailure(
            error,
            prompt_id=normalize_message_record(run_response.data, route=LEGACY_MESSAGE_ROUTE).get("id"),
        )
    return legacy_run_reply_result(
        session_id,
        run_response.data,
        reply_response.data,
        api_path=_legacy_api_path(capabilities),
    )


def _request_timeout(client, timeout, deadline=None):
    if deadline is not None:
        return deadline.require_time()
    default_timeout = getattr(client, "timeout", None)
    if timeout is None:
        return default_timeout
    return timeout


def _session_message_result(session_id, prompt_message_id, assistant_message, capabilities):
    assistant_record = normalize_message_record(assistant_message, route=SESSION_MESSAGE_ROUTE)
    raw_status = _message_raw_status(assistant_record, default="completed")
    status = short_status(raw_status)
    return {
        "session_id": session_id,
        "message_ids": {
            "user": prompt_message_id,
            "assistant": assistant_record.get("id"),
        },
        "status": status,
        "raw_status": raw_status,
        "terminal_state": status,
        "api_path": {"message": _route_plan_path(capabilities, "blocking_message", SESSION_MESSAGE_PATH)},
        "execution_strategy": "session_message",
        "fallback": {
            "available": capabilities.get("legacy_fallback_available", False),
            "strategy": "legacy_run_reply",
            "used": False,
        },
        "cost": assistant_record.get("cost"),
        "tokens": assistant_record.get("tokens"),
        "text": assistant_record.get("text"),
    }


def _message_raw_status(message, *, default=None):
    return message.get("raw_status") or message.get("status") or default


def _legacy_api_path(capabilities):
    return {
        "run": _route_plan_path(capabilities, "legacy_run", LEGACY_RUN_PATH),
        "reply": _route_plan_path(capabilities, "legacy_reply", LEGACY_REPLY_PATH),
    }


def _route_plan_path(capabilities, name, fallback):
    route_plan = capabilities.get("route_plan") if isinstance(capabilities, dict) else None
    if isinstance(route_plan, dict) and route_plan.get(name):
        return route_plan[name]
    return fallback
