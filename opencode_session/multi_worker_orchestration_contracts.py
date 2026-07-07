from dataclasses import dataclass
from typing import Optional

from opencode_session.schema_run import RunRecord

EXECUTION_POLICY_FAIL_FAST = "fail_fast"
EXECUTION_POLICY_CONTINUE = "continue"
EXECUTION_POLICIES = {EXECUTION_POLICY_FAIL_FAST, EXECUTION_POLICY_CONTINUE}


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
    outcome: Optional[object] = None


@dataclass(frozen=True)
class DependencyOrderedSerialRunFlowRequest:
    run: RunRecord
    cleanup_requested: bool
    execution_policy: str
