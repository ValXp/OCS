from dataclasses import dataclass, field
from typing import Optional

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.disposable_session_lifecycle import cleanup_disposable_sessions
from opencode_session.worker_session_provisioning import (
    recoverable_worker_session_creations_by_worker,
)
from opencode_session.worker_state import is_worker_record, worker_record_for_mutation


@dataclass
class WorkerCleanupOutcome:
    deleted_session_ids: list = field(default_factory=list)
    error: Optional[OpenCodeApiError] = None


def cleanup_created_worker_sessions(client, worker, session_ids):
    record = worker_record_for_mutation(worker)
    cleanup = record.ensure_cleanup()
    attempted_session_ids = list(session_ids)
    cleanup_outcome = cleanup_disposable_sessions(client, attempted_session_ids)
    cleanup_record = cleanup_outcome.record
    deleted_session_ids = list(cleanup_record["deleted"])
    verified_session_ids = list(cleanup_record["verified"])
    pending_session_ids = [
        session_id
        for session_id in attempted_session_ids
        if session_id not in verified_session_ids
    ]
    errors = cleanup_record["errors"]

    cleanup["deleted"] = bool(verified_session_ids) and not errors
    if errors:
        cleanup["error"] = errors[0]["error"]
    else:
        cleanup.pop("error", None)
    if pending_session_ids:
        cleanup["sessions"] = pending_session_ids
    else:
        cleanup.pop("sessions", None)
    if verified_session_ids:
        if len(verified_session_ids) > 1 or errors:
            cleanup["verified"] = verified_session_ids
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
            cleanup = worker.cleanup
            if not isinstance(cleanup, dict) or cleanup.get("deleted"):
                continue
            for session_id in _string_list(cleanup.get("sessions")):
                _append_unique_session_id(session_ids_by_worker.setdefault(worker_id, []), session_id)
    recovered_session_ids_by_worker = recoverable_worker_session_creations_by_worker(run)
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
