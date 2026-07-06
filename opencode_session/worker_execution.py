import inspect
from dataclasses import dataclass, field
from typing import Optional

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure, execute_blocking_prompt
from opencode_session.disposable_session_lifecycle import cleanup_disposable_sessions
from opencode_session.schema_common import DomainRecord
from opencode_session.session_ids import require_session_id
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired
from opencode_session.worker_lifecycle import WorkerTransition
from opencode_session.worker_state import (
    apply_worker_transition_to_worker,
    apply_worker_result,
    mark_worker_active,
    mark_worker_failed,
    mark_worker_timeout,
    schedule_worker_retry,
    worker_retry_available,
    worker_timeout_reason,
)


COMPLETED = "completed"
RETRY_SCHEDULED = "retry_scheduled"
TERMINAL_FAILURE = "terminal_failure"

_ATTEMPT_COMPLETED = "completed"
_ATTEMPT_FAILED = "failed"


@dataclass
class WorkerSessionOutcome:
    session_id: Optional[str]
    created_session_id: Optional[str] = None


@dataclass
class WorkerExecutionOutcome:
    kind: str
    created_session_ids: list = field(default_factory=list)
    error: Optional[str] = None
    failure_category: Optional[str] = None
    run: Optional[DomainRecord] = None


@dataclass
class WorkerTransitionSinkOutcome:
    run: DomainRecord
    worker: Optional[DomainRecord] = None


@dataclass
class WorkerAttemptOutcome:
    kind: str
    result: Optional[DomainRecord] = None
    failure_category: Optional[str] = None
    reason: Optional[str] = None
    prompt_id: Optional[str] = None


@dataclass
class WorkerAttemptTransition:
    kind: str
    created_session_id: Optional[str] = None
    error: Optional[str] = None
    failure_category: Optional[str] = None
    worker_transition: Optional[WorkerTransition] = None


@dataclass
class WorkerCleanupOutcome:
    deleted_session_ids: list = field(default_factory=list)
    error: Optional[OpenCodeApiError] = None


class WorkerExecutionTimeout(TimeoutExpired):
    pass


def ensure_worker_session(
    client,
    run,
    worker,
    *,
    session_id=None,
    agent=None,
    model=None,
    treat_falsey_session_as_missing=False,
):
    worker_session_id = session_id or worker.get("session_id")
    created_session_id = None
    missing_session = not worker_session_id if treat_falsey_session_as_missing else worker_session_id is None
    if missing_session:
        create_response = client.create_session_response(run["directory"], agent=agent, model=model)
        worker_session_id = require_session_id(create_response)
        created_session_id = worker_session_id
    worker["session_id"] = worker_session_id
    if agent is not None:
        worker["agent"] = agent
    if model is not None:
        worker["model"] = model
    return WorkerSessionOutcome(worker_session_id, created_session_id)


def provision_worker_session(
    client,
    run,
    worker,
    *,
    session_id=None,
    agent=None,
    model=None,
    create_session=True,
):
    if create_session:
        return ensure_worker_session(
            client,
            run,
            worker,
            session_id=session_id,
            agent=agent,
            model=model,
            treat_falsey_session_as_missing=True,
        )
    worker["session_id"] = session_id or worker.get("session_id")
    if agent is not None:
        worker["agent"] = agent
    if model is not None:
        worker["model"] = model
    return WorkerSessionOutcome(worker.get("session_id"))


def execute_single_worker_attempt(client, worker, prompt, capabilities, *, executor):
    attempt_session_id = worker["session_id"]
    try:
        result = _call_worker_with_deadline(
            worker,
            lambda deadline, attempt_session_id=attempt_session_id: _execute_with_optional_deadline(
                executor,
                client,
                attempt_session_id,
                prompt,
                capabilities,
                deadline=deadline,
            ),
        )
    except WorkerExecutionTimeout:
        return WorkerAttemptOutcome(_ATTEMPT_FAILED, failure_category="timeout", reason=worker_timeout_reason(worker))
    except OpenCodeApiError as error:
        return WorkerAttemptOutcome(_ATTEMPT_FAILED, failure_category="api", reason=str(error))
    except BlockingProviderFailure as error:
        return WorkerAttemptOutcome(
            _ATTEMPT_FAILED,
            failure_category="provider",
            reason=str(error),
            prompt_id=error.prompt_id,
        )
    return WorkerAttemptOutcome(_ATTEMPT_COMPLETED, result=result)


def apply_worker_attempt_transition(client, run, worker, attempt, *, now, agent=None, model=None):
    if attempt.kind == _ATTEMPT_COMPLETED:
        return _apply_completed_attempt(worker, attempt.result)
    if attempt.failure_category == "timeout":
        return _apply_timeout_attempt_failure(client, run, worker, attempt.reason, now, agent=agent, model=model)
    if attempt.failure_category == "api":
        return _apply_retryable_attempt_failure(worker, "api", attempt.reason, error_prefix="api failure")
    if attempt.failure_category == "provider":
        return _apply_retryable_attempt_failure(
            worker,
            "provider",
            attempt.reason,
            error_prefix="provider failure",
            prompt_ids=(attempt.prompt_id,),
        )
    failure_transition = mark_worker_failed(worker, "unknown", attempt.reason or "worker attempt failed")
    return WorkerAttemptTransition(
        TERMINAL_FAILURE,
        error=attempt.reason or "worker attempt failed",
        failure_category="unknown",
        worker_transition=failure_transition,
    )


def execute_worker_attempts(
    client,
    run,
    worker,
    prompt,
    capabilities,
    *,
    executor=execute_blocking_prompt,
    now,
    session_id=None,
    agent=None,
    model=None,
    create_session=True,
    stop_after_retry=False,
    transition_sink=None,
):
    created_session_ids = []
    session_outcome = provision_worker_session(
        client,
        run,
        worker,
        session_id=session_id,
        agent=agent,
        model=model,
        create_session=create_session,
    )
    if session_outcome.created_session_id is not None:
        created_session_ids.append(session_outcome.created_session_id)
    sink_outcome = _record_worker_transition(
        transition_sink,
        run,
        worker,
        WorkerTransition.provisioned(worker),
    )
    run = sink_outcome.run
    worker = sink_outcome.worker

    while True:
        active_transition = mark_worker_active(worker, now=now)
        sink_outcome = _record_worker_transition(transition_sink, run, worker, active_transition)
        run = sink_outcome.run
        worker = sink_outcome.worker
        attempt = execute_single_worker_attempt(
            client,
            worker,
            prompt,
            capabilities,
            executor=executor,
        )
        transition = apply_worker_attempt_transition(client, run, worker, attempt, now=now, agent=agent, model=model)
        if transition.created_session_id is not None:
            created_session_ids.append(transition.created_session_id)
        sink_outcome = _record_worker_transition(transition_sink, run, worker, transition.worker_transition)
        run = sink_outcome.run
        worker = sink_outcome.worker
        if transition.kind == RETRY_SCHEDULED and not stop_after_retry:
            continue
        return WorkerExecutionOutcome(
            transition.kind,
            created_session_ids,
            transition.error,
            transition.failure_category,
            run,
        )


def _record_worker_transition(transition_sink, run, worker, transition):
    if transition is None:
        return WorkerTransitionSinkOutcome(run, worker)
    if transition_sink is None:
        return WorkerTransitionSinkOutcome(run, apply_worker_transition_to_worker(worker, transition))
    outcome = transition_sink(run, worker, transition)
    if outcome is None:
        return WorkerTransitionSinkOutcome(run, worker)
    persisted_run = outcome.run
    persisted_worker = outcome.worker
    if persisted_worker is None:
        persisted_worker = persisted_run.get("workers", {}).get(transition.worker_id)
    if persisted_worker is None:
        persisted_worker = worker
    return WorkerTransitionSinkOutcome(persisted_run, persisted_worker)


def _apply_completed_attempt(worker, result):
    prompt_id = result["message_ids"].get("user")
    transition = apply_worker_result(worker, result, prompt_ids=(prompt_id,))
    return WorkerAttemptTransition(COMPLETED, worker_transition=transition)


def _apply_timeout_attempt_failure(client, run, worker, reason, now, *, agent=None, model=None):
    manual_retry_available = worker_retry_available(worker, "timeout")
    transition = mark_worker_timeout(worker, reason, now, manual_retry_required=manual_retry_available)
    if manual_retry_available:
        return WorkerAttemptTransition(
            TERMINAL_FAILURE,
            error=f"{reason}; automatic timeout retry skipped because the timed-out request may still be running",
            failure_category="timeout",
            worker_transition=transition,
        )
    return WorkerAttemptTransition(TERMINAL_FAILURE, error=reason, failure_category="timeout", worker_transition=transition)


def _apply_retryable_attempt_failure(worker, category, reason, *, error_prefix, prompt_ids=()):
    retry_transition = schedule_worker_retry(worker, category, reason, prompt_ids=prompt_ids)
    if retry_transition:
        return WorkerAttemptTransition(RETRY_SCHEDULED, failure_category=category, worker_transition=retry_transition)
    failure_transition = mark_worker_failed(worker, category, reason, prompt_ids=prompt_ids)
    return WorkerAttemptTransition(
        TERMINAL_FAILURE,
        error=f"{error_prefix}: {reason}",
        failure_category=category,
        worker_transition=failure_transition,
    )


def cleanup_created_worker_sessions(client, worker, session_ids):
    cleanup = worker.setdefault("cleanup", {"requested": True, "deleted": False})
    cleanup_outcome = cleanup_disposable_sessions(client, session_ids)
    cleanup_record = cleanup_outcome.record
    deleted_session_ids = list(cleanup_record["deleted"])
    errors = cleanup_record["errors"]

    cleanup["deleted"] = bool(cleanup_record["verified"]) and not errors
    if errors:
        cleanup["error"] = errors[0]["error"]
    else:
        cleanup.pop("error", None)
    if deleted_session_ids:
        if len(deleted_session_ids) > 1 or errors:
            cleanup["sessions"] = deleted_session_ids
        else:
            cleanup.pop("sessions", None)
    else:
        cleanup.pop("sessions", None)
    if cleanup_record["verified"]:
        if len(cleanup_record["verified"]) > 1 or errors:
            cleanup["verified"] = list(cleanup_record["verified"])
        else:
            cleanup.pop("verified", None)
    else:
        cleanup.pop("verified", None)
    if errors:
        return WorkerCleanupOutcome(deleted_session_ids, cleanup_outcome.first_error)
    return WorkerCleanupOutcome(deleted_session_ids)


def _call_worker_with_deadline(worker, callback):
    timeout = worker.get("timeout_seconds")
    deadline = TimeoutDeadline(timeout) if timeout is not None else None
    try:
        if deadline is not None:
            deadline.require_time()
        return callback(deadline)
    except TimeoutExpired as error:
        raise WorkerExecutionTimeout() from error
    except TimeoutError as error:
        raise WorkerExecutionTimeout() from error


def _execute_with_optional_deadline(executor, client, session_id, prompt, capabilities, *, deadline):
    if deadline is not None and _accepts_keyword(executor, "deadline"):
        return executor(client, session_id, prompt, capabilities, deadline=deadline)
    if deadline is not None and _accepts_keyword(executor, "timeout"):
        return executor(client, session_id, prompt, capabilities, timeout=deadline.require_time())
    return executor(client, session_id, prompt, capabilities)


def _accepts_keyword(callable_object, name):
    try:
        signature = inspect.signature(callable_object)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.kind in {inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}:
            if parameter.name == name:
                return True
    return False
