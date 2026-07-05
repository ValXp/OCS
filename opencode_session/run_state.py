import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import detect_capabilities
from opencode_session.run_record import DEFAULT_SERVER_URL
from opencode_session.run_persistence import persist_run_mutation, persist_run_summary, persist_worker_update
from opencode_session.run_start_policy import (
    mark_orchestration_start_failed,
)
from opencode_session.run_start_core import RunStartCore, remember_created_worker_sessions
from opencode_session.run_store import RunStoreError
from opencode_session.worker_execution import WorkerExecutionTimeout
from opencode_session.worker_state import (
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
    ensure_worker as _ensure_worker,
    exit_code_for_run as _exit_code_for_run,
    mark_worker_active as _mark_worker_active,
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
        self.core = RunStartCore(
            persist_worker_update=self._persist_worker_update,
            refresh_run_summary=_refresh_run_summary,
            client_factory=self.client_factory,
            capability_detector=self.capability_detector,
            executor=self.executor,
            now=self.now,
        )

    def start(self, request):
        run = self._prepare_run_for_start(request)
        worker = _ensure_worker(run, request.worker_id, role=request.role)

        created_session_ids_by_worker = {}
        try:
            probe = self.core.probe_capabilities(run)
            client = probe.client
            if probe.start_error is not None:
                mark_orchestration_start_failed(run, [worker], probe.start_error)
                self._persist_worker_update(run, worker)
                return SingleWorkerRunStartOutcome(run, EX_UNSUPPORTED, probe.start_error)

            session_id = request.session_id or worker.get("session_id")
            outcome = self.core.execute_worker(
                client,
                run,
                worker,
                request.prompt,
                probe.capabilities,
                session_id=session_id,
                agent=request.agent,
                model=request.model,
            )
            if request.cleanup:
                remember_created_worker_sessions(created_session_ids_by_worker, worker, outcome.created_session_ids)
        except OpenCodeApiError as error:
            mark_orchestration_start_failed(run, [worker], str(error))
            self._persist_worker_update(run, worker)
            return SingleWorkerRunStartOutcome(run, EX_UNAVAILABLE, f"api failure: {error}")
        if request.cleanup:
            cleanup_failure = self.core.cleanup_created_workers(client, run, created_session_ids_by_worker)
            if cleanup_failure is not None:
                return SingleWorkerRunStartOutcome(run, cleanup_failure.exit_code, cleanup_failure.error)
        if outcome.error is not None:
            self._persist_summary(run)
            return SingleWorkerRunStartOutcome(run, _exit_code_for_run(run), outcome.error)
        self._persist_summary(run)
        return SingleWorkerRunStartOutcome(run, _exit_code_for_run(run))

    def _prepare_run_for_start(self, request):
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
        def prepare(latest_run):
            if request.directory is not None:
                latest_run["directory"] = str(Path(request.directory).resolve())
            if request.server_url is not None:
                latest_run["server_url"] = request.server_url
            worker = _ensure_worker(latest_run, request.worker_id, role=request.role)
            worker["prompt"] = request.prompt
            latest_run["status"] = "active"
            _mark_worker_active(worker)

        return self._persist_mutation(run, prepare)

    def _persist_mutation(self, run, mutator):
        return persist_run_mutation(self.store, run, mutator, now=self.now)

    def _persist_worker_update(self, run, worker):
        persist_worker_update(self.store, run, worker, refresh_run_summary=_refresh_run_summary, now=self.now)

    def _persist_summary(self, run):
        persist_run_summary(self.store, run, refresh_run_summary=_refresh_run_summary, now=self.now)


def _server_default():
    return os.environ.get("OPENCODE_SERVER_URL") or os.environ.get("OPENCODE_SERVER") or DEFAULT_SERVER_URL


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
