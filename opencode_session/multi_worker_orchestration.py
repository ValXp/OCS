from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import detect_capabilities
from opencode_session.run_persistence import persist_run_mutation, persist_run_summary, persist_worker_updates
from opencode_session.run_start_core import RunStartCore, remember_created_worker_sessions
from opencode_session.run_start_policy import mark_orchestration_start_failed
from opencode_session.run_store import RunStoreError
from opencode_session.worker_execution import RETRY_SCHEDULED
from opencode_session.worker_dependencies import analyze_worker_dependencies
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
    refresh_run_summary as _refresh_worker_run_summary,
    worker_prompt as _worker_prompt,
    workers_in_dependency_order as _workers_in_dependency_order,
)


workers_in_dependency_order = _workers_in_dependency_order

EXECUTION_POLICY_FAIL_FAST = "fail_fast"
EXECUTION_POLICY_CONTINUE = "continue"
EXECUTION_POLICIES = {EXECUTION_POLICY_FAIL_FAST, EXECUTION_POLICY_CONTINUE}


@dataclass
class DependencyOrderedSerialRunStartRequest:
    name: str
    worker_id: str
    role: str
    directory: Optional[str] = None
    server_url: Optional[str] = None
    session_id: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    execution_policy: str = EXECUTION_POLICY_FAIL_FAST
    cleanup: bool = False


@dataclass
class DependencyOrderedSerialRunStartOutcome:
    run: dict
    exit_code: int
    error: Optional[str] = None


class DependencyOrderedSerialRunOrchestrationService:
    """Run prompted workers one at a time after their dependencies are satisfied."""

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
            refresh_run_summary=refresh_orchestration_run_summary,
            client_factory=self.client_factory,
            capability_detector=self.capability_detector,
            executor=self.executor,
            now=self.now,
        )

    def start(self, request):
        run = self.store.load_run(request.name)
        execution_policy = _normalize_execution_policy(request.execution_policy)

        def prepare(latest_run):
            if request.directory is not None:
                latest_run["directory"] = str(Path(request.directory).resolve())
            if request.server_url is not None:
                latest_run["server_url"] = request.server_url
            if request.session_id is not None or request.agent is not None or request.model is not None:
                worker = _ensure_orchestration_worker(latest_run, request.worker_id, role=request.role)
                if request.session_id is not None:
                    worker["session_id"] = request.session_id
                if request.agent is not None:
                    worker["agent"] = request.agent
                if request.model is not None:
                    worker["model"] = request.model

        run = self._persist_mutation(run, prepare)
        if not any(_worker_prompt(worker) for worker in run.get("workers", {}).values() if isinstance(worker, dict)):
            raise RunStoreError(f"run '{request.name}' has no worker prompts; pass --prompt or add workers with --prompt")
        return self._start_prompted_workers(run, cleanup=request.cleanup, execution_policy=execution_policy)

    def _start_prompted_workers(self, run, *, cleanup=False, execution_policy=EXECUTION_POLICY_FAIL_FAST):
        created_session_ids_by_worker = {}
        client = None
        first_error_outcome = None
        dependency_analysis = self._mark_and_persist_dependency_blocked_workers(run)
        if dependency_analysis.blockers_by_worker_id or not dependency_analysis.ready_worker_ids:
            if not dependency_analysis.ready_worker_ids:
                return DependencyOrderedSerialRunStartOutcome(run, _exit_code_for_orchestration_run(run))

        try:
            probe = self.core.probe_capabilities(run)
            client = probe.client
            if probe.start_error is not None:
                self._mark_prompted_workers_failed(run, probe.start_error)
                return DependencyOrderedSerialRunStartOutcome(run, EX_UNSUPPORTED, probe.start_error)

            run["status"] = "active"
            self._persist_mutation(run, _mark_run_active)
            while True:
                ready_workers = _ready_prompted_workers(run.get("workers", {}))
                if not ready_workers:
                    self._mark_and_persist_dependency_blocked_workers(run)
                    break

                outcome = self._execute_ready_workers_serially(
                    client,
                    run,
                    ready_workers,
                    probe.capabilities,
                    cleanup=cleanup,
                    created_session_ids_by_worker=created_session_ids_by_worker,
                    execution_policy=execution_policy,
                )
                if outcome is not None:
                    if first_error_outcome is None:
                        first_error_outcome = outcome
                    self._mark_and_persist_dependency_blocked_workers(run)
                    if execution_policy == EXECUTION_POLICY_FAIL_FAST:
                        cleanup_error = (
                            self._cleanup_created_workers(client, run, created_session_ids_by_worker) if cleanup else None
                        )
                        if cleanup_error is not None:
                            return cleanup_error
                        return DependencyOrderedSerialRunStartOutcome(
                            run,
                            _exit_code_for_orchestration_run(run),
                            outcome.error,
                        )

                if not _ready_prompted_workers(run.get("workers", {})):
                    self._mark_and_persist_dependency_blocked_workers(run)
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
            return DependencyOrderedSerialRunStartOutcome(run, EX_UNAVAILABLE, f"api failure: {error}")

        cleanup_error = self._cleanup_created_workers(client, run, created_session_ids_by_worker) if cleanup else None
        if cleanup_error is not None:
            return cleanup_error
        return DependencyOrderedSerialRunStartOutcome(
            run,
            _exit_code_for_orchestration_run(run),
            first_error_outcome.error if first_error_outcome is not None else None,
        )

    def _execute_ready_workers_serially(
        self,
        client,
        run,
        ready_workers,
        capabilities,
        *,
        cleanup,
        created_session_ids_by_worker,
        execution_policy,
    ):
        first_error_outcome = None
        attempt_workers = list(ready_workers)
        while attempt_workers:
            retry_workers = []
            for worker in attempt_workers:
                outcome = self.core.execute_worker(
                    client,
                    run,
                    worker,
                    _worker_prompt(worker),
                    capabilities,
                    agent=worker.get("agent"),
                    model=worker.get("model"),
                    stop_after_retry=True,
                )
                if cleanup:
                    remember_created_worker_sessions(
                        created_session_ids_by_worker,
                        worker,
                        outcome.created_session_ids,
                    )
                if outcome.kind == RETRY_SCHEDULED:
                    retry_workers.append(worker)
                    continue
                if outcome.error is not None:
                    if first_error_outcome is None:
                        first_error_outcome = outcome
                    if execution_policy == EXECUTION_POLICY_FAIL_FAST:
                        return outcome
            self._persist_summary(run)
            attempt_workers = retry_workers
        return first_error_outcome

    def _cleanup_created_workers(self, client, run, created_session_ids_by_worker):
        cleanup_failure = self.core.cleanup_created_workers(client, run, created_session_ids_by_worker)
        if cleanup_failure is not None:
            return DependencyOrderedSerialRunStartOutcome(run, cleanup_failure.exit_code, cleanup_failure.error)
        return None

    def _mark_prompted_workers_failed(self, run, error):
        workers = _pending_prompted_workers(run.get("workers", {}))
        mark_orchestration_start_failed(run, workers, error)
        self._persist_workers(run, workers)

    def _mark_and_persist_dependency_blocked_workers(self, run):
        analysis = _mark_dependency_blocked_workers(run)
        blocked_workers = [
            run.get("workers", {}).get(worker_id)
            for worker_id in sorted(analysis.blockers_by_worker_id)
            if isinstance(run.get("workers", {}).get(worker_id), dict)
        ]
        if blocked_workers:
            self._persist_workers(run, blocked_workers)
        else:
            self._persist_summary(run)
        return analysis

    def _persist_mutation(self, run, mutator):
        return persist_run_mutation(self.store, run, mutator, now=self.now)

    def _persist_worker_update(self, run, worker):
        persist_worker_updates(
            self.store,
            run,
            [worker],
            refresh_run_summary=refresh_orchestration_run_summary,
            now=self.now,
        )

    def _persist_workers(self, run, workers):
        persist_worker_updates(
            self.store,
            run,
            workers,
            refresh_run_summary=refresh_orchestration_run_summary,
            now=self.now,
        )

    def _persist_summary(self, run):
        persist_run_summary(
            self.store,
            run,
            refresh_run_summary=refresh_orchestration_run_summary,
            now=self.now,
        )


def refresh_orchestration_run_summary(run):
    _refresh_worker_run_summary(run, include_unprompted_when_no_prompts=True)


def _mark_run_active(run):
    run["status"] = "active"


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


def _mark_dependency_blocked_workers(run):
    workers = run.get("workers", {})
    analysis = analyze_worker_dependencies(workers)
    for worker_id in sorted(analysis.blockers_by_worker_id):
        worker = workers.get(worker_id)
        if isinstance(worker, dict):
            _mark_dependency_blocked(worker, analysis.blockers_by_worker_id[worker_id])
    return analysis


def _normalize_execution_policy(policy):
    normalized = (policy or EXECUTION_POLICY_FAIL_FAST).replace("-", "_")
    if normalized not in EXECUTION_POLICIES:
        raise RunStoreError(f"unsupported execution policy '{policy}'")
    return normalized


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
