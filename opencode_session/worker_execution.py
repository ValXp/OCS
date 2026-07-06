from dataclasses import dataclass, field
from typing import Optional

from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.schema_common import RunRecord
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
    worker_field,
)


@dataclass
class WorkerExecutionOutcome:
    kind: str
    created_session_ids: list = field(default_factory=list)
    error: Optional[str] = None
    failure_category: Optional[str] = None
    run: Optional[RunRecord] = None

class WorkerExecutionExecutor:
    def __init__(
        self,
        *,
        apply_transition,
        executor=execute_blocking_prompt,
        now,
        session_journal=None,
        session_provisioner=None,
    ):
        self.apply_transition = apply_transition
        self.executor = coerce_worker_prompt_executor(executor)
        self.now = now
        self.session_provisioner = session_provisioner or WorkerSessionProvisioner(session_journal=session_journal)

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
        provisioning = self.session_provisioner.provision(
            client,
            run,
            worker,
            session_id=session_id,
            agent=agent,
            model=model,
            create_session=create_session,
            cleanup_requested=cleanup_requested,
        )
        run = provisioning.run
        worker = provisioning.worker
        session_outcome = provisioning.outcome
        if session_outcome.created_session_id is not None:
            created_session_ids.append(session_outcome.created_session_id)
            created_session_ids_for_next_attempt.append(session_outcome.created_session_id)
        run, worker = self._apply_transition(run, worker, WorkerTransition.provisioned(worker))
        run, worker = self.session_provisioner.finalize_best_effort(run, worker, provisioning)

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
                WorkerTransition.attempt_started(worker_field(worker, "id"), attempt_record),
            )
            attempt = execute_single_worker_attempt(
                client,
                worker,
                prompt,
                capabilities,
                executor=self.executor,
            )
            transition = apply_worker_attempt_transition(
                worker,
                attempt,
                now=self.now,
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
