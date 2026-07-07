import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.api_transport import OpenCodeApiError
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
    RemoteJournalRecord,
    RemoteMutationOperation,
    RemoteMutationRecovery,
    RemoteMutationResult,
)
from opencode_session.run_persistence import persist_worker_transitions
from opencode_session.run_prompt_worker import ensure_prompt_worker
from opencode_session.run_record import RunRecordError, upsert_worker_record
from opencode_session.run_store import RunStoreError
from opencode_session.schema_admission import NormalizedAdmissionRecord
from opencode_session.schema_run import RunRecord
from opencode_session.schema_session import NormalizedAbortRecord
from opencode_session.schema_worker import HydratedWorker
from opencode_session.status import short_status
from opencode_session.session_lifecycle import abort_record, is_session_not_found_error
from opencode_session.worker_active_attempt_recovery import recover_expired_active_attempts
from opencode_session.worker_state import (
    is_worker_record,
    mark_dependency_blocked,
    mark_worker_aborted,
    mark_worker_active,
    worker_record_for_mutation,
)


REMOTE_MUTATION_JOURNAL_FIELD = "remote_mutation_journal"
_REMOTE_MUTATION_RECOVERY = RemoteMutationRecovery(REMOTE_MUTATION_JOURNAL_FIELD)
STEER_PROMPT_KIND = "steer_prompt"
ABORT_WORKER_KIND = "abort_worker"
DISCARD_REMOTE_MUTATION_OPERATION = "discard_remote_mutation"
FINALIZE_REMOTE_MUTATION_OPERATION = "finalize_remote_mutation"
CALL_STEER_PROMPT_OPERATION = "call_steer_prompt"
CALL_ABORT_WORKER_OPERATION = "call_abort_worker"


STEER_PROMPT_REMOTE_MUTATION = RemoteMutationOperation(
    kind=STEER_PROMPT_KIND,
    discard_cleanup_operation=DISCARD_REMOTE_MUTATION_OPERATION,
    finalize_cleanup_operation=FINALIZE_REMOTE_MUTATION_OPERATION,
    call_remote_operation=CALL_STEER_PROMPT_OPERATION,
)
ABORT_WORKER_REMOTE_MUTATION = RemoteMutationOperation(
    kind=ABORT_WORKER_KIND,
    discard_cleanup_operation=DISCARD_REMOTE_MUTATION_OPERATION,
    finalize_cleanup_operation=FINALIZE_REMOTE_MUTATION_OPERATION,
    call_remote_operation=CALL_ABORT_WORKER_OPERATION,
)


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
    worker: Optional[HydratedWorker] = None
    workers: Sequence[HydratedWorker] = ()


@dataclass
class RunSteerResult:
    run: RunRecord
    worker: HydratedWorker
    admission: NormalizedAdmissionRecord


@dataclass
class RunAbortResult:
    run: RunRecord
    worker: HydratedWorker
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
        run = self.store.load_run(name)
        return self._recover_active_attempts(run)

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
            worker for worker in workers_in_dependency_order(workers) if isinstance(worker.result, dict)
        )
        if not completed_workers:
            raise RunStoreError(f"run '{name}' has no collected worker results", kind="missing")
        return RunCollectResult(run, workers=completed_workers)

    def steer_worker(self, name, worker_id, text, *, delivery, message_id=None):
        run = self.store.load_run(name)
        _run_worker_with_session(run, worker_id)
        client = self.client_factory(run["server_url"])
        capabilities = self.capability_detector(client)
        configure_client_route_plan(client, capabilities)
        prompt_message_id = message_id or _new_prompt_message_id()
        mutation_id = _new_remote_mutation_id()

        def intent_from_run(latest_run):
            latest_worker = _run_worker_with_session(latest_run, worker_id)
            return _steer_prompt_intent(
                mutation_id,
                worker_id=worker_id,
                session_id=latest_worker.session_id,
                message_id=prompt_message_id,
                delivery=delivery,
                text=text,
            )

        def call_remote(latest_run, intent):
            return admit_prompt(
                client,
                capabilities,
                intent.fields["session_id"],
                intent.fields["text"],
                intent.fields["delivery"],
                message_id=intent.fields["message_id"],
            )

        def apply_result(result, intent):
            admission = result.record
            admitted_worker_id = intent.fields["worker_id"]
            admitted_message_id = admission["message_id"]
            return RemoteMutationResult(
                mutate_run=lambda latest_run: _remember_prompt_id(
                    latest_run,
                    admitted_worker_id,
                    admitted_message_id,
                )
            )

        transaction = self._remote_transaction(mutation_id, STEER_PROMPT_REMOTE_MUTATION)

        execution = transaction.runner().execute(
            run,
            intent_factory=intent_from_run,
            call_remote=call_remote,
            apply_result=apply_result,
        )
        run = execution.run
        admission = execution.remote_result.record
        return RunSteerResult(run=run, worker=run["workers"][worker_id], admission=admission)

    def abort_worker(self, name, worker_id):
        run = self.store.load_run(name)
        _run_worker_with_session(run, worker_id)
        client = self.client_factory(run["server_url"])
        mutation_id = _new_remote_mutation_id()

        def intent_from_run(latest_run):
            latest_worker = _run_worker_with_session(latest_run, worker_id)
            return _abort_worker_intent(
                mutation_id,
                worker_id=worker_id,
                session_id=latest_worker.session_id,
            )

        def call_remote(latest_run, intent):
            session_id = intent.fields["session_id"]
            try:
                return client.abort_session_response(session_id)
            except OpenCodeApiError as error:
                if is_session_not_found_error(error):
                    raise RunWorkerSessionNotFound(session_id) from error
                raise

        def apply_result(response, intent):
            aborted_worker_id = intent.fields["worker_id"]
            aborted_session_id = intent.fields["session_id"]
            return RemoteMutationResult(
                mutate_run=lambda latest_run: _apply_abort_worker(
                    latest_run,
                    aborted_worker_id,
                    aborted_session_id,
                    response.data,
                )
            )

        transaction = self._remote_transaction(mutation_id, ABORT_WORKER_REMOTE_MUTATION)

        execution = transaction.runner().execute(
            run,
            intent_factory=intent_from_run,
            call_remote=call_remote,
            apply_result=apply_result,
        )
        run = execution.run
        response = execution.remote_result
        abort = abort_record(execution.intent.fields["session_id"], response.data)
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

    def _recover_active_attempts(self, run):
        recoveries = recover_expired_active_attempts(run.get("workers", {}), now=self.now)
        if not recoveries:
            return run
        return persist_worker_transitions(
            self.store,
            run,
            [recovery.transition for recovery in recoveries],
            refresh_run_summary=refresh_orchestration_run_summary,
            now=self.now,
        ).run


class RunWorkerSessionNotFound(Exception):
    def __init__(self, session_id):
        super().__init__(f"session not found: {session_id}")
        self.session_id = session_id


def recoverable_remote_mutation_entries(run, *, kind=None):
    return _REMOTE_MUTATION_RECOVERY.pending_entries(run, kind=kind)


def _steer_prompt_intent(mutation_id, *, worker_id, session_id, message_id, delivery, text):
    return RemoteJournalRecord(
        id=mutation_id,
        kind=STEER_PROMPT_KIND,
        fields={
            "worker_id": worker_id,
            "session_id": session_id,
            "message_id": message_id,
            "delivery": delivery,
            "text": text,
        },
    )


def _remember_prompt_id(latest_run, worker_id, message_id):
    latest_worker = _run_worker_with_session(latest_run, worker_id)
    latest_record = worker_record_for_mutation(latest_worker, worker_id)
    latest_record.remember_prompt_id(message_id)


def _abort_worker_intent(mutation_id, *, worker_id, session_id):
    return RemoteJournalRecord(
        id=mutation_id,
        kind=ABORT_WORKER_KIND,
        fields={
            "worker_id": worker_id,
            "session_id": session_id,
        },
    )


def _apply_abort_worker(latest_run, worker_id, session_id, response_data):
    latest_worker = _run_worker_with_session(latest_run, worker_id)
    latest_record = worker_record_for_mutation(latest_worker, worker_id)
    abort = abort_record(session_id, response_data)
    latest_record.apply_transition(mark_worker_aborted(latest_record, abort))
    refresh_orchestration_run_summary(latest_run)


def _worker_result(run, worker_id):
    worker = run.get("workers", {}).get(worker_id)
    if not is_worker_record(worker):
        raise RunStoreError(f"worker '{worker_id}' not found in run '{run['name']}'", kind="missing")
    result = worker.result
    if not isinstance(result, dict):
        raise RunStoreError(f"worker '{worker_id}' in run '{run['name']}' has no collected result", kind="missing")
    return worker


def _run_worker_with_session(run, worker_id):
    worker = run.get("workers", {}).get(worker_id)
    if not is_worker_record(worker):
        raise RunStoreError(f"worker '{worker_id}' not found in run '{run['name']}'", kind="missing")
    if not worker.session_id:
        raise RunStoreError(f"worker '{worker_id}' in run '{run['name']}' has no session", kind="missing")
    return worker


def _new_prompt_message_id():
    return f"msg_{uuid.uuid4().hex}"


def _new_remote_mutation_id():
    return f"remote_mutation_{uuid.uuid4().hex}"


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
