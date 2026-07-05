from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import detect_capabilities
from opencode_session.run_start_policy import (
    blocking_execution_start_error,
    mark_orchestration_start_failed,
)
from opencode_session.run_store import RunStoreError
from opencode_session.worker_dependencies import analyze_worker_dependencies
from opencode_session.worker_execution import (
    cleanup_created_worker_sessions,
    execute_worker_attempts,
)
from opencode_session.worker_status import is_runnable_status
from opencode_session.worker_state import (
    EX_ABORTED,
    EX_BLOCKED,
    EX_PARTIAL,
    EX_TIMEOUT,
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
    ensure_worker as _ensure_orchestration_worker,
    exit_code_for_run as _exit_code_for_orchestration_run,
    mark_dependency_blocked as _mark_dependency_blocked,
    mark_worker_failed as _mark_worker_failed,
    refresh_run_summary as _refresh_worker_run_summary,
    worker_prompt as _worker_prompt,
    workers_in_dependency_order as _workers_in_dependency_order,
)


workers_in_dependency_order = _workers_in_dependency_order


@dataclass
class MultiWorkerRunStartRequest:
    name: str
    worker_id: str
    role: str
    directory: Optional[str] = None
    server_url: Optional[str] = None
    session_id: Optional[str] = None
    cleanup: bool = False


@dataclass
class MultiWorkerRunStartOutcome:
    run: dict
    exit_code: int
    error: Optional[str] = None


class MultiWorkerRunOrchestrationService:
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
        run = self.store.load_run(request.name)
        if request.directory is not None:
            run["directory"] = str(Path(request.directory).resolve())
        if request.server_url is not None:
            run["server_url"] = request.server_url
        if request.session_id is not None:
            worker = _ensure_orchestration_worker(run, request.worker_id, role=request.role)
            worker["session_id"] = request.session_id
        if not any(_worker_prompt(worker) for worker in run.get("workers", {}).values() if isinstance(worker, dict)):
            raise RunStoreError(f"run '{request.name}' has no worker prompts; pass --prompt or add workers with --prompt")
        return self._start_prompted_workers(run, cleanup=request.cleanup)

    def _start_prompted_workers(self, run, *, cleanup=False):
        created_session_ids_by_worker = {}
        client = None
        if _mark_invalid_dependency_graph_workers(run):
            refresh_orchestration_run_summary(run)
            self._save(run)
            return MultiWorkerRunStartOutcome(run, _exit_code_for_orchestration_run(run))

        try:
            client = self.client_factory(run["server_url"])
            capabilities = self.capability_detector(client)
            message = blocking_execution_start_error(capabilities)
            if message is not None:
                self._mark_prompted_workers_failed(run, message)
                return MultiWorkerRunStartOutcome(run, EX_UNSUPPORTED, message)

            run["status"] = "active"
            self._save(run)
            while True:
                workers = run.get("workers", {})
                ready_workers = _ready_prompted_workers(workers)
                if not ready_workers:
                    _mark_dependency_blocked_workers(run)
                    refresh_orchestration_run_summary(run)
                    self._save(run)
                    break

                attempt_workers = list(ready_workers)
                while attempt_workers:
                    retry_workers = []
                    for worker in attempt_workers:
                        if not worker.get("session_id"):
                            worker["session_id"] = None
                        outcome = execute_worker_attempts(
                            client,
                            run,
                            worker,
                            _worker_prompt(worker),
                            capabilities,
                            executor=self.executor,
                            now=self.now,
                            agent=worker.get("agent"),
                            model=worker.get("model"),
                            create_session=True,
                            stop_after_retry=True,
                            on_worker_update=lambda: self._save(run),
                        )
                        if cleanup:
                            for created_session_id in outcome.created_session_ids:
                                worker.setdefault("cleanup", {"requested": True, "deleted": False})
                                _created_session_ids(created_session_ids_by_worker, worker).append(created_session_id)
                        if outcome.kind == "retry":
                            retry_workers.append(worker)
                            continue
                        if outcome.error is not None:
                            _mark_dependency_blocked_workers(run)
                            refresh_orchestration_run_summary(run)
                            self._save(run)
                            cleanup_error = (
                                self._cleanup_created_workers(client, run, created_session_ids_by_worker)
                                if cleanup
                                else None
                            )
                            if cleanup_error is not None:
                                return cleanup_error
                            return MultiWorkerRunStartOutcome(
                                run,
                                _exit_code_for_orchestration_run(run),
                                outcome.error,
                            )

                    refresh_orchestration_run_summary(run)
                    if retry_workers:
                        self._save(run)
                        attempt_workers = retry_workers
                        continue
                    self._save(run)
                    break

                if not _ready_prompted_workers(run.get("workers", {})):
                    _mark_dependency_blocked_workers(run)
                    refresh_orchestration_run_summary(run)
                    self._save(run)
                    if not _pending_prompted_workers(run.get("workers", {})):
                        break
        except OpenCodeApiError as error:
            self._mark_prompted_workers_failed(run, str(error))
            cleanup_error = (
                self._cleanup_created_workers(client, run, created_session_ids_by_worker)
                if cleanup and client is not None and created_session_ids_by_worker
                else None
            )
            if cleanup_error is not None:
                return cleanup_error
            return MultiWorkerRunStartOutcome(run, EX_UNAVAILABLE, f"api failure: {error}")

        cleanup_error = self._cleanup_created_workers(client, run, created_session_ids_by_worker) if cleanup else None
        if cleanup_error is not None:
            return cleanup_error
        return MultiWorkerRunStartOutcome(run, _exit_code_for_orchestration_run(run))

    def _cleanup_created_workers(self, client, run, created_session_ids_by_worker):
        workers = run.get("workers", {})
        for worker_id, session_ids in created_session_ids_by_worker.items():
            worker = workers.get(worker_id)
            if not isinstance(worker, dict):
                continue
            cleanup_outcome = cleanup_created_worker_sessions(client, worker, session_ids)
            if cleanup_outcome.error is not None:
                _mark_worker_failed(worker, "api", f"disposable session cleanup failed: {cleanup_outcome.error}")
                refresh_orchestration_run_summary(run)
                self._save(run)
                return MultiWorkerRunStartOutcome(
                    run,
                    EX_UNAVAILABLE,
                    f"api failure: disposable session cleanup failed: {cleanup_outcome.error}",
                )
        self._save(run)
        return None

    def _mark_prompted_workers_failed(self, run, error):
        mark_orchestration_start_failed(run, _prompted_unfinished_workers(run), error)
        self._save(run)

    def _save(self, run):
        save_orchestration_run(self.store, run, now=self.now)


def refresh_orchestration_run_summary(run):
    _refresh_worker_run_summary(run, include_unprompted_when_no_prompts=True)


def save_orchestration_run(store, run, *, now=None):
    clock = now or _utc_now
    run["updated_at"] = clock()
    store.save_run(run)


def _ready_prompted_workers(workers):
    analysis = analyze_worker_dependencies(workers)
    return [workers[worker_id] for worker_id in analysis.ready_worker_ids]


def _pending_prompted_workers(workers):
    return [
        worker
        for worker in workers.values()
        if isinstance(worker, dict)
        and _worker_prompt(worker)
        and is_runnable_status(worker.get("status"))
    ]


def _prompted_unfinished_workers(run):
    return [
        worker
        for worker in run.get("workers", {}).values()
        if isinstance(worker, dict) and _worker_prompt(worker) and worker.get("status") not in {"done", "failed"}
    ]


def _mark_invalid_dependency_graph_workers(run):
    workers = run.get("workers", {})
    analysis = analyze_worker_dependencies(workers)
    for worker_id in sorted(analysis.invalid_graph_blockers_by_worker_id):
        worker = workers.get(worker_id)
        if isinstance(worker, dict):
            _mark_dependency_blocked(worker, analysis.invalid_graph_blockers_by_worker_id[worker_id])
    return bool(analysis.invalid_graph_blockers_by_worker_id)


def _mark_dependency_blocked_workers(run):
    workers = run.get("workers", {})
    analysis = analyze_worker_dependencies(workers)
    for worker_id in sorted(analysis.dependency_blockers_by_worker_id):
        worker = workers.get(worker_id)
        if isinstance(worker, dict):
            _mark_dependency_blocked(worker, analysis.dependency_blockers_by_worker_id[worker_id])


def _created_session_ids(created_session_ids_by_worker, worker):
    return created_session_ids_by_worker.setdefault(worker.get("id"), [])


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
