import uuid
from dataclasses import dataclass
from typing import Optional

from opencode_session.remote_journal import PersistedRemoteMutationJournal
from opencode_session.schema_common import RunRecord, Worker
from opencode_session.session_ids import require_session_id
from opencode_session.worker_state import (
    WorkerRecord,
    is_worker_mapping,
    sync_worker_record,
    worker_record_for_mutation,
)


WORKER_SESSION_JOURNAL_FIELD = "worker_session_journal"
WORKER_SESSION_CREATE_KIND = "worker_session_create"


@dataclass
class WorkerSessionOutcome:
    session_id: Optional[str]
    created_session_id: Optional[str] = None


@dataclass(frozen=True)
class WorkerSessionCreationIntent:
    id: str
    worker_id: str
    cleanup_requested: bool = False


@dataclass
class WorkerSessionProvisioning:
    run: RunRecord
    worker: Worker
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

    def record_intent(self, run, worker, *, agent=None, model=None, cleanup_requested=False):
        intent = WorkerSessionCreationIntent(
            self.id_factory(),
            worker["id"],
            cleanup_requested=bool(cleanup_requested),
        )
        entry = {
            "id": intent.id,
            "kind": WORKER_SESSION_CREATE_KIND,
            "status": "intent",
            "worker_id": intent.worker_id,
            "directory": run.get("directory"),
            "cleanup_requested": intent.cleanup_requested,
            "intent_recorded_at": self.now(),
        }
        if agent is not None:
            entry["agent"] = agent
        if model is not None:
            entry["model"] = model

        updated_run = self._transaction(intent).record_intent(run, entry)
        return updated_run, _latest_worker(updated_run, worker), intent

    def record_created(self, run, worker, intent, session_id, *, agent=None, model=None):
        fields = {
            "status": "created",
            "session_id": session_id,
            "created_session_ids": [session_id],
            "created_at": self.now(),
        }
        missing_entry = {
            "id": intent.id,
            "kind": WORKER_SESSION_CREATE_KIND,
            "worker_id": intent.worker_id,
            "cleanup_requested": intent.cleanup_requested,
            **fields,
        }

        def update_worker(latest_run):
            latest_worker = _ensure_latest_worker(latest_run, intent.worker_id)
            latest_record = worker_record_for_mutation(latest_worker, intent.worker_id)
            latest_record.set_session(session_id, agent=agent, model=model)
            if intent.cleanup_requested:
                latest_record.remember_session_for_cleanup(session_id)
            sync_worker_record(latest_worker, latest_record)

        updated_run = self._transaction(intent).mark_applied(
            run,
            fields,
            before_mark=update_worker,
            missing_entry=missing_entry,
        )
        return updated_run, _latest_worker(updated_run, worker)

    def discard_intent_best_effort(self, run, worker, intent):
        return self._remove_best_effort(run, worker, intent, operation="discard_worker_session_create")

    def finalize_best_effort(self, run, worker, intent):
        return self._remove_best_effort(run, worker, intent, operation="finalize_worker_session_create")

    def _remove_best_effort(self, run, worker, intent, *, operation):
        transaction = self._transaction(intent)
        if operation == "discard_worker_session_create":
            updated_run = transaction.discard_intent_best_effort(run)
        else:
            updated_run = transaction.finalize_best_effort(run)
        return updated_run, _latest_worker(updated_run, worker)

    def _transaction(self, intent):
        return self.transactions.transaction(
            intent.id,
            WORKER_SESSION_CREATE_KIND,
            discard_operation="discard_worker_session_create",
            finalize_operation="finalize_worker_session_create",
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
        session_intent = None
        if self.session_journal is not None and will_create_worker_session(
            worker,
            session_id=session_id,
            create_session=create_session,
        ):
            run, worker, session_intent = self.session_journal.record_intent(
                run,
                worker,
                agent=agent,
                model=model,
                cleanup_requested=cleanup_requested,
            )
        try:
            session_outcome = provision_worker_session(
                client,
                run,
                worker,
                session_id=session_id,
                agent=agent,
                model=model,
                create_session=create_session,
            )
        except Exception:
            if session_intent is not None:
                run, worker = self.session_journal.discard_intent_best_effort(run, worker, session_intent)
            raise
        if session_outcome.created_session_id is not None and session_intent is not None:
            run, worker = self.session_journal.record_created(
                run,
                worker,
                session_intent,
                session_outcome.created_session_id,
                agent=agent,
                model=model,
            )
        return WorkerSessionProvisioning(run, worker, session_outcome, session_intent)

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
    record = worker_record_for_mutation(worker)
    worker_session_id = session_id or record.get("session_id")
    created_session_id = None
    missing_session = not worker_session_id if treat_falsey_session_as_missing else worker_session_id is None
    if missing_session:
        create_response = client.create_session_response(run["directory"], agent=agent, model=model)
        worker_session_id = require_session_id(create_response)
        created_session_id = worker_session_id
    record.set_session(worker_session_id, agent=agent, model=model)
    sync_worker_record(worker, record)
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
    record = worker_record_for_mutation(worker)
    record.set_session(session_id or record.get("session_id"), agent=agent, model=model)
    sync_worker_record(worker, record)
    return WorkerSessionOutcome(record.get("session_id"))


def will_create_worker_session(worker, *, session_id=None, create_session=True):
    if not create_session:
        return False
    return not (session_id or worker.get("session_id"))


def _latest_worker(run, fallback_worker):
    worker_id = fallback_worker.get("id") if is_worker_mapping(fallback_worker) else None
    latest_worker = run.get("workers", {}).get(worker_id) if isinstance(run, dict) and worker_id else None
    return latest_worker if is_worker_mapping(latest_worker) else fallback_worker


def _ensure_latest_worker(run, worker_id):
    workers = run.setdefault("workers", {})
    worker = workers.get(worker_id)
    if not is_worker_mapping(worker):
        worker = WorkerRecord.default_fields(worker_id)
        workers[worker_id] = worker
    return worker


def _new_worker_session_journal_id():
    return f"worker_session_create_{uuid.uuid4().hex}"
