import os
import uuid
from pathlib import Path

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure, execute_blocking_prompt
from opencode_session.capabilities import unsupported_reasons
from opencode_session.cli_policy import EX_UNAVAILABLE, EX_UNSUPPORTED
from opencode_session.event_watcher import SessionEventWatcher
from opencode_session.events import is_terminal_event
from opencode_session.formatting import compact_bool, compact_value
from opencode_session.prompt_admission import admit_prompt
from opencode_session.schema_common import first_present
from opencode_session.schema_normalization import iter_normalized_message_records
from opencode_session.schema_session_adapter import session_value
from opencode_session.status import short_status
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired
from opencode_session.validation_cleanup import format_cleanup_summary
from opencode_session.validation_harness import DisposableValidationHarness, ValidationCheck


LIVE_VALIDATE_ENV = "OCS_LIVE_VALIDATE"
LIVE_SESSION_PREFIX = "ocs-live-"
LIVE_VALIDATE_PROMPT = "Reply exactly PONG."
LIVE_EVENT_OBSERVATION_TIMEOUT = 1.0


class LiveValidationFailure(Exception):
    def __init__(self, message, *, exit_code=EX_UNAVAILABLE):
        super().__init__(message)
        self.exit_code = exit_code


def run_live_validate(args, client, *, print_error, unavailable_exit=EX_UNAVAILABLE, unsupported_exit=EX_UNSUPPORTED):
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

    validation = LiveValidation(args, client, directory, validation_id, result, unsupported_exit)

    return DisposableValidationHarness(
        client,
        result,
        default_exit_code=unavailable_exit,
        cleanup_failure_message="disposable live validation session cleanup failed",
    ).run(
        validation.checks(),
        failure_types=(LiveValidationFailure,),
        json_output=args.json,
        compact_formatter=format_live_validate_compact,
        failure_prefix="live-provider validation failed",
        print_error=print_error,
        cleanup_summary_formatter=format_cleanup_summary,
    )


class LiveValidation:
    def __init__(self, args, client, directory, validation_id, result, unsupported_exit):
        self.args = args
        self.client = client
        self.directory = directory
        self.validation_id = validation_id
        self.result = result
        self.unsupported_exit = unsupported_exit
        self.capabilities = None

    def checks(self):
        return (
            ValidationCheck("capabilities", self.check_capabilities),
            ValidationCheck("wait", self.wait_route),
            ValidationCheck("v2_steer", self.v2_steer),
            ValidationCheck("run_blocking", self.run_blocking),
        )

    def check_capabilities(self, harness):
        self.capabilities = harness.detect_capabilities()
        require_live_validate_capabilities(self.capabilities, unsupported_exit=self.unsupported_exit)
        return harness.result["checks"].get("capabilities")

    def wait_route(self, _harness):
        return live_wait_record(self.capabilities)

    def v2_steer(self, harness):
        steer_session_id = self.create_live_session(harness, "steer")
        self.result["session_ids"]["steer"] = steer_session_id
        steer_message_id = f"msg_{self.validation_id}-steer"
        steer_result = admit_prompt(
            self.client,
            self.capabilities,
            steer_session_id,
            LIVE_VALIDATE_PROMPT,
            "steer",
            message_id=steer_message_id,
            map_unsupported=False,
        )
        steer = steer_result.record
        steer.update(live_steer_execution_observation(self.client, steer, self.capabilities))
        return steer

    def run_blocking(self, harness):
        run_session_id = self.create_live_session(harness, "run_blocking")
        self.result["session_ids"]["run_blocking"] = run_session_id
        try:
            run_blocking = execute_blocking_prompt(self.client, run_session_id, LIVE_VALIDATE_PROMPT, self.capabilities)
        except BlockingProviderFailure as error:
            raise LiveValidationFailure(f"provider failure: {error}") from error
        run_blocking["succeeded"] = run_blocking["status"] == "done"
        run_blocking["pong"] = is_exact_pong(run_blocking["text"])
        if not run_blocking["pong"]:
            raise LiveValidationFailure("live provider did not reply exactly PONG")
        return run_blocking

    def create_live_session(self, harness, role):
        create_response = self.client.create_session_response(
            self.directory,
            agent=self.args.agent,
            model=self.args.model,
            title=f"{self.validation_id}-{role}",
            metadata={
                "disposable": True,
                "kind": "live-provider-validation",
                "live_provider": True,
                "prefix": self.args.prefix,
                "validation_id": self.validation_id,
                "role": role,
            },
        )
        session_id = session_value(create_response.data, "id", "sessionID", "sessionId")
        if not session_id:
            raise LiveValidationFailure("session creation response did not include a session id")
        harness.track_session(session_id)
        return session_id


def require_live_validate_capabilities(capabilities, *, unsupported_exit=EX_UNSUPPORTED):
    reasons = unsupported_reasons(capabilities)
    if not capabilities["v2_prompt_support"]:
        reasons.append("missing v2 steer admission: POST /api/session/{sessionID}/prompt")
    if not capabilities.get("blocking_execution_available"):
        reasons.append(
            "missing blocking run execution: POST /session/{sessionID}/message or legacy "
            "POST /session/{sessionID}/run + POST /session/{sessionID}/reply"
        )
    if reasons:
        raise LiveValidationFailure(
            f"unsupported OpenCode server; {'; '.join(reasons)}",
            exit_code=unsupported_exit,
        )


def live_wait_record(capabilities):
    wait_route = capabilities["route_availability"]["v2_wait"]
    return {
        "available": wait_route["available"],
        "api_path": wait_route["path"],
        "status": "available" if wait_route["available"] else "unavailable",
    }


def live_steer_execution_observation(client, steer, capabilities):
    wait_route = capabilities["route_availability"]["v2_wait"]
    wait_observation = None
    if wait_route["available"] and "?" not in wait_route["path"]:
        try:
            response = client.wait_session_response(steer["session_id"], wait_route["path"])
        except OpenCodeApiError as error:
            wait_observation = execution_observation(
                "unknown",
                source="wait",
                status="unknown",
                reason="observation_failed",
                error=str(error),
            )
        else:
            status = short_status(first_present(response.data, "status", "state", "phase"))
            if status in {"active", "done"}:
                return execution_observation(True, source="wait", status=status, reason="observed_execution_state")
            if status == "queued":
                return execution_observation(False, source="wait", status=status, reason="observed_not_executed_state")
            wait_observation = execution_observation("unknown", source="wait", status=status, reason="no_execution_evidence")
    message_observation = live_message_execution_observation(client, steer)
    if message_observation["executed"] != "unknown":
        return message_observation
    event_route = capabilities["route_availability"]["events"]
    if event_route["available"]:
        return live_event_execution_observation(client, steer, event_route["path"])
    return message_observation if wait_observation is None else wait_observation


def live_message_execution_observation(client, steer):
    try:
        session = client.get_session_response(steer["session_id"]).data
    except OpenCodeApiError as error:
        return execution_observation(
            "unknown",
            source="message",
            status="unknown",
            reason="observation_failed",
            error=str(error),
        )
    status = assistant_message_status(session)
    if status is not None:
        return execution_observation(True, source="message", status=status, reason="observed_assistant_message")
    return execution_observation("unknown", source="message", status="unknown", reason="no_execution_evidence")


def assistant_message_status(session):
    for message in iter_normalized_message_records(session):
        role = str(message.get("role") or "").lower()
        if "assistant" not in role:
            continue
        status = message.get("status")
        if message.get("text") or message.get("tokens") is not None or message.get("cost") is not None:
            return status or "unknown"
        if status in {"active", "done"}:
            return status
    return None


def live_event_execution_observation(client, steer, event_path):
    deadline = TimeoutDeadline(LIVE_EVENT_OBSERVATION_TIMEOUT)
    try:
        watcher = SessionEventWatcher(client, event_path, steer["session_id"])
        for event in watcher.iter_events(deadline=deadline):
            observation = event_execution_observation(event)
            if observation["executed"] != "unknown":
                return observation
            if is_terminal_event(event):
                break
    except TimeoutExpired:
        return execution_observation(
            "unknown",
            source="event",
            status="unknown",
            reason="observation_timed_out",
        )
    except OpenCodeApiError as error:
        return execution_observation(
            "unknown",
            source="event",
            status="unknown",
            reason="observation_failed",
            error=str(error),
        )
    return execution_observation("unknown", source="event", status="unknown", reason="no_execution_evidence")


def event_execution_observation(event):
    status = event.get("status") or "unknown"
    if event.get("kind") in {"text", "tool", "step"}:
        return execution_observation(True, source="event", status=status, reason="observed_execution_event")
    if event.get("kind") == "status" and status in {"active", "done"}:
        return execution_observation(True, source="event", status=status, reason="observed_execution_event")
    return execution_observation("unknown", source="event", status=status, reason="no_execution_evidence")


def execution_observation(executed, *, source, status, reason, error=None):
    evidence = {"source": source, "status": status or "unknown", "reason": reason}
    if error is not None:
        evidence["error"] = error
    return {"executed": executed, "execution_evidence": evidence}


def is_exact_pong(text):
    return str(text).strip() == "PONG"


def format_live_validate_compact(result):
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
        ("pong", compact_bool(run_blocking.get("pong"))),
        ("cleanup", result["cleanup"].get("status")),
    ]
    return "live_validate " + " ".join(f"{key}={compact_value(value)}" for key, value in fields)


def live_validate_env_failure(print_error, dataerr_exit):
    if os.environ.get(LIVE_VALIDATE_ENV) == "1":
        return None
    print_error(f"live-provider validation disabled; set {LIVE_VALIDATE_ENV}=1 to allow token-consuming provider calls")
    return dataerr_exit
