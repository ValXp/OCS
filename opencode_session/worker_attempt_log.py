from collections.abc import Mapping
from copy import deepcopy


def new_worker_attempt_record(worker, *, started_at, created_session_ids=()):
    attempts = worker.get("attempts") if isinstance(worker, Mapping) else None
    attempt_count = len(attempts) if isinstance(attempts, list) else 0
    return {
        "id": f"attempt-{attempt_count + 1}",
        "session_id": worker.get("session_id") if isinstance(worker, Mapping) else None,
        "created_session_ids": list(created_session_ids),
        "status": "active",
        "started_at": started_at,
        "finished_at": None,
    }


def _append_attempt(worker, attempt):
    if not attempt:
        return
    attempts = worker.get("attempts") if isinstance(worker.get("attempts"), list) else []
    attempt = deepcopy(attempt)
    if any(isinstance(existing, dict) and existing.get("id") == attempt.get("id") for existing in attempts):
        return
    worker["attempts"] = [*deepcopy(attempts), attempt]


def _finalize_attempt(worker, finalization):
    if not finalization:
        return
    attempt_id = finalization.get("id")
    fields = finalization.get("fields") if isinstance(finalization.get("fields"), dict) else {}
    attempts = worker.get("attempts") if isinstance(worker.get("attempts"), list) else []
    finalized = []
    found = False
    for attempt in attempts:
        if isinstance(attempt, dict) and attempt.get("id") == attempt_id:
            updated = deepcopy(attempt)
            updated.update(deepcopy(fields))
            finalized.append(updated)
            found = True
        else:
            finalized.append(deepcopy(attempt))
    if found:
        worker["attempts"] = finalized
