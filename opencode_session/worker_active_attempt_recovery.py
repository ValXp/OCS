from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from opencode_session.worker_attempt_policy import (
    WorkerExecutionTimeout,
    apply_worker_attempt_transition,
    classify_worker_attempt_exception,
)
from opencode_session.worker_state import (
    WORKER_LIFECYCLE_ACTIVE_WAIT,
    is_worker_record,
    worker_lifecycle_state,
)


@dataclass(frozen=True)
class ActiveAttemptRecovery:
    worker_id: str
    attempt_id: str
    error: Optional[str]
    transition: object


def recover_expired_active_attempts(workers, *, now):
    now_value = now()
    now_instant = _parse_instant(now_value)
    recoveries = []
    for worker_id in sorted(workers if isinstance(workers, dict) else {}):
        worker = workers[worker_id]
        if not is_worker_record(worker):
            continue
        attempt = _recoverable_active_attempt(worker)
        if attempt is None or not _active_attempt_lease_expired(worker, attempt, now_instant):
            continue
        recovery = _timeout_active_attempt(worker, attempt, now_value=now_value)
        if recovery is not None:
            recoveries.append(recovery)
    return tuple(recoveries)


def _recoverable_active_attempt(worker):
    if worker_lifecycle_state(worker) != WORKER_LIFECYCLE_ACTIVE_WAIT:
        return None
    attempts = worker.attempts if isinstance(worker.attempts, list) else []
    for attempt in reversed(attempts):
        if not isinstance(attempt, dict):
            continue
        attempt_id = attempt.get("id")
        if attempt_id and attempt.get("status") == "active" and not attempt.get("finished_at"):
            return attempt
    return None


def _active_attempt_lease_expired(worker, attempt, now_instant):
    timeout_seconds = _timeout_seconds(worker)
    if timeout_seconds is None or now_instant is None:
        return False
    started_at = _parse_instant(worker.timeout_started_at) or _parse_instant(attempt.get("started_at"))
    if started_at is None:
        return True
    return started_at + timedelta(seconds=timeout_seconds) <= now_instant


def _timeout_active_attempt(worker, attempt, *, now_value):
    outcome = classify_worker_attempt_exception(worker, WorkerExecutionTimeout())
    transition = apply_worker_attempt_transition(worker, outcome, now=lambda: now_value)
    if transition.worker_transition is None:
        return None
    finalized_transition = transition.worker_transition.with_finalized_attempt(
        attempt["id"],
        _attempt_recovery_fields(transition, finished_at=now_value),
    )
    return ActiveAttemptRecovery(
        worker_id=worker.worker_id,
        attempt_id=attempt["id"],
        error=transition.error,
        transition=finalized_transition,
    )


def _attempt_recovery_fields(transition, *, finished_at):
    fields = {
        "status": "failed",
        "finished_at": finished_at,
    }
    if transition.error is not None:
        fields["error"] = transition.error
    if transition.failure_category is not None:
        fields["failure_category"] = transition.failure_category
    return fields


def _timeout_seconds(worker):
    try:
        timeout_seconds = float(worker.timeout_seconds)
    except (TypeError, ValueError):
        return None
    if timeout_seconds < 0:
        return None
    return timeout_seconds


def _parse_instant(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        instant = datetime.fromisoformat(text)
    except ValueError:
        return None
    if instant.tzinfo is None:
        return instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)
