from copy import deepcopy


def new_worker_attempt_record(worker, *, started_at, created_session_ids=()):
    attempts = _worker_get(worker, "attempts")
    attempt_count = len(attempts) if isinstance(attempts, list) else 0
    return {
        "id": f"attempt-{attempt_count + 1}",
        "session_id": _worker_get(worker, "session_id"),
        "created_session_ids": list(created_session_ids),
        "status": "active",
        "started_at": started_at,
        "finished_at": None,
    }


def _append_attempt(worker, attempt):
    if not attempt:
        return
    attempts = _worker_get(worker, "attempts")
    attempts = attempts if isinstance(attempts, list) else []
    attempt = deepcopy(attempt)
    if any(isinstance(existing, dict) and existing.get("id") == attempt.get("id") for existing in attempts):
        return
    _worker_set(worker, "attempts", [*deepcopy(attempts), attempt])


def _finalize_attempt(worker, finalization):
    if not finalization:
        return
    attempt_id = finalization.get("id")
    fields = finalization.get("fields") if isinstance(finalization.get("fields"), dict) else {}
    attempts = _worker_get(worker, "attempts")
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
        _worker_set(worker, "attempts", finalized)


def _worker_get(worker, field_name, default=None):
    field = getattr(worker, "field", None)
    if callable(field):
        return field(field_name, default)
    getter = getattr(worker, "get", None)
    if not callable(getter):
        return default
    return getter(field_name, default)


def _worker_set(worker, field_name, value):
    setter = getattr(worker, "set_field", None)
    if callable(setter):
        setter(field_name, value)
        return
    worker[field_name] = value
