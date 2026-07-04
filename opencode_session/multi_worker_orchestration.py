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


EX_UNAVAILABLE = 69
EX_UNSUPPORTED = 70
EX_TIMEOUT = 124
EX_PARTIAL = 1
EX_BLOCKED = 75
EX_ABORTED = 130


@dataclass
class MultiWorkerRunStartRequest:
    name: str
    worker_id: str
    role: str
    directory: Optional[str] = None
    server_url: Optional[str] = None
    session_id: Optional[str] = None


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
        return self._start_prompted_workers(run)

    def _start_prompted_workers(self, run):
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
                self._save(run)

                attempt_workers = list(ready_workers)
                while attempt_workers:
                    retry_workers = []
                    for worker in attempt_workers:
                        worker["status"] = "active"
                        worker["next_eligible_action"] = "wait"
                        worker["timeout_started_at"] = self.now() if worker.get("timeout_seconds") else None
                        try:
                            result = _call_worker_with_timeout(
                                worker,
                                lambda worker=worker: self.executor(
                                    client,
                                    worker["session_id"],
                                    _worker_prompt(worker),
                                    capabilities,
                                ),
                            )
                        except TimeoutExpired:
                            reason = _worker_timeout_reason(worker)
                            if _schedule_worker_retry(worker, "timeout", reason):
                                worker["timed_out_at"] = self.now()
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

        return MultiWorkerRunStartOutcome(run, _exit_code_for_orchestration_run(run))

    def _mark_prompted_workers_failed(self, run, error):
        run["status"] = "failed"
        for worker in run.get("workers", {}).values():
            if isinstance(worker, dict) and _worker_prompt(worker) and worker.get("status") not in {"done", "failed"}:
                _mark_worker_failed(worker, "api", error)
        self._save(run)

    def _save(self, run):
        save_orchestration_run(self.store, run, now=self.now)


def _ensure_orchestration_worker(run, worker_id, *, role):
    workers = run.setdefault("workers", {})
    worker = workers.get(worker_id)
    if not isinstance(worker, dict):
        worker = {}
    worker.setdefault("id", worker_id)
    worker.setdefault("role", role)
    worker.setdefault("session_id", None)
    worker.setdefault("agent", None)
    worker.setdefault("model", None)
    worker.setdefault("dependencies", [])
    worker.setdefault("prompt_ids", [])
    worker.setdefault("retry_count", 0)
    worker.setdefault("retry_limit", 0)
    worker.setdefault("retryable_failures", [])
    worker.setdefault("timeout_seconds", None)
    worker.setdefault("timeout_policy", "timeout")
    worker.setdefault("timeout_started_at", None)
    worker.setdefault("timed_out_at", None)
    worker.setdefault("failure_category", None)
    worker.setdefault("failure_reason", None)
    worker.setdefault("last_failure_category", None)
    worker.setdefault("last_failure_reason", None)
    worker.setdefault("next_eligible_action", "start")
    worker.setdefault("blockers", [])
    worker.setdefault("output_refs", [])
    if not worker.get("role"):
        worker["role"] = role
    worker["id"] = worker_id
    workers[worker_id] = worker
    return worker


def refresh_orchestration_run_summary(run):
    workers = run.get("workers", {})
    prompted_workers = [worker for worker in workers.values() if isinstance(worker, dict) and _worker_prompt(worker)]
    status_workers = prompted_workers or [worker for worker in workers.values() if isinstance(worker, dict)]
    run["output_refs"] = _worker_output_refs_in_dependency_order(workers)
    if not status_workers:
        return
    statuses = {worker.get("status") for worker in status_workers}
    if statuses == {"done"}:
        run["status"] = "done"
    elif any(status == "failed" for status in statuses):
        run["status"] = "failed"
    elif any(status == "aborted" for status in statuses):
        run["status"] = "aborted"
    elif any(status == "timeout" for status in statuses):
        run["status"] = "timeout"
    elif any(status == "blocked" for status in statuses):
        run["status"] = "blocked"
    elif any(status == "active" for status in statuses):
        run["status"] = "active"
    else:
        run["status"] = "queued"


def workers_in_dependency_order(workers):
    ordered = []
    visited = set()
    visiting = set()

    def visit(worker_id):
        if worker_id in visited or worker_id in visiting:
            return
        visiting.add(worker_id)
        worker = workers.get(worker_id)
        if isinstance(worker, dict):
            for dependency in worker.get("dependencies", []):
                visit(dependency)
            ordered.append(worker)
        visiting.remove(worker_id)
        visited.add(worker_id)

    for worker_id in sorted(workers):
        visit(worker_id)
    return ordered


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


def _worker_output_refs_in_dependency_order(workers):
    ordered = []
    for worker in workers_in_dependency_order(workers):
        worker_id = worker.get("id")
        if worker.get("status") != "done":
            continue
        for output_ref in worker.get("output_refs", []):
            if isinstance(output_ref, str) and output_ref.startswith("assistant:"):
                ordered.append(f"{worker_id}:{output_ref.split(':', 1)[1]}")
            else:
                ordered.append(f"{worker_id}:{output_ref}")
    return ordered


def _mark_worker_failed(worker, category, reason):
    worker["status"] = "failed"
    worker["error"] = reason
    worker["failure_category"] = category
    worker["failure_reason"] = reason
    worker["last_failure_category"] = category
    worker["last_failure_reason"] = reason
    worker["next_eligible_action"] = "retry" if _worker_retry_available(worker, category) else "none"


def _schedule_worker_retry(worker, category, reason):
    if not _worker_retry_available(worker, category):
        return False
    worker["retry_count"] = int(worker.get("retry_count") or 0) + 1
    worker["status"] = "active"
    worker["failure_category"] = None
    worker["failure_reason"] = None
    worker["last_failure_category"] = category
    worker["last_failure_reason"] = reason
    worker["next_eligible_action"] = "retry"
    return True


def _worker_retry_available(worker, category):
    retryable = set(worker.get("retryable_failures") or [])
    if category not in retryable and "all" not in retryable:
        return False
    try:
        retry_count = int(worker.get("retry_count") or 0)
        retry_limit = int(worker.get("retry_limit") or 0)
    except (TypeError, ValueError):
        return False
    return retry_count < retry_limit


def _call_worker_with_timeout(worker, callback):
    timeout = worker.get("timeout_seconds")
    return TimeoutDeadline(timeout).run(callback)


def _worker_timeout_reason(worker):
    return f"worker timed out after {_format_timeout(worker.get('timeout_seconds'))}s"


def _format_timeout(timeout):
    return str(timeout)


def _mark_worker_timeout(worker, reason, now):
    status = worker.get("timeout_policy") or "timeout"
    worker["status"] = status
    worker["error"] = reason
    worker["failure_category"] = "timeout"
    worker["failure_reason"] = reason
    worker["last_failure_category"] = "timeout"
    worker["last_failure_reason"] = reason
    worker["timed_out_at"] = now()
    worker["output_refs"] = []
    if status == "blocked":
        worker["blockers"] = ["timeout"]
        worker["next_eligible_action"] = "resolve_blocker"
    else:
        worker["next_eligible_action"] = "none"


def _apply_worker_result(worker, result):
    worker["result"] = result
    worker["status"] = result["status"]
    worker["failure_category"] = None
    worker["failure_reason"] = None
    worker["next_eligible_action"] = "collect" if result["status"] == "done" else "none"
    assistant_message_id = result["message_ids"].get("assistant")
    worker["output_refs"] = [f"assistant:{assistant_message_id}"] if result["status"] == "done" and assistant_message_id else []


def _exit_code_for_orchestration_run(run):
    status = run.get("status")
    if status == "done":
        return 0
    if status == "timeout":
        return EX_TIMEOUT
    if status == "blocked":
        return EX_BLOCKED
    if status == "aborted":
        return EX_ABORTED
    if _has_partial_worker_success(run):
        return EX_PARTIAL
    return EX_UNAVAILABLE


def _has_partial_worker_success(run):
    workers = [worker for worker in (run.get("workers") or {}).values() if isinstance(worker, dict) and _worker_prompt(worker)]
    if not workers:
        return False
    statuses = {worker.get("status") for worker in workers}
    return "done" in statuses and any(status in {"failed", "blocked", "aborted", "timeout"} for status in statuses)


def _worker_prompt(worker):
    prompt = worker.get("prompt")
    if prompt is None:
        return None
    return str(prompt)


def _session_value(session, *names):
    session = session if isinstance(session, dict) else {}
    for name in names:
        value = session.get(name)
        if value is not None:
            return value
    info = session.get("info")
    if isinstance(info, dict):
        for name in names:
            value = info.get(name)
            if value is not None:
                return value
    return None


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
