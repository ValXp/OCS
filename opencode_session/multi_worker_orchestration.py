from dataclasses import dataclass
from datetime import datetime, timezone
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
from opencode_session.run_record import (
    ensure_run_worker,
    run_worker,
    run_workers,
    set_run_directory,
    set_run_server_url,
    set_run_status,
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
from opencode_session.worker_active_attempt_recovery import recover_expired_active_attempts
from opencode_session.worker_execution import WorkerExecutionExecutor
from opencode_session.worker_session_provisioning import WorkerSessionCreationJournal
from opencode_session.worker_dependencies import analyze_worker_dependencies
from opencode_session.worker_state import (
    WorkerTransition,
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


@dataclass(frozen=True)
class DependencyOrderedSerialRecoveryResult:
    run: RunRecord
    error: Optional[str] = None


@dataclass(frozen=True)
class DependencyOrderedSerialPlanningResult:
    run: RunRecord
    serial_step: DependencyOrderedSerialStep

    def skipped_outcome(self, recovery_error, execution_policy):
        if recovery_error is not None and execution_policy == EXECUTION_POLICY_FAIL_FAST:
            return DependencyOrderedSerialRunStartOutcome(
                self.run,
                _exit_code_for_orchestration_run(self.run),
                recovery_error,
            )
        if self.serial_step.worker_id is None:
            return DependencyOrderedSerialRunStartOutcome(
                self.run,
                _exit_code_for_orchestration_run(self.run),
                recovery_error,
            )
        return None


@dataclass(frozen=True)
class DependencyOrderedSerialExecutionResult:
    run: RunRecord
    client: Optional[object] = None
    created_session_ids_by_worker: Optional[dict] = None
    first_error: Optional[str] = None
    terminal_error: Optional[str] = None
    terminal_exit_code: Optional[int] = None
    terminal_exit_code_from_run: bool = False

    @classmethod
    def completed(cls, run, client, created_session_ids_by_worker, first_error):
        return cls(
            run,
            client=client,
            created_session_ids_by_worker=created_session_ids_by_worker,
            first_error=first_error,
        )

    @classmethod
    def unsupported(cls, run, start_error, created_session_ids_by_worker):
        return cls(
            run,
            created_session_ids_by_worker=created_session_ids_by_worker,
            terminal_error=start_error,
            terminal_exit_code=EX_UNSUPPORTED,
        )

    @classmethod
    def api_failure(cls, run, client, created_session_ids_by_worker, error):
        return cls(
            run,
            client=client,
            created_session_ids_by_worker=created_session_ids_by_worker,
            terminal_error=f"api failure: {error}",
            terminal_exit_code=EX_UNAVAILABLE,
        )

    @classmethod
    def fail_fast(cls, run, client, created_session_ids_by_worker, error):
        return cls(
            run,
            client=client,
            created_session_ids_by_worker=created_session_ids_by_worker,
            terminal_error=error,
            terminal_exit_code_from_run=True,
        )

    def finish_outcome(self, run, recovery_error):
        if self.terminal_exit_code_from_run:
            return DependencyOrderedSerialRunStartOutcome(
                run,
                _exit_code_for_orchestration_run(run),
                self.terminal_error,
            )
        if self.terminal_exit_code is not None:
            return DependencyOrderedSerialRunStartOutcome(run, self.terminal_exit_code, self.terminal_error)
        return DependencyOrderedSerialRunStartOutcome(
            run,
            _exit_code_for_orchestration_run(run),
            recovery_error or self.first_error,
        )


@dataclass(frozen=True)
class DependencyOrderedSerialCleanupResult:
    run: RunRecord
    outcome: Optional[DependencyOrderedSerialRunStartOutcome] = None


class DependencyOrderedSerialRecoveryPhase:
    def __init__(self, store, *, now):
        self.store = store
        self.now = now

    def recover(self, run):
        recoveries = recover_expired_active_attempts(run_workers(run), now=self.now)
        if not recoveries:
            return DependencyOrderedSerialRecoveryResult(run)
        result = persist_worker_transitions(
            self.store,
            run,
            [recovery.transition for recovery in recoveries],
            refresh_run_summary=refresh_orchestration_run_summary,
            now=self.now,
        )
        first_error = next((recovery.error for recovery in recoveries if recovery.error is not None), None)
        return DependencyOrderedSerialRecoveryResult(result.run, first_error)


class DependencyOrderedSerialPlanningPhase:
    def __init__(self, store, *, now, plan_serial_step):
        self.store = store
        self.now = now
        self.plan_serial_step = plan_serial_step

    def plan(self, run):
        step = self.plan_serial_step(run_workers(run))
        if step.dependency_blocked_transitions:
            result = persist_worker_transitions(
                self.store,
                run,
                step.dependency_blocked_transitions,
                refresh_run_summary=refresh_orchestration_run_summary,
                now=self.now,
            )
            return DependencyOrderedSerialPlanningResult(result.run, step)
        run = persist_run_summary(
            self.store,
            run,
            refresh_run_summary=refresh_orchestration_run_summary,
            now=self.now,
        )
        return DependencyOrderedSerialPlanningResult(run, step)


class DependencyOrderedSerialExecutionPhase:
    def __init__(
        self,
        *,
        capability_probe,
        activate_run,
        mark_prompted_workers_failed,
        plan_next_serial_step,
        worker_execution_executor,
    ):
        self.capability_probe = capability_probe
        self.activate_run = activate_run
        self.mark_prompted_workers_failed = mark_prompted_workers_failed
        self.plan_next_serial_step = plan_next_serial_step
        self.worker_execution_executor = worker_execution_executor

    def execute(self, run, serial_step, *, cleanup_requested, execution_policy):
        client = None
        created_session_ids_by_worker = {} if cleanup_requested else None
        try:
            probe_outcome = self.capability_probe.probe(run)
            if probe_outcome.start_error is not None:
                run = self.mark_prompted_workers_failed(run, probe_outcome.start_error)
                return DependencyOrderedSerialExecutionResult.unsupported(
                    run,
                    probe_outcome.start_error,
                    created_session_ids_by_worker,
                )

            client = probe_outcome.client
            run = self.activate_run(run)
            run, first_error_outcome, fail_fast_outcome = self._execute_serial_workers(
                run,
                serial_step,
                client,
                probe_outcome.capabilities,
                created_session_ids_by_worker,
                execution_policy=execution_policy,
            )
            if fail_fast_outcome is not None:
                return DependencyOrderedSerialExecutionResult.fail_fast(
                    run,
                    client,
                    created_session_ids_by_worker,
                    fail_fast_outcome.error,
                )
            first_error = first_error_outcome.error if first_error_outcome is not None else None
            return DependencyOrderedSerialExecutionResult.completed(
                run,
                client,
                created_session_ids_by_worker,
                first_error,
            )
        except OpenCodeApiError as error:
            run = self.mark_prompted_workers_failed(run, str(error))
            return DependencyOrderedSerialExecutionResult.api_failure(
                run,
                client,
                created_session_ids_by_worker,
                error,
            )

    def _execute_serial_workers(
        self,
        run,
        serial_step,
        client,
        capabilities,
        created_session_ids_by_worker,
        *,
        execution_policy,
    ):
        first_error_outcome = None
        fail_fast_outcome = None
        worker_id = serial_step.worker_id
        while worker_id is not None:
            run, worker_outcome = self._execute_selected_worker(
                run,
                worker_id,
                client,
                capabilities,
                created_session_ids_by_worker,
            )
            if worker_outcome is not None and worker_outcome.error is not None:
                first_error_outcome = first_error_outcome or worker_outcome
                if execution_policy == EXECUTION_POLICY_FAIL_FAST:
                    fail_fast_outcome = worker_outcome

            # Persist the replan before returning on fail-fast so dependents record their blockers.
            planning = self.plan_next_serial_step(run)
            run = planning.run
            serial_step = planning.serial_step
            if fail_fast_outcome is not None:
                break
            worker_id = serial_step.worker_id
        return run, first_error_outcome, fail_fast_outcome

    def _execute_selected_worker(self, run, worker_id, client, capabilities, created_session_ids_by_worker):
        worker = _worker_by_id(run_workers(run), worker_id)
        if worker is None:
            return run, None
        outcome = self.worker_execution_executor.execute(
            client,
            run,
            worker,
            _worker_prompt(worker),
            capabilities,
            agent=worker.agent,
            model=worker.model,
            create_session=True,
            cleanup_requested=created_session_ids_by_worker is not None,
        )
        run = outcome.run or run
        if created_session_ids_by_worker is not None:
            current_worker = run_worker(run, worker.worker_id, worker)
            remember_created_worker_sessions(created_session_ids_by_worker, current_worker, outcome.created_session_ids)
        return run, outcome


class DependencyOrderedSerialCleanupPhase:
    def __init__(self, cleanup_executor, *, cleanup_planner=None):
        self.cleanup_executor = cleanup_executor
        self.cleanup_planner = cleanup_planner or CreatedWorkerCleanupPlanner()

    def cleanup(self, client, run, created_session_ids_by_worker):
        if created_session_ids_by_worker is None or client is None:
            return DependencyOrderedSerialCleanupResult(run)
        cleanup_plan = self.cleanup_planner.plan(created_session_ids_by_worker, run)
        if not cleanup_plan.steps:
            return DependencyOrderedSerialCleanupResult(run)
        cleanup_result = self.cleanup_executor.cleanup(client, run, cleanup_plan)
        if cleanup_result.error is not None:
            return DependencyOrderedSerialCleanupResult(
                cleanup_result.run,
                DependencyOrderedSerialRunStartOutcome(
                    cleanup_result.run,
                    cleanup_result.exit_code,
                    cleanup_result.error,
                ),
            )
        return DependencyOrderedSerialCleanupResult(cleanup_result.run)


class DependencyOrderedSerialRunOrchestrationService:
    """Run prompted workers one at a time after their dependencies are satisfied.

    Serial execution is a product guarantee: each loop step persists blockers, selects at most one ready
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
        self.capability_probe = RunStartCapabilityProbe(
            client_factory=self.client_factory,
            capability_detector=self.capability_detector,
        )
        self.cleanup_executor = CreatedWorkerCleanupExecutor(
            persist_worker_transition=self._persist_worker_transition,
            refresh_run_summary=refresh_orchestration_run_summary,
        )
        self.worker_execution_executor = WorkerExecutionExecutor(
            apply_transition=self._persist_worker_execution_transition,
            executor=self.executor,
            now=self.now,
            session_journal=self._worker_session_journal(),
        )
        self.recovery_phase = DependencyOrderedSerialRecoveryPhase(self.store, now=self.now)
        self.planning_phase = DependencyOrderedSerialPlanningPhase(
            self.store,
            now=self.now,
            plan_serial_step=lambda workers: self._plan_serial_step(workers),
        )
        self.execution_phase = DependencyOrderedSerialExecutionPhase(
            capability_probe=self.capability_probe,
            activate_run=lambda run: self._activate_run(run),
            mark_prompted_workers_failed=lambda run, error: self._mark_prompted_workers_failed(run, error),
            plan_next_serial_step=lambda run: self.planning_phase.plan(run),
            worker_execution_executor=self.worker_execution_executor,
        )
        self.cleanup_phase = DependencyOrderedSerialCleanupPhase(self.cleanup_executor)

    def start(self, request):
        run = self.store.load_run(request.name)
        execution_policy = _normalize_execution_policy(request.execution_policy)
        run = self._persist_mutation(
            run,
            lambda latest_run: apply_dependency_ordered_start_request(latest_run, request),
        )
        if not any(_worker_prompt(worker) for worker in run_workers(run).values() if is_worker_record(worker)):
            raise RunStoreError(f"run '{request.name}' has no worker prompts; pass --prompt or add workers with --prompt")
        return self._start_prompted_workers(run, cleanup=request.cleanup, execution_policy=execution_policy)

    def _start_prompted_workers(self, run, *, cleanup=False, execution_policy=EXECUTION_POLICY_FAIL_FAST):
        recovery = self.recovery_phase.recover(run)
        planning = self.planning_phase.plan(recovery.run)
        skipped_outcome = planning.skipped_outcome(recovery.error, execution_policy)
        if skipped_outcome is not None:
            return skipped_outcome

        execution = self.execution_phase.execute(
            planning.run,
            planning.serial_step,
            cleanup_requested=cleanup,
            execution_policy=execution_policy,
        )
        cleanup_result = self.cleanup_phase.cleanup(
            execution.client,
            execution.run,
            execution.created_session_ids_by_worker,
        )
        if cleanup_result.outcome is not None:
            return cleanup_result.outcome
        return execution.finish_outcome(cleanup_result.run, recovery.error)

    def _plan_serial_step(self, workers):
        return plan_dependency_ordered_serial_step(workers)

    def _activate_run(self, run):
        return self._persist_mutation(run, _mark_run_active)

    def _mark_prompted_workers_failed(self, run, error):
        workers = _pending_prompted_workers(run_workers(run))
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
            else run_worker(result.run, transition.worker_id)
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
    set_run_status(run, "active")


def plan_dependency_ordered_serial_step(workers):
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
    )


def apply_dependency_ordered_start_request(run, request):
    if request.directory is not None:
        set_run_directory(run, request.directory)
    if request.server_url is not None:
        set_run_server_url(run, request.server_url)
    if request.session_id is not None or request.agent is not None or request.model is not None:
        worker = ensure_run_worker(run, request.worker_id, role=request.role)
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
