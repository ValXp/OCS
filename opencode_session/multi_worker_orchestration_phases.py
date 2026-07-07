from opencode_session.api_transport import OpenCodeApiError
from opencode_session.multi_worker_execution_outcome import (
    DependencyOrderedSerialExecutionResult,
    DependencyOrderedSerialRunStartOutcome,
    skipped_dependency_ordered_serial_outcome,
)
from opencode_session.multi_worker_orchestration_contracts import (
    EXECUTION_POLICY_FAIL_FAST,
    DependencyOrderedSerialCleanupRequest,
    DependencyOrderedSerialCleanupResult,
    DependencyOrderedSerialExecutionRequest,
    DependencyOrderedSerialPlanningRequest,
    DependencyOrderedSerialPlanningResult,
    DependencyOrderedSerialRecoveryRequest,
    DependencyOrderedSerialRecoveryResult,
    DependencyOrderedSerialStep,
)
from opencode_session.run_record import run_worker, run_workers, set_run_status
from opencode_session.run_start_core import CreatedWorkerCleanupPlanner, remember_created_worker_sessions
from opencode_session.run_start_policy import mark_orchestration_start_failed
from opencode_session.worker_dependencies import analyze_worker_dependencies
from opencode_session.worker_active_attempt_recovery import recover_expired_active_attempts
from opencode_session.worker_state import (
    WorkerTransition,
    is_executable_worker,
    is_worker_record,
    refresh_run_summary as _refresh_worker_run_summary,
    worker_prompt as _worker_prompt,
    workers_in_dependency_order,
)


class DependencyOrderedSerialRecoveryPhase:
    def __init__(self, persistence):
        self.persistence = persistence

    def recover(self, request):
        run = request.run
        recoveries = recover_expired_active_attempts(run_workers(run), now=self.persistence.now)
        if not recoveries:
            return DependencyOrderedSerialRecoveryResult(run)
        run = self.persistence.persist_transitions(run, [recovery.transition for recovery in recoveries])
        first_error = next((recovery.error for recovery in recoveries if recovery.error is not None), None)
        return DependencyOrderedSerialRecoveryResult(run, first_error)


class DependencyOrderedSerialPlanningPhase:
    def __init__(self, persistence):
        self.persistence = persistence

    def plan(self, request):
        run = request.run
        step = plan_dependency_ordered_serial_step(run_workers(run))
        if step.dependency_blocked_transitions:
            return DependencyOrderedSerialPlanningResult(
                self.persistence.persist_transitions(run, step.dependency_blocked_transitions),
                step,
            )
        return DependencyOrderedSerialPlanningResult(self.persistence.persist_summary(run), step)


class DependencyOrderedSerialExecutionPhase:
    def __init__(self, *, persistence, capability_probe, planning_phase, worker_execution_executor):
        self.persistence = persistence
        self.capability_probe = capability_probe
        self.planning_phase = planning_phase
        self.worker_execution_executor = worker_execution_executor

    def execute(self, request):
        run = request.run
        client = None
        created_session_ids_by_worker = {} if request.cleanup_requested else None
        try:
            probe_outcome = self.capability_probe.probe(run)
            if probe_outcome.start_error is not None:
                run = self._mark_prompted_workers_failed(run, probe_outcome.start_error)
                return DependencyOrderedSerialExecutionResult.unsupported(
                    run,
                    probe_outcome.start_error,
                    created_session_ids_by_worker,
                )

            client = probe_outcome.client
            run = self.persistence.persist_mutation(run, mark_run_active)
            run, first_error_outcome, fail_fast_outcome = self._execute_serial_workers(
                run,
                request.serial_step,
                client,
                probe_outcome.capabilities,
                created_session_ids_by_worker,
                execution_policy=request.execution_policy,
            )
            if fail_fast_outcome is not None:
                return DependencyOrderedSerialExecutionResult.fail_fast(
                    run,
                    client,
                    created_session_ids_by_worker,
                    fail_fast_outcome.error,
                )
            first_error = first_error_outcome.error if first_error_outcome is not None else None
            return DependencyOrderedSerialExecutionResult.completed(run, client, created_session_ids_by_worker, first_error)
        except OpenCodeApiError as error:
            run = self._mark_prompted_workers_failed(run, str(error))
            return DependencyOrderedSerialExecutionResult.api_failure(run, client, created_session_ids_by_worker, error)

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

            # Persist the replan before fail-fast returns so dependents record their blockers.
            planning = self.planning_phase.plan(DependencyOrderedSerialPlanningRequest(run))
            run = planning.run
            serial_step = planning.serial_step
            if fail_fast_outcome is not None:
                break
            worker_id = serial_step.worker_id
        return run, first_error_outcome, fail_fast_outcome

    def _execute_selected_worker(self, run, worker_id, client, capabilities, created_session_ids_by_worker):
        worker = worker_by_id(run_workers(run), worker_id)
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

    def _mark_prompted_workers_failed(self, run, error):
        workers = pending_prompted_workers(run_workers(run))
        return self.persistence.persist_transitions(run, mark_orchestration_start_failed(run, workers, error))


class DependencyOrderedSerialCleanupPhase:
    def __init__(self, cleanup_executor, *, cleanup_planner=None):
        self.cleanup_executor = cleanup_executor
        self.cleanup_planner = cleanup_planner or CreatedWorkerCleanupPlanner()

    def cleanup(self, request):
        if request.created_session_ids_by_worker is None or request.client is None:
            return DependencyOrderedSerialCleanupResult(request.run)
        cleanup_plan = self.cleanup_planner.plan(request.created_session_ids_by_worker, request.run)
        if not cleanup_plan.steps:
            return DependencyOrderedSerialCleanupResult(request.run)
        cleanup_result = self.cleanup_executor.cleanup(request.client, request.run, cleanup_plan)
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


class DependencyOrderedSerialRunFlow:
    def __init__(self, *, recovery_phase, planning_phase, execution_phase, cleanup_phase):
        self.recovery_phase = recovery_phase
        self.planning_phase = planning_phase
        self.execution_phase = execution_phase
        self.cleanup_phase = cleanup_phase

    def start(self, request):
        recovery = self.recovery_phase.recover(DependencyOrderedSerialRecoveryRequest(request.run))
        planning = self.planning_phase.plan(DependencyOrderedSerialPlanningRequest(recovery.run))
        skipped_outcome = skipped_dependency_ordered_serial_outcome(
            planning.run,
            planning.serial_step,
            recovery.error,
            request.execution_policy,
        )
        if skipped_outcome is not None:
            return skipped_outcome

        execution = self.execution_phase.execute(
            DependencyOrderedSerialExecutionRequest(
                planning.run,
                planning.serial_step,
                cleanup_requested=request.cleanup_requested,
                execution_policy=request.execution_policy,
            )
        )
        cleanup_result = self.cleanup_phase.cleanup(
            DependencyOrderedSerialCleanupRequest(
                execution.cleanup_context.client,
                execution.run,
                execution.cleanup_context.created_session_ids_by_worker,
            )
        )
        if cleanup_result.outcome is not None:
            return cleanup_result.outcome
        return execution.finish_outcome(cleanup_result.run, recovery.error)


def refresh_orchestration_run_summary(run):
    _refresh_worker_run_summary(run, include_unprompted_when_no_prompts=True)


def plan_dependency_ordered_serial_step(workers):
    workers = workers if isinstance(workers, dict) else {}
    analysis = analyze_worker_dependencies(workers)
    blocked_worker_ids = set(analysis.blockers_by_worker_id)
    selected_worker_id = analysis.ready_worker_ids[0] if analysis.ready_worker_ids else None
    return DependencyOrderedSerialStep(
        worker_id=selected_worker_id,
        dependency_blocked_transitions=tuple(
            _dependency_blocked_transition(workers[worker_id], analysis.blockers_by_worker_id[worker_id])
            for worker_id in sorted(blocked_worker_ids)
            if is_worker_record(workers.get(worker_id))
        ),
    )


def mark_run_active(run):
    set_run_status(run, "active")


def worker_by_id(workers, worker_id):
    worker = workers.get(worker_id) if isinstance(workers, dict) else None
    return worker if is_worker_record(worker) else None


def pending_prompted_workers(workers):
    return [workers[worker_id] for worker_id in pending_prompted_worker_ids(workers)]


def pending_prompted_worker_ids(workers):
    return tuple(
        worker_id
        for worker_id in sorted(workers)
        if is_worker_record(workers.get(worker_id)) and is_executable_worker(workers[worker_id])
    )


def _dependency_blocked_transition(worker, blockers):
    return WorkerTransition.dependency_blocked(worker.worker_id, blockers)
