from dataclasses import dataclass
from typing import Any, Optional

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import configure_client_route_plan, detect_capabilities
from opencode_session.run_start_policy import blocking_execution_start_error
from opencode_session.schema_common import DomainRecord
from opencode_session.worker_execution import (
    RETRY_SCHEDULED,
    WorkerExecutionOutcome,
    apply_worker_attempt_transition,
    cleanup_created_worker_sessions,
    execute_single_worker_attempt,
    provision_worker_session,
)
from opencode_session.worker_state import EX_UNAVAILABLE, WorkerTransition, mark_worker_active


@dataclass
class CapabilityProbeOutcome:
    client: Any
    capabilities: DomainRecord
    start_error: Optional[str] = None


@dataclass
class PersistedTransitionOutcome:
    run: DomainRecord
    worker: Optional[DomainRecord] = None


@dataclass
class CleanupWorkersOutcome:
    run: DomainRecord
    exit_code: int
    error: Optional[str] = None


class RunStartCore:
    def __init__(
        self,
        *,
        persist_worker_transition,
        refresh_run_summary,
        client_factory=OpenCodeApiClient,
        capability_detector=detect_capabilities,
        executor=execute_blocking_prompt,
        now,
    ):
        self.persist_worker_transition = persist_worker_transition
        self.refresh_run_summary = refresh_run_summary
        self.client_factory = client_factory
        self.capability_detector = capability_detector
        self.executor = executor
        self.now = now

    def probe_capabilities(self, run):
        client = self.client_factory(run["server_url"])
        capabilities = self.capability_detector(client)
        configure_client_route_plan(client, capabilities)
        return CapabilityProbeOutcome(client, capabilities, blocking_execution_start_error(capabilities))

    def execute_worker(
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
        stop_after_retry=False,
    ):
        created_session_ids = []
        session_outcome = provision_worker_session(
            client,
            run,
            worker,
            session_id=session_id,
            agent=agent,
            model=model,
            create_session=True,
        )
        if session_outcome.created_session_id is not None:
            created_session_ids.append(session_outcome.created_session_id)
        persisted = self._persist_transition(run, WorkerTransition.provisioned(worker))
        run = persisted.run
        worker = persisted.worker

        while True:
            active_transition = mark_worker_active(worker, now=self.now)
            persisted = self._persist_transition(run, active_transition)
            run = persisted.run
            worker = persisted.worker
            attempt = execute_single_worker_attempt(
                client,
                worker,
                prompt,
                capabilities,
                executor=self.executor,
            )
            transition = apply_worker_attempt_transition(client, run, worker, attempt, now=self.now, agent=agent, model=model)
            if transition.created_session_id is not None:
                created_session_ids.append(transition.created_session_id)
            persisted = self._persist_transition(run, transition.worker_transition)
            run = persisted.run
            worker = persisted.worker
            if transition.kind == RETRY_SCHEDULED and not stop_after_retry:
                continue
            return WorkerExecutionOutcome(
                transition.kind,
                created_session_ids,
                transition.error,
                transition.failure_category,
                run,
            )

    def cleanup_created_workers(self, client, run, created_session_ids_by_worker):
        first_error = None
        current_run = run
        for worker_id, session_ids in created_session_ids_by_worker.items():
            worker = current_run.get("workers", {}).get(worker_id)
            if not isinstance(worker, dict):
                continue
            cleanup_outcome = cleanup_created_worker_sessions(client, worker, session_ids)
            persisted = self._persist_transition(current_run, WorkerTransition.cleanup_updated(worker))
            current_run = persisted.run
            if cleanup_outcome.error is not None:
                if first_error is None:
                    first_error = cleanup_outcome.error
        if first_error is not None:
            self.refresh_run_summary(current_run)
            return CleanupWorkersOutcome(
                current_run,
                EX_UNAVAILABLE,
                f"api failure: disposable session cleanup failed: {first_error}",
            )
        return CleanupWorkersOutcome(current_run, 0)

    def _persist_transition(self, run, transition):
        result = self.persist_worker_transition(run, transition)
        worker = result.workers[0] if result.workers else result.run.get("workers", {}).get(transition.worker_id)
        return PersistedTransitionOutcome(result.run, worker)


def remember_created_worker_sessions(created_session_ids_by_worker, worker, session_ids):
    if not session_ids:
        return
    worker.setdefault("cleanup", {"requested": True, "deleted": False})
    created_session_ids_by_worker.setdefault(worker.get("id"), []).extend(session_ids)
