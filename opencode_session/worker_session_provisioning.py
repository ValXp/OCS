import uuid
from dataclasses import dataclass
from typing import Optional

from opencode_session.remote_journal import (
    MISSING_INTENT_RECORD_APPLIED,
    OUTBOX_STATE_APPLIED,
    OUTBOX_STATE_REMOTE_SUCCEEDED,
    PersistedRemoteMutationJournal,
    RemoteJournalRecord,
    RemoteMutationOperation,
    RemoteMutationRecovery,
    RemoteMutationResult,
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
DISCARD_WORKER_SESSION_CREATE_OPERATION = "discard_worker_session_create"
FINALIZE_WORKER_SESSION_CREATE_OPERATION = "finalize_worker_session_create"
CALL_WORKER_SESSION_CREATE_OPERATION = "call_worker_session_create"


WORKER_SESSION_CREATE_OPERATION = RemoteMutationOperation(
    kind=WORKER_SESSION_CREATE_KIND,
    discard_cleanup_operation=DISCARD_WORKER_SESSION_CREATE_OPERATION,
    finalize_cleanup_operation=FINALIZE_WORKER_SESSION_CREATE_OPERATION,
    call_remote_operation=CALL_WORKER_SESSION_CREATE_OPERATION,
)
_WORKER_SESSION_RECOVERY = RemoteMutationRecovery(WORKER_SESSION_JOURNAL_FIELD)


@dataclass
class WorkerSessionOutcome:
    session_id: Optional[str]
    created_session_id: Optional[str] = None


@dataclass(frozen=True)
class WorkerSessionCreationJournalEntry:
    worker_id: str
    cleanup_requested: bool
    session_ids: tuple

    @classmethod
    def from_journal_entry(cls, entry):
        if not isinstance(entry, dict) or entry.get("kind") != WORKER_SESSION_CREATE_KIND:
            return None
        worker_id = entry.get("worker_id")
        if not isinstance(worker_id, str) or not worker_id:
            return None
        session_ids = []
        for session_id in _string_list(entry.get("created_session_ids")):
            _append_unique_session_id(session_ids, session_id)
        _append_unique_session_id(session_ids, entry.get("session_id"))
        return cls(
            worker_id=worker_id,
            cleanup_requested=entry.get("cleanup_requested") is True,
            session_ids=tuple(session_ids),
        )


@dataclass
class WorkerSessionProvisioning:
    run: RunRecord
    worker: HydratedWorker
    outcome: WorkerSessionOutcome
    intent: Optional[RemoteJournalRecord] = None


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

        def intent_from_run(latest_run):
            latest_worker = _coerce_worker_record(latest_run, worker)
            return _worker_session_creation_intent(
                intent_id,
                latest_run,
                latest_worker,
                agent=agent,
                model=model,
                cleanup_requested=cleanup_requested,
                intent_recorded_at=self.now(),
            )

        def apply_created_session(session_outcome, intent):
            if session_outcome.created_session_id is None:
                return RemoteMutationResult.keep_pending()
            return self._created_session_result(
                intent,
                session_outcome.created_session_id,
                agent=agent,
                model=model,
            )

        transaction = self.transactions.transaction(intent_id, WORKER_SESSION_CREATE_OPERATION)

        execution = transaction.runner().execute(
            run,
            intent_factory=intent_from_run,
            call_remote=call_remote,
            apply_result=apply_created_session,
        )
        return execution.run, _latest_worker(execution.run, worker), execution.remote_result, execution.intent

    def record_intent(self, run, worker, *, agent=None, model=None, cleanup_requested=False):
        intent = _worker_session_creation_intent(
            self.id_factory(),
            run,
            worker,
            agent=agent,
            model=model,
            cleanup_requested=cleanup_requested,
            intent_recorded_at=self.now(),
        )

        updated_run = self._transaction(intent).record_intent(run, intent)
        return updated_run, _latest_worker(updated_run, worker), intent

    def record_created(self, run, worker, intent, session_id, *, agent=None, model=None):
        result = self._created_session_result(intent, session_id, agent=agent, model=model)
        updated_run = self._transaction(intent).mark_applied(
            run,
            result.applied_record,
            run_update=result.run_update,
            missing_intent_policy=result.missing_intent_policy,
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

    def _created_session_result(self, intent, session_id, *, agent=None, model=None):
        return _created_session_result(
            intent,
            session_id,
            created_at=self.now(),
            agent=agent,
            model=model,
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
            def call_remote(latest_run, intent):
                latest_worker = _ensure_latest_worker(latest_run, intent.fields["worker_id"])
                return provision_worker_session(
                    client,
                    latest_run,
                    latest_worker,
                    session_id=session_id,
                    agent=agent,
                    model=model,
                    create_session=create_session,
                    session_metadata=worker_session_creation_metadata(latest_run, intent),
                )

            run, worker, session_outcome, session_intent = self.session_journal.run_creation(
                run,
                worker,
                call_remote=call_remote,
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
    session_metadata=None,
    treat_falsey_session_as_missing=False,
):
    record = _coerce_worker_record(run, worker)
    worker_session_id = session_id or record.session_id
    created_session_id = None
    missing_session = not worker_session_id if treat_falsey_session_as_missing else worker_session_id is None
    if missing_session:
        create_options = {"agent": agent, "model": model}
        if session_metadata is not None:
            create_options["metadata"] = session_metadata
        create_response = client.create_session_response(run["directory"], **create_options)
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
    session_metadata=None,
):
    if create_session:
        return ensure_worker_session(
            client,
            run,
            worker,
            session_id=session_id,
            agent=agent,
            model=model,
            session_metadata=session_metadata,
            treat_falsey_session_as_missing=True,
        )
    record = _coerce_worker_record(run, worker)
    record.set_session(session_id or record.session_id, agent=agent, model=model)
    return WorkerSessionOutcome(record.session_id)


def will_create_worker_session(worker, *, session_id=None, create_session=True):
    if not create_session:
        return False
    return not (session_id or worker.session_id)


def worker_session_creation_metadata(run, intent):
    metadata = {
        "ocs.remote_mutation_kind": WORKER_SESSION_CREATE_KIND,
        "ocs.remote_mutation_id": intent.id,
        "ocs.worker_id": intent.fields["worker_id"],
        "ocs.cleanup_requested": "true" if intent.fields.get("cleanup_requested") is True else "false",
    }
    run_name = run.get("name")
    if isinstance(run_name, str) and run_name:
        metadata["ocs.run_name"] = run_name
    return metadata


def recoverable_worker_session_creations_by_worker(run):
    session_ids_by_worker = {}
    for entry in _WORKER_SESSION_RECOVERY.pending_entries(
        run,
        kind=WORKER_SESSION_CREATE_KIND,
        outbox_states=(OUTBOX_STATE_REMOTE_SUCCEEDED, OUTBOX_STATE_APPLIED),
    ):
        creation = WorkerSessionCreationJournalEntry.from_journal_entry(entry)
        if creation is None or not creation.cleanup_requested:
            continue
        session_ids = session_ids_by_worker.setdefault(creation.worker_id, [])
        for session_id in creation.session_ids:
            _append_unique_session_id(session_ids, session_id)
    return {worker_id: session_ids for worker_id, session_ids in session_ids_by_worker.items() if session_ids}


def _worker_session_creation_intent(
    intent_id,
    run,
    worker,
    *,
    agent=None,
    model=None,
    cleanup_requested=False,
    intent_recorded_at=None,
):
    fields = {
        "worker_id": worker.worker_id,
        "directory": run.get("directory"),
        "cleanup_requested": bool(cleanup_requested),
        "intent_recorded_at": intent_recorded_at,
    }
    if agent is not None:
        fields["agent"] = agent
    if model is not None:
        fields["model"] = model
    return RemoteJournalRecord(
        id=intent_id,
        kind=WORKER_SESSION_CREATE_KIND,
        fields=fields,
    )


def _created_session_result(intent, session_id, *, created_at, agent=None, model=None):
    worker_id = intent.fields["worker_id"]
    cleanup_requested = intent.fields.get("cleanup_requested") is True
    return RemoteMutationResult.record_applied(
        RemoteJournalRecord(
            id=intent.id,
            kind=WORKER_SESSION_CREATE_KIND,
            fields={
                "worker_id": worker_id,
                "cleanup_requested": cleanup_requested,
                "session_id": session_id,
                "created_session_ids": [session_id],
                "created_at": created_at,
            },
        ),
        run_update=lambda latest_run: _apply_created_worker_session(
            latest_run,
            worker_id,
            session_id,
            cleanup_requested=cleanup_requested,
            agent=agent,
            model=model,
        ),
        missing_intent_policy=MISSING_INTENT_RECORD_APPLIED,
    )


def _apply_created_worker_session(latest_run, worker_id, session_id, *, cleanup_requested, agent=None, model=None):
    latest_worker = _ensure_latest_worker(latest_run, worker_id)
    latest_record = worker_record_for_mutation(latest_worker, worker_id)
    latest_record.set_session(session_id, agent=agent, model=model)
    if cleanup_requested:
        latest_record.remember_session_for_cleanup(session_id)


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


def _string_list(value):
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _append_unique_session_id(session_ids, session_id):
    if isinstance(session_id, str) and session_id and session_id not in session_ids:
        session_ids.append(session_id)


def _new_worker_session_journal_id():
    return f"worker_session_create_{uuid.uuid4().hex}"
