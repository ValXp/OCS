from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import detect_capabilities
from opencode_session.run_persistence import (
    persist_run_mutation,
    persist_run_summary,
    persist_worker_transitions,
    persist_worker_updates,
)
from opencode_session.run_start_core import RunStartCore, remember_created_worker_sessions
from opencode_session.run_start_policy import mark_orchestration_start_failed
from opencode_session.run_store import RunStoreError
from opencode_session.worker_execution import RETRY_SCHEDULED
from opencode_session.worker_dependencies import analyze_worker_dependencies
from opencode_session.worker_model import is_executable_worker
from opencode_session.worker_state import (
    EX_ABORTED,
    EX_BLOCKED,
    EX_PARTIAL,
    EX_TIMEOUT,
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
    WorkerTransition,
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


@dataclass(frozen=True)
class DependencyScheduleTick:
    ready_worker_ids: tuple
    dependency_blocked_transitions: tuple
    has_pending_workers: bool
    blockers_by_worker_id: dict


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
        schedule_tick = self._persist_schedule_tick(run)
        if schedule_tick.blockers_by_worker_id or not schedule_tick.ready_worker_ids:
            if not schedule_tick.ready_worker_ids:
                return DependencyOrderedSerialRunStartOutcome(run, _exit_code_for_orchestration_run(run))

        try:
            probe = self.core.probe_capabilities(run)
            client = probe.client
            if probe.start_error is not None:
                self._mark_prompted_workers_failed(run, probe.start_error)
                return DependencyOrderedSerialRunStartOutcome(run, EX_UNSUPPORTED, probe.start_error)

            run["status"] = "active"
            self._persist_mutation(run, _mark_run_active)
            while schedule_tick.ready_worker_ids:
                ready_workers = _workers_by_ids(run.get("workers", {}), schedule_tick.ready_worker_ids)
                outcome = self._execute_ready_workers_serially(
                    client,
                    run,
                    ready_workers,
                    probe.capabilities,
                    cleanup=cleanup,
                    created_session_ids_by_worker=created_session_ids_by_worker,
                    execution_policy=execution_policy,
                )
                schedule_tick = self._persist_schedule_tick(run)
                if outcome is not None:
                    if first_error_outcome is None:
                        first_error_outcome = outcome
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

    def _persist_schedule_tick(self, run):
        tick = schedule_dependency_ordered_tick(run.get("workers", {}))
        if tick.dependency_blocked_transitions:
            persist_worker_transitions(
                self.store,
                run,
                tick.dependency_blocked_transitions,
                refresh_run_summary=refresh_orchestration_run_summary,
                now=self.now,
            )
        else:
            self._persist_summary(run)
        return tick

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


def schedule_dependency_ordered_tick(workers):
    workers = workers if isinstance(workers, dict) else {}
    analysis = analyze_worker_dependencies(workers)
    blocked_worker_ids = set(analysis.blockers_by_worker_id)
    return DependencyScheduleTick(
        ready_worker_ids=analysis.ready_worker_ids,
        dependency_blocked_transitions=tuple(
            _dependency_blocked_transition(workers[worker_id], analysis.blockers_by_worker_id[worker_id])
            for worker_id in sorted(blocked_worker_ids)
            if isinstance(workers.get(worker_id), dict)
        ),
        has_pending_workers=bool(_pending_prompted_worker_ids(workers, blocked_worker_ids=blocked_worker_ids)),
        blockers_by_worker_id=analysis.blockers_by_worker_id,
    )


def _workers_by_ids(workers, worker_ids):
    return [workers[worker_id] for worker_id in worker_ids if isinstance(workers.get(worker_id), dict)]


def _pending_prompted_workers(workers):
    return [
        workers[worker_id]
        for worker_id in _pending_prompted_worker_ids(workers)
    ]


def _pending_prompted_worker_ids(workers, *, blocked_worker_ids=()):
    blocked_worker_ids = set(blocked_worker_ids)
    return tuple(
        worker_id
        for worker_id in sorted(workers)
        if worker_id not in blocked_worker_ids
        and isinstance(workers.get(worker_id), dict)
        and is_executable_worker(workers[worker_id])
    )


def _dependency_blocked_transition(worker, blockers):
    blocked_worker = deepcopy(worker)
    _mark_dependency_blocked(blocked_worker, blockers)
    return WorkerTransition.replace_with_worker(blocked_worker)


def _normalize_execution_policy(policy):
    normalized = (policy or EXECUTION_POLICY_FAIL_FAST).replace("-", "_")
    if normalized not in EXECUTION_POLICIES:
        raise RunStoreError(f"unsupported execution policy '{policy}'")
    return normalized


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
