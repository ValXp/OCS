from dataclasses import dataclass

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.multi_worker_orchestration import DependencyOrderedSerialRunStartRequest
from opencode_session.run_prompt_worker import ensure_prompt_worker


@dataclass
class PromptedSingleWorkerStartRequest:
    name: str
    worker_id: str
    role: str
    prompt: str
    directory: str = None
    server_url: str = None
    session_id: str = None
    agent: str = None
    model: str = None
    cleanup: bool = False
    default_server_url: str = None


def start_single_worker_run(store, service, **request_kwargs):
    request = PromptedSingleWorkerStartRequest(**request_kwargs)
    ensure_prompt_worker(store, request)
    return service.start(
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


CAPABILITIES = {
    "route_availability": {
        "blocking_message": {"path": "/session/{sessionID}/message", "method": "POST", "available": False},
        "legacy_run": {"path": "/session/{sessionID}/run", "method": "POST", "available": True},
        "legacy_reply": {"path": "/session/{sessionID}/reply", "method": "POST", "available": True},
    },
    "blocking_message_available": False,
    "blocking_execution_available": True,
    "legacy_fallback_available": True,
}

UNSUPPORTED_CAPABILITIES = {
    "route_availability": {
        "blocking_message": {"path": "/session/{sessionID}/message", "method": "POST", "available": False},
        "legacy_run": {"path": "/session/{sessionID}/run", "method": "POST", "available": False},
        "legacy_reply": {"path": "/session/{sessionID}/reply", "method": "POST", "available": False},
    },
    "blocking_message_available": False,
    "blocking_execution_available": False,
    "legacy_fallback_available": False,
}


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeClient:
    def __init__(self, session_ids=None):
        self.timeout = 3
        self.requests = []
        self.session_ids = list(session_ids or ["ses_new"])

    def create_session_response(self, directory, *, agent=None, model=None):
        self.requests.append(("create", directory, agent, model))
        return FakeResponse({"id": self.session_ids.pop(0), "directory": directory})

    def delete_session_response(self, session_id):
        self.requests.append(("delete", session_id))

    def delete_session(self, session_id):
        response = self.delete_session_response(session_id)
        return response.data if response is not None else None

    def get_session(self, session_id):
        self.requests.append(("get", session_id))
        raise OpenCodeApiError(f"session not found: {session_id}", status=404)
