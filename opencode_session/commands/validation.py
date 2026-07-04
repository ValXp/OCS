import argparse
import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocker_inventory import blocker_counts_for_session, load_blocker_counts
from opencode_session.blocking_execution import (
    BlockingProviderFailure as _BlockingProviderFailure,
    execute_blocking_prompt as _execute_blocking_prompt,
    message_text as _message_text,
    message_tokens as _message_tokens,
    message_value as _message_value,
    skipped_blocking_execution_result as _no_live_run_reply_result,
)
from opencode_session.capabilities import unsupported_reasons
from opencode_session.events import is_terminal_event, normalize_event
from opencode_session.formatting import (
    compact_bool as _compact_bool,
    compact_list as _compact_list,
    compact_value as _compact_value,
)
from opencode_session.prompt_admission import admit_prompt as admit_prompt_service
from opencode_session.records import collection_sessions as _collection_sessions
from opencode_session.records import first_present as _first_present
from opencode_session.records import session_value as _session_value
from opencode_session.status import short_status
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired as _WatchTimeout
from opencode_session.validation_harness import (
    DisposableValidationHarness,
    delete_and_verify_session as _delete_and_verify_session,
)


SMOKE_SESSION_PREFIX = "ocs-smoke-"
SMOKE_EVENT_TIMEOUT_SECONDS = 10.0
LIVE_VALIDATE_ENV = "OCS_LIVE_VALIDATE"
LIVE_SESSION_PREFIX = "ocs-live-"
LIVE_VALIDATE_PROMPT = "Reply exactly PONG."
LIVE_EVENT_OBSERVATION_TIMEOUT = 1.0
EX_UNAVAILABLE = 69
EX_UNSUPPORTED = 70
EX_DATAERR = 65


def add_validation_parsers(subparsers, *, add_server_argument, handler):
    smoke_parser = subparsers.add_parser("smoke", help="run a deterministic no-live OpenCode smoke test")
    smoke_parser.add_argument("--directory", default=".", help="target directory for disposable smoke sessions")
    smoke_parser.add_argument("--prefix", default=SMOKE_SESSION_PREFIX, help="recognizable disposable session prefix")
    smoke_parser.add_argument(
        "--no-live-model",
        action="store_true",
        default=True,
        help="keep smoke in no-live-model mode; live-provider validation is separate",
    )
    smoke_parser.add_argument(
        "--event-timeout",
        type=_positive_float,
        default=SMOKE_EVENT_TIMEOUT_SECONDS,
        help="event watch timeout in seconds",
    )
    smoke_parser.add_argument("--event-limit", type=_positive_int, default=3, help="maximum matching events to observe")
    add_server_argument(smoke_parser)
    smoke_parser.add_argument("--json", action="store_true", help="print smoke result JSON")
    smoke_parser.set_defaults(command_handler=handler)

    live_parser = subparsers.add_parser(
        "live_validate",
        help=f"run opt-in live-provider validation; requires {LIVE_VALIDATE_ENV}=1",
        description=(
            "Run an explicit live-provider validation using the minimal prompt: Reply exactly PONG.\n"
            f"Requires {LIVE_VALIDATE_ENV}=1; expected token use is two minimal PONG prompts at most.\n"
            "Creates disposable sessions and verifies they are deleted before the command exits."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    live_parser.add_argument("--directory", default=".", help="target directory for disposable live validation sessions")
    live_parser.add_argument("--prefix", default=LIVE_SESSION_PREFIX, help="recognizable disposable live session prefix")
    live_parser.add_argument("--agent", help="agent name for disposable live validation sessions")
    live_parser.add_argument("--model", help="model name for disposable live validation sessions")
    add_server_argument(live_parser)
    live_parser.add_argument("--json", action="store_true", help="print live validation result JSON")
    live_parser.set_defaults(command_handler=handler)

    cleanup_parser = subparsers.add_parser("cleanup", help="delete stale disposable smoke sessions")
    cleanup_parser.add_argument("--directory", default=".", help="target directory to clean")
    cleanup_parser.add_argument("--prefix", default=SMOKE_SESSION_PREFIX, help="disposable session prefix to match")
    add_server_argument(cleanup_parser)
    cleanup_parser.add_argument("--json", action="store_true", help="print cleanup result JSON")
    cleanup_parser.set_defaults(command_handler=handler)


def handle_validation_command(
    args,
    *,
    print_error,
    unavailable_exit,
    unsupported_exit,
    dataerr_exit,
    client_factory=OpenCodeApiClient,
):
    if args.command not in {"smoke", "live_validate", "cleanup"}:
        return 64
    if args.command == "live_validate":
        env_failure = _live_validate_env_failure(print_error, dataerr_exit)
        if env_failure is not None:
            return env_failure
    try:
        client = client_factory(args.server)
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit
    if args.command == "smoke":
        return _run_smoke(
            args,
            client,
            print_error=print_error,
            unavailable_exit=unavailable_exit,
            unsupported_exit=unsupported_exit,
        )
    if args.command == "live_validate":
        return _run_live_validate(
            args,
            client,
            print_error=print_error,
            unavailable_exit=unavailable_exit,
            unsupported_exit=unsupported_exit,
            dataerr_exit=dataerr_exit,
        )
    return _cleanup_disposable_command(args, client, print_error=print_error, unavailable_exit=unavailable_exit)


class _SmokeFailure(Exception):
    def __init__(self, message, *, exit_code=EX_UNAVAILABLE):
        super().__init__(message)
        self.exit_code = exit_code


def _run_smoke(args, client, *, print_error, unavailable_exit=EX_UNAVAILABLE, unsupported_exit=EX_UNSUPPORTED):
    directory = str(Path(args.directory).resolve())
    smoke_id = f"{args.prefix}{uuid.uuid4().hex[:10]}"
    result = {
        "status": "active",
        "ok": False,
        "health": None,
        "version": None,
        "directory": directory,
        "prefix": args.prefix,
        "session_id": None,
        "mode": "no-live-model",
        "no_live_model": bool(args.no_live_model),
        "checks": {},
        "event_types": [],
        "cleanup": {"status": "queued", "deleted": [], "verified": []},
    }

    def validate(harness):
        capabilities = harness.detect_capabilities()
        _require_smoke_capabilities(capabilities, unsupported_exit=unsupported_exit)

        create_response = client.create_session_response(
            directory,
            title=smoke_id,
            metadata={
                "disposable": True,
                "prefix": args.prefix,
                "smoke_id": smoke_id,
                "no_live_model": bool(args.no_live_model),
            },
        )
        session_id = _session_value(create_response.data, "id", "sessionID", "sessionId")
        if not session_id:
            raise _SmokeFailure("session creation response did not include a session id")
        harness.track_session(session_id)
        result["session_id"] = session_id
        result["checks"]["create"] = {"status": "done", "session_id": session_id, "title": smoke_id}

        event_collector = _SmokeEventCollector(
            client,
            session_id,
            capabilities["route_availability"]["events"]["path"],
            args.event_limit,
        )
        event_collector.start()
        event_collector.wait_open(args.event_timeout)

        steer_message_id = f"msg_{smoke_id}-steer"
        steer_result = admit_prompt_service(
            client,
            capabilities,
            session_id,
            "ocs smoke steer",
            "steer",
            message_id=steer_message_id,
            map_unsupported=False,
        )
        admission = steer_result.record
        result["checks"]["steer"] = admission

        event_types = event_collector.collect(args.event_timeout)
        result["event_types"] = event_types
        result["checks"]["events"] = {"status": "done", "types": event_types}

        if args.no_live_model:
            result["checks"]["run_blocking"] = _no_live_run_reply_result(session_id, capabilities)
        else:
            try:
                result["checks"]["run_blocking"] = _execute_blocking_prompt(client, session_id, "ocs smoke", capabilities)
            except _BlockingProviderFailure as error:
                raise _SmokeFailure(f"provider failure: {error}") from error

        result["checks"]["blockers"] = _smoke_blocker_summary(client, session_id)

    return DisposableValidationHarness(
        client,
        result,
        default_exit_code=unavailable_exit,
        cleanup_failure_message="disposable session cleanup failed",
    ).run(
        validate,
        failure_types=(_SmokeFailure,),
        json_output=args.json,
        compact_formatter=_format_smoke_compact,
        failure_prefix="smoke failed",
        print_error=print_error,
        cleanup_summary_formatter=_format_cleanup_summary,
    )


def _require_smoke_capabilities(capabilities, *, unsupported_exit=EX_UNSUPPORTED):
    reasons = unsupported_reasons(capabilities)
    if reasons:
        raise _SmokeFailure(f"unsupported OpenCode server; {'; '.join(reasons)}", exit_code=unsupported_exit)
    if not capabilities["v2_prompt_support"]:
        raise _SmokeFailure("unsupported OpenCode server; missing v2 steer admission", exit_code=unsupported_exit)
    if not capabilities["event_support"]:
        raise _SmokeFailure(
            "unsupported OpenCode server; missing event stream: GET /api/event or GET /event or GET /global/event",
            exit_code=unsupported_exit,
        )


def _run_live_validate(
    args,
    client,
    *,
    print_error,
    unavailable_exit=EX_UNAVAILABLE,
    unsupported_exit=EX_UNSUPPORTED,
    dataerr_exit=EX_DATAERR,
):
    env_failure = _live_validate_env_failure(print_error, dataerr_exit)
    if env_failure is not None:
        return env_failure

    directory = str(Path(args.directory).resolve())
    validation_id = f"{args.prefix}{uuid.uuid4().hex[:10]}"
    result = {
        "status": "active",
        "ok": False,
        "mode": "live-provider",
        "gate": {"env": LIVE_VALIDATE_ENV, "enabled": True, "required": "1"},
        "prompt": LIVE_VALIDATE_PROMPT,
        "health": None,
        "version": None,
        "directory": directory,
        "prefix": args.prefix,
        "session_ids": {"steer": None, "run_blocking": None},
        "checks": {},
        "cleanup": {"status": "queued", "deleted": [], "verified": []},
    }

    def validate(harness):
        def create_live_session(role):
            create_response = client.create_session_response(
                directory,
                agent=args.agent,
                model=args.model,
                title=f"{validation_id}-{role}",
                metadata={
                    "disposable": True,
                    "kind": "live-provider-validation",
                    "live_provider": True,
                    "prefix": args.prefix,
                    "validation_id": validation_id,
                    "role": role,
                },
            )
            session_id = _session_value(create_response.data, "id", "sessionID", "sessionId")
            if not session_id:
                raise _LiveValidationFailure("session creation response did not include a session id")
            harness.track_session(session_id)
            return session_id

        capabilities = harness.detect_capabilities()
        _require_live_validate_capabilities(capabilities, unsupported_exit=unsupported_exit)
        result["checks"]["wait"] = _live_wait_record(capabilities)

        steer_session_id = create_live_session("steer")
        result["session_ids"]["steer"] = steer_session_id
        steer_message_id = f"msg_{validation_id}-steer"
        steer_result = admit_prompt_service(
            client,
            capabilities,
            steer_session_id,
            LIVE_VALIDATE_PROMPT,
            "steer",
            message_id=steer_message_id,
            map_unsupported=False,
        )
        steer = steer_result.record
        steer.update(_live_steer_execution_observation(client, steer, capabilities))
        result["checks"]["v2_steer"] = steer

        run_session_id = create_live_session("run_blocking")
        result["session_ids"]["run_blocking"] = run_session_id
        try:
            run_blocking = _execute_blocking_prompt(client, run_session_id, LIVE_VALIDATE_PROMPT, capabilities)
        except _BlockingProviderFailure as error:
            raise _LiveValidationFailure(f"provider failure: {error}") from error
        run_blocking["succeeded"] = run_blocking["status"] == "done"
        run_blocking["pong"] = _is_exact_pong(run_blocking["text"])
        if not run_blocking["pong"]:
            raise _LiveValidationFailure("live provider did not reply exactly PONG")
        result["checks"]["run_blocking"] = run_blocking

    return DisposableValidationHarness(
        client,
        result,
        default_exit_code=unavailable_exit,
        cleanup_failure_message="disposable live validation session cleanup failed",
    ).run(
        validate,
        failure_types=(_LiveValidationFailure,),
        json_output=args.json,
        compact_formatter=_format_live_validate_compact,
        failure_prefix="live-provider validation failed",
        print_error=print_error,
        cleanup_summary_formatter=_format_cleanup_summary,
    )


class _LiveValidationFailure(Exception):
    def __init__(self, message, *, exit_code=EX_UNAVAILABLE):
        super().__init__(message)
        self.exit_code = exit_code


def _require_live_validate_capabilities(capabilities, *, unsupported_exit=EX_UNSUPPORTED):
    reasons = unsupported_reasons(capabilities)
    if not capabilities["v2_prompt_support"]:
        reasons.append("missing v2 steer admission: POST /api/session/{sessionID}/prompt")
    if not capabilities.get("blocking_execution_available"):
        reasons.append(
            "missing blocking run execution: POST /session/{sessionID}/message or legacy "
            "POST /session/{sessionID}/run + POST /session/{sessionID}/reply"
        )
    if reasons:
        raise _LiveValidationFailure(
            f"unsupported OpenCode server; {'; '.join(reasons)}",
            exit_code=unsupported_exit,
        )


def _live_wait_record(capabilities):
    wait_route = capabilities["route_availability"]["v2_wait"]
    return {
        "available": wait_route["available"],
        "api_path": wait_route["path"],
        "status": "available" if wait_route["available"] else "unavailable",
    }


def _live_steer_execution_observation(client, steer, capabilities):
    wait_route = capabilities["route_availability"]["v2_wait"]
    wait_observation = None
    if wait_route["available"] and "?" not in wait_route["path"]:
        try:
            response = client.wait_session_response(steer["session_id"], wait_route["path"])
        except OpenCodeApiError as error:
            wait_observation = _execution_observation(
                "unknown",
                source="wait",
                status="unknown",
                reason="observation_failed",
                error=str(error),
            )
        else:
            status = short_status(_first_present(response.data, "status", "state", "phase"))
            if status in {"active", "done"}:
                return _execution_observation(True, source="wait", status=status, reason="observed_execution_state")
            if status == "queued":
                return _execution_observation(False, source="wait", status=status, reason="observed_not_executed_state")
            wait_observation = _execution_observation("unknown", source="wait", status=status, reason="no_execution_evidence")
    message_observation = _live_message_execution_observation(client, steer)
    if message_observation["executed"] != "unknown":
        return message_observation
    event_route = capabilities["route_availability"]["events"]
    if event_route["available"]:
        return _live_event_execution_observation(client, steer, event_route["path"])
    return message_observation if wait_observation is None else wait_observation


def _live_message_execution_observation(client, steer):
    try:
        session = client.get_session_response(steer["session_id"]).data
    except OpenCodeApiError as error:
        return _execution_observation(
            "unknown",
            source="message",
            status="unknown",
            reason="observation_failed",
            error=str(error),
        )
    status = _assistant_message_status(session)
    if status is not None:
        return _execution_observation(True, source="message", status=status, reason="observed_assistant_message")
    return _execution_observation("unknown", source="message", status="unknown", reason="no_execution_evidence")


def _assistant_message_status(session):
    for message in _iter_message_evidence_candidates(session):
        role = str(_first_present(message, "role", "author", "speaker", "type", "kind") or "").lower()
        if "assistant" not in role:
            continue
        status = short_status(_first_present(message, "status", "state", "phase"))
        if _message_text(message) or _message_tokens(message) is not None or _message_value(message, "cost") is not None:
            return status or "unknown"
        if status in {"active", "done"}:
            return status
    return None


def _live_event_execution_observation(client, steer, event_path):
    deadline = TimeoutDeadline(LIVE_EVENT_OBSERVATION_TIMEOUT)
    try:
        for raw_event in client.stream_events(event_path, deadline=deadline):
            event = normalize_event(raw_event, steer["session_id"])
            if event is None:
                continue
            observation = _event_execution_observation(event)
            if observation["executed"] != "unknown":
                return observation
            if is_terminal_event(event):
                break
    except _WatchTimeout:
        return _execution_observation(
            "unknown",
            source="event",
            status="unknown",
            reason="observation_timed_out",
        )
    except OpenCodeApiError as error:
        return _execution_observation(
            "unknown",
            source="event",
            status="unknown",
            reason="observation_failed",
            error=str(error),
        )
    return _execution_observation("unknown", source="event", status="unknown", reason="no_execution_evidence")


def _event_execution_observation(event):
    status = event.get("status") or "unknown"
    if event.get("kind") in {"text", "tool", "step"}:
        return _execution_observation(True, source="event", status=status, reason="observed_execution_event")
    if event.get("kind") == "status" and status in {"active", "done"}:
        return _execution_observation(True, source="event", status=status, reason="observed_execution_event")
    return _execution_observation("unknown", source="event", status=status, reason="no_execution_evidence")


def _iter_message_evidence_candidates(data):
    if not isinstance(data, dict):
        return
    for key in ("message", "assistant", "reply", "output"):
        value = data.get(key)
        if isinstance(value, dict):
            yield value
    for key in ("messages", "items", "entries"):
        value = data.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                yield item


def _execution_observation(executed, *, source, status, reason, error=None):
    evidence = {"source": source, "status": status or "unknown", "reason": reason}
    if error is not None:
        evidence["error"] = error
    return {"executed": executed, "execution_evidence": evidence}


def _is_exact_pong(text):
    return str(text).strip() == "PONG"


def _format_live_validate_compact(result):
    steer = result["checks"].get("v2_steer") or {}
    wait = result["checks"].get("wait") or {}
    run_blocking = result["checks"].get("run_blocking") or {}
    fields = [
        ("status", result["status"]),
        ("mode", result["mode"]),
        ("health", result["health"]),
        ("version", result["version"]),
        ("steer", steer.get("status")),
        ("wait", wait.get("status")),
        ("run", run_blocking.get("status")),
        ("pong", _compact_bool(run_blocking.get("pong"))),
        ("cleanup", result["cleanup"].get("status")),
    ]
    return "live_validate " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


class _SmokeEventCollector:
    def __init__(self, client, session_id, event_path, event_limit):
        self.client = client
        self.session_id = session_id
        self.event_path = event_path
        self.event_limit = event_limit
        self.opened = threading.Event()
        self.items = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def wait_open(self, timeout):
        if self.opened.wait(timeout):
            return
        error = self._first_error()
        if error is not None:
            raise error
        raise _SmokeFailure(f"event stream did not open within {_format_timeout(timeout)}s")

    def collect(self, timeout):
        event_types = []
        deadline = time.monotonic() + timeout
        while len(event_types) < self.event_limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                kind, value = self.items.get(timeout=remaining)
            except queue.Empty:
                break
            if kind == "error":
                raise value
            if kind == "done":
                break
            event = value
            event_type = event.get("type") or event.get("kind")
            if event_type and event_type not in event_types:
                event_types.append(event_type)
            if is_terminal_event(event):
                break
        if event_types:
            return event_types
        if self.thread.is_alive():
            raise _SmokeFailure(f"event stream timed out after {_format_timeout(timeout)}s")
        raise _SmokeFailure("event stream produced no events for disposable session")

    def _run(self):
        try:
            for raw_event in self.client.stream_events(self.event_path, on_open=self.opened.set):
                event = normalize_event(raw_event, self.session_id)
                if event is None:
                    continue
                self.items.put(("event", event))
        except OpenCodeApiError as error:
            self.items.put(("error", error))
        finally:
            self.items.put(("done", None))

    def _first_error(self):
        pending = []
        error = None
        while True:
            try:
                item = self.items.get_nowait()
            except queue.Empty:
                break
            pending.append(item)
            if item[0] == "error":
                error = item[1]
                break
        for item in pending:
            self.items.put(item)
        return error


def _smoke_blocker_summary(client, session_id):
    try:
        blocker_counts = load_blocker_counts(client)
    except OpenCodeApiError as error:
        return {"status": "skipped", "error": str(error), "permissions": None, "questions": None, "total": None}
    return {"status": "done", **blocker_counts_for_session(blocker_counts, session_id)}


def _format_smoke_compact(result):
    run = result["checks"].get("run_blocking") or {}
    blockers = result["checks"].get("blockers") or {}
    fields = [
        ("status", result["status"]),
        ("health", result["health"]),
        ("version", result["version"]),
        ("session", result["session_id"]),
        ("steer", (result["checks"].get("steer") or {}).get("status")),
        ("run", run.get("status")),
        ("events", _compact_list(result.get("event_types"))),
        ("blockers", blockers.get("total")),
        ("cleanup", result["cleanup"].get("status")),
        ("no_live_model", _compact_bool(result["no_live_model"])),
    ]
    return "smoke " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_cleanup_summary(cleanup):
    return " ".join(
        [
            f"cleanup={cleanup.get('status')}",
            f"deleted={len(cleanup.get('deleted') or [])}",
            f"verified={len(cleanup.get('verified') or [])}",
        ]
    )


def _cleanup_disposable_command(args, client, *, print_error, unavailable_exit=EX_UNAVAILABLE):
    directory = str(Path(args.directory).resolve()) if args.directory else None
    try:
        response = client.list_sessions_response()
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit

    sessions = [
        session
        for session in _collection_sessions(response.data)
        if _is_disposable_session(session, prefix=args.prefix, directory=directory)
    ]
    result = {
        "status": "done",
        "prefix": args.prefix,
        "directory": directory,
        "stale": len(sessions),
        "sessions": [_session_value(session, "id", "sessionID", "sessionId") for session in sessions],
        "deleted": [],
        "verified": [],
        "errors": [],
    }
    for session in sessions:
        session_id = _session_value(session, "id", "sessionID", "sessionId")
        if not session_id:
            result["status"] = "failed"
            result["errors"].append({"session_id": None, "error": "session has no id"})
            continue
        error = _delete_and_verify_session(client, session_id)
        if error is not None:
            result["status"] = "failed"
            result["errors"].append({"session_id": session_id, "error": str(error)})
            continue
        result["deleted"].append(session_id)
        result["verified"].append(session_id)

    if result["status"] != "done":
        print_error(f"cleanup failed: {_format_cleanup_command_compact(result)}")
        return unavailable_exit
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(_format_cleanup_command_compact(result))
    return 0


def _is_disposable_session(session, *, prefix, directory):
    if directory is not None and _session_value(session, "directory", "cwd") != directory:
        return False
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    values = [
        _session_value(session, "id", "sessionID", "sessionId"),
        _session_value(session, "title", "name"),
        metadata.get("smoke_id"),
        metadata.get("prefix"),
        metadata.get("disposable_prefix"),
    ]
    return any(str(value).startswith(prefix) for value in values if value is not None)


def _format_cleanup_command_compact(result):
    fields = [
        ("stale", result["stale"]),
        ("deleted", len(result["deleted"])),
        ("verified", len(result["verified"])),
        ("prefix", result["prefix"]),
        ("dir", result["directory"]),
    ]
    return "cleanup " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _live_validate_env_failure(print_error, dataerr_exit):
    if os.environ.get(LIVE_VALIDATE_ENV) == "1":
        return None
    print_error(f"live-provider validation disabled; set {LIVE_VALIDATE_ENV}=1 to allow token-consuming provider calls")
    return dataerr_exit


def _positive_float(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _positive_int(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _format_timeout(timeout):
    return str(timeout)
