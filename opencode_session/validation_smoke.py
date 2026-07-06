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
from opencode_session.schema_session_adapter import session_value
from opencode_session.validation_cleanup import format_cleanup_summary
from opencode_session.validation_harness import DisposableValidationHarness, ValidationCheck


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

    validation = SmokeValidation(args, client, directory, smoke_id, result, unsupported_exit)

    return DisposableValidationHarness(
        client,
        result,
        default_exit_code=unavailable_exit,
        cleanup_failure_message="disposable session cleanup failed",
    ).run(
        validation.checks(),
        failure_types=(SmokeFailure,),
        json_output=args.json,
        compact_formatter=format_smoke_compact,
        failure_prefix="smoke failed",
        print_error=print_error,
        cleanup_summary_formatter=format_cleanup_summary,
    )


class SmokeValidation:
    def __init__(self, args, client, directory, smoke_id, result, unsupported_exit):
        self.args = args
        self.client = client
        self.directory = directory
        self.smoke_id = smoke_id
        self.result = result
        self.unsupported_exit = unsupported_exit
        self.capabilities = None
        self.session_id = None
        self.event_collector = None

    def checks(self):
        return (
            ValidationCheck("capabilities", self.check_capabilities),
            ValidationCheck("create", self.create_session),
            ValidationCheck("steer", self.steer_prompt),
            ValidationCheck("events", self.collect_events),
            ValidationCheck("run_blocking", self.run_blocking),
            ValidationCheck("blockers", self.blockers),
        )

    def check_capabilities(self, harness):
        self.capabilities = harness.detect_capabilities()
        require_smoke_capabilities(self.capabilities, unsupported_exit=self.unsupported_exit)
        return harness.result["checks"].get("capabilities")

    def create_session(self, harness):
        create_response = self.client.create_session_response(
            self.directory,
            title=self.smoke_id,
            metadata={
                "disposable": True,
                "prefix": self.args.prefix,
                "smoke_id": self.smoke_id,
                "no_live_model": bool(self.args.no_live_model),
            },
        )
        self.session_id = session_value(create_response.data, "id")
        if not self.session_id:
            raise SmokeFailure("session creation response did not include a session id")
        harness.track_session(self.session_id)
        self.result["session_id"] = self.session_id
        self.event_collector = harness.track_resource(
            SmokeEventCollector(
                self.client,
                self.session_id,
                self.capabilities["route_availability"]["events"]["path"],
                self.args.event_limit,
            )
        )
        self.event_collector.start()
        self.event_collector.wait_open(self.args.event_timeout)
        return {"status": "done", "session_id": self.session_id, "title": self.smoke_id}

    def steer_prompt(self, _harness):
        steer_message_id = f"msg_{self.smoke_id}-steer"
        steer_result = admit_prompt(
            self.client,
            self.capabilities,
            self.session_id,
            "ocs smoke steer",
            "steer",
            message_id=steer_message_id,
            map_unsupported=False,
        )
        return steer_result.record

    def collect_events(self, _harness):
        event_types = self.event_collector.collect(self.args.event_timeout)
        self.result["event_types"] = event_types
        return {"status": "done", "types": event_types}

    def run_blocking(self, _harness):
        if self.args.no_live_model:
            return skipped_blocking_execution_result(self.session_id, self.capabilities)
        try:
            return execute_blocking_prompt(self.client, self.session_id, "ocs smoke", self.capabilities)
        except BlockingProviderFailure as error:
            raise SmokeFailure(f"provider failure: {error}") from error

    def blockers(self, _harness):
        return smoke_blocker_summary(self.client, self.session_id)


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

    def close(self):
        return self.watcher.close()


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
