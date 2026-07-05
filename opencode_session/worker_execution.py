from dataclasses import dataclass, field
from typing import Optional

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure, execute_blocking_prompt
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired
from opencode_session.worker_state import (
    apply_worker_result,
    create_isolated_timeout_retry_session,
    mark_worker_failed,
    mark_worker_timeout,
    schedule_worker_retry,
    session_value,
    worker_timeout_reason,
)


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
        worker_session_id = session_value(create_response.data, "id", "sessionID", "sessionId")
        created_session_id = worker_session_id
    worker["session_id"] = worker_session_id
    if agent is not None:
        worker["agent"] = agent
    if model is not None:
        worker["model"] = model
    return WorkerSessionOutcome(worker_session_id, created_session_id)


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
    if create_session:
        session_outcome = ensure_worker_session(client, run, worker, session_id=session_id, agent=agent, model=model)
        if session_outcome.created_session_id is not None:
            created_session_ids.append(session_outcome.created_session_id)
        _notify_worker_update(on_worker_update)
    else:
        worker["session_id"] = session_id or worker.get("session_id")
        if agent is not None:
            worker["agent"] = agent
        if model is not None:
            worker["model"] = model

    while True:
        worker["status"] = "active"
        worker["next_eligible_action"] = "wait"
        worker["timeout_started_at"] = now() if worker.get("timeout_seconds") else None
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
            reason = worker_timeout_reason(worker)
            if schedule_worker_retry(worker, "timeout", reason):
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
                    _notify_worker_update(on_worker_update)
                    return WorkerExecutionOutcome("failed", created_session_ids, f"api failure: {message}", "api")
                created_session_ids.append(retry_session_id)
                _notify_worker_update(on_worker_update)
                if stop_after_retry:
                    return WorkerExecutionOutcome("retry", created_session_ids, failure_category="timeout")
                continue
            mark_worker_timeout(worker, reason, now)
            _notify_worker_update(on_worker_update)
            return WorkerExecutionOutcome("failed", created_session_ids, reason, "timeout")
        except OpenCodeApiError as error:
            if schedule_worker_retry(worker, "api", str(error)):
                _notify_worker_update(on_worker_update)
                if stop_after_retry:
                    return WorkerExecutionOutcome("retry", created_session_ids, failure_category="api")
                continue
            mark_worker_failed(worker, "api", str(error))
            _notify_worker_update(on_worker_update)
            return WorkerExecutionOutcome("failed", created_session_ids, f"api failure: {error}", "api")
        except BlockingProviderFailure as error:
            if error.prompt_id is not None:
                worker["prompt_ids"] = [error.prompt_id]
            if schedule_worker_retry(worker, "provider", str(error)):
                _notify_worker_update(on_worker_update)
                if stop_after_retry:
                    return WorkerExecutionOutcome("retry", created_session_ids, failure_category="provider")
                continue
            mark_worker_failed(worker, "provider", str(error))
            _notify_worker_update(on_worker_update)
            return WorkerExecutionOutcome("failed", created_session_ids, f"provider failure: {error}", "provider")

        prompt_id = result["message_ids"].get("user")
        if prompt_id is not None:
            worker["prompt_ids"] = [prompt_id]
        apply_worker_result(worker, result)
        _notify_worker_update(on_worker_update)
        return WorkerExecutionOutcome("completed", created_session_ids)


def cleanup_created_worker_sessions(client, worker, session_ids):
    cleanup = worker.setdefault("cleanup", {"requested": True, "deleted": False})
    deleted_session_ids = []
    for session_id in session_ids:
        try:
            client.delete_session(session_id)
        except OpenCodeApiError as error:
            cleanup["error"] = str(error)
            if len(deleted_session_ids) > 1:
                cleanup["sessions"] = deleted_session_ids
            return WorkerCleanupOutcome(deleted_session_ids, error)
        deleted_session_ids.append(session_id)
    if deleted_session_ids:
        cleanup["deleted"] = True
        if len(deleted_session_ids) > 1:
            cleanup["sessions"] = deleted_session_ids
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
