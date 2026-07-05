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
WORKER_LIST_FIELDS = (
    "dependencies",
    "prompt_ids",
    "retryable_failures",
    "blockers",
    "output_refs",
)

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


@dataclass(frozen=True)
class WorkerRecord:
    worker_id: str
    fields: dict
    has_explicit_lifecycle: bool = False

    @classmethod
    def from_worker(cls, worker, worker_id=None):
        fields = dict(worker) if isinstance(worker, dict) else {}
        resolved_worker_id = fields.get("id") or worker_id
        has_explicit_lifecycle = isinstance(worker, dict) and bool(worker.get("lifecycle_state"))
        return cls(resolved_worker_id, fields, has_explicit_lifecycle)

    @classmethod
    def default_fields(cls, worker_id):
        return {
            "id": worker_id,
            "role": None,
            "session_id": None,
            "agent": None,
            "model": None,
            "dependencies": [],
            "prompt_ids": [],
            "status": WORKER_STATUS_QUEUED,
            "retry_count": 0,
            "retry_limit": 0,
            "retryable_failures": [],
            "timeout_seconds": None,
            "timeout_policy": WORKER_STATUS_TIMEOUT,
            "timeout_started_at": None,
            "timed_out_at": None,
            "lifecycle_state": WORKER_LIFECYCLE_QUEUED,
            "failure_category": None,
            "failure_reason": None,
            "last_failure_category": None,
            "last_failure_reason": None,
            "next_eligible_action": WORKER_ACTION_START,
            "blockers": [],
            "output_refs": [],
        }

    @classmethod
    def public_state(cls, lifecycle_state):
        return PUBLIC_WORKER_STATE_BY_LIFECYCLE.get(lifecycle_state, (None, WORKER_ACTION_NONE))

    @classmethod
    def public_state_fields(cls, lifecycle_state):
        status, action = cls.public_state(lifecycle_state)
        return {
            "lifecycle_state": lifecycle_state,
            "status": status,
            "next_eligible_action": action,
        }

    @classmethod
    def lifecycle_set_fields(cls, worker_id, lifecycle_state):
        fields = {"id": worker_id}
        fields.update(cls.public_state_fields(lifecycle_state))
        return fields

    @property
    def lifecycle_state(self):
        lifecycle_state = self.fields.get("lifecycle_state")
        if lifecycle_state in WORKER_LIFECYCLE_STATES:
            return lifecycle_state
        return self._infer_lifecycle_state(self.fields)

    @property
    def status(self):
        return self.public_state(self.lifecycle_state)[0]

    @property
    def next_eligible_action(self):
        return self.public_state(self.lifecycle_state)[1]

    def scheduling_state(self):
        return WorkerSchedulingState(
            self.lifecycle_state,
            self.status,
            self.next_eligible_action,
            worker_has_prompt(self.fields),
        )

    def to_worker(self):
        normalized = self.default_fields(self.worker_id)
        normalized.update(self.fields)
        normalized["id"] = normalized.get("id") or self.worker_id
        for key in WORKER_LIST_FIELDS:
            value = normalized.get(key)
            normalized[key] = value if isinstance(value, list) else []
        if normalized.get("retry_count") is None:
            normalized["retry_count"] = 0
        if normalized.get("retry_limit") is None:
            normalized["retry_limit"] = 0
        if not normalized.get("timeout_policy"):
            normalized["timeout_policy"] = WORKER_STATUS_TIMEOUT
        if not normalized.get("status"):
            normalized["status"] = WORKER_STATUS_QUEUED
        else:
            normalized["status"] = short_status(normalized["status"])
        lifecycle_source = dict(normalized)
        if not self.has_explicit_lifecycle:
            lifecycle_source.pop("lifecycle_state", None)
        lifecycle_record = WorkerRecord.from_worker(lifecycle_source, normalized["id"])
        normalized.update(lifecycle_record.serialized_public_state())
        return normalized

    def serialized_public_state(self):
        return self.public_state_fields(self.lifecycle_state)

    @staticmethod
    def _infer_lifecycle_state(worker):
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


@dataclass(frozen=True)
class WorkerSchedulingState:
    lifecycle_state: Optional[str]
    status: Optional[str]
    next_eligible_action: str
    has_prompt: bool

    @classmethod
    def from_worker(cls, worker):
        if not isinstance(worker, dict):
            return cls(None, None, "none", False)
        return WorkerRecord.from_worker(worker).scheduling_state()

    def can_execute(self):
        return self.has_prompt and self.next_eligible_action in EXECUTABLE_WORKER_ACTIONS

    def can_block_for_dependency(self):
        return self.has_prompt and is_dependency_blockable_status(self.status)


def next_eligible_worker_action(worker):
    if not isinstance(worker, dict):
        return WORKER_ACTION_NONE
    return WorkerRecord.from_worker(worker).next_eligible_action


def public_worker_state(lifecycle_state):
    return WorkerRecord.public_state(lifecycle_state)


def public_worker_state_fields(lifecycle_state):
    return WorkerRecord.public_state_fields(lifecycle_state)


def worker_lifecycle_state(worker):
    if not isinstance(worker, dict):
        return None
    return WorkerRecord.from_worker(worker).lifecycle_state


def infer_worker_lifecycle_state(worker):
    return WorkerRecord._infer_lifecycle_state(worker)


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
