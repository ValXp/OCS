import uuid
from dataclasses import dataclass
from typing import Optional

from opencode_session.remote_journal import (
    PersistedRemoteMutationJournal,
    RemoteMutationApplication,
    RemoteMutationOperation,
)
from opencode_session.schema_run import RunRecord
from opencode_session.schema_worker import HydratedWorker
from opencode_session.session_ids import require_session_id
from opencode_session.worker_state import (
    WorkerRecord,
    is_worker_record,
    worker_record_for_mutation,
)


WORKER_SESSION_JOURNAL_FIELD = "worker_session_journal"
WORKER_SESSION_CREATE_KIND = "worker_session_create"
WORKER_SESSION_CREATE_OPERATION = RemoteMutationOperation(
    kind=WORKER_SESSION_CREATE_KIND,
    discard_cleanup_operation="discard_worker_session_create",
    finalize_cleanup_operation="finalize_worker_session_create",
)


@dataclass
class WorkerSessionOutcome:
    session_id: Optional[str]
    created_session_id: Optional[str] = None


@dataclass(frozen=True)
class WorkerSessionCreationIntent:
    id: str
    worker_id: str
    directory: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    cleanup_requested: bool = False
    intent_recorded_at: Optional[str] = None

    def to_journal_entry(self):
        entry = {
            "id": self.id,
            "kind": WORKER_SESSION_CREATE_OPERATION.kind,
            "status": "intent",
            "worker_id": self.worker_id,
            "directory": self.directory,
            "cleanup_requested": self.cleanup_requested,
            "intent_recorded_at": self.intent_recorded_at,
        }
        if self.agent is not None:
            entry["agent"] = self.agent
        if self.model is not None:
            entry["model"] = self.model
        return entry


@dataclass(frozen=True)
class WorkerSessionCreatedRecord:
    id: str
    worker_id: str
    session_id: str
    cleanup_requested: bool
    created_at: str

    def to_journal_update(self):
        return {
            "status": "created",
            "session_id": self.session_id,
            "created_session_ids": [self.session_id],
            "created_at": self.created_at,
        }

    def to_journal_entry(self):
        return {
            "id": self.id,
            "kind": WORKER_SESSION_CREATE_OPERATION.kind,
            "worker_id": self.worker_id,
            "cleanup_requested": self.cleanup_requested,
            **self.to_journal_update(),
        }


@dataclass
class WorkerSessionProvisioning:
    run: RunRecord
    worker: HydratedWorker
    outcome: WorkerSessionOutcome
    intent: Optional[WorkerSessionCreationIntent] = None


class WorkerSessionCreationJournal:
    def __init__(self, persist_run_mutation, *, now, id_factory=None):
        self.persist_run_mutation = persist_run_mutation
        self.now = now
        self.id_factory = id_factory or _new_worker_session_journal_id
        self.transactions = PersistedRemoteMutationJournal(
            WORKER_SESSION_JOURNAL_FIELD,
            self.persist_run_mutation,
            now=self.now,
        )

    def run_creation(self, run, worker, *, call_remote, agent=None, model=None, cleanup_requested=False):
        intent_id = self.id_factory()
        transaction = self.transactions.transaction(intent_id, WORKER_SESSION_CREATE_OPERATION)

        def creation_intent(latest_run):
            latest_worker = _coerce_worker_record(latest_run, worker)
            return WorkerSessionCreationIntent(
                id=intent_id,
                worker_id=latest_worker.worker_id,
                directory=latest_run.get("directory"),
                agent=agent,
                model=model,
                cleanup_requested=bool(cleanup_requested),
                intent_recorded_at=self.now(),
            )

        def apply_created_session(session_outcome, intent):
            if session_outcome.created_session_id is None:
                return RemoteMutationApplication(finalize=False)
            return self._created_session_application(
                intent,
                session_outcome.created_session_id,
                agent=agent,
                model=model,
            )

        execution = transaction.runner().execute(
            run,
            intent_factory=creation_intent,
            call_remote=call_remote,
            apply_result=apply_created_session,
        )
        return execution.run, _latest_worker(execution.run, worker), execution.remote_result, execution.intent

    def record_intent(self, run, worker, *, agent=None, model=None, cleanup_requested=False):
        intent = WorkerSessionCreationIntent(
            id=self.id_factory(),
            worker_id=worker.worker_id,
            directory=run.get("directory"),
            agent=agent,
            model=model,
            cleanup_requested=bool(cleanup_requested),
            intent_recorded_at=self.now(),
        )

        updated_run = self._transaction(intent).record_intent(run, intent)
        return updated_run, _latest_worker(updated_run, worker), intent

    def record_created(self, run, worker, intent, session_id, *, agent=None, model=None):
        application = self._created_session_application(intent, session_id, agent=agent, model=model)
        updated_run = self._transaction(intent).mark_applied(
            run,
            application.journal_update,
            mutate_run=application.mutate_run,
            append_if_missing=application.append_if_missing,
        )
        return updated_run, _latest_worker(updated_run, worker)

    def discard_intent_best_effort(self, run, worker, intent):
        updated_run = self._transaction(intent).discard_intent_best_effort(run)
        return updated_run, _latest_worker(updated_run, worker)

    def finalize_best_effort(self, run, worker, intent):
        updated_run = self._transaction(intent).finalize_best_effort(run)
        return updated_run, _latest_worker(updated_run, worker)

    def _transaction(self, intent):
        return self.transactions.transaction(
            intent.id,
            WORKER_SESSION_CREATE_OPERATION,
        )

    def _created_session_application(self, intent, session_id, *, agent=None, model=None):
        created_record = WorkerSessionCreatedRecord(
            id=intent.id,
            worker_id=intent.worker_id,
            session_id=session_id,
            cleanup_requested=intent.cleanup_requested,
            created_at=self.now(),
        )

        def update_worker(latest_run):
            latest_worker = _ensure_latest_worker(latest_run, intent.worker_id)
            latest_record = worker_record_for_mutation(latest_worker, intent.worker_id)
            latest_record.set_session(session_id, agent=agent, model=model)
            if intent.cleanup_requested:
                latest_record.remember_session_for_cleanup(session_id)

        return RemoteMutationApplication(
            journal_update=created_record,
            mutate_run=update_worker,
            append_if_missing=True,
            finalize=False,
        )


class WorkerSessionProvisioner:
    def __init__(self, session_journal=None):
        self.session_journal = session_journal

    def provision(
        self,
        client,
        run,
        worker,
        *,
        session_id=None,
        agent=None,
        model=None,
        create_session=True,
        cleanup_requested=False,
    ):
        if self.session_journal is not None and will_create_worker_session(
            worker,
            session_id=session_id,
            create_session=create_session,
        ):
            def create_worker_session(latest_run, intent):
                latest_worker = _ensure_latest_worker(latest_run, intent.worker_id)
                return provision_worker_session(
                    client,
                    latest_run,
                    latest_worker,
                    session_id=session_id,
                    agent=agent,
                    model=model,
                    create_session=create_session,
                )

            run, worker, session_outcome, session_intent = self.session_journal.run_creation(
                run,
                worker,
                call_remote=create_worker_session,
                agent=agent,
                model=model,
                cleanup_requested=cleanup_requested,
            )
            return WorkerSessionProvisioning(run, worker, session_outcome, session_intent)

        worker = _coerce_worker_record(run, worker)
        session_outcome = provision_worker_session(
            client,
            run,
            worker,
            session_id=session_id,
            agent=agent,
            model=model,
            create_session=create_session,
        )
        return WorkerSessionProvisioning(run, worker, session_outcome)

    def finalize_best_effort(self, run, worker, provisioning):
        if provisioning.intent is None:
            return run, worker
        return self.session_journal.finalize_best_effort(run, worker, provisioning.intent)


def ensure_worker_session(
    client,
    run,
    worker,
    *,
    session_id=None,
    agent=None,
    model=None,
    treat_falsey_session_as_missing=False,
):
    record = _coerce_worker_record(run, worker)
    worker_session_id = session_id or record.session_id
    created_session_id = None
    missing_session = not worker_session_id if treat_falsey_session_as_missing else worker_session_id is None
    if missing_session:
        create_response = client.create_session_response(run["directory"], agent=agent, model=model)
        worker_session_id = require_session_id(create_response)
        created_session_id = worker_session_id
    record.set_session(worker_session_id, agent=agent, model=model)
    return WorkerSessionOutcome(worker_session_id, created_session_id)


def provision_worker_session(
    client,
    run,
    worker,
    *,
    session_id=None,
    agent=None,
    model=None,
    create_session=True,
):
    if create_session:
        return ensure_worker_session(
            client,
            run,
            worker,
            session_id=session_id,
            agent=agent,
            model=model,
            treat_falsey_session_as_missing=True,
        )
    record = _coerce_worker_record(run, worker)
    record.set_session(session_id or record.session_id, agent=agent, model=model)
    return WorkerSessionOutcome(record.session_id)


def will_create_worker_session(worker, *, session_id=None, create_session=True):
    if not create_session:
        return False
    return not (session_id or worker.session_id)


def _latest_worker(run, fallback_worker):
    worker_id = fallback_worker.worker_id if is_worker_record(fallback_worker) else None
    if isinstance(run, dict) and worker_id:
        return _ensure_latest_worker(run, worker_id)
    if isinstance(fallback_worker, WorkerRecord):
        return fallback_worker
    return worker_record_for_mutation(fallback_worker, worker_id).to_worker()


def _ensure_latest_worker(run, worker_id):
    workers = run.setdefault("workers", {})
    worker = workers.get(worker_id)
    if isinstance(worker, WorkerRecord):
        return worker
    if is_worker_record(worker):
        worker = worker_record_for_mutation(worker, worker_id).to_worker()
    else:
        worker = WorkerRecord.default_fields(worker_id)
    workers[worker_id] = worker
    return worker


def _coerce_worker_record(run, worker):
    if isinstance(worker, WorkerRecord):
        return worker
    worker_id = worker.worker_id if is_worker_record(worker) else None
    if isinstance(run, dict) and worker_id:
        return _ensure_latest_worker(run, worker_id)
    return worker_record_for_mutation(worker, worker_id).to_worker()


def _new_worker_session_journal_id():
    return f"worker_session_create_{uuid.uuid4().hex}"
