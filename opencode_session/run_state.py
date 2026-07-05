from dataclasses import dataclass
from typing import Optional

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import detect_capabilities
from opencode_session.cli_policy import server_default
from opencode_session.multi_worker_orchestration import (
    DependencyOrderedSerialRunOrchestrationService,
    DependencyOrderedSerialRunStartOutcome,
    DependencyOrderedSerialRunStartRequest,
)
from opencode_session.run_store import RunStoreError
from opencode_session.worker_execution import WorkerExecutionTimeout


@dataclass
class SingleWorkerRunStartRequest:
    name: str
    worker_id: str
    role: str
    prompt: str
    directory: Optional[str] = None
    server_url: Optional[str] = None
    session_id: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    cleanup: bool = False
    default_server_url: Optional[str] = None


SingleWorkerRunStartOutcome = DependencyOrderedSerialRunStartOutcome


class SingleWorkerRunStateService:
    """Compatibility adapter for the dependency-ordered orchestration engine."""

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
        self.orchestration = DependencyOrderedSerialRunOrchestrationService(
            store,
            client_factory=client_factory,
            capability_detector=capability_detector,
            executor=executor,
            now=now,
        )

    def start(self, request):
        self._ensure_prompted_worker(request)
        return self.orchestration.start(
            DependencyOrderedSerialRunStartRequest(
                name=request.name,
                worker_id=request.worker_id,
                role=request.role,
                directory=request.directory,
                server_url=request.server_url,
                session_id=request.session_id,
                agent=request.agent,
                model=request.model,
                cleanup=request.cleanup,
            )
        )

    def _ensure_prompted_worker(self, request):
        try:
            self.store.load_run(request.name)
        except RunStoreError as error:
            if error.kind != "missing":
                raise
            self.store.create_run(
                request.name,
                directory=request.directory or ".",
                server_url=request.server_url or request.default_server_url or server_default(),
            )
        self.store.upsert_worker(
            request.name,
            request.worker_id,
            role=request.role,
            prompt=request.prompt,
            status="queued",
            session_id=request.session_id,
            agent=request.agent,
            model=request.model,
        )
