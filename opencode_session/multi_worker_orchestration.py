from dataclasses import dataclass
from typing import Optional

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import detect_capabilities
from opencode_session.domain_helpers import utc_now
from opencode_session.multi_worker_orchestration_contracts import (
    EXECUTION_POLICIES,
    EXECUTION_POLICY_CONTINUE,
    EXECUTION_POLICY_FAIL_FAST,
    DependencyOrderedSerialCleanupRequest,
    DependencyOrderedSerialCleanupResult,
    DependencyOrderedSerialExecutionRequest,
    DependencyOrderedSerialPlanningRequest,
    DependencyOrderedSerialPlanningResult,
    DependencyOrderedSerialRecoveryRequest,
    DependencyOrderedSerialRecoveryResult,
    DependencyOrderedSerialRunFlowRequest,
    DependencyOrderedSerialStep,
)
from opencode_session.multi_worker_orchestration_persistence import DependencyOrderedSerialRunPersistence
from opencode_session.multi_worker_orchestration_phases import (
    DependencyOrderedSerialCleanupPhase,
    DependencyOrderedSerialExecutionPhase,
    DependencyOrderedSerialPlanningPhase,
    DependencyOrderedSerialRecoveryPhase,
    DependencyOrderedSerialRunFlow,
    plan_dependency_ordered_serial_step,
    refresh_orchestration_run_summary,
    workers_in_dependency_order,
)
from opencode_session.run_record import (
    ensure_run_worker,
    run_workers,
    set_run_directory,
    set_run_server_url,
)
from opencode_session.run_start_core import (
    CreatedWorkerCleanupExecutor,
    RunStartCapabilityProbe,
)
from opencode_session.run_store import RunStoreError
from opencode_session.worker_execution import WorkerExecutionExecutor
from opencode_session.worker_session_provisioning import WorkerSessionCreationJournal
from opencode_session.worker_state import (
    is_worker_record,
    worker_record_for_mutation,
    worker_prompt as _worker_prompt,
)


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
        self.now = now or utc_now
        self.persistence = DependencyOrderedSerialRunPersistence(
            self.store,
            now=self.now,
            refresh_run_summary=refresh_orchestration_run_summary,
        )
        self.capability_probe = RunStartCapabilityProbe(
            client_factory=self.client_factory,
            capability_detector=self.capability_detector,
        )
        self.cleanup_executor = CreatedWorkerCleanupExecutor(
            persist_worker_transition=self.persistence.persist_worker_transition,
            refresh_run_summary=refresh_orchestration_run_summary,
        )
        self.worker_execution_executor = WorkerExecutionExecutor(
            apply_transition=self.persistence.persist_worker_execution_transition,
            executor=self.executor,
            now=self.now,
            session_journal=WorkerSessionCreationJournal(self.persistence.persist_mutation, now=self.now),
        )
        self.recovery_phase = DependencyOrderedSerialRecoveryPhase(self.persistence)
        self.planning_phase = DependencyOrderedSerialPlanningPhase(self.persistence)
        self.execution_phase = DependencyOrderedSerialExecutionPhase(
            persistence=self.persistence,
            capability_probe=self.capability_probe,
            planning_phase=self.planning_phase,
            worker_execution_executor=self.worker_execution_executor,
        )
        self.cleanup_phase = DependencyOrderedSerialCleanupPhase(self.cleanup_executor)
        self.run_flow = DependencyOrderedSerialRunFlow(
            recovery_phase=self.recovery_phase,
            planning_phase=self.planning_phase,
            execution_phase=self.execution_phase,
            cleanup_phase=self.cleanup_phase,
        )

    def start(self, request):
        run = self.store.load_run(request.name)
        execution_policy = _normalize_execution_policy(request.execution_policy)

        def apply_start_request(latest_run):
            apply_dependency_ordered_start_request(latest_run, request)

        run = self.persistence.persist_mutation(run, apply_start_request)
        if not any(_worker_prompt(worker) for worker in run_workers(run).values() if is_worker_record(worker)):
            raise RunStoreError(f"run '{request.name}' has no worker prompts; pass --prompt or add workers with --prompt")
        return self.run_flow.start(
            DependencyOrderedSerialRunFlowRequest(
                run,
                cleanup_requested=request.cleanup,
                execution_policy=execution_policy,
            )
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


def _normalize_execution_policy(policy):
    normalized = (policy or EXECUTION_POLICY_FAIL_FAST).replace("-", "_")
    if normalized not in EXECUTION_POLICIES:
        raise RunStoreError(f"unsupported execution policy '{policy}'")
    return normalized
