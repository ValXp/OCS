from opencode_session.worker_state import WorkerRecord


def new_worker_attempt_record(worker, *, started_at, created_session_ids=()):
    return _require_attempt_worker(worker).new_attempt_record(
        started_at=started_at,
        created_session_ids=created_session_ids,
    )


def _append_attempt(worker, attempt):
    if not attempt:
        return
    _require_attempt_worker(worker).append_attempt(attempt)


def _finalize_attempt(worker, finalization):
    if not finalization:
        return
    attempt_id = finalization.get("id")
    fields = finalization.get("fields") if isinstance(finalization.get("fields"), dict) else {}
    _require_attempt_worker(worker).finalize_attempt(attempt_id, fields)


def _require_attempt_worker(worker):
    if isinstance(worker, WorkerRecord):
        return worker
    raise TypeError("worker attempts require WorkerRecord; hydrate raw mappings at the storage boundary")
