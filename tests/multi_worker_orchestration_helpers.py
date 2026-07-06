import tempfile

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.multi_worker_orchestration import DependencyOrderedSerialRunOrchestrationService
from opencode_session.run_store import RunStore


RUN_NAME = "demo"
SERVER_URL = "http://opencode.example"
NOW = "2026-07-03T00:00:00Z"


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
    def __init__(self, session_ids, *, delete_failures=None):
        self.requests = []
        self.session_ids = list(session_ids)
        self.delete_failures = dict(delete_failures or {})

    def create_session_response(self, directory, *, agent=None, model=None):
        self.requests.append(("create", directory, agent, model))
        return FakeResponse({"id": self.session_ids.pop(0), "directory": directory})

    def delete_session_response(self, session_id):
        self.requests.append(("delete", session_id))
        if session_id in self.delete_failures:
            raise OpenCodeApiError(self.delete_failures[session_id])

    def delete_session(self, session_id):
        response = self.delete_session_response(session_id)
        return response.data if response is not None else None

    def get_session(self, session_id):
        self.requests.append(("get", session_id))
        raise OpenCodeApiError(f"session not found: {session_id}", status=404)


class DependencyOrderedSerialServiceScenario:
    def __init__(
        self,
        test_case,
        *,
        client=None,
        session_ids=None,
        capabilities=CAPABILITIES,
        capability_detector=None,
        executor=None,
    ):
        self.test_case = test_case
        self.client = client if client is not None else FakeClient([] if session_ids is None else session_ids)
        self.capabilities = capabilities
        self.capability_detector = capability_detector
        self.executor = executor or self._unexpected_execute

    def __enter__(self):
        self._store_root = tempfile.TemporaryDirectory()
        self._directory = tempfile.TemporaryDirectory()
        self.directory = self._directory.name
        self.store = RunStore(self._store_root.name)
        self.store.create_run(RUN_NAME, directory=self.directory, server_url=SERVER_URL)
        return self

    def __exit__(self, exc_type, exc, tb):
        self._directory.cleanup()
        self._store_root.cleanup()

    def add_worker(self, worker_id, **changes):
        changes.setdefault("role", worker_id)
        status = changes.pop("status", None)
        if status is not None:
            changes["lifecycle_state"] = _LIFECYCLE_STATE_BY_STATUS[status]
        self.store.upsert_worker(RUN_NAME, worker_id, **changes)

    def request(self, worker_id, *, role=None, **changes):
        from opencode_session.multi_worker_orchestration import DependencyOrderedSerialRunStartRequest

        return DependencyOrderedSerialRunStartRequest(
            name=RUN_NAME,
            worker_id=worker_id,
            role=role or worker_id,
            **changes,
        )

    def service(self, *, store=None, client=None, capability_detector=None, executor=None):
        selected_detector = capability_detector if capability_detector is not None else self.capability_detector
        if selected_detector is None:
            selected_detector = lambda client: self.capabilities
        selected_client = client if client is not None else self.client
        return DependencyOrderedSerialRunOrchestrationService(
            store or self.store,
            client_factory=lambda url: selected_client,
            capability_detector=selected_detector,
            executor=executor or self.executor,
            now=lambda: NOW,
        )

    def start(self, worker_id, *, role=None, **request_changes):
        return self.service().start(self.request(worker_id, role=role, **request_changes))

    def load_run(self):
        return self.store.load_run(RUN_NAME)

    def _unexpected_execute(self, *args, **kwargs):
        self.test_case.fail("worker should not execute")


def assert_single_worker_attempt(test_case, worker, *, status, session_id):
    attempts = worker.get("attempts")
    test_case.assertIsInstance(attempts, list)
    test_case.assertEqual(len(attempts), 1)
    attempt = attempts[0]
    test_case.assertEqual(attempt.get("session_id"), session_id)
    test_case.assertEqual(attempt.get("status"), status)
    return attempt


def assert_blocked_worker(test_case, run, worker_id, blockers):
    worker = run["workers"][worker_id]
    test_case.assertEqual(worker["status"], "blocked")
    test_case.assertEqual(worker["blockers"], blockers)
    test_case.assertEqual(worker["next_eligible_action"], "resolve_blocker")


_LIFECYCLE_STATE_BY_STATUS = {
    "queued": "queued",
    "active": "active_wait",
    "blocked": "blocked_dependency",
    "done": "done_collect",
    "failed": "failed_terminal",
    "aborted": "aborted",
    "timeout": "timeout_terminal",
}
