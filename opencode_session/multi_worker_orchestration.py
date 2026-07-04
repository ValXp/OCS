from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocking_execution import (
    BlockingProviderFailure,
    blocking_execution_strategy,
    execute_blocking_prompt,
    unsupported_blocking_execution_message,
)
from opencode_session.capabilities import detect_capabilities
from opencode_session.run_store import RunStoreError
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired
from opencode_session.worker_state import (
    EX_ABORTED,
    EX_BLOCKED,
    EX_PARTIAL,
    EX_TIMEOUT,
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
    apply_worker_result as _apply_worker_result,
    create_isolated_timeout_retry_session as _create_isolated_timeout_retry_session,
    ensure_worker as _ensure_orchestration_worker,
    exit_code_for_run as _exit_code_for_orchestration_run,
    mark_worker_failed as _mark_worker_failed,
    mark_worker_timeout as _mark_worker_timeout,
    refresh_run_summary as _refresh_worker_run_summary,
    schedule_worker_retry as _schedule_worker_retry,
    session_value as _session_value,
    worker_prompt as _worker_prompt,
    worker_timeout_reason as _worker_timeout_reason,
    workers_in_dependency_order,
)


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
        try:
            client = self.client_factory(run["server_url"])
            capabilities = self.capability_detector(client)
            if blocking_execution_strategy(capabilities) is None:
                message = unsupported_blocking_execution_message()
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

                for worker in ready_workers:
                    worker["status"] = "active"
                    worker["next_eligible_action"] = "wait"
                self._save(run)

                for worker in ready_workers:
                    if not worker.get("session_id"):
                        create_response = client.create_session_response(
                            run["directory"], agent=worker.get("agent"), model=worker.get("model")
                        )
                        worker["session_id"] = _session_value(create_response.data, "id", "sessionID", "sessionId")
                        if cleanup and worker["session_id"]:
                            worker["cleanup"] = {"requested": True, "deleted": False}
                            _created_session_ids(created_session_ids_by_worker, worker).append(worker["session_id"])
                self._save(run)

                attempt_workers = list(ready_workers)
                while attempt_workers:
                    retry_workers = []
                    for worker in attempt_workers:
                        worker["status"] = "active"
                        worker["next_eligible_action"] = "wait"
                        worker["timeout_started_at"] = self.now() if worker.get("timeout_seconds") else None
                        attempt_session_id = worker["session_id"]
                        try:
                            result = _call_worker_with_timeout(
                                worker,
                                lambda worker=worker, attempt_session_id=attempt_session_id: self.executor(
                                    client,
                                    attempt_session_id,
                                    _worker_prompt(worker),
                                    capabilities,
                                ),
                            )
                        except TimeoutExpired:
                            reason = _worker_timeout_reason(worker)
                            if _schedule_worker_retry(worker, "timeout", reason):
                                timed_out_at = self.now()
                                worker["timed_out_at"] = timed_out_at
                                retry_session_id = _create_isolated_timeout_retry_session(
                                    client,
                                    run,
                                    worker,
                                    reason,
                                    timed_out_at,
                                )
                                if cleanup:
                                    worker.setdefault("cleanup", {"requested": True, "deleted": False})
                                    _created_session_ids(created_session_ids_by_worker, worker).append(retry_session_id)
                                retry_workers.append(worker)
                                continue
                            _mark_worker_timeout(worker, reason, self.now)
                            _mark_dependency_blocked_workers(run)
                            refresh_orchestration_run_summary(run)
                            self._save(run)
                            return MultiWorkerRunStartOutcome(run, _exit_code_for_orchestration_run(run), reason)
                        except OpenCodeApiError as error:
                            if _schedule_worker_retry(worker, "api", str(error)):
                                retry_workers.append(worker)
                                continue
                            _mark_worker_failed(worker, "api", str(error))
                            _mark_dependency_blocked_workers(run)
                            refresh_orchestration_run_summary(run)
                            self._save(run)
                            return MultiWorkerRunStartOutcome(
                                run,
                                _exit_code_for_orchestration_run(run),
                                f"api failure: {error}",
                            )
                        except BlockingProviderFailure as error:
                            if error.prompt_id is not None:
                                worker["prompt_ids"] = [error.prompt_id]
                            if _schedule_worker_retry(worker, "provider", str(error)):
                                retry_workers.append(worker)
                                continue
                            _mark_worker_failed(worker, "provider", str(error))
                            _mark_dependency_blocked_workers(run)
                            refresh_orchestration_run_summary(run)
                            self._save(run)
                            return MultiWorkerRunStartOutcome(
                                run,
                                _exit_code_for_orchestration_run(run),
                                f"provider failure: {error}",
                            )
                        prompt_id = result["message_ids"].get("user")
                        if prompt_id is not None:
                            worker["prompt_ids"] = [prompt_id]
                        _apply_worker_result(worker, result)

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
            cleanup = worker.setdefault("cleanup", {"requested": True, "deleted": False})
            deleted_session_ids = []
            for session_id in session_ids:
                try:
                    client.delete_session(session_id)
                except OpenCodeApiError as error:
                    cleanup["error"] = str(error)
                    if len(deleted_session_ids) > 1:
                        cleanup["sessions"] = deleted_session_ids
                    _mark_worker_failed(worker, "api", f"disposable session cleanup failed: {error}")
                    refresh_orchestration_run_summary(run)
                    self._save(run)
                    return MultiWorkerRunStartOutcome(
                        run,
                        EX_UNAVAILABLE,
                        f"api failure: disposable session cleanup failed: {error}",
                    )
                deleted_session_ids.append(session_id)
            if deleted_session_ids:
                cleanup["deleted"] = True
                if len(deleted_session_ids) > 1:
                    cleanup["sessions"] = deleted_session_ids
        self._save(run)
        return None

    def _mark_prompted_workers_failed(self, run, error):
        run["status"] = "failed"
        for worker in run.get("workers", {}).values():
            if isinstance(worker, dict) and _worker_prompt(worker) and worker.get("status") not in {"done", "failed"}:
                _mark_worker_failed(worker, "api", error)
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
    ready = []
    for worker_id in sorted(workers):
        worker = workers[worker_id]
        if not isinstance(worker, dict) or not _worker_prompt(worker):
            continue
        if worker.get("status") in {"done", "failed", "aborted", "timeout"}:
            continue
        if _dependencies_done(worker, workers):
            ready.append(worker)
    return ready


def _pending_prompted_workers(workers):
    return [
        worker
        for worker in workers.values()
        if isinstance(worker, dict)
        and _worker_prompt(worker)
        and worker.get("status") not in {"done", "failed", "aborted", "timeout", "blocked"}
    ]


def _mark_dependency_blocked_workers(run):
    workers = run.get("workers", {})
    for worker in workers.values():
        if not isinstance(worker, dict) or not _worker_prompt(worker):
            continue
        if worker.get("status") in {"done", "failed", "aborted", "timeout"}:
            continue
        if _dependencies_failed(worker, workers):
            worker["status"] = "blocked"
            worker["blockers"] = [f"dependency:{dependency}" for dependency in worker.get("dependencies", [])]
            worker["next_eligible_action"] = "resolve_blocker"


def _dependencies_done(worker, workers):
    for dependency in worker.get("dependencies", []):
        dependency_worker = workers.get(dependency)
        if not isinstance(dependency_worker, dict) or dependency_worker.get("status") != "done":
            return False
    return True


def _dependencies_failed(worker, workers):
    for dependency in worker.get("dependencies", []):
        dependency_worker = workers.get(dependency)
        if not isinstance(dependency_worker, dict):
            return True
        if dependency_worker.get("status") in {"failed", "aborted", "timeout", "blocked"}:
            return True
    return False


def _created_session_ids(created_session_ids_by_worker, worker):
    return created_session_ids_by_worker.setdefault(worker.get("id"), [])


def _call_worker_with_timeout(worker, callback):
    timeout = worker.get("timeout_seconds")
    return TimeoutDeadline(timeout).run(callback)


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
