import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.capabilities import configure_client_route_plan, detect_capabilities
from opencode_session.multi_worker_orchestration import (
    DependencyOrderedSerialRunOrchestrationService,
    DependencyOrderedSerialRunStartRequest,
    refresh_orchestration_run_summary,
    workers_in_dependency_order,
)
from opencode_session.prompt_admission import admit_prompt
from opencode_session.remote_journal import (
    PersistedRemoteMutationJournal,
    RemoteMutationOperation,
    RemoteMutationRecovery,
)
from opencode_session.run_prompt_worker import ensure_prompt_worker
from opencode_session.run_record import RunRecordError, upsert_worker_record
from opencode_session.run_store import RunStoreError
from opencode_session.status import short_status
from opencode_session.schema_common import NormalizedAbortRecord, NormalizedAdmissionRecord, RunRecord, Worker
from opencode_session.session_lifecycle import abort_record, is_session_not_found_error
from opencode_session.worker_state import (
    is_worker_mapping,
    mark_dependency_blocked,
    mark_worker_aborted,
    mark_worker_active,
    worker_record_for_mutation,
)


REMOTE_MUTATION_JOURNAL_FIELD = "remote_mutation_journal"
_REMOTE_MUTATION_RECOVERY = RemoteMutationRecovery(REMOTE_MUTATION_JOURNAL_FIELD)
STEER_PROMPT_REMOTE_MUTATION = RemoteMutationOperation(
    kind="steer_prompt",
    discard_cleanup_operation="discard_remote_mutation",
    finalize_cleanup_operation="finalize_remote_mutation",
)
ABORT_WORKER_REMOTE_MUTATION = RemoteMutationOperation(
    kind="abort_worker",
    discard_cleanup_operation="discard_remote_mutation",
    finalize_cleanup_operation="finalize_remote_mutation",
)


@dataclass(frozen=True)
class SteerPromptIntentRecord:
    id: str
    worker_id: str
    session_id: str
    message_id: str
    delivery: str
    text: str

    def to_journal_entry(self):
        return {
            "id": self.id,
            "kind": STEER_PROMPT_REMOTE_MUTATION.kind,
            "worker_id": self.worker_id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "delivery": self.delivery,
            "text": self.text,
        }


@dataclass(frozen=True)
class AbortWorkerIntentRecord:
    id: str
    worker_id: str
    session_id: str

    def to_journal_entry(self):
        return {
            "id": self.id,
            "kind": ABORT_WORKER_REMOTE_MUTATION.kind,
            "worker_id": self.worker_id,
            "session_id": self.session_id,
        }


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
    run: RunRecord
    worker: Optional[Worker] = None
    workers: Sequence[Worker] = ()


@dataclass
class RunSteerResult:
    run: RunRecord
    worker: Worker
    admission: NormalizedAdmissionRecord


@dataclass
class RunAbortResult:
    run: RunRecord
    worker: Worker
    abort: NormalizedAbortRecord
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
        self.remote_mutations = PersistedRemoteMutationJournal(
            REMOTE_MUTATION_JOURNAL_FIELD,
            self._persist_run_mutation,
            now=self.now,
        )

    def create_run(self, name, *, directory, server_url):
        return self.store.create_run(name, directory=directory, server_url=server_url)

    def load_run(self, name):
        return self.store.load_run(name)

    def upsert_worker(self, name, worker_id, **changes):
        lifecycle_state = changes.pop("lifecycle_state", None)
        if lifecycle_state is not None:
            raise RunStoreError(
                "raw lifecycle_state updates are not supported by run commands; "
                "use --status active/blocked or the explicit start/abort/result transitions"
            )
        status = changes.pop("status", None)
        if status is None:
            return self.store.upsert_worker(name, worker_id, **changes)

        status = short_status(status)
        if status not in {"active", "blocked"}:
            raise RunStoreError(
                f"worker status '{status}' cannot be set manually; "
                "use run start, run abort, or reducer-owned result/failure/timeout handling"
            )

        def mutate(run):
            try:
                upsert_worker_record(run, worker_id, changes, now=self.now())
            except RunRecordError as error:
                raise RunStoreError(str(error), kind=error.kind) from error
            worker = run["workers"][worker_id]
            if status == "active":
                transition = mark_worker_active(worker, now=self.now)
            else:
                blockers = [blocker for blocker in changes.get("blockers") or [] if str(blocker).strip()]
                if not blockers:
                    raise RunStoreError("--status blocked requires at least one --blocker")
                transition = mark_dependency_blocked(worker, blockers)
            record = worker_record_for_mutation(worker, worker_id)
            record.apply_transition(transition)
            run["updated_at"] = self.now()

        return self.store.update_run(name, mutate)

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
        configure_client_route_plan(client, capabilities)
        prompt_message_id = message_id or _new_prompt_message_id()
        mutation_id = _new_remote_mutation_id()
        transaction = self._remote_transaction(mutation_id, STEER_PROMPT_REMOTE_MUTATION)

        def prompt_intent(latest_run):
            latest_worker = _run_worker_with_session(latest_run, worker_id)
            return SteerPromptIntentRecord(
                id=mutation_id,
                worker_id=worker_id,
                session_id=latest_worker["session_id"],
                message_id=prompt_message_id,
                delivery=delivery,
                text=text,
            )

        run = transaction.record_intent_from(run, prompt_intent)
        try:
            result = admit_prompt(
                client,
                capabilities,
                worker["session_id"],
                text,
                delivery,
                message_id=prompt_message_id,
            )
        except Exception:
            transaction.discard_intent_best_effort(run)
            raise
        admission = result.record
        admitted_message_id = admission["message_id"]

        def record_prompt_admission(latest_run):
            latest_worker = _run_worker_with_session(latest_run, worker_id)
            latest_record = worker_record_for_mutation(latest_worker, worker_id)
            latest_record.remember_prompt_id(admitted_message_id)

        run = transaction.finalize(run, mutate_run=record_prompt_admission)
        return RunSteerResult(run=run, worker=run["workers"][worker_id], admission=admission)

    def abort_worker(self, name, worker_id):
        run = self.store.load_run(name)
        worker = _run_worker_with_session(run, worker_id)
        client = self.client_factory(run["server_url"])
        mutation_id = _new_remote_mutation_id()
        transaction = self._remote_transaction(mutation_id, ABORT_WORKER_REMOTE_MUTATION)

        def abort_intent(latest_run):
            latest_worker = _run_worker_with_session(latest_run, worker_id)
            return AbortWorkerIntentRecord(
                id=mutation_id,
                worker_id=worker_id,
                session_id=latest_worker["session_id"],
            )

        run = transaction.record_intent_from(run, abort_intent)
        try:
            response = client.abort_session_response(worker["session_id"])
        except Exception as error:
            transaction.discard_intent_best_effort(run)
            if isinstance(error, OpenCodeApiError) and is_session_not_found_error(error):
                raise RunWorkerSessionNotFound(worker["session_id"]) from error
            raise
        abort = abort_record(worker["session_id"], response.data)

        def mark_aborted(latest_run):
            latest_worker = _run_worker_with_session(latest_run, worker_id)
            latest_record = worker_record_for_mutation(latest_worker, worker_id)
            latest_record.apply_transition(mark_worker_aborted(latest_record, abort))
            refresh_orchestration_run_summary(latest_run)

        run = transaction.finalize(run, mutate_run=mark_aborted)
        return RunAbortResult(run=run, worker=run["workers"][worker_id], abort=abort, raw_body=response.body)

    def _remote_transaction(self, mutation_id, operation):
        return self.remote_mutations.transaction(
            mutation_id,
            operation,
        )

    def _persist_run_mutation(self, run, mutator):
        return self._update_run(run["name"], mutator)

    def _update_run(self, name, mutator):
        def update(run):
            mutator(run)
            run["updated_at"] = self.now()

        return self.store.update_run(name, update)


class RunWorkerSessionNotFound(Exception):
    def __init__(self, session_id):
        super().__init__(f"session not found: {session_id}")
        self.session_id = session_id


def recoverable_remote_mutation_entries(run, *, kind=None):
    return _REMOTE_MUTATION_RECOVERY.pending_entries(run, kind=kind)


def _worker_result(run, worker_id):
    worker = run.get("workers", {}).get(worker_id)
    if not is_worker_mapping(worker):
        raise RunStoreError(f"worker '{worker_id}' not found in run '{run['name']}'", kind="missing")
    result = worker.get("result")
    if not isinstance(result, dict):
        raise RunStoreError(f"worker '{worker_id}' in run '{run['name']}' has no collected result", kind="missing")
    return worker


def _run_worker_with_session(run, worker_id):
    worker = run.get("workers", {}).get(worker_id)
    if not is_worker_mapping(worker):
        raise RunStoreError(f"worker '{worker_id}' not found in run '{run['name']}'", kind="missing")
    if not worker.get("session_id"):
        raise RunStoreError(f"worker '{worker_id}' in run '{run['name']}' has no session", kind="missing")
    return worker


def _new_prompt_message_id():
    return f"msg_{uuid.uuid4().hex}"


def _new_remote_mutation_id():
    return f"remote_mutation_{uuid.uuid4().hex}"


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
