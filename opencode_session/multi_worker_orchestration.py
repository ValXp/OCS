from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.api_transport import OpenCodeApiError
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import detect_capabilities
from opencode_session.cli_policy import (
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
    exit_code_for_run as _exit_code_for_orchestration_run,
)
from opencode_session.run_persistence import (
    persist_run_mutation,
    persist_run_summary,
    persist_worker_transitions,
)
from opencode_session.run_start_core import (
    CreatedWorkerCleanupExecutor,
    CreatedWorkerCleanupPlanner,
    RunStartCapabilityProbe,
    remember_created_worker_sessions,
)
from opencode_session.run_start_policy import mark_orchestration_start_failed
from opencode_session.run_store import RunStoreError
from opencode_session.schema_run import RunRecord
from opencode_session.worker_execution import (
    WorkerExecutionExecutor,
    WorkerExecutionOutcome,
)
from opencode_session.worker_session_provisioning import WorkerSessionCreationJournal
from opencode_session.worker_dependencies import analyze_worker_dependencies
from opencode_session.worker_state import (
    WorkerTransition,
    ensure_worker as _ensure_orchestration_worker,
    is_executable_worker,
    is_worker_record,
    refresh_run_summary as _refresh_worker_run_summary,
    worker_record_for_mutation,
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
class DependencyOrderedSerialStep:
    worker_id: Optional[str]
    dependency_blocked_transitions: tuple
    blockers_by_worker_id: dict


class DependencyOrderedSerialPlanner:
    def plan(self, workers):
        workers = workers if isinstance(workers, dict) else {}
        analysis = analyze_worker_dependencies(workers)
        blocked_worker_ids = set(analysis.blockers_by_worker_id)

        # Serial orchestration intentionally selects one ready worker per persisted step.
        selected_worker_id = analysis.ready_worker_ids[0] if analysis.ready_worker_ids else None
        return DependencyOrderedSerialStep(
            worker_id=selected_worker_id,
            dependency_blocked_transitions=tuple(
                _dependency_blocked_transition(workers[worker_id], analysis.blockers_by_worker_id[worker_id])
                for worker_id in sorted(blocked_worker_ids)
                if is_worker_record(workers.get(worker_id))
            ),
            blockers_by_worker_id=analysis.blockers_by_worker_id,
        )


class DurableDependencySerialScheduler:
    def __init__(self, store, *, now, planner=None):
        self.store = store
        self.now = now
        self.planner = planner or DependencyOrderedSerialPlanner()

    def persist_next_step(self, run):
        step = self.planner.plan(run.get("workers", {}))
        if step.dependency_blocked_transitions:
            result = persist_worker_transitions(
                self.store,
                run,
                step.dependency_blocked_transitions,
                refresh_run_summary=refresh_orchestration_run_summary,
                now=self.now,
            )
            return result.run, step
        else:
            run = persist_run_summary(
                self.store,
                run,
                refresh_run_summary=refresh_orchestration_run_summary,
                now=self.now,
            )
        return run, step


@dataclass
class SerialWorkerExecutionOutcome:
    run: RunRecord
    first_error_outcome: Optional[WorkerExecutionOutcome] = None
    fail_fast_outcome: Optional[WorkerExecutionOutcome] = None


@dataclass(frozen=True)
class SerialRunLoopPlan:
    worker_id: Optional[str]
    first_error_outcome: Optional[WorkerExecutionOutcome] = None
    fail_fast_outcome: Optional[WorkerExecutionOutcome] = None


class DependencyOrderedSerialRunLoopPlanner:
    def initial_plan(self, serial_step):
        return SerialRunLoopPlan(worker_id=serial_step.worker_id)

    def after_worker(self, previous_plan, worker_outcome, next_step):
        first_error_outcome = previous_plan.first_error_outcome or worker_outcome.first_error_outcome
        if worker_outcome.fail_fast_outcome is not None:
            return SerialRunLoopPlan(None, first_error_outcome, worker_outcome.fail_fast_outcome)
        return SerialRunLoopPlan(next_step.worker_id, first_error_outcome)


class DependencyOrderedSerialRunLoopExecutor:
    def __init__(self, scheduler, worker_executor, *, planner=None):
        self.scheduler = scheduler
        self.worker_executor = worker_executor
        self.planner = planner or DependencyOrderedSerialRunLoopPlanner()

    def execute(
        self,
        run,
        serial_step,
        client,
        capabilities,
        session_tracker,
        *,
        execution_policy,
    ):
        loop_plan = self.planner.initial_plan(serial_step)
        while loop_plan.worker_id is not None:
            worker_outcome = self.worker_executor.execute_next(
                run,
                loop_plan.worker_id,
                client,
                capabilities,
                session_tracker=session_tracker,
                execution_policy=execution_policy,
            )
            run = worker_outcome.run
            run, serial_step = self.scheduler.persist_next_step(run)
            loop_plan = self.planner.after_worker(loop_plan, worker_outcome, serial_step)
            if loop_plan.fail_fast_outcome is not None:
                return SerialWorkerExecutionOutcome(
                    run,
                    loop_plan.first_error_outcome,
                    loop_plan.fail_fast_outcome,
                )
        return SerialWorkerExecutionOutcome(run, loop_plan.first_error_outcome)


class DependencyOrderedSerialRunStartOutcomePolicy:
    def no_selected_worker(self, run):
        return DependencyOrderedSerialRunStartOutcome(run, _exit_code_for_orchestration_run(run))

    def unsupported_probe(self, run, start_error):
        return DependencyOrderedSerialRunStartOutcome(run, EX_UNSUPPORTED, start_error)

    def api_failure(self, run, error):
        return DependencyOrderedSerialRunStartOutcome(run, EX_UNAVAILABLE, f"api failure: {error}")

    def execution_finished(self, run, first_error_outcome):
        return DependencyOrderedSerialRunStartOutcome(
            run,
            _exit_code_for_orchestration_run(run),
            first_error_outcome.error if first_error_outcome is not None else None,
        )


class DisposableSessionTracker:
    def __init__(self, enabled=False, *, cleanup_planner=None):
        self.enabled = enabled
        self.cleanup_planner = cleanup_planner or CreatedWorkerCleanupPlanner()
        self.created_session_ids_by_worker = {}

    def remember_worker_outcome(self, run, fallback_worker, outcome):
        if not self.enabled:
            return
        current_worker = (outcome.run or run).get("workers", {}).get(fallback_worker.worker_id, fallback_worker)
        remember_created_worker_sessions(
            self.created_session_ids_by_worker,
            current_worker,
            outcome.created_session_ids,
        )

    def cleanup(self, cleanup_executor, client, run):
        if not self.enabled or client is None:
            return run, None
        cleanup_plan = self.cleanup_planner.plan(self.created_session_ids_by_worker, run)
        if not cleanup_plan.steps:
            return run, None
        cleanup_result = cleanup_executor.cleanup(client, run, cleanup_plan)
        if cleanup_result.error is not None:
            return cleanup_result.run, DependencyOrderedSerialRunStartOutcome(
                cleanup_result.run,
                cleanup_result.exit_code,
                cleanup_result.error,
            )
        return cleanup_result.run, None


class SelectedSerialWorkerExecutor:
    def __init__(self, worker_executor):
        self.worker_executor = worker_executor

    def execute_next(
        self,
        run,
        worker_id,
        client,
        capabilities,
        *,
        session_tracker,
        execution_policy,
    ):
        first_error_outcome = None
        worker = _worker_by_id(run.get("workers", {}), worker_id)
        if worker is None:
            return SerialWorkerExecutionOutcome(run)
        outcome = self._execute_single_worker(client, run, worker, capabilities, session_tracker)
        run = outcome.run or run
        current_worker = run.get("workers", {}).get(worker.worker_id, worker)
        session_tracker.remember_worker_outcome(run, current_worker, outcome)
        if outcome.error is not None:
            first_error_outcome = outcome
            if execution_policy == EXECUTION_POLICY_FAIL_FAST:
                return SerialWorkerExecutionOutcome(run, first_error_outcome, outcome)
        return SerialWorkerExecutionOutcome(run, first_error_outcome)

    def _execute_single_worker(self, client, run, worker, capabilities, session_tracker):
        return self.worker_executor.execute(
            client,
            run,
            worker,
            _worker_prompt(worker),
            capabilities,
            agent=worker.agent,
            model=worker.model,
            create_session=True,
            cleanup_requested=getattr(session_tracker, "enabled", False),
        )


class DependencyOrderedSerialRunOrchestrationService:
    """Run prompted workers one at a time after their dependencies are satisfied.

    Serial execution is a product guarantee: each scheduler step persists blockers, selects at most one ready
    worker, executes it, and replans from durable state before selecting the next worker.
    """

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
        self.scheduler = DurableDependencySerialScheduler(self.store, now=self.now)
        self.capability_probe = RunStartCapabilityProbe(
            client_factory=self.client_factory,
            capability_detector=self.capability_detector,
        )
        self.cleanup_executor = CreatedWorkerCleanupExecutor(
            persist_worker_transition=self._persist_worker_transition,
            refresh_run_summary=refresh_orchestration_run_summary,
        )
        self.outcome_policy = DependencyOrderedSerialRunStartOutcomePolicy()
        self.worker_execution_executor = WorkerExecutionExecutor(
            apply_transition=self._persist_worker_execution_transition,
            executor=self.executor,
            now=self.now,
            session_journal=self._worker_session_journal(),
        )
        self.worker_executor = SelectedSerialWorkerExecutor(self.worker_execution_executor)
        self.run_loop_executor = DependencyOrderedSerialRunLoopExecutor(self.scheduler, self.worker_executor)

    def start(self, request):
        run = self.store.load_run(request.name)
        execution_policy = _normalize_execution_policy(request.execution_policy)
        run = self._persist_mutation(
            run,
            lambda latest_run: apply_dependency_ordered_start_request(latest_run, request),
        )
        if not any(_worker_prompt(worker) for worker in run.get("workers", {}).values() if is_worker_record(worker)):
            raise RunStoreError(f"run '{request.name}' has no worker prompts; pass --prompt or add workers with --prompt")
        return self._start_prompted_workers(run, cleanup=request.cleanup, execution_policy=execution_policy)

    def _start_prompted_workers(self, run, *, cleanup=False, execution_policy=EXECUTION_POLICY_FAIL_FAST):
        run, serial_step = self.scheduler.persist_next_step(run)
        if serial_step.worker_id is None:
            return self.outcome_policy.no_selected_worker(run)

        client = None
        session_tracker = DisposableSessionTracker(cleanup)

        try:
            probe_outcome = self.capability_probe.probe(run)
            if probe_outcome.start_error is not None:
                run = self._mark_prompted_workers_failed(run, probe_outcome.start_error)
                return self.outcome_policy.unsupported_probe(run, probe_outcome.start_error)

            client = probe_outcome.client
            run = self._activate_run(run)
            execution_outcome = self.run_loop_executor.execute(
                run,
                serial_step,
                client,
                probe_outcome.capabilities,
                session_tracker,
                execution_policy=execution_policy,
            )
            run = execution_outcome.run
            if execution_outcome.fail_fast_outcome is not None:
                run, cleanup_error = session_tracker.cleanup(self.cleanup_executor, client, run)
                return cleanup_error or self.outcome_policy.execution_finished(run, execution_outcome.fail_fast_outcome)
        except OpenCodeApiError as error:
            run = self._mark_prompted_workers_failed(run, str(error))
            run, cleanup_error = session_tracker.cleanup(self.cleanup_executor, client, run)
            if cleanup_error:
                return cleanup_error
            return self.outcome_policy.api_failure(run, error)

        run, cleanup_error = session_tracker.cleanup(self.cleanup_executor, client, run)
        return cleanup_error or self.outcome_policy.execution_finished(run, execution_outcome.first_error_outcome)

    def _activate_run(self, run):
        return self._persist_mutation(run, _mark_run_active)

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

    def _persist_worker_execution_transition(self, run, worker, transition):
        result = self._persist_worker_transition(run, transition)
        persisted_worker = (
            result.workers[0]
            if result.workers
            else result.run.get("workers", {}).get(transition.worker_id)
        )
        return result.run, persisted_worker or worker

    def _worker_session_journal(self):
        return WorkerSessionCreationJournal(self._persist_mutation, now=self.now)

    def _persist_transitions(self, run, transitions):
        result = persist_worker_transitions(
            self.store,
            run,
            transitions,
            refresh_run_summary=refresh_orchestration_run_summary,
            now=self.now,
        )
        return result.run


def refresh_orchestration_run_summary(run):
    _refresh_worker_run_summary(run, include_unprompted_when_no_prompts=True)


def _mark_run_active(run):
    run["status"] = "active"


def plan_dependency_ordered_serial_step(workers):
    return DependencyOrderedSerialPlanner().plan(workers)


def apply_dependency_ordered_start_request(run, request):
    if request.directory is not None:
        run["directory"] = str(Path(request.directory).resolve())
    if request.server_url is not None:
        run["server_url"] = request.server_url
    if request.session_id is not None or request.agent is not None or request.model is not None:
        worker = _ensure_orchestration_worker(run, request.worker_id, role=request.role)
        worker_record = worker_record_for_mutation(worker, request.worker_id)
        worker_record.set_session(
            request.session_id if request.session_id is not None else worker_record.session_id,
            agent=request.agent,
            model=request.model,
        )


def _worker_by_id(workers, worker_id):
    worker = workers.get(worker_id) if isinstance(workers, dict) else None
    return worker if is_worker_record(worker) else None


def _pending_prompted_workers(workers):
    return [
        workers[worker_id]
        for worker_id in _pending_prompted_worker_ids(workers)
    ]


def _pending_prompted_worker_ids(workers):
    return tuple(
        worker_id
        for worker_id in sorted(workers)
        if is_worker_record(workers.get(worker_id))
        and is_executable_worker(workers[worker_id])
    )


def _dependency_blocked_transition(worker, blockers):
    return WorkerTransition.dependency_blocked(worker.worker_id, blockers)


def _normalize_execution_policy(policy):
    normalized = (policy or EXECUTION_POLICY_FAIL_FAST).replace("-", "_")
    if normalized not in EXECUTION_POLICIES:
        raise RunStoreError(f"unsupported execution policy '{policy}'")
    return normalized


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
