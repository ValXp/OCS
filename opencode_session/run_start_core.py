from dataclasses import dataclass
from typing import Optional, Protocol

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import configure_client_route_plan, detect_capabilities
from opencode_session.cli_policy import EX_UNAVAILABLE
from opencode_session.run_start_policy import blocking_execution_start_error
from opencode_session.schema_capabilities import CapabilitiesRecord
from opencode_session.schema_run import RunRecord
from opencode_session.schema_worker import HydratedWorker
from opencode_session.worker_cleanup_recovery import (
    cleanup_created_worker_sessions,
    recoverable_created_worker_sessions_by_worker,
)
from opencode_session.worker_execution import (
    WorkerExecutionExecutor,
)
from opencode_session.worker_session_provisioning import WorkerSessionCreationJournal
from opencode_session.worker_state import (
    WorkerTransition,
    is_worker_record,
    worker_record_for_mutation,
)


@dataclass
class CapabilityProbeOutcome:
    client: "RunStartClientProtocol"
    capabilities: CapabilitiesRecord
    start_error: Optional[str] = None


@dataclass
class PersistedTransitionOutcome:
    run: RunRecord
    worker: Optional[HydratedWorker] = None


@dataclass
class CleanupWorkersOutcome:
    run: RunRecord
    exit_code: int
    error: Optional[str] = None


@dataclass(frozen=True)
class CreatedWorkerCleanupStep:
    worker_id: str
    session_ids: tuple


@dataclass(frozen=True)
class CreatedWorkerCleanupPlan:
    steps: tuple = ()


class RunStartClientProtocol(Protocol):
    def configure_route_plan(self, route_plan): ...

    def get_health(self, *, deadline=None): ...

    def get_openapi_doc(self, *, deadline=None): ...

    def create_session_response(self, directory, *, agent=None, model=None, title=None, metadata=None): ...

    def message_session_response(self, session_id, message, *, message_id=None, timeout=None, deadline=None): ...

    def run_session_response(self, session_id, message, *, timeout=None, deadline=None): ...

    def reply_session_response(self, session_id, *, timeout=None, deadline=None): ...

    def delete_session_response(self, session_id): ...

    def get_session(self, session_id): ...


class RunStartCapabilityProbe:
    def __init__(
        self,
        *,
        client_factory=OpenCodeApiClient,
        capability_detector=detect_capabilities,
    ):
        self.client_factory = client_factory
        self.capability_detector = capability_detector

    def probe(self, run):
        client = self.client_factory(run["server_url"])
        capabilities = self.capability_detector(client)
        configure_client_route_plan(client, capabilities)
        return CapabilityProbeOutcome(client, capabilities, blocking_execution_start_error(capabilities))


class CreatedWorkerCleanupPlanner:
    def plan(self, created_session_ids_by_worker, run):
        session_ids_by_worker = recoverable_cleanup_sessions(created_session_ids_by_worker, run)
        return CreatedWorkerCleanupPlan(
            tuple(
                CreatedWorkerCleanupStep(worker_id, tuple(session_ids))
                for worker_id, session_ids in session_ids_by_worker.items()
            )
        )


class CreatedWorkerCleanupExecutor:
    def __init__(self, *, persist_worker_transition, refresh_run_summary):
        self.persist_worker_transition = persist_worker_transition
        self.refresh_run_summary = refresh_run_summary

    def cleanup(self, client, run, cleanup_plan):
        first_error = None
        current_run = run
        for step in cleanup_plan.steps:
            workers = current_run.setdefault("workers", {})
            worker = workers.get(step.worker_id)
            if not is_worker_record(worker):
                continue
            worker = worker_record_for_mutation(worker, step.worker_id).to_worker()
            workers[step.worker_id] = worker
            cleanup_outcome = cleanup_created_worker_sessions(client, worker, step.session_ids)
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


class RunStartCore:
    def __init__(
        self,
        *,
        persist_worker_transition,
        refresh_run_summary,
        executor=execute_blocking_prompt,
        persist_run_mutation=None,
        now,
    ):
        self.persist_worker_transition = persist_worker_transition
        self.refresh_run_summary = refresh_run_summary
        self.executor = executor
        self.persist_run_mutation = persist_run_mutation
        self.now = now
        self.worker_executor = WorkerExecutionExecutor(
            apply_transition=self._persist_worker_execution_transition,
            executor=self.executor,
            now=self.now,
            session_journal=self._worker_session_journal(),
        )

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
        cleanup_requested=False,
    ):
        return self.worker_executor.execute(
            client,
            run,
            worker,
            prompt,
            capabilities,
            session_id=session_id,
            agent=agent,
            model=model,
            create_session=True,
            cleanup_requested=cleanup_requested,
        )

    def _persist_transition(self, run, transition):
        result = self.persist_worker_transition(run, transition)
        worker = result.workers[0] if result.workers else result.run.get("workers", {}).get(transition.worker_id)
        return PersistedTransitionOutcome(result.run, worker)

    def _persist_worker_execution_transition(self, run, worker, transition):
        persisted = self._persist_transition(run, transition)
        return persisted.run, persisted.worker or worker

    def _worker_session_journal(self):
        if self.persist_run_mutation is None:
            return None
        return WorkerSessionCreationJournal(self.persist_run_mutation, now=self.now)


def remember_created_worker_sessions(created_session_ids_by_worker, worker, session_ids):
    if not session_ids:
        return
    worker.ensure_cleanup()
    remembered_session_ids = created_session_ids_by_worker.setdefault(worker.worker_id, [])
    for session_id in session_ids:
        if session_id not in remembered_session_ids:
            remembered_session_ids.append(session_id)


def recoverable_cleanup_sessions(created_session_ids_by_worker, run):
    merged = {worker_id: list(session_ids) for worker_id, session_ids in created_session_ids_by_worker.items()}
    for worker_id, session_ids in recoverable_created_worker_sessions_by_worker(run).items():
        remembered = merged.setdefault(worker_id, [])
        for session_id in session_ids:
            if session_id not in remembered:
                remembered.append(session_id)
    return {worker_id: session_ids for worker_id, session_ids in merged.items() if session_ids}
