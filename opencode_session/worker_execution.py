from dataclasses import dataclass, field
from typing import Optional

from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.schema_run import RunRecord
from opencode_session.worker_attempt_execution import (
    coerce_worker_prompt_executor,
    execute_single_worker_attempt,
)
from opencode_session.worker_attempt_policy import (
    COMPLETED,
    RETRY_SCHEDULED,
    WorkerExecutionTimeout,
    apply_worker_attempt_transition,
)
from opencode_session.worker_attempt_log import new_worker_attempt_record
from opencode_session.worker_session_provisioning import WorkerSessionProvisioner
from opencode_session.worker_state import (
    WorkerTransition,
    apply_worker_transition_to_worker,
    mark_worker_active,
)


@dataclass
class WorkerExecutionOutcome:
    kind: str
    created_session_ids: list = field(default_factory=list)
    error: Optional[str] = None
    failure_category: Optional[str] = None
    run: Optional[RunRecord] = None


class WorkerExecutionPersistenceBoundary:
    """Owns persisted run/worker refreshes and remote session outbox updates during execution."""

    def __init__(
        self,
        *,
        run,
        worker,
        apply_transition,
        session_journal=None,
        session_provisioner=None,
    ):
        self.run = run
        self.worker = worker
        self._apply_transition = apply_transition
        self._session_provisioner = session_provisioner or WorkerSessionProvisioner(session_journal=session_journal)

    def provision_session(
        self,
        client,
        *,
        session_id=None,
        agent=None,
        model=None,
        create_session=True,
        cleanup_requested=False,
    ):
        provisioning = self._session_provisioner.provision(
            client,
            self.run,
            self.worker,
            session_id=session_id,
            agent=agent,
            model=model,
            create_session=create_session,
            cleanup_requested=cleanup_requested,
        )
        self._replace_state(provisioning.run, provisioning.worker)
        self.apply_worker_transition(WorkerTransition.provisioned(self.worker))
        self._finalize_provisioning_best_effort(provisioning)
        return provisioning.outcome

    def apply_worker_transition(self, transition):
        if transition is None:
            return self.worker
        applied_run, applied_worker = self._apply_transition(self.run, self.worker, transition)
        self._replace_state(applied_run, applied_worker or self.worker)
        return self.worker

    def _finalize_provisioning_best_effort(self, provisioning):
        run, worker = self._session_provisioner.finalize_best_effort(self.run, self.worker, provisioning)
        self._replace_state(run, worker)

    def _replace_state(self, run, worker):
        self.run = run
        self.worker = worker


class WorkerExecutionExecutor:
    def __init__(
        self,
        *,
        apply_transition,
        executor=execute_blocking_prompt,
        now,
        session_journal=None,
        session_provisioner=None,
        persistence_boundary_factory=WorkerExecutionPersistenceBoundary,
    ):
        self.apply_transition = apply_transition
        self.executor = coerce_worker_prompt_executor(executor)
        self.now = now
        self.session_journal = session_journal
        self.session_provisioner = session_provisioner
        self.persistence_boundary_factory = persistence_boundary_factory

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
    ):
        created_session_ids = []
        created_session_ids_for_attempt = []
        persistence = self.persistence_boundary_factory(
            run=run,
            worker=worker,
            apply_transition=self.apply_transition,
            session_journal=self.session_journal,
            session_provisioner=self.session_provisioner,
        )
        session_outcome = persistence.provision_session(
            client,
            session_id=session_id,
            agent=agent,
            model=model,
            create_session=create_session,
            cleanup_requested=cleanup_requested,
        )
        if session_outcome.created_session_id is not None:
            created_session_ids.append(session_outcome.created_session_id)
            created_session_ids_for_attempt.append(session_outcome.created_session_id)

        active_transition = mark_worker_active(persistence.worker, now=self.now)
        persistence.apply_worker_transition(active_transition)
        attempt_record = new_worker_attempt_record(
            persistence.worker,
            started_at=self.now(),
            created_session_ids=created_session_ids_for_attempt,
        )
        persistence.apply_worker_transition(
            WorkerTransition.attempt_started(persistence.worker.worker_id, attempt_record),
        )
        attempt = execute_single_worker_attempt(
            client,
            persistence.worker,
            prompt,
            capabilities,
            executor=self.executor,
        )
        transition = apply_worker_attempt_transition(
            persistence.worker,
            attempt,
            now=self.now,
        )
        if transition.created_session_id is not None:
            created_session_ids.append(transition.created_session_id)
        transition.worker_transition = _with_finalized_attempt(
            transition.worker_transition,
            attempt_record["id"],
            transition,
            attempt,
            finished_at=self.now(),
        )
        persistence.apply_worker_transition(transition.worker_transition)
        return WorkerExecutionOutcome(
            transition.kind,
            created_session_ids,
            transition.error,
            transition.failure_category,
            persistence.run,
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
