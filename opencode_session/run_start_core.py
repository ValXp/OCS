from dataclasses import dataclass
from typing import Optional

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import detect_capabilities
from opencode_session.run_start_policy import blocking_execution_start_error, mark_orchestration_cleanup_failed
from opencode_session.worker_execution import cleanup_created_worker_sessions, execute_worker_attempts
from opencode_session.worker_state import EX_UNAVAILABLE, WorkerTransition


@dataclass
class CapabilityProbeOutcome:
    client: object
    capabilities: dict
    start_error: Optional[str] = None


@dataclass
class CleanupFailureOutcome:
    exit_code: int
    error: str


class RunStartCore:
    def __init__(
        self,
        *,
        persist_worker_update,
        refresh_run_summary,
        client_factory=OpenCodeApiClient,
        capability_detector=detect_capabilities,
        executor=execute_blocking_prompt,
        now,
    ):
        self.persist_worker_update = persist_worker_update
        self.refresh_run_summary = refresh_run_summary
        self.client_factory = client_factory
        self.capability_detector = capability_detector
        self.executor = executor
        self.now = now

    def probe_capabilities(self, run):
        client = self.client_factory(run["server_url"])
        capabilities = self.capability_detector(client)
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
            stop_after_retry=stop_after_retry,
            on_worker_update=lambda updated_worker, transition: self.persist_worker_update(run, transition or updated_worker),
        )

    def cleanup_created_workers(self, client, run, created_session_ids_by_worker):
        workers = run.get("workers", {})
        first_error = None
        for worker_id, session_ids in created_session_ids_by_worker.items():
            worker = workers.get(worker_id)
            if not isinstance(worker, dict):
                continue
            cleanup_outcome = cleanup_created_worker_sessions(client, worker, session_ids)
            self.persist_worker_update(run, WorkerTransition.cleanup_updated(worker))
            if cleanup_outcome.error is not None:
                if first_error is None:
                    first_error = cleanup_outcome.error
                transition = mark_orchestration_cleanup_failed(run, worker, str(cleanup_outcome.error))
                self.persist_worker_update(run, transition)
        if first_error is not None:
            self.refresh_run_summary(run)
            return CleanupFailureOutcome(
                EX_UNAVAILABLE,
                f"api failure: disposable session cleanup failed: {first_error}",
            )
        return None


def remember_created_worker_sessions(created_session_ids_by_worker, worker, session_ids):
    if not session_ids:
        return
    worker.setdefault("cleanup", {"requested": True, "deleted": False})
    created_session_ids_by_worker.setdefault(worker.get("id"), []).extend(session_ids)
