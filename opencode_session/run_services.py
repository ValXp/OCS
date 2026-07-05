from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.capabilities import detect_capabilities
from opencode_session.multi_worker_orchestration import (
    DependencyOrderedSerialRunOrchestrationService,
    DependencyOrderedSerialRunStartRequest,
    refresh_orchestration_run_summary,
    workers_in_dependency_order,
)
from opencode_session.prompt_admission import admit_prompt
from opencode_session.run_prompt_worker import ensure_prompt_worker
from opencode_session.run_store import RunStoreError
from opencode_session.session_lifecycle import abort_record, is_session_not_found_error
from opencode_session.worker_state import mark_worker_aborted


@dataclass
class RunStartRequest:
    name: str
    worker_id: str
    role: str
    prompt: Optional[str] = None
    directory: Optional[str] = None
    server_url: Optional[str] = None
    session_id: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    execution_policy: str = "fail_fast"
    cleanup: bool = False
    default_server_url: Optional[str] = None


@dataclass
class RunCollectResult:
    run: dict
    worker: Optional[dict] = None
    workers: Sequence[dict] = ()


@dataclass
class RunSteerResult:
    run: dict
    worker: dict
    admission: dict


@dataclass
class RunAbortResult:
    run: dict
    worker: dict
    abort: dict
    raw_body: str


class RunCommandService:
    def __init__(
        self,
        store,
        *,
        client_factory=OpenCodeApiClient,
        capability_detector=detect_capabilities,
        now=None,
    ):
        self.store = store
        self.client_factory = client_factory
        self.capability_detector = capability_detector
        self.now = now or _utc_now

    def create_run(self, name, *, directory, server_url):
        return self.store.create_run(name, directory=directory, server_url=server_url)

    def load_run(self, name):
        return self.store.load_run(name)

    def upsert_worker(self, name, worker_id, **changes):
        return self.store.upsert_worker(name, worker_id, **changes)

    def start_run(self, request):
        if request.prompt is not None:
            ensure_prompt_worker(self.store, request)
        return DependencyOrderedSerialRunOrchestrationService(
            self.store,
            client_factory=self.client_factory,
            capability_detector=self.capability_detector,
            now=self.now,
        ).start(
            DependencyOrderedSerialRunStartRequest(
                name=request.name,
                worker_id=request.worker_id,
                role=request.role,
                directory=request.directory,
                server_url=request.server_url,
                session_id=request.session_id,
                agent=request.agent,
                model=request.model,
                execution_policy=request.execution_policy,
                cleanup=request.cleanup,
            )
        )

    def collect_results(self, name, *, worker_id=None):
        run = self.store.load_run(name)
        workers = run.get("workers", {})
        if worker_id is not None:
            return RunCollectResult(run, worker=_worker_result(run, worker_id))
        if len(workers) == 1:
            only_worker_id = next(iter(workers))
            return RunCollectResult(run, worker=_worker_result(run, only_worker_id))
        completed_workers = tuple(
            worker for worker in workers_in_dependency_order(workers) if isinstance(worker.get("result"), dict)
        )
        if not completed_workers:
            raise RunStoreError(f"run '{name}' has no collected worker results", kind="missing")
        return RunCollectResult(run, workers=completed_workers)

    def steer_worker(self, name, worker_id, text, *, delivery, message_id=None):
        run = self.store.load_run(name)
        worker = _run_worker_with_session(run, worker_id)
        client = self.client_factory(run["server_url"])
        capabilities = self.capability_detector(client)
        result = admit_prompt(client, capabilities, worker["session_id"], text, delivery, message_id=message_id)
        admission = result.record
        admitted_message_id = admission["message_id"]

        def append_prompt_id(latest_run):
            latest_worker = _run_worker_with_session(latest_run, worker_id)
            prompt_ids = latest_worker.setdefault("prompt_ids", [])
            if admitted_message_id not in prompt_ids:
                prompt_ids.append(admitted_message_id)

        run = self._update_run(name, append_prompt_id)
        return RunSteerResult(run=run, worker=run["workers"][worker_id], admission=admission)

    def abort_worker(self, name, worker_id):
        run = self.store.load_run(name)
        worker = _run_worker_with_session(run, worker_id)
        client = self.client_factory(run["server_url"])
        try:
            response = client.abort_session_response(worker["session_id"])
        except OpenCodeApiError as error:
            if is_session_not_found_error(error):
                raise RunWorkerSessionNotFound(worker["session_id"]) from error
            raise
        abort = abort_record(worker["session_id"], response.data)

        def mark_aborted(latest_run):
            latest_worker = _run_worker_with_session(latest_run, worker_id)
            mark_worker_aborted(latest_worker, abort)
            refresh_orchestration_run_summary(latest_run)

        run = self._update_run(name, mark_aborted)
        return RunAbortResult(run=run, worker=run["workers"][worker_id], abort=abort, raw_body=response.body)

    def _update_run(self, name, mutator):
        def update(run):
            mutator(run)
            run["updated_at"] = self.now()

        return self.store.update_run(name, update)

class RunWorkerSessionNotFound(Exception):
    def __init__(self, session_id):
        super().__init__(f"session not found: {session_id}")
        self.session_id = session_id


def _worker_result(run, worker_id):
    worker = run.get("workers", {}).get(worker_id)
    if not isinstance(worker, dict):
        raise RunStoreError(f"worker '{worker_id}' not found in run '{run['name']}'", kind="missing")
    result = worker.get("result")
    if not isinstance(result, dict):
        raise RunStoreError(f"worker '{worker_id}' in run '{run['name']}' has no collected result", kind="missing")
    return worker


def _run_worker_with_session(run, worker_id):
    worker = run.get("workers", {}).get(worker_id)
    if not isinstance(worker, dict):
        raise RunStoreError(f"worker '{worker_id}' not found in run '{run['name']}'", kind="missing")
    if not worker.get("session_id"):
        raise RunStoreError(f"worker '{worker_id}' in run '{run['name']}' has no session", kind="missing")
    return worker


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
