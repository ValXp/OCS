import uuid
from pathlib import Path

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocker_inventory import blocker_counts_for_session, load_blocker_counts
from opencode_session.blocking_execution import (
    BlockingProviderFailure,
    execute_blocking_prompt,
    skipped_blocking_execution_result,
)
from opencode_session.capabilities import unsupported_reasons
from opencode_session.cli_policy import EX_UNAVAILABLE, EX_UNSUPPORTED
from opencode_session.event_watcher import (
    BackgroundSessionEventWatcher,
    EventWatchEmpty,
    EventWatchOpenTimeout,
    EventWatchTimeout,
)
from opencode_session.formatting import compact_bool, compact_list, compact_value
from opencode_session.prompt_admission import admit_prompt
from opencode_session.records import session_value
from opencode_session.validation_cleanup import format_cleanup_summary
from opencode_session.validation_harness import DisposableValidationHarness


SMOKE_SESSION_PREFIX = "ocs-smoke-"
SMOKE_EVENT_TIMEOUT_SECONDS = 10.0


class SmokeFailure(Exception):
    def __init__(self, message, *, exit_code=EX_UNAVAILABLE):
        super().__init__(message)
        self.exit_code = exit_code


def run_smoke(args, client, *, print_error, unavailable_exit=EX_UNAVAILABLE, unsupported_exit=EX_UNSUPPORTED):
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
        require_smoke_capabilities(capabilities, unsupported_exit=unsupported_exit)

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
        session_id = session_value(create_response.data, "id", "sessionID", "sessionId")
        if not session_id:
            raise SmokeFailure("session creation response did not include a session id")
        harness.track_session(session_id)
        result["session_id"] = session_id
        result["checks"]["create"] = {"status": "done", "session_id": session_id, "title": smoke_id}

        event_collector = SmokeEventCollector(
            client,
            session_id,
            capabilities["route_availability"]["events"]["path"],
            args.event_limit,
        )
        event_collector.start()
        event_collector.wait_open(args.event_timeout)

        steer_message_id = f"msg_{smoke_id}-steer"
        steer_result = admit_prompt(
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
            result["checks"]["run_blocking"] = skipped_blocking_execution_result(session_id, capabilities)
        else:
            try:
                result["checks"]["run_blocking"] = execute_blocking_prompt(client, session_id, "ocs smoke", capabilities)
            except BlockingProviderFailure as error:
                raise SmokeFailure(f"provider failure: {error}") from error

        result["checks"]["blockers"] = smoke_blocker_summary(client, session_id)

    return DisposableValidationHarness(
        client,
        result,
        default_exit_code=unavailable_exit,
        cleanup_failure_message="disposable session cleanup failed",
    ).run(
        validate,
        failure_types=(SmokeFailure,),
        json_output=args.json,
        compact_formatter=format_smoke_compact,
        failure_prefix="smoke failed",
        print_error=print_error,
        cleanup_summary_formatter=format_cleanup_summary,
    )


def require_smoke_capabilities(capabilities, *, unsupported_exit=EX_UNSUPPORTED):
    reasons = unsupported_reasons(capabilities)
    if reasons:
        raise SmokeFailure(f"unsupported OpenCode server; {'; '.join(reasons)}", exit_code=unsupported_exit)
    if not capabilities["v2_prompt_support"]:
        raise SmokeFailure("unsupported OpenCode server; missing v2 steer admission", exit_code=unsupported_exit)
    if not capabilities["event_support"]:
        raise SmokeFailure(
            "unsupported OpenCode server; missing event stream: GET /api/event or GET /event or GET /global/event",
            exit_code=unsupported_exit,
        )


class SmokeEventCollector:
    def __init__(self, client, session_id, event_path, event_limit):
        self.event_limit = event_limit
        self.watcher = BackgroundSessionEventWatcher(client, event_path, session_id)

    def start(self):
        self.watcher.start()

    def wait_open(self, timeout):
        try:
            self.watcher.wait_open(timeout)
        except EventWatchOpenTimeout as error:
            raise SmokeFailure(f"event stream did not open within {format_timeout(error.timeout)}s") from error

    def collect(self, timeout):
        try:
            events = self.watcher.collect(timeout=timeout, limit=self.event_limit)
        except EventWatchTimeout as error:
            raise SmokeFailure(f"event stream timed out after {format_timeout(error.timeout)}s") from error
        except EventWatchEmpty as error:
            raise SmokeFailure("event stream produced no events for disposable session") from error
        event_types = []
        for event in events:
            event_type = event.get("type") or event.get("kind")
            if event_type and event_type not in event_types:
                event_types.append(event_type)
        return event_types


def smoke_blocker_summary(client, session_id):
    try:
        blocker_counts = load_blocker_counts(client)
    except OpenCodeApiError as error:
        return {"status": "skipped", "error": str(error), "permissions": None, "questions": None, "total": None}
    return {"status": "done", **blocker_counts_for_session(blocker_counts, session_id)}


def format_smoke_compact(result):
    run = result["checks"].get("run_blocking") or {}
    blockers = result["checks"].get("blockers") or {}
    fields = [
        ("status", result["status"]),
        ("health", result["health"]),
        ("version", result["version"]),
        ("session", result["session_id"]),
        ("steer", (result["checks"].get("steer") or {}).get("status")),
        ("run", run.get("status")),
        ("events", compact_list(result.get("event_types"))),
        ("blockers", blockers.get("total")),
        ("cleanup", result["cleanup"].get("status")),
        ("no_live_model", compact_bool(result["no_live_model"])),
    ]
    return "smoke " + " ".join(f"{key}={compact_value(value)}" for key, value in fields)


def format_timeout(timeout):
    return str(timeout)
