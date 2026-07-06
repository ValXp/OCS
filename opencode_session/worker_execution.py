import inspect
import uuid
from dataclasses import dataclass, field
from typing import Optional

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure, execute_blocking_prompt
from opencode_session.disposable_session_lifecycle import cleanup_disposable_sessions
from opencode_session.remote_journal import PersistedRemoteMutationJournal, RemoteMutationJournal
from opencode_session.schema_common import ExecutionResultRecord, RunRecord, Worker
from opencode_session.session_ids import require_session_id
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired
from opencode_session.worker_attempt_log import new_worker_attempt_record
from opencode_session.worker_state import (
    WorkerTransition,
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
WORKER_SESSION_JOURNAL_FIELD = "worker_session_journal"
WORKER_SESSION_CREATE_KIND = "worker_session_create"
_WORKER_SESSION_JOURNAL = RemoteMutationJournal(WORKER_SESSION_JOURNAL_FIELD)

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
    run: Optional[RunRecord] = None


@dataclass
class WorkerAttemptOutcome:
    kind: str
    result: Optional[ExecutionResultRecord] = None
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


@dataclass(frozen=True)
class WorkerSessionCreationIntent:
    id: str
    worker_id: str
    cleanup_requested: bool = False


class WorkerSessionCreationJournal:
    def __init__(self, persist_run_mutation, *, now, id_factory=None):
        self.persist_run_mutation = persist_run_mutation
        self.now = now
        self.id_factory = id_factory or _new_worker_session_journal_id
        self.journal = PersistedRemoteMutationJournal(
            WORKER_SESSION_JOURNAL_FIELD,
            self.persist_run_mutation,
            now=self.now,
        )

    def record_intent(self, run, worker, *, agent=None, model=None, cleanup_requested=False):
        intent = WorkerSessionCreationIntent(
            self.id_factory(),
            worker["id"],
            cleanup_requested=bool(cleanup_requested),
        )
        entry = {
            "id": intent.id,
            "kind": WORKER_SESSION_CREATE_KIND,
            "status": "intent",
            "worker_id": intent.worker_id,
            "directory": run.get("directory"),
            "cleanup_requested": intent.cleanup_requested,
            "intent_recorded_at": self.now(),
        }
        if agent is not None:
            entry["agent"] = agent
        if model is not None:
            entry["model"] = model

        updated_run = self.journal.record_intent(run, entry)
        return updated_run, _latest_worker(updated_run, worker), intent

    def record_created(self, run, worker, intent, session_id, *, agent=None, model=None):
        fields = {
            "status": "created",
            "session_id": session_id,
            "created_session_ids": [session_id],
            "created_at": self.now(),
        }
        missing_entry = {
            "id": intent.id,
            "kind": WORKER_SESSION_CREATE_KIND,
            "worker_id": intent.worker_id,
            "cleanup_requested": intent.cleanup_requested,
            **fields,
        }

        def update_worker(latest_run):
            latest_worker = _ensure_latest_worker(latest_run, intent.worker_id)
            latest_worker["session_id"] = session_id
            if agent is not None:
                latest_worker["agent"] = agent
            if model is not None:
                latest_worker["model"] = model
            if intent.cleanup_requested:
                _remember_worker_session_for_cleanup(latest_worker, session_id)

        updated_run = self.journal.mark_applied(
            run,
            intent.id,
            fields,
            before_mark=update_worker,
            missing_entry=missing_entry,
        )
        return updated_run, _latest_worker(updated_run, worker)

    def discard_intent_best_effort(self, run, worker, intent):
        return self._remove_best_effort(run, worker, intent, operation="discard_worker_session_create")

    def finalize_best_effort(self, run, worker, intent):
        return self._remove_best_effort(run, worker, intent, operation="finalize_worker_session_create")

    def _remove_best_effort(self, run, worker, intent, *, operation):
        updated_run = self.journal.finalize_best_effort(run, intent.id, operation=operation)
        return updated_run, _latest_worker(updated_run, worker)


class WorkerExecutionTimeout(TimeoutExpired):
    pass


class WorkerExecutionExecutor:
    def __init__(self, *, apply_transition, executor=execute_blocking_prompt, now, session_journal=None):
        self.apply_transition = apply_transition
        self.executor = executor
        self.now = now
        self.session_journal = session_journal

    def execute(
        self,
        client,
        run,
        worker,
        prompt,
        capabilities,
        *,
        session_id=None,
        agent=None,
        model=None,
        create_session=True,
        cleanup_requested=False,
        stop_after_retry=False,
    ):
        created_session_ids = []
        created_session_ids_for_next_attempt = []
        session_intent = None
        if self.session_journal is not None and _will_create_worker_session(
            worker,
            session_id=session_id,
            create_session=create_session,
        ):
            run, worker, session_intent = self.session_journal.record_intent(
                run,
                worker,
                agent=agent,
                model=model,
                cleanup_requested=cleanup_requested,
            )
        try:
            session_outcome = provision_worker_session(
                client,
                run,
                worker,
                session_id=session_id,
                agent=agent,
                model=model,
                create_session=create_session,
            )
        except Exception:
            if session_intent is not None:
                run, worker = self.session_journal.discard_intent_best_effort(run, worker, session_intent)
            raise
        if session_outcome.created_session_id is not None:
            created_session_ids.append(session_outcome.created_session_id)
            created_session_ids_for_next_attempt.append(session_outcome.created_session_id)
            if session_intent is not None:
                run, worker = self.session_journal.record_created(
                    run,
                    worker,
                    session_intent,
                    session_outcome.created_session_id,
                    agent=agent,
                    model=model,
                )
        run, worker = self._apply_transition(run, worker, WorkerTransition.provisioned(worker))
        if session_intent is not None:
            run, worker = self.session_journal.finalize_best_effort(run, worker, session_intent)

        while True:
            active_transition = mark_worker_active(worker, now=self.now)
            run, worker = self._apply_transition(run, worker, active_transition)
            attempt_record = new_worker_attempt_record(
                worker,
                started_at=self.now(),
                created_session_ids=created_session_ids_for_next_attempt,
            )
            created_session_ids_for_next_attempt = []
            run, worker = self._apply_transition(
                run,
                worker,
                WorkerTransition.attempt_started(worker["id"], attempt_record),
            )
            attempt = execute_single_worker_attempt(
                client,
                worker,
                prompt,
                capabilities,
                executor=self.executor,
            )
            transition = apply_worker_attempt_transition(
                client,
                run,
                worker,
                attempt,
                now=self.now,
                agent=agent,
                model=model,
            )
            if transition.created_session_id is not None:
                created_session_ids.append(transition.created_session_id)
                created_session_ids_for_next_attempt.append(transition.created_session_id)
            transition.worker_transition = _with_finalized_attempt(
                transition.worker_transition,
                attempt_record["id"],
                transition,
                attempt,
                finished_at=self.now(),
            )
            run, worker = self._apply_transition(run, worker, transition.worker_transition)
            if transition.kind == RETRY_SCHEDULED and not stop_after_retry:
                continue
            return WorkerExecutionOutcome(
                transition.kind,
                created_session_ids,
                transition.error,
                transition.failure_category,
                run,
            )

    def _apply_transition(self, run, worker, transition):
        if transition is None:
            return run, worker
        applied_run, applied_worker = self.apply_transition(run, worker, transition)
        return applied_run, applied_worker or worker


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
):
    return WorkerExecutionExecutor(
        apply_transition=_apply_in_memory_worker_transition,
        executor=executor,
        now=now,
    ).execute(
        client,
        run,
        worker,
        prompt,
        capabilities,
        session_id=session_id,
        agent=agent,
        model=model,
        create_session=create_session,
        cleanup_requested=False,
        stop_after_retry=stop_after_retry,
    )


def _apply_in_memory_worker_transition(run, worker, transition):
    return run, apply_worker_transition_to_worker(worker, transition)


def _with_finalized_attempt(worker_transition, attempt_id, transition, attempt, *, finished_at):
    if worker_transition is None:
        return None
    return worker_transition.with_finalized_attempt(
        attempt_id,
        _attempt_finalization_fields(transition, attempt, finished_at=finished_at),
    )


def _attempt_finalization_fields(transition, attempt, *, finished_at):
    fields = {
        "status": _attempt_status(transition),
        "finished_at": finished_at,
    }
    if transition.error is not None:
        fields["error"] = transition.error
    if transition.failure_category is not None:
        fields["failure_category"] = transition.failure_category
    if isinstance(attempt.result, dict):
        fields["result_status"] = attempt.result.get("status")
        message_ids = attempt.result.get("message_ids") if isinstance(attempt.result.get("message_ids"), dict) else {}
        if message_ids.get("user") is not None:
            fields["user_message_id"] = message_ids["user"]
        if message_ids.get("assistant") is not None:
            fields["assistant_message_id"] = message_ids["assistant"]
    if attempt.prompt_id is not None:
        fields["user_message_id"] = attempt.prompt_id
    return fields


def _attempt_status(transition):
    if transition.kind == COMPLETED:
        return "completed"
    if transition.kind == RETRY_SCHEDULED:
        return "retry_scheduled"
    return "failed"


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


def recoverable_created_worker_sessions_by_worker(run):
    session_ids_by_worker = {}
    workers = run.get("workers", {}) if isinstance(run, dict) else {}
    if isinstance(workers, dict):
        for worker_id, worker in workers.items():
            if not isinstance(worker, dict):
                continue
            cleanup = worker.get("cleanup")
            if not isinstance(cleanup, dict) or cleanup.get("deleted"):
                continue
            for session_id in _string_list(cleanup.get("sessions")):
                _append_unique_session_id(session_ids_by_worker.setdefault(worker_id, []), session_id)
    for entry in _worker_session_journal_entries(run):
        if entry.get("kind") != WORKER_SESSION_CREATE_KIND or not entry.get("cleanup_requested"):
            continue
        worker_id = entry.get("worker_id")
        if not worker_id:
            continue
        session_ids = list(_string_list(entry.get("created_session_ids")))
        if entry.get("session_id"):
            session_ids.append(entry["session_id"])
        for session_id in session_ids:
            _append_unique_session_id(session_ids_by_worker.setdefault(worker_id, []), session_id)
    return {worker_id: session_ids for worker_id, session_ids in session_ids_by_worker.items() if session_ids}


def _will_create_worker_session(worker, *, session_id=None, create_session=True):
    if not create_session:
        return False
    return not (session_id or worker.get("session_id"))


def _latest_worker(run, fallback_worker):
    worker_id = fallback_worker.get("id") if isinstance(fallback_worker, dict) else None
    latest_worker = run.get("workers", {}).get(worker_id) if isinstance(run, dict) and worker_id else None
    return latest_worker if isinstance(latest_worker, dict) else fallback_worker


def _ensure_latest_worker(run, worker_id):
    workers = run.setdefault("workers", {})
    worker = workers.get(worker_id)
    if not isinstance(worker, dict):
        worker = {"id": worker_id}
        workers[worker_id] = worker
    return worker


def _worker_session_journal_entries(run):
    return _WORKER_SESSION_JOURNAL.pending_entries(run)


def _remember_worker_session_for_cleanup(worker, session_id):
    cleanup = worker.setdefault("cleanup", {"requested": True, "deleted": False})
    cleanup["requested"] = True
    cleanup["deleted"] = False
    sessions = cleanup.get("sessions")
    if not isinstance(sessions, list):
        sessions = []
    _append_unique_session_id(sessions, session_id)
    cleanup["sessions"] = sessions


def _string_list(value):
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _append_unique_session_id(session_ids, session_id):
    if isinstance(session_id, str) and session_id and session_id not in session_ids:
        session_ids.append(session_id)


def _new_worker_session_journal_id():
    return f"worker_session_create_{uuid.uuid4().hex}"


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
