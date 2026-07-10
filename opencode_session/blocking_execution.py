from opencode_session.api_transport import OpenCodeApiError
from opencode_session.api_profile import (
    OpenCodeServerProfile,
    server_profile_from_capabilities,
)
from opencode_session.blocking_timeout import (
    BlockingExecutionTimeout,
    execute_with_timeout_abort,
    new_session_message_id,
)
from opencode_session.formatting import compact_value as _compact_value
from opencode_session.schema_helpers import tokens_total
from opencode_session.schema_message_adapter import (
    MESSAGE_REQUIRE_FINAL_ASSISTANT,
    MESSAGE_REQUIRE_ID,
    message_raw_status,
    normalize_message_result,
)
from opencode_session.status import short_status


DEFAULT_BLOCKING_EXECUTION_TIMEOUT_SECONDS = 120


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
    message_id=None,
):
    profile = server_profile_from_capabilities(capabilities)
    strategy = profile.blocking_execution_strategy(capabilities)
    if strategy == "session_message":
        return _execute_session_message_prompt(client, session_id, prompt, capabilities, profile, timeout, deadline, message_id)
    if strategy == "legacy_run_reply":
        return _execute_legacy_run_reply_prompt(client, session_id, prompt, capabilities, profile, timeout, deadline)
    raise OpenCodeApiError(unsupported_blocking_execution_message())


def legacy_run_reply_result(session_id, run_message, reply_message, *, api_path=None, profile=None):
    profile = profile or OpenCodeServerProfile.default()
    route = profile.message_route("legacy_run")
    run_result = _known_message_result(
        run_message,
        route=route,
        source="legacy run response",
        label="user",
        requirement=MESSAGE_REQUIRE_ID,
    )
    _raise_incomplete_message_schema(run_result, source="legacy run response", prompt_id=None)
    reply_result = _known_message_result(
        reply_message,
        route=route,
        source="legacy reply response",
        prompt_id=run_result.record.get("id"),
        label="assistant",
        requirement=MESSAGE_REQUIRE_FINAL_ASSISTANT,
    )
    _raise_incomplete_message_schema(
        reply_result,
        source="legacy reply response",
        prompt_id=run_result.record.get("id"),
    )
    return _legacy_run_reply_result_from_records(
        session_id,
        run_result.record,
        reply_result.record,
        api_path=api_path,
        profile=profile,
    )


def _legacy_run_reply_result_from_records(session_id, run_record, reply_record, *, api_path=None, profile=None):
    profile = profile or OpenCodeServerProfile.default()
    raw_status = message_raw_status(reply_record, default="completed")
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
    return normalize_message_result(message, route=route).provider_failure


def _execute_session_message_prompt(
    client, session_id, prompt, capabilities, profile, timeout, deadline, message_id
):
    message_id = message_id or new_session_message_id()
    kwargs = {"message_id": message_id, "timeout": _request_timeout(client, timeout, deadline)}
    if deadline is not None:
        kwargs["deadline"] = deadline
    response = execute_with_timeout_abort(
        client,
        session_id,
        message_id,
        lambda: client.message_session_response(session_id, prompt, **kwargs),
    )
    route = profile.message_route("blocking_message")
    assistant_result = _known_message_result(
        response.data,
        route=route,
        source="blocking message response",
        prompt_id=message_id,
        label="assistant",
        requirement=MESSAGE_REQUIRE_FINAL_ASSISTANT,
    )
    _raise_provider_failure(assistant_result, prompt_id=message_id)
    _raise_incomplete_message_schema(assistant_result, source="blocking message response", prompt_id=message_id)
    return _session_message_result(session_id, message_id, assistant_result.record, capabilities, profile)


def _execute_legacy_run_reply_prompt(client, session_id, prompt, capabilities, profile, timeout, deadline):
    run_kwargs = {"timeout": _request_timeout(client, timeout, deadline)}
    if deadline is not None:
        run_kwargs["deadline"] = deadline
    run_response = execute_with_timeout_abort(
        client,
        session_id,
        None,
        lambda: client.run_session_response(session_id, prompt, **run_kwargs),
    )
    route = profile.message_route("legacy_run")
    run_result = _known_message_result(
        run_response.data,
        route=route,
        source="legacy run response",
        label="user",
        requirement=MESSAGE_REQUIRE_ID,
    )
    _raise_provider_failure(run_result, prompt_id=run_result.record.get("id"))
    _raise_incomplete_message_schema(run_result, source="legacy run response", prompt_id=None)
    reply_kwargs = {"timeout": _request_timeout(client, timeout, deadline)}
    if deadline is not None:
        reply_kwargs["deadline"] = deadline
    reply_response = execute_with_timeout_abort(
        client,
        session_id,
        run_result.record.get("id"),
        lambda: client.reply_session_response(session_id, **reply_kwargs),
    )
    reply_result = _known_message_result(
        reply_response.data,
        route=route,
        source="legacy reply response",
        prompt_id=run_result.record.get("id"),
        label="assistant",
        requirement=MESSAGE_REQUIRE_FINAL_ASSISTANT,
    )
    _raise_provider_failure(reply_result, prompt_id=run_result.record.get("id"))
    _raise_incomplete_message_schema(
        reply_result,
        source="legacy reply response",
        prompt_id=run_result.record.get("id"),
    )
    return _legacy_run_reply_result_from_records(
        session_id,
        run_result.record,
        reply_result.record,
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
    assistant_record = assistant_message
    raw_status = message_raw_status(assistant_record, default="completed")
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


def _known_message_result(
    message,
    *,
    route,
    source,
    prompt_id=None,
    requirement=None,
    label="message",
):
    result = normalize_message_result(message, route=route, requirement=requirement, label=label)
    if not result.known:
        failure = f"unrecognized message schema from {source}: {_compact_value(result.record.get('raw'))}"
        raise BlockingProviderFailure(
            failure,
            prompt_id=prompt_id,
        )
    return result


def _raise_provider_failure(result, *, prompt_id):
    if result.provider_failure:
        raise BlockingProviderFailure(result.provider_failure, prompt_id=prompt_id)


def _raise_incomplete_message_schema(result, *, source, prompt_id):
    if not result.incomplete_reason:
        return
    raise BlockingProviderFailure(
        f"incomplete message schema from {source}: {result.incomplete_reason}",
        prompt_id=prompt_id,
    )


def _legacy_api_path(capabilities):
    return server_profile_from_capabilities(capabilities).legacy_api_path()
