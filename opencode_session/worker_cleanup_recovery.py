from dataclasses import dataclass, field
from typing import Optional

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.disposable_session_lifecycle import cleanup_disposable_sessions
from opencode_session.remote_journal import RemoteMutationRecovery
from opencode_session.worker_session_provisioning import (
    WORKER_SESSION_CREATE_KIND,
    WORKER_SESSION_JOURNAL_FIELD,
)
from opencode_session.worker_state import is_worker_record, worker_field, worker_record_for_mutation


_WORKER_SESSION_RECOVERY = RemoteMutationRecovery(WORKER_SESSION_JOURNAL_FIELD)


@dataclass
class WorkerCleanupOutcome:
    deleted_session_ids: list = field(default_factory=list)
    error: Optional[OpenCodeApiError] = None


def cleanup_created_worker_sessions(client, worker, session_ids):
    record = worker_record_for_mutation(worker)
    cleanup = record.ensure_cleanup()
    cleanup_outcome = cleanup_disposable_sessions(client, session_ids)
    cleanup_record = cleanup_outcome.record
    deleted_session_ids = list(cleanup_record["deleted"])
    errors = cleanup_record["errors"]

    cleanup["deleted"] = bool(cleanup_record["verified"]) and not errors
    if errors:
        cleanup["error"] = errors[0]["error"]
    else:
        cleanup.pop("error", None)
    if deleted_session_ids:
        if len(deleted_session_ids) > 1 or errors:
            cleanup["sessions"] = deleted_session_ids
        else:
            cleanup.pop("sessions", None)
    else:
        cleanup.pop("sessions", None)
    if cleanup_record["verified"]:
        if len(cleanup_record["verified"]) > 1 or errors:
            cleanup["verified"] = list(cleanup_record["verified"])
        else:
            cleanup.pop("verified", None)
    else:
        cleanup.pop("verified", None)
    if errors:
        return WorkerCleanupOutcome(deleted_session_ids, cleanup_outcome.first_error)
    return WorkerCleanupOutcome(deleted_session_ids)


def recoverable_created_worker_sessions_by_worker(run):
    session_ids_by_worker = {}
    workers = run.get("workers", {}) if isinstance(run, dict) else {}
    if isinstance(workers, dict):
        for worker_id, worker in workers.items():
            if not is_worker_record(worker):
                continue
            cleanup = worker_field(worker, "cleanup")
            if not isinstance(cleanup, dict) or cleanup.get("deleted"):
                continue
            for session_id in _string_list(cleanup.get("sessions")):
                _append_unique_session_id(session_ids_by_worker.setdefault(worker_id, []), session_id)
    recovered_session_ids_by_worker = _WORKER_SESSION_RECOVERY.values_by_owner(
        run,
        kind=WORKER_SESSION_CREATE_KIND,
        owner_field="worker_id",
        list_fields=("created_session_ids",),
        value_fields=("session_id",),
        required_fields={"cleanup_requested": True},
    )
    for worker_id, session_ids in recovered_session_ids_by_worker.items():
        for session_id in session_ids:
            _append_unique_session_id(session_ids_by_worker.setdefault(worker_id, []), session_id)
    return {worker_id: session_ids for worker_id, session_ids in session_ids_by_worker.items() if session_ids}


def _string_list(value):
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _append_unique_session_id(session_ids, session_id):
    if isinstance(session_id, str) and session_id and session_id not in session_ids:
        session_ids.append(session_id)
