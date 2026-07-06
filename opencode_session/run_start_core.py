from dataclasses import dataclass
from typing import Optional, Protocol

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import configure_client_route_plan, detect_capabilities
from opencode_session.run_start_policy import blocking_execution_start_error
from opencode_session.schema_common import CapabilitiesRecord, RunRecord, WorkerRecordShape
from opencode_session.worker_execution import (
    cleanup_created_worker_sessions,
    execute_worker_attempts,
)
from opencode_session.worker_domain import WorkerTransition
from opencode_session.worker_state import EX_UNAVAILABLE


@dataclass
class CapabilityProbeOutcome:
    client: "RunStartClientProtocol"
    capabilities: CapabilitiesRecord
    start_error: Optional[str] = None


@dataclass
class PersistedTransitionOutcome:
    run: RunRecord
    worker: Optional[WorkerRecordShape] = None


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
        return execute_worker_attempts(
            client,
            run,
            worker,
            prompt,
            capabilities,
            executor=self.executor,
            now=self.now,
            session_id=session_id,
            agent=agent,
            model=model,
            create_session=True,
            stop_after_retry=stop_after_retry,
            transition_sink=self._persist_execution_transition,
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

    def _persist_execution_transition(self, run, _worker, transition):
        return self._persist_transition(run, transition)


def remember_created_worker_sessions(created_session_ids_by_worker, worker, session_ids):
    if not session_ids:
        return
    worker.setdefault("cleanup", {"requested": True, "deleted": False})
    created_session_ids_by_worker.setdefault(worker.get("id"), []).extend(session_ids)
