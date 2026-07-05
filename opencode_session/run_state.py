from dataclasses import dataclass
from typing import Optional

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.capabilities import detect_capabilities
from opencode_session.multi_worker_orchestration import (
    DependencyOrderedSerialRunOrchestrationService,
    DependencyOrderedSerialRunStartOutcome,
    DependencyOrderedSerialRunStartRequest,
)
from opencode_session.run_prompt_worker import ensure_prompt_worker


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
        ensure_prompt_worker(self.store, request)
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
