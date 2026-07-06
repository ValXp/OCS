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
)
from opencode_session.run_start_core import RunStartCore, remember_created_worker_sessions
from opencode_session.run_start_policy import mark_orchestration_start_failed
from opencode_session.run_store import RunStoreError
from opencode_session.schema_common import RunRecord
from opencode_session.worker_execution import RETRY_SCHEDULED, WorkerExecutionOutcome
from opencode_session.worker_dependencies import analyze_worker_dependencies
from opencode_session.worker_domain import WorkerTransition, is_executable_worker
from opencode_session.worker_state import (
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
    ensure_worker as _ensure_orchestration_worker,
    exit_code_for_run as _exit_code_for_orchestration_run,
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
    run: RunRecord
    exit_code: int
    error: Optional[str] = None


@dataclass(frozen=True)
class DependencyScheduleTick:
    ready_worker_ids: tuple
    dependency_blocked_transitions: tuple
    has_pending_workers: bool
    blockers_by_worker_id: dict


class DependencySchedulePlanner:
    def plan(self, workers):
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


class DurableDependencyScheduler:
    def __init__(self, store, *, now, planner=None):
        self.store = store
        self.now = now
        self.planner = planner or DependencySchedulePlanner()

    def persist_next_tick(self, run):
        tick = self.planner.plan(run.get("workers", {}))
        if tick.dependency_blocked_transitions:
            result = persist_worker_transitions(
                self.store,
                run,
                tick.dependency_blocked_transitions,
                refresh_run_summary=refresh_orchestration_run_summary,
                now=self.now,
            )
            return result.run, tick
        else:
            run = persist_run_summary(
                self.store,
                run,
                refresh_run_summary=refresh_orchestration_run_summary,
                now=self.now,
            )
        return run, tick


@dataclass
class WorkerWaveExecutionOutcome:
    run: RunRecord
    first_error_outcome: Optional[WorkerExecutionOutcome] = None
    fail_fast_outcome: Optional[WorkerExecutionOutcome] = None


class DisposableSessionTracker:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.created_session_ids_by_worker = {}

    def remember_worker_outcome(self, run, fallback_worker, outcome):
        if not self.enabled:
            return
        current_worker = (outcome.run or run).get("workers", {}).get(fallback_worker.get("id"), fallback_worker)
        remember_created_worker_sessions(
            self.created_session_ids_by_worker,
            current_worker,
            outcome.created_session_ids,
        )

    def cleanup(self, core, client, run):
        if not self.enabled or client is None or not self.created_session_ids_by_worker:
            return run, None
        cleanup_result = core.cleanup_created_workers(client, run, self.created_session_ids_by_worker)
        if cleanup_result.error is not None:
            return cleanup_result.run, DependencyOrderedSerialRunStartOutcome(
                cleanup_result.run,
                cleanup_result.exit_code,
                cleanup_result.error,
            )
        return cleanup_result.run, None


class ReadyWorkerWaveExecutor:
    def __init__(self, core, *, persist_summary):
        self.core = core
        self.persist_summary = persist_summary

    def execute_wave(
        self,
        run,
        ready_worker_ids,
        client,
        capabilities,
        *,
        session_tracker,
        execution_policy,
    ):
        first_error_outcome = None
        attempt_workers = _workers_by_ids(run.get("workers", {}), ready_worker_ids)
        while attempt_workers:
            retry_workers = []
            for worker in attempt_workers:
                outcome = self._execute_single_worker(client, run, worker, capabilities)
                run = outcome.run or run
                current_worker = run.get("workers", {}).get(worker.get("id"), worker)
                session_tracker.remember_worker_outcome(run, current_worker, outcome)
                if outcome.kind == RETRY_SCHEDULED:
                    retry_workers.append(current_worker)
                    continue
                if outcome.error is not None:
                    if first_error_outcome is None:
                        first_error_outcome = outcome
                    if execution_policy == EXECUTION_POLICY_FAIL_FAST:
                        return WorkerWaveExecutionOutcome(run, first_error_outcome, outcome)
            run = self.persist_summary(run)
            attempt_workers = retry_workers
        return WorkerWaveExecutionOutcome(run, first_error_outcome)

    def _execute_single_worker(self, client, run, worker, capabilities):
        return self.core.execute_worker(
            client,
            run,
            worker,
            _worker_prompt(worker),
            capabilities,
            agent=worker.get("agent"),
            model=worker.get("model"),
            stop_after_retry=True,
        )


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
        self.scheduler = DurableDependencyScheduler(self.store, now=self.now)
        self.core = RunStartCore(
            persist_worker_transition=self._persist_worker_transition,
            refresh_run_summary=refresh_orchestration_run_summary,
            client_factory=self.client_factory,
            capability_detector=self.capability_detector,
            executor=self.executor,
            now=self.now,
        )
        self.wave_executor = ReadyWorkerWaveExecutor(self.core, persist_summary=self._persist_summary)

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
        run, schedule_tick = self.scheduler.persist_next_tick(run)
        early_outcome = self._outcome_if_no_ready_workers(run, schedule_tick)
        if early_outcome is not None:
            return early_outcome

        client = None
        session_tracker = DisposableSessionTracker(cleanup)
        first_error_outcome = None

        try:
            probe_outcome = self._probe_execution(run)
            if probe_outcome.start_error is not None:
                return self._unsupported_probe_outcome(run, probe_outcome)

            client = probe_outcome.client
            run = self._activate_run(run)
            execution_outcome = self._execute_ready_waves(
                run,
                schedule_tick,
                client,
                probe_outcome.capabilities,
                session_tracker,
                execution_policy=execution_policy,
            )
            run = execution_outcome.run
            first_error_outcome = execution_outcome.first_error_outcome
            if execution_outcome.fail_fast_outcome is not None:
                run, cleanup_error = session_tracker.cleanup(self.core, client, run)
                return cleanup_error or self._execution_outcome(run, execution_outcome.fail_fast_outcome)
        except OpenCodeApiError as error:
            run = self._mark_prompted_workers_failed(run, str(error))
            run, cleanup_error = session_tracker.cleanup(self.core, client, run)
            if cleanup_error:
                return cleanup_error
            return DependencyOrderedSerialRunStartOutcome(run, EX_UNAVAILABLE, f"api failure: {error}")

        run, cleanup_error = session_tracker.cleanup(self.core, client, run)
        return cleanup_error or self._execution_outcome(run, first_error_outcome)

    def _execute_ready_waves(
        self,
        run,
        schedule_tick,
        client,
        capabilities,
        session_tracker,
        *,
        execution_policy,
    ):
        first_error_outcome = None
        while schedule_tick.ready_worker_ids:
            wave_outcome = self.wave_executor.execute_wave(
                run,
                schedule_tick.ready_worker_ids,
                client,
                capabilities,
                session_tracker=session_tracker,
                execution_policy=execution_policy,
            )
            run = wave_outcome.run
            run, schedule_tick = self.scheduler.persist_next_tick(run)
            if first_error_outcome is None:
                first_error_outcome = wave_outcome.first_error_outcome
            if wave_outcome.fail_fast_outcome is not None:
                return WorkerWaveExecutionOutcome(run, first_error_outcome, wave_outcome.fail_fast_outcome)
        return WorkerWaveExecutionOutcome(run, first_error_outcome)

    def _outcome_if_no_ready_workers(self, run, schedule_tick):
        if schedule_tick.ready_worker_ids:
            return None
        return DependencyOrderedSerialRunStartOutcome(run, _exit_code_for_orchestration_run(run))

    def _probe_execution(self, run):
        return self.core.probe_capabilities(run)

    def _unsupported_probe_outcome(self, run, probe_outcome):
        run = self._mark_prompted_workers_failed(run, probe_outcome.start_error)
        return DependencyOrderedSerialRunStartOutcome(run, EX_UNSUPPORTED, probe_outcome.start_error)

    def _activate_run(self, run):
        return self._persist_mutation(run, _mark_run_active)

    def _execution_outcome(self, run, first_error_outcome):
        return DependencyOrderedSerialRunStartOutcome(
            run,
            _exit_code_for_orchestration_run(run),
            first_error_outcome.error if first_error_outcome is not None else None,
        )

    def _mark_prompted_workers_failed(self, run, error):
        workers = _pending_prompted_workers(run.get("workers", {}))
        transitions = mark_orchestration_start_failed(run, workers, error)
        return self._persist_transitions(run, transitions)

    def _persist_mutation(self, run, mutator):
        return persist_run_mutation(self.store, run, mutator, now=self.now)

    def _persist_worker_transition(self, run, transition):
        return persist_worker_transitions(
            self.store,
            run,
            [transition],
            refresh_run_summary=refresh_orchestration_run_summary,
            now=self.now,
        )

    def _persist_transitions(self, run, transitions):
        result = persist_worker_transitions(
            self.store,
            run,
            transitions,
            refresh_run_summary=refresh_orchestration_run_summary,
            now=self.now,
        )
        return result.run

    def _persist_summary(self, run):
        return persist_run_summary(
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
    return DependencySchedulePlanner().plan(workers)


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
    return WorkerTransition.dependency_blocked(worker["id"], blockers)


def _normalize_execution_policy(policy):
    normalized = (policy or EXECUTION_POLICY_FAIL_FAST).replace("-", "_")
    if normalized not in EXECUTION_POLICIES:
        raise RunStoreError(f"unsupported execution policy '{policy}'")
    return normalized


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
