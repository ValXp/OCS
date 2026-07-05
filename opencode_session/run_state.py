import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import detect_capabilities
from opencode_session.run_record import DEFAULT_SERVER_URL
from opencode_session.run_start_policy import (
    blocking_execution_start_error,
    mark_orchestration_start_failed,
)
from opencode_session.run_store import RunStoreError
from opencode_session.worker_execution import (
    WorkerExecutionTimeout,
    cleanup_created_worker_sessions,
    execute_worker_attempts,
)
from opencode_session.worker_state import (
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
    ensure_worker as _ensure_worker,
    exit_code_for_run as _exit_code_for_run,
    mark_worker_active as _mark_worker_active,
    mark_worker_failed as _mark_worker_failed,
    refresh_run_summary as _refresh_run_summary,
)


@dataclass
class SingleWorkerRunStartRequest:
    name: str
    worker_id: str
    role: str
    prompt: str
    directory: Optional[str] = None
    server_url: Optional[str] = None
    session_id: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    cleanup: bool = False
    default_server_url: Optional[str] = None


@dataclass
class SingleWorkerRunStartOutcome:
    run: dict
    exit_code: int
    error: Optional[str] = None


class SingleWorkerRunStateService:
    def __init__(
        self,
        store,
        *,
        client_factory=OpenCodeApiClient,
        capability_detector=detect_capabilities,
        executor=execute_blocking_prompt,
        now=None,
    ):
        self.store = store
        self.client_factory = client_factory
        self.capability_detector = capability_detector
        self.executor = executor
        self.now = now or _utc_now

    def start(self, request):
        run = self._load_or_create_run(request)
        worker = _ensure_worker(run, request.worker_id, role=request.role)
        worker["prompt"] = request.prompt
        run["status"] = "active"
        _mark_worker_active(worker)
        self._save(run)

        created_session_ids = []
        try:
            client = self.client_factory(run["server_url"])
            capabilities = self.capability_detector(client)
            message = blocking_execution_start_error(capabilities)
            if message is not None:
                mark_orchestration_start_failed(run, [worker], message)
                self._save(run)
                return SingleWorkerRunStartOutcome(run, EX_UNSUPPORTED, message)

            session_id = request.session_id or worker.get("session_id")
            outcome = execute_worker_attempts(
                client,
                run,
                worker,
                request.prompt,
                capabilities,
                executor=self.executor,
                now=self.now,
                session_id=session_id,
                agent=request.agent,
                model=request.model,
                on_worker_update=lambda: self._save(run),
            )
        except OpenCodeApiError as error:
            mark_orchestration_start_failed(run, [worker], str(error))
            self._save(run)
            return SingleWorkerRunStartOutcome(run, EX_UNAVAILABLE, f"api failure: {error}")
        created_session_ids.extend(outcome.created_session_ids)
        _refresh_run_summary(run)
        if outcome.error is not None:
            self._save(run)
            return SingleWorkerRunStartOutcome(run, _exit_code_for_run(run), outcome.error)
        if request.cleanup:
            worker["cleanup"] = {"requested": True, "deleted": False}
            cleanup_outcome = cleanup_created_worker_sessions(client, worker, created_session_ids)
            if cleanup_outcome.error is not None:
                run["status"] = "failed"
                _mark_worker_failed(worker, "api", str(cleanup_outcome.error), retryable=False)
                self._save(run)
                return SingleWorkerRunStartOutcome(
                    run,
                    EX_UNAVAILABLE,
                    f"api failure: disposable session cleanup failed: {cleanup_outcome.error}",
                )
        self._save(run)
        return SingleWorkerRunStartOutcome(run, _exit_code_for_run(run))

    def _load_or_create_run(self, request):
        try:
            run = self.store.load_run(request.name)
        except RunStoreError as error:
            if error.kind != "missing":
                raise
            run = self.store.create_run(
                request.name,
                directory=request.directory or ".",
                server_url=request.server_url or request.default_server_url or _server_default(),
            )
        else:
            if request.directory is not None:
                run["directory"] = str(Path(request.directory).resolve())
            if request.server_url is not None:
                run["server_url"] = request.server_url
        return run

    def _save(self, run):
        run["updated_at"] = self.now()
        self.store.save_run(run)


def _server_default():
    return os.environ.get("OPENCODE_SERVER_URL") or os.environ.get("OPENCODE_SERVER") or DEFAULT_SERVER_URL


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
