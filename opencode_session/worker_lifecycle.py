from dataclasses import dataclass

from opencode_session.status import short_status


WORKER_STATUS_QUEUED = "queued"
WORKER_STATUS_ACTIVE = "active"
WORKER_STATUS_BLOCKED = "blocked"
WORKER_STATUS_DONE = "done"
WORKER_STATUS_FAILED = "failed"
WORKER_STATUS_ABORTED = "aborted"
WORKER_STATUS_TIMEOUT = "timeout"

WORKER_ACTION_START = "start"
WORKER_ACTION_WAIT = "wait"
WORKER_ACTION_RETRY = "retry"
WORKER_ACTION_RESOLVE_BLOCKER = "resolve_blocker"
WORKER_ACTION_COLLECT = "collect"
WORKER_ACTION_NONE = "none"

WORKER_LIFECYCLE_QUEUED = "queued"
WORKER_LIFECYCLE_ACTIVE_WAIT = "active_wait"
WORKER_LIFECYCLE_ACTIVE_RETRY = "active_retry"
WORKER_LIFECYCLE_BLOCKED_DEPENDENCY = "blocked_dependency"
WORKER_LIFECYCLE_BLOCKED_TIMEOUT = "blocked_timeout"
WORKER_LIFECYCLE_DONE_COLLECT = "done_collect"
WORKER_LIFECYCLE_FAILED_RETRY = "failed_retry"
WORKER_LIFECYCLE_FAILED_TERMINAL = "failed_terminal"
WORKER_LIFECYCLE_TIMEOUT_RETRY = "timeout_retry"
WORKER_LIFECYCLE_TIMEOUT_TERMINAL = "timeout_terminal"
WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY = "timeout_failed_retry"
WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL = "timeout_failed_terminal"
WORKER_LIFECYCLE_TIMEOUT_ABORTED = "timeout_aborted"
WORKER_LIFECYCLE_ABORTED = "aborted"

BLOCKED_WORKER_STATUS = WORKER_STATUS_BLOCKED
TERMINAL_WORKER_STATUSES = frozenset(
    {WORKER_STATUS_DONE, WORKER_STATUS_FAILED, WORKER_STATUS_ABORTED, WORKER_STATUS_TIMEOUT}
)
FAILED_DEPENDENCY_STATUSES = frozenset(
    {WORKER_STATUS_FAILED, WORKER_STATUS_ABORTED, WORKER_STATUS_TIMEOUT, WORKER_STATUS_BLOCKED}
)
EXECUTABLE_WORKER_ACTIONS = frozenset({WORKER_ACTION_START, WORKER_ACTION_RETRY})

PUBLIC_WORKER_STATE_BY_LIFECYCLE = {
    WORKER_LIFECYCLE_QUEUED: (WORKER_STATUS_QUEUED, WORKER_ACTION_START),
    WORKER_LIFECYCLE_ACTIVE_WAIT: (WORKER_STATUS_ACTIVE, WORKER_ACTION_WAIT),
    WORKER_LIFECYCLE_ACTIVE_RETRY: (WORKER_STATUS_ACTIVE, WORKER_ACTION_RETRY),
    WORKER_LIFECYCLE_BLOCKED_DEPENDENCY: (WORKER_STATUS_BLOCKED, WORKER_ACTION_RESOLVE_BLOCKER),
    WORKER_LIFECYCLE_BLOCKED_TIMEOUT: (WORKER_STATUS_BLOCKED, WORKER_ACTION_RESOLVE_BLOCKER),
    WORKER_LIFECYCLE_DONE_COLLECT: (WORKER_STATUS_DONE, WORKER_ACTION_COLLECT),
    WORKER_LIFECYCLE_FAILED_RETRY: (WORKER_STATUS_FAILED, WORKER_ACTION_RETRY),
    WORKER_LIFECYCLE_FAILED_TERMINAL: (WORKER_STATUS_FAILED, WORKER_ACTION_NONE),
    WORKER_LIFECYCLE_TIMEOUT_RETRY: (WORKER_STATUS_TIMEOUT, WORKER_ACTION_RETRY),
    WORKER_LIFECYCLE_TIMEOUT_TERMINAL: (WORKER_STATUS_TIMEOUT, WORKER_ACTION_NONE),
    WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY: (WORKER_STATUS_FAILED, WORKER_ACTION_RETRY),
    WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL: (WORKER_STATUS_FAILED, WORKER_ACTION_NONE),
    WORKER_LIFECYCLE_TIMEOUT_ABORTED: (WORKER_STATUS_ABORTED, WORKER_ACTION_NONE),
    WORKER_LIFECYCLE_ABORTED: (WORKER_STATUS_ABORTED, WORKER_ACTION_NONE),
}
WORKER_LIFECYCLE_STATES = frozenset(PUBLIC_WORKER_STATE_BY_LIFECYCLE)


def public_worker_state(lifecycle_state):
    return PUBLIC_WORKER_STATE_BY_LIFECYCLE.get(lifecycle_state, (None, WORKER_ACTION_NONE))


def public_worker_state_fields(lifecycle_state):
    status, action = public_worker_state(lifecycle_state)
    return {
        "lifecycle_state": lifecycle_state,
        "status": status,
        "next_eligible_action": action,
    }


def worker_lifecycle_set_fields(worker_id, lifecycle_state):
    return {"id": worker_id, "lifecycle_state": lifecycle_state}


def worker_retry_available(worker, category=None):
    if not isinstance(worker, dict):
        return False
    if worker.get("failure_retryable") is False:
        return False
    retryable = set(worker.get("retryable_failures") or [])
    if not retryable:
        return False
    if category is None:
        category = worker.get("failure_category") or worker.get("last_failure_category")
    if category and category not in retryable and "all" not in retryable:
        return False
    try:
        retry_count = int(worker.get("retry_count") or 0)
        retry_limit = int(worker.get("retry_limit") or 0)
    except (TypeError, ValueError):
        return False
    return retry_count < retry_limit


def is_blocked_status(status):
    return short_status(status) == BLOCKED_WORKER_STATUS


def is_terminal_status(status):
    return short_status(status) in TERMINAL_WORKER_STATUSES


def is_runnable_status(status):
    return not is_terminal_status(status) and not is_blocked_status(status)


def is_dependency_blockable_status(status):
    return is_runnable_status(status)


def is_failed_dependency_status(status):
    return short_status(status) in FAILED_DEPENDENCY_STATUSES


@dataclass(frozen=True)
class WorkerSchedulingState:
    lifecycle_state: str | None
    status: str | None
    next_eligible_action: str
    has_prompt: bool

    @classmethod
    def from_worker(cls, worker):
        if not isinstance(worker, dict):
            return cls(None, None, WORKER_ACTION_NONE, False)
        lifecycle_state = worker_lifecycle_state(worker)
        status, next_eligible_action = public_worker_state(lifecycle_state)
        return cls(lifecycle_state, status, next_eligible_action, worker_has_prompt(worker))

    def can_execute(self):
        return self.has_prompt and self.next_eligible_action in EXECUTABLE_WORKER_ACTIONS

    def can_block_for_dependency(self):
        return self.has_prompt and is_dependency_blockable_status(self.status)


def worker_lifecycle_state(worker):
    if not isinstance(worker, dict):
        return None
    lifecycle_state = worker.get("lifecycle_state")
    if lifecycle_state in WORKER_LIFECYCLE_STATES:
        return lifecycle_state
    return infer_worker_lifecycle_state(worker)


def infer_worker_lifecycle_state(worker):
    status = short_status(worker.get("status") if isinstance(worker, dict) else None)
    if status == WORKER_STATUS_QUEUED:
        return WORKER_LIFECYCLE_QUEUED
    if status == WORKER_STATUS_ACTIVE:
        if worker.get("next_eligible_action") == WORKER_ACTION_RETRY:
            return WORKER_LIFECYCLE_ACTIVE_RETRY
        return WORKER_LIFECYCLE_ACTIVE_WAIT
    if is_blocked_status(status):
        if worker.get("failure_category") == WORKER_STATUS_TIMEOUT or WORKER_STATUS_TIMEOUT in set(
            worker.get("blockers") or []
        ):
            return WORKER_LIFECYCLE_BLOCKED_TIMEOUT
        return WORKER_LIFECYCLE_BLOCKED_DEPENDENCY
    if status == WORKER_STATUS_DONE:
        return WORKER_LIFECYCLE_DONE_COLLECT
    if status == WORKER_STATUS_FAILED:
        if worker.get("failure_category") == WORKER_STATUS_TIMEOUT:
            if worker_retry_available(worker, WORKER_STATUS_TIMEOUT):
                return WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY
            return WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL
        if worker_retry_available(worker):
            return WORKER_LIFECYCLE_FAILED_RETRY
        return WORKER_LIFECYCLE_FAILED_TERMINAL
    if status == WORKER_STATUS_TIMEOUT:
        if worker_retry_available(worker, WORKER_STATUS_TIMEOUT):
            return WORKER_LIFECYCLE_TIMEOUT_RETRY
        return WORKER_LIFECYCLE_TIMEOUT_TERMINAL
    if status == WORKER_STATUS_ABORTED:
        if worker.get("failure_category") == WORKER_STATUS_TIMEOUT:
            return WORKER_LIFECYCLE_TIMEOUT_ABORTED
        return WORKER_LIFECYCLE_ABORTED
    return WORKER_LIFECYCLE_QUEUED


def latest_prompt_ids_are_retry_marker(latest_worker):
    return (
        isinstance(latest_worker, dict)
        and worker_lifecycle_state(latest_worker) == WORKER_LIFECYCLE_ACTIVE_RETRY
        and latest_worker.get("last_failure_category") is not None
    )


def next_eligible_worker_action(worker):
    if not isinstance(worker, dict):
        return WORKER_ACTION_NONE
    return WorkerSchedulingState.from_worker(worker).next_eligible_action


def worker_has_prompt(worker):
    if not isinstance(worker, dict):
        return False
    prompt = worker.get("prompt")
    return prompt is not None and bool(str(prompt))


def is_executable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_execute()


def is_dependency_blockable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_block_for_dependency()
