from dataclasses import dataclass
from typing import Optional

from opencode_session.cli_policy import (
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
    exit_code_for_run as _exit_code_for_orchestration_run,
)
from opencode_session.multi_worker_orchestration_contracts import EXECUTION_POLICY_FAIL_FAST
from opencode_session.schema_run import RunRecord


@dataclass
class DependencyOrderedSerialRunStartOutcome:
    run: RunRecord
    exit_code: int
    error: Optional[str] = None


@dataclass(frozen=True)
class DependencyOrderedSerialExecutionCleanupContext:
    client: Optional[object]
    created_session_ids_by_worker: Optional[dict]


class DependencyOrderedSerialExecutionOutcome:
    def finish_outcome(self, run, recovery_error):
        raise NotImplementedError


@dataclass(frozen=True)
class DependencyOrderedSerialExecutionCompleted(DependencyOrderedSerialExecutionOutcome):
    first_error: Optional[str] = None

    def finish_outcome(self, run, recovery_error):
        return DependencyOrderedSerialRunStartOutcome(
            run,
            _exit_code_for_orchestration_run(run),
            recovery_error or self.first_error,
        )


@dataclass(frozen=True)
class DependencyOrderedSerialExecutionUnsupported(DependencyOrderedSerialExecutionOutcome):
    error: str

    def finish_outcome(self, run, recovery_error):
        return DependencyOrderedSerialRunStartOutcome(run, EX_UNSUPPORTED, self.error)


@dataclass(frozen=True)
class DependencyOrderedSerialExecutionApiFailure(DependencyOrderedSerialExecutionOutcome):
    error: str

    def finish_outcome(self, run, recovery_error):
        return DependencyOrderedSerialRunStartOutcome(run, EX_UNAVAILABLE, self.error)


@dataclass(frozen=True)
class DependencyOrderedSerialExecutionFailFast(DependencyOrderedSerialExecutionOutcome):
    error: str

    def finish_outcome(self, run, recovery_error):
        return DependencyOrderedSerialRunStartOutcome(run, _exit_code_for_orchestration_run(run), self.error)


@dataclass(frozen=True)
class DependencyOrderedSerialExecutionResult:
    run: RunRecord
    cleanup_context: DependencyOrderedSerialExecutionCleanupContext
    outcome: DependencyOrderedSerialExecutionOutcome

    @classmethod
    def completed(cls, run, client, created_session_ids_by_worker, first_error):
        return cls(
            run,
            DependencyOrderedSerialExecutionCleanupContext(client, created_session_ids_by_worker),
            DependencyOrderedSerialExecutionCompleted(first_error),
        )

    @classmethod
    def unsupported(cls, run, start_error, created_session_ids_by_worker):
        return cls(
            run,
            DependencyOrderedSerialExecutionCleanupContext(None, created_session_ids_by_worker),
            DependencyOrderedSerialExecutionUnsupported(start_error),
        )

    @classmethod
    def api_failure(cls, run, client, created_session_ids_by_worker, error):
        return cls(
            run,
            DependencyOrderedSerialExecutionCleanupContext(client, created_session_ids_by_worker),
            DependencyOrderedSerialExecutionApiFailure(f"api failure: {error}"),
        )

    @classmethod
    def fail_fast(cls, run, client, created_session_ids_by_worker, error):
        return cls(
            run,
            DependencyOrderedSerialExecutionCleanupContext(client, created_session_ids_by_worker),
            DependencyOrderedSerialExecutionFailFast(error),
        )

    def finish_outcome(self, run, recovery_error):
        return self.outcome.finish_outcome(run, recovery_error)


def skipped_dependency_ordered_serial_outcome(run, serial_step, recovery_error, execution_policy):
    if recovery_error is not None and execution_policy == EXECUTION_POLICY_FAIL_FAST:
        return DependencyOrderedSerialRunStartOutcome(
            run,
            _exit_code_for_orchestration_run(run),
            recovery_error,
        )
    if serial_step.worker_id is None:
        return DependencyOrderedSerialRunStartOutcome(
            run,
            _exit_code_for_orchestration_run(run),
            recovery_error,
        )
    return None
