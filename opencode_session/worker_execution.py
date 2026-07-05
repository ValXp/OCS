from dataclasses import dataclass, field
from typing import Optional

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure, execute_blocking_prompt
from opencode_session.session_ids import require_session_id
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired
from opencode_session.worker_state import (
    apply_worker_result,
    mark_worker_active,
    mark_worker_failed,
    mark_worker_timeout,
    schedule_worker_retry,
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


@dataclass
class WorkerAttemptOutcome:
    kind: str
    result: Optional[dict] = None
    failure_category: Optional[str] = None
    reason: Optional[str] = None
    prompt_id: Optional[str] = None


@dataclass
class WorkerAttemptTransition:
    kind: str
    created_session_id: Optional[str] = None
    error: Optional[str] = None
    failure_category: Optional[str] = None


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


def create_isolated_timeout_retry_session(client, run, worker, reason, created_at, *, agent=None, model=None):
    timed_out_session_id = worker.get("session_id")
    retry_agent = agent if agent is not None else worker.get("agent")
    retry_model = model if model is not None else worker.get("model")
    create_response = client.create_session_response(run["directory"], agent=retry_agent, model=retry_model)
    retry_session_id = require_session_id(create_response, "timeout retry session creation")
    if retry_session_id == timed_out_session_id:
        raise OpenCodeApiError(f"timeout retry session creation returned original in-flight session '{timed_out_session_id}'")
    worker["session_id"] = retry_session_id
    _timeout_retry_sessions(worker).append(
        {
            "timed_out_session_id": timed_out_session_id,
            "retry_session_id": retry_session_id,
            "reason": reason,
            "created_at": created_at,
        }
    )
    return retry_session_id


def _timeout_retry_sessions(worker):
    sessions = worker.get("timeout_retry_sessions")
    if not isinstance(sessions, list):
        sessions = []
        worker["timeout_retry_sessions"] = sessions
    return sessions


def execute_single_worker_attempt(client, worker, prompt, capabilities, *, executor, now):
    mark_worker_active(worker, now=now)
    attempt_session_id = worker["session_id"]
    try:
        result = _call_worker_with_timeout(
            worker,
            lambda attempt_session_id=attempt_session_id: executor(
                client,
                attempt_session_id,
                prompt,
                capabilities,
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
        if attempt.prompt_id is not None:
            worker["prompt_ids"] = [attempt.prompt_id]
        return _apply_retryable_attempt_failure(worker, "provider", attempt.reason, error_prefix="provider failure")
    mark_worker_failed(worker, "unknown", attempt.reason or "worker attempt failed")
    return WorkerAttemptTransition(
        TERMINAL_FAILURE,
        error=attempt.reason or "worker attempt failed",
        failure_category="unknown",
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
    on_worker_update=None,
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
    if create_session:
        _notify_worker_update(on_worker_update)

    while True:
        attempt = execute_single_worker_attempt(client, worker, prompt, capabilities, executor=executor, now=now)
        transition = apply_worker_attempt_transition(client, run, worker, attempt, now=now, agent=agent, model=model)
        if transition.created_session_id is not None:
            created_session_ids.append(transition.created_session_id)
        _notify_worker_update(on_worker_update)
        if transition.kind == RETRY_SCHEDULED and not stop_after_retry:
            continue
        return WorkerExecutionOutcome(
            transition.kind,
            created_session_ids,
            transition.error,
            transition.failure_category,
        )


def _apply_completed_attempt(worker, result):
    prompt_id = result["message_ids"].get("user")
    if prompt_id is not None:
        worker["prompt_ids"] = [prompt_id]
    apply_worker_result(worker, result)
    return WorkerAttemptTransition(COMPLETED)


def _apply_timeout_attempt_failure(client, run, worker, reason, now, *, agent=None, model=None):
    if not schedule_worker_retry(worker, "timeout", reason):
        mark_worker_timeout(worker, reason, now)
        return WorkerAttemptTransition(TERMINAL_FAILURE, error=reason, failure_category="timeout")
    timed_out_at = now()
    worker["timed_out_at"] = timed_out_at
    try:
        retry_session_id = create_isolated_timeout_retry_session(
            client,
            run,
            worker,
            reason,
            timed_out_at,
            agent=agent,
            model=model,
        )
    except OpenCodeApiError as error:
        message = f"timeout retry session creation failed: {error}"
        mark_worker_failed(worker, "api", message)
        return WorkerAttemptTransition(TERMINAL_FAILURE, error=f"api failure: {message}", failure_category="api")
    return WorkerAttemptTransition(RETRY_SCHEDULED, created_session_id=retry_session_id, failure_category="timeout")


def _apply_retryable_attempt_failure(worker, category, reason, *, error_prefix):
    if schedule_worker_retry(worker, category, reason):
        return WorkerAttemptTransition(RETRY_SCHEDULED, failure_category=category)
    mark_worker_failed(worker, category, reason)
    return WorkerAttemptTransition(TERMINAL_FAILURE, error=f"{error_prefix}: {reason}", failure_category=category)


def cleanup_created_worker_sessions(client, worker, session_ids):
    cleanup = worker.setdefault("cleanup", {"requested": True, "deleted": False})
    deleted_session_ids = []
    errors = []
    for session_id in session_ids:
        try:
            client.delete_session(session_id)
        except OpenCodeApiError as error:
            errors.append(error)
            continue
        deleted_session_ids.append(session_id)

    cleanup["deleted"] = bool(deleted_session_ids) and not errors
    if errors:
        cleanup["error"] = str(errors[0])
    else:
        cleanup.pop("error", None)
    if deleted_session_ids:
        if len(deleted_session_ids) > 1 or errors:
            cleanup["sessions"] = deleted_session_ids
        else:
            cleanup.pop("sessions", None)
    else:
        cleanup.pop("sessions", None)
    if errors:
        return WorkerCleanupOutcome(deleted_session_ids, errors[0])
    return WorkerCleanupOutcome(deleted_session_ids)


def _call_worker_with_timeout(worker, callback):
    timeout = worker.get("timeout_seconds")
    try:
        return TimeoutDeadline(timeout).run(callback)
    except TimeoutExpired as error:
        raise WorkerExecutionTimeout() from error


def _notify_worker_update(callback):
    if callback is not None:
        callback()
