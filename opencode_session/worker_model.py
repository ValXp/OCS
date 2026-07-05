from dataclasses import dataclass
from typing import Optional

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

BLOCKED_WORKER_STATUS = WORKER_STATUS_BLOCKED
TERMINAL_WORKER_STATUSES = frozenset(
    {WORKER_STATUS_DONE, WORKER_STATUS_FAILED, WORKER_STATUS_ABORTED, WORKER_STATUS_TIMEOUT}
)
FAILED_DEPENDENCY_STATUSES = frozenset(
    {WORKER_STATUS_FAILED, WORKER_STATUS_ABORTED, WORKER_STATUS_TIMEOUT, WORKER_STATUS_BLOCKED}
)
EXECUTABLE_WORKER_ACTIONS = frozenset({WORKER_ACTION_START, WORKER_ACTION_RETRY})


@dataclass(frozen=True)
class WorkerSchedulingState:
    status: Optional[str]
    next_eligible_action: str
    has_prompt: bool

    @classmethod
    def from_worker(cls, worker):
        if not isinstance(worker, dict):
            return cls(None, "none", False)
        return cls(
            short_status(worker.get("status")),
            next_eligible_worker_action(worker),
            worker_has_prompt(worker),
        )

    def can_execute(self):
        return self.has_prompt and self.next_eligible_action in EXECUTABLE_WORKER_ACTIONS

    def can_block_for_dependency(self):
        return self.has_prompt and is_dependency_blockable_status(self.status)


def next_eligible_worker_action(worker):
    status = short_status(worker.get("status") if isinstance(worker, dict) else None)
    if status == WORKER_STATUS_QUEUED:
        return WORKER_ACTION_START
    if status == WORKER_STATUS_ACTIVE:
        return WORKER_ACTION_RETRY if worker.get("next_eligible_action") == WORKER_ACTION_RETRY else WORKER_ACTION_WAIT
    if is_blocked_status(status):
        return WORKER_ACTION_RESOLVE_BLOCKER
    if status == WORKER_STATUS_DONE:
        return WORKER_ACTION_COLLECT
    if status == WORKER_STATUS_TIMEOUT and worker_retry_available(worker, WORKER_STATUS_TIMEOUT):
        return WORKER_ACTION_RETRY
    if status == WORKER_STATUS_FAILED and worker_retry_available(worker):
        return WORKER_ACTION_RETRY
    return WORKER_ACTION_NONE


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


def worker_has_prompt(worker):
    if not isinstance(worker, dict):
        return False
    prompt = worker.get("prompt")
    return prompt is not None and bool(str(prompt))


def is_executable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_execute()


def is_dependency_blockable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_block_for_dependency()


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
