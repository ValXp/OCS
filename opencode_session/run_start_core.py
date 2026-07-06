from dataclasses import dataclass
from typing import Optional, Protocol

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import configure_client_route_plan, detect_capabilities
from opencode_session.cli_policy import EX_UNAVAILABLE
from opencode_session.run_start_policy import blocking_execution_start_error
from opencode_session.schema_common import CapabilitiesRecord, HydratedWorker, RunRecord
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
    WorkerRecord,
    is_worker_mapping,
    worker_field,
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


class RunStartCore:
    def __init__(
        self,
        *,
        persist_worker_transition,
        refresh_run_summary,
        client_factory=OpenCodeApiClient,
        capability_detector=detect_capabilities,
        executor=execute_blocking_prompt,
        persist_run_mutation=None,
        now,
    ):
        self.persist_worker_transition = persist_worker_transition
        self.refresh_run_summary = refresh_run_summary
        self.client_factory = client_factory
        self.capability_detector = capability_detector
        self.executor = executor
        self.persist_run_mutation = persist_run_mutation
        self.now = now
        self.worker_executor = WorkerExecutionExecutor(
            apply_transition=self._persist_worker_execution_transition,
            executor=self.executor,
            now=self.now,
            session_journal=self._worker_session_journal(),
        )

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
        cleanup_requested=False,
        stop_after_retry=False,
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
            stop_after_retry=stop_after_retry,
        )

    def cleanup_created_workers(self, client, run, created_session_ids_by_worker):
        first_error = None
        current_run = run
        for worker_id, session_ids in created_session_ids_by_worker.items():
            workers = current_run.setdefault("workers", {})
            worker = workers.get(worker_id)
            if not is_worker_mapping(worker):
                continue
            worker = worker_record_for_mutation(worker, worker_id).to_worker()
            workers[worker_id] = worker
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
    if isinstance(worker, WorkerRecord):
        worker.ensure_cleanup()
    else:
        worker.setdefault("cleanup", {"requested": True, "deleted": False})
    remembered_session_ids = created_session_ids_by_worker.setdefault(worker_field(worker, "id"), [])
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
