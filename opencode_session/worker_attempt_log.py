from collections.abc import MutableMapping
from copy import deepcopy


def new_worker_attempt_record(worker, *, started_at, created_session_ids=()):
    api = _attempt_api(worker)
    if api is not None:
        return api.new_attempt_record(
            started_at=started_at,
            created_session_ids=created_session_ids,
        )
    if not isinstance(worker, MutableMapping):
        raise TypeError("worker attempts require WorkerRecord attempt methods")
    attempts = worker.get("attempts")
    attempts = attempts if isinstance(attempts, list) else []
    return {
        "id": f"attempt-{len(attempts) + 1}",
        "session_id": worker.get("session_id"),
        "created_session_ids": list(created_session_ids),
        "status": "active",
        "started_at": started_at,
        "finished_at": None,
    }


def _append_attempt(worker, attempt):
    if not attempt:
        return
    api = _attempt_api(worker)
    if api is not None:
        api.append_attempt(attempt)
        return
    if not isinstance(worker, MutableMapping):
        raise TypeError("worker attempts require WorkerRecord attempt methods")
    attempts = worker.get("attempts")
    attempts = attempts if isinstance(attempts, list) else []
    attempt = deepcopy(attempt)
    if any(isinstance(existing, dict) and existing.get("id") == attempt.get("id") for existing in attempts):
        return
    worker["attempts"] = [*deepcopy(attempts), attempt]


def _finalize_attempt(worker, finalization):
    if not finalization:
        return
    attempt_id = finalization.get("id")
    fields = finalization.get("fields") if isinstance(finalization.get("fields"), dict) else {}
    api = _attempt_api(worker)
    if api is not None:
        api.finalize_attempt(attempt_id, fields)
        return
    if not isinstance(worker, MutableMapping):
        raise TypeError("worker attempts require WorkerRecord attempt methods")
    attempts = worker.get("attempts")
    attempts = attempts if isinstance(attempts, list) else []
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


def _attempt_api(worker):
    for method_name in ("new_attempt_record", "append_attempt", "finalize_attempt"):
        if not callable(getattr(worker, method_name, None)):
            return None
    return worker
