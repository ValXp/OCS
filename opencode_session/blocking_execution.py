import json
import uuid

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.api_profile import (
    OpenCodeServerProfile,
    server_profile_from_capabilities,
)
from opencode_session.formatting import compact_value as _compact_value
from opencode_session.schema_common import tokens_total
from opencode_session.schema_message_adapter import normalize_message_record
from opencode_session.status import short_status


DEFAULT_BLOCKING_EXECUTION_TIMEOUT_SECONDS = 120
TERMINAL_BLOCKING_STATUSES = {"done", "failed", "aborted", "timeout"}


class BlockingProviderFailure(Exception):
    def __init__(self, message, *, prompt_id=None):
        super().__init__(message)
        self.prompt_id = prompt_id


def blocking_execution_strategy(capabilities):
    return server_profile_from_capabilities(capabilities).blocking_execution_strategy(capabilities)


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
    profile = server_profile_from_capabilities(capabilities)
    strategy = profile.blocking_execution_strategy(capabilities)
    if strategy == "session_message":
        return _execute_session_message_prompt(client, session_id, prompt, capabilities, profile, timeout, deadline)
    if strategy == "legacy_run_reply":
        return _execute_legacy_run_reply_prompt(client, session_id, prompt, capabilities, profile, timeout, deadline)
    raise OpenCodeApiError(unsupported_blocking_execution_message())


def legacy_run_reply_result(session_id, run_message, reply_message, *, api_path=None, profile=None):
    profile = profile or OpenCodeServerProfile.default()
    route = profile.message_route("legacy_run")
    run_record = _normalize_known_message_record(
        run_message,
        route=route,
        source="legacy run response",
    )
    _require_message_id(
        run_record,
        source="legacy run response",
        prompt_id=None,
        label="user",
    )
    reply_record = _normalize_known_message_record(
        reply_message,
        route=route,
        source="legacy reply response",
        prompt_id=run_record.get("id"),
    )
    _require_final_assistant_success_invariants(
        reply_record,
        source="legacy reply response",
        prompt_id=run_record.get("id"),
    )
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
        "api_path": api_path or profile.legacy_api_path(),
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


def _execute_session_message_prompt(client, session_id, prompt, capabilities, profile, timeout, deadline):
    message_id = f"msg_{uuid.uuid4().hex}"
    kwargs = {"message_id": message_id, "timeout": _request_timeout(client, timeout, deadline)}
    if deadline is not None:
        kwargs["deadline"] = deadline
    response = client.message_session_response(session_id, prompt, **kwargs)
    route = profile.message_route("blocking_message")
    assistant_record = _normalize_known_message_record(
        response.data,
        route=route,
        source="blocking message response",
        prompt_id=message_id,
    )
    error = provider_failure(assistant_record, route=route)
    if error:
        raise BlockingProviderFailure(error, prompt_id=message_id)
    return _session_message_result(session_id, message_id, assistant_record, capabilities, profile)


def _execute_legacy_run_reply_prompt(client, session_id, prompt, capabilities, profile, timeout, deadline):
    run_kwargs = {"timeout": _request_timeout(client, timeout, deadline)}
    if deadline is not None:
        run_kwargs["deadline"] = deadline
    run_response = client.run_session_response(session_id, prompt, **run_kwargs)
    route = profile.message_route("legacy_run")
    run_record = _normalize_known_message_record(
        run_response.data,
        route=route,
        source="legacy run response",
    )
    error = provider_failure(run_record, route=route)
    if error:
        raise BlockingProviderFailure(
            error,
            prompt_id=run_record.get("id"),
        )
    _require_message_id(
        run_record,
        source="legacy run response",
        prompt_id=None,
        label="user",
    )
    reply_kwargs = {"timeout": _request_timeout(client, timeout, deadline)}
    if deadline is not None:
        reply_kwargs["deadline"] = deadline
    reply_response = client.reply_session_response(session_id, **reply_kwargs)
    reply_record = _normalize_known_message_record(
        reply_response.data,
        route=route,
        source="legacy reply response",
        prompt_id=run_record.get("id"),
    )
    error = provider_failure(reply_record, route=route)
    if error:
        raise BlockingProviderFailure(
            error,
            prompt_id=run_record.get("id"),
        )
    return legacy_run_reply_result(
        session_id,
        run_record,
        reply_record,
        api_path=_legacy_api_path(capabilities),
        profile=profile,
    )


def _request_timeout(client, timeout, deadline=None):
    if deadline is not None:
        return deadline.require_time()
    default_timeout = getattr(client, "timeout", None)
    if timeout is None:
        return default_timeout
    return timeout


def _session_message_result(session_id, prompt_message_id, assistant_message, capabilities, profile):
    assistant_record = _normalize_known_message_record(
        assistant_message,
        route=profile.message_route("blocking_message"),
        source="blocking message response",
        prompt_id=prompt_message_id,
    )
    _require_final_assistant_success_invariants(
        assistant_record,
        source="blocking message response",
        prompt_id=prompt_message_id,
    )
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
        "api_path": profile.blocking_api_path(),
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


def _normalize_known_message_record(message, *, route, source, prompt_id=None):
    record = normalize_message_record(message, route=route)
    if record.get("schema_status") == "unknown":
        failure = f"unrecognized message schema from {source}: {_compact_value(record.get('raw'))}"
        raise BlockingProviderFailure(
            failure,
            prompt_id=prompt_id,
        )
    return record


def _require_message_id(record, *, source, prompt_id, label):
    if record.get("id"):
        return
    _raise_incomplete_message_schema(
        source,
        f"missing {label} message id",
        prompt_id=prompt_id,
    )


def _require_final_assistant_success_invariants(record, *, source, prompt_id):
    _require_message_id(record, source=source, prompt_id=prompt_id, label="assistant")
    if _has_message_text(record) or _has_explicit_terminal_status(record):
        return
    _raise_incomplete_message_schema(
        source,
        "missing assistant text or explicit terminal status",
        prompt_id=prompt_id,
    )


def _has_message_text(record):
    return record.get("text") not in (None, "")


def _has_explicit_terminal_status(record):
    raw_status = _message_raw_status(record)
    return raw_status is not None and short_status(raw_status) in TERMINAL_BLOCKING_STATUSES


def _raise_incomplete_message_schema(source, reason, *, prompt_id):
    raise BlockingProviderFailure(
        f"incomplete message schema from {source}: {reason}",
        prompt_id=prompt_id,
    )


def _legacy_api_path(capabilities):
    return server_profile_from_capabilities(capabilities).legacy_api_path()
