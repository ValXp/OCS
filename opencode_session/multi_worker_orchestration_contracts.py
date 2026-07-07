from dataclasses import dataclass
from typing import Optional

from opencode_session.cli_policy import exit_code_for_run as _exit_code_for_orchestration_run
from opencode_session.multi_worker_execution_outcome import DependencyOrderedSerialRunStartOutcome
from opencode_session.run_persistence import (
    persist_run_mutation,
    persist_run_summary,
    persist_worker_transitions,
)
from opencode_session.run_record import run_worker, set_run_status
from opencode_session.schema_run import RunRecord
from opencode_session.worker_dependencies import analyze_worker_dependencies
from opencode_session.worker_state import (
    WorkerTransition,
    is_executable_worker,
    is_worker_record,
    refresh_run_summary as _refresh_worker_run_summary,
    workers_in_dependency_order as _workers_in_dependency_order,
)


workers_in_dependency_order = _workers_in_dependency_order

EXECUTION_POLICY_FAIL_FAST = "fail_fast"
EXECUTION_POLICY_CONTINUE = "continue"
EXECUTION_POLICIES = {EXECUTION_POLICY_FAIL_FAST, EXECUTION_POLICY_CONTINUE}


class DependencyOrderedSerialRunPersistence:
    def __init__(self, store, *, now):
        self.store = store
        self.now = now

    def persist_mutation(self, run, mutator):
        return persist_run_mutation(self.store, run, mutator, now=self.now)

    def persist_worker_transition(self, run, transition):
        return persist_worker_transitions(
            self.store,
            run,
            [transition],
            refresh_run_summary=refresh_orchestration_run_summary,
            now=self.now,
        )

    def persist_worker_execution_transition(self, run, worker, transition):
        result = self.persist_worker_transition(run, transition)
        persisted_worker = result.workers[0] if result.workers else run_worker(result.run, transition.worker_id)
        return result.run, persisted_worker or worker

    def persist_transitions(self, run, transitions):
        result = persist_worker_transitions(
            self.store,
            run,
            transitions,
            refresh_run_summary=refresh_orchestration_run_summary,
            now=self.now,
        )
        return result.run

    def persist_summary(self, run):
        return persist_run_summary(
            self.store,
            run,
            refresh_run_summary=refresh_orchestration_run_summary,
            now=self.now,
        )


@dataclass(frozen=True)
class DependencyOrderedSerialStep:
    worker_id: Optional[str]
    dependency_blocked_transitions: tuple


@dataclass(frozen=True)
class DependencyOrderedSerialRecoveryRequest:
    run: RunRecord


@dataclass(frozen=True)
class DependencyOrderedSerialRecoveryResult:
    run: RunRecord
    error: Optional[str] = None


@dataclass(frozen=True)
class DependencyOrderedSerialPlanningRequest:
    run: RunRecord


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
class DependencyOrderedSerialExecutionRequest:
    run: RunRecord
    serial_step: DependencyOrderedSerialStep
    cleanup_requested: bool
    execution_policy: str


@dataclass(frozen=True)
class DependencyOrderedSerialCleanupRequest:
    client: Optional[object]
    run: RunRecord
    created_session_ids_by_worker: Optional[dict]


@dataclass(frozen=True)
class DependencyOrderedSerialCleanupResult:
    run: RunRecord
    outcome: Optional[DependencyOrderedSerialRunStartOutcome] = None


@dataclass(frozen=True)
class DependencyOrderedSerialRunFlowRequest:
    run: RunRecord
    cleanup_requested: bool
    execution_policy: str


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
