from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional

from opencode_session.schema_common import WORKER_REQUIRED_FIELD_NAMES
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

EX_UNAVAILABLE = 69
EX_UNSUPPORTED = 70
EX_TIMEOUT = 124
EX_PARTIAL = 1
EX_BLOCKED = 75
EX_ABORTED = 130

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


class WorkerTransitionName(str, Enum):
    PROVISIONED = "provisioned"
    ACTIVE = "active"
    ATTEMPT_STARTED = "attempt_started"
    FAILED = "failed"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    ABORTED = "aborted"
    RETRY_SCHEDULED = "retry_scheduled"
    TIMED_OUT = "timed_out"
    RESULT_APPLIED = "result_applied"
    CLEANUP_UPDATED = "cleanup_updated"
    SNAPSHOT_APPLIED = "snapshot_applied"

    def __str__(self):
        return self.value


@dataclass(frozen=True)
class WorkerLifecycleMetadata:
    status: str
    next_eligible_action: str
    status_alias: bool = False
    terminal_status: bool = False
    failed_dependency_status: bool = False
    executable: bool = False
    timeout_origin: bool = False
    status_priority: int = 0
    exit_code: Optional[int] = None
    source_transitions: frozenset = frozenset()
    target_transitions: frozenset = frozenset()


@dataclass(frozen=True)
class WorkerTransitionMetadata:
    name: WorkerTransitionName
    source_states: frozenset
    target_states: frozenset
    apply_method: str
    target_lifecycle: object = None
    is_legal_transition: object = None
    public_lifecycle_transition: bool = True


@dataclass(frozen=True)
class WorkerTransitionResult:
    applied: bool
    worker: object
    reason: Optional[str] = None
    stale_snapshot_recovery: bool = False

    @property
    def skipped(self):
        return not self.applied


class WorkerTransitionError(ValueError):
    def __init__(self, result):
        self.result = result
        super().__init__(result.reason or "worker transition skipped")


def _lifecycle_metadata(
    status,
    next_eligible_action,
    *,
    status_alias=False,
    terminal_status=False,
    failed_dependency_status=False,
    executable=False,
    timeout_origin=False,
    status_priority=0,
    exit_code=None,
    source_transitions=(),
    target_transitions=(),
):
    return WorkerLifecycleMetadata(
        status,
        next_eligible_action,
        status_alias=status_alias,
        terminal_status=terminal_status,
        failed_dependency_status=failed_dependency_status,
        executable=executable,
        timeout_origin=timeout_origin,
        status_priority=status_priority,
        exit_code=exit_code,
        source_transitions=frozenset(source_transitions),
        target_transitions=frozenset(target_transitions),
    )


def _transition_metadata(
    name,
    *,
    source_states=(),
    target_states=(),
    apply_method=None,
    target_lifecycle=None,
    is_legal_transition=None,
    public_lifecycle_transition=True,
):
    return WorkerTransitionMetadata(
        name,
        frozenset(source_states),
        frozenset(target_states),
        apply_method or name.value,
        target_lifecycle=target_lifecycle,
        is_legal_transition=is_legal_transition,
        public_lifecycle_transition=public_lifecycle_transition,
    )


WORKER_LIFECYCLE_METADATA = {
    WORKER_LIFECYCLE_QUEUED: _lifecycle_metadata(
        WORKER_STATUS_QUEUED,
        WORKER_ACTION_START,
        status_alias=True,
        executable=True,
        status_priority=0,
    ),
    WORKER_LIFECYCLE_ACTIVE_WAIT: _lifecycle_metadata(
        WORKER_STATUS_ACTIVE,
        WORKER_ACTION_WAIT,
        status_alias=True,
        status_priority=1,
    ),
    WORKER_LIFECYCLE_ACTIVE_RETRY: _lifecycle_metadata(
        WORKER_STATUS_ACTIVE,
        WORKER_ACTION_RETRY,
        executable=True,
        status_priority=1,
    ),
    WORKER_LIFECYCLE_BLOCKED_DEPENDENCY: _lifecycle_metadata(
        WORKER_STATUS_BLOCKED,
        WORKER_ACTION_RESOLVE_BLOCKER,
        status_alias=True,
        failed_dependency_status=True,
        status_priority=2,
        exit_code=EX_BLOCKED,
    ),
    WORKER_LIFECYCLE_BLOCKED_TIMEOUT: _lifecycle_metadata(
        WORKER_STATUS_BLOCKED,
        WORKER_ACTION_RESOLVE_BLOCKER,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=2,
        exit_code=EX_BLOCKED,
    ),
    WORKER_LIFECYCLE_DONE_COLLECT: _lifecycle_metadata(
        WORKER_STATUS_DONE,
        WORKER_ACTION_COLLECT,
        status_alias=True,
        terminal_status=True,
        status_priority=3,
        exit_code=0,
    ),
    WORKER_LIFECYCLE_FAILED_RETRY: _lifecycle_metadata(
        WORKER_STATUS_FAILED,
        WORKER_ACTION_RETRY,
        terminal_status=True,
        failed_dependency_status=True,
        executable=True,
        status_priority=6,
        exit_code=EX_UNAVAILABLE,
    ),
    WORKER_LIFECYCLE_FAILED_TERMINAL: _lifecycle_metadata(
        WORKER_STATUS_FAILED,
        WORKER_ACTION_NONE,
        status_alias=True,
        terminal_status=True,
        failed_dependency_status=True,
        status_priority=6,
        exit_code=EX_UNAVAILABLE,
    ),
    WORKER_LIFECYCLE_TIMEOUT_RETRY: _lifecycle_metadata(
        WORKER_STATUS_TIMEOUT,
        WORKER_ACTION_RETRY,
        terminal_status=True,
        failed_dependency_status=True,
        executable=True,
        timeout_origin=True,
        status_priority=4,
        exit_code=EX_TIMEOUT,
    ),
    WORKER_LIFECYCLE_TIMEOUT_TERMINAL: _lifecycle_metadata(
        WORKER_STATUS_TIMEOUT,
        WORKER_ACTION_NONE,
        status_alias=True,
        terminal_status=True,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=4,
        exit_code=EX_TIMEOUT,
    ),
    WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY: _lifecycle_metadata(
        WORKER_STATUS_FAILED,
        WORKER_ACTION_RETRY,
        terminal_status=True,
        failed_dependency_status=True,
        executable=True,
        timeout_origin=True,
        status_priority=6,
        exit_code=EX_UNAVAILABLE,
    ),
    WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL: _lifecycle_metadata(
        WORKER_STATUS_FAILED,
        WORKER_ACTION_NONE,
        terminal_status=True,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=6,
        exit_code=EX_UNAVAILABLE,
    ),
    WORKER_LIFECYCLE_TIMEOUT_ABORTED: _lifecycle_metadata(
        WORKER_STATUS_ABORTED,
        WORKER_ACTION_NONE,
        terminal_status=True,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=5,
        exit_code=EX_ABORTED,
    ),
    WORKER_LIFECYCLE_ABORTED: _lifecycle_metadata(
        WORKER_STATUS_ABORTED,
        WORKER_ACTION_NONE,
        status_alias=True,
        terminal_status=True,
        failed_dependency_status=True,
        status_priority=5,
        exit_code=EX_ABORTED,
    ),
}


def _status_aliases_by_lifecycle_metadata():
    aliases = {}
    for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items():
        if not metadata.status_alias:
            continue
        if metadata.status in aliases:
            raise ValueError(f"duplicate worker status alias: {metadata.status}")
        aliases[metadata.status] = lifecycle_state
    return aliases


def _status_values_by_lifecycle_metadata(field_name, *, skip_none=False):
    values = {}
    for metadata in WORKER_LIFECYCLE_METADATA.values():
        value = getattr(metadata, field_name)
        if skip_none and value is None:
            continue
        if metadata.status in values and values[metadata.status] != value:
            raise ValueError(f"conflicting worker status {field_name}: {metadata.status}")
        values[metadata.status] = value
    return values

BLOCKED_WORKER_STATUS = WORKER_STATUS_BLOCKED
TERMINAL_WORKER_STATUSES = frozenset(
    metadata.status for metadata in WORKER_LIFECYCLE_METADATA.values() if metadata.terminal_status
)
FAILED_DEPENDENCY_STATUSES = frozenset(
    metadata.status for metadata in WORKER_LIFECYCLE_METADATA.values() if metadata.failed_dependency_status
)
EXECUTABLE_WORKER_ACTIONS = frozenset(
    metadata.next_eligible_action for metadata in WORKER_LIFECYCLE_METADATA.values() if metadata.executable
)
PUBLIC_WORKER_STATE_BY_LIFECYCLE = {
    lifecycle_state: (metadata.status, metadata.next_eligible_action)
    for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items()
}
WORKER_LIFECYCLE_STATES = frozenset(WORKER_LIFECYCLE_METADATA)
WORKER_LIFECYCLE_STATE_BY_STATUS_ALIAS = _status_aliases_by_lifecycle_metadata()
WORKER_STATUS_PRIORITY_BY_STATUS = _status_values_by_lifecycle_metadata("status_priority")
WORKER_EXIT_CODE_BY_STATUS = _status_values_by_lifecycle_metadata("exit_code", skip_none=True)

WORKER_LIST_FIELDS = (
    "dependencies",
    "prompt_ids",
    "retryable_failures",
    "blockers",
    "output_refs",
)
WORKER_OPTIONAL_LIST_FIELDS = ("attempts",)
WORKER_SNAPSHOT_STATE_FIELDS = (
    "lifecycle_state",
    "retry_count",
    "timeout_started_at",
    "timed_out_at",
    "failure_category",
    "failure_reason",
    "last_failure_category",
    "last_failure_reason",
    "blockers",
    "output_refs",
    "error",
    "failure_retryable",
    "manual_retry_required",
    "result",
    "attempts",
    "cleanup",
    "abort",
)
REMOVABLE_WORKER_TRANSITION_FIELDS = ("error", "failure_retryable", "manual_retry_required")
UNSET_TRANSITION_FIELD = object()


def status_priority(status):
    return WORKER_STATUS_PRIORITY_BY_STATUS.get(short_status(status), WORKER_STATUS_PRIORITY_BY_STATUS[WORKER_STATUS_QUEUED])


def merge_status(incoming, current):
    if not isinstance(incoming, str) or not isinstance(current, str):
        return incoming
    return current if status_priority(current) > status_priority(incoming) else incoming


def status_owner(incoming_status, current_status):
    if not isinstance(incoming_status, str) or not isinstance(current_status, str):
        return "incoming"
    return "current" if status_priority(current_status) > status_priority(incoming_status) else "incoming"


def aggregate_run_status(statuses):
    statuses = [short_status(status) for status in statuses]
    if not statuses:
        return None
    if statuses == [WORKER_STATUS_DONE] or all(status == WORKER_STATUS_DONE for status in statuses):
        return WORKER_STATUS_DONE
    candidates = [status for status in statuses if status != WORKER_STATUS_DONE]
    status = max(candidates, key=status_priority)
    if status not in WORKER_STATUS_PRIORITY_BY_STATUS:
        return WORKER_STATUS_QUEUED
    return status


def exit_code_for_status(status, *, partial_success=False):
    status = short_status(status)
    if status == WORKER_STATUS_FAILED and partial_success:
        return EX_PARTIAL
    return WORKER_EXIT_CODE_BY_STATUS.get(status, EX_UNAVAILABLE)


def public_worker_state(lifecycle_state):
    return PUBLIC_WORKER_STATE_BY_LIFECYCLE.get(lifecycle_state, (None, WORKER_ACTION_NONE))


def public_worker_state_fields(lifecycle_state):
    status, action = public_worker_state(lifecycle_state)
    return {
        "lifecycle_state": lifecycle_state,
        "status": status,
        "next_eligible_action": action,
    }


def worker_lifecycle_source_states(transition_name):
    return _worker_transition_metadata(transition_name).source_states


def worker_lifecycle_target_states(transition_name):
    return _worker_transition_metadata(transition_name).target_states


def worker_lifecycle_state_for_status_alias(status):
    return WORKER_LIFECYCLE_STATE_BY_STATUS_ALIAS.get(short_status(status))


def worker_lifecycle_state_for_public_state(status, action, *, timeout_origin=False, default=None):
    status = short_status(status)
    for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items():
        if (
            metadata.status == status
            and metadata.next_eligible_action == action
            and metadata.timeout_origin == timeout_origin
        ):
            return lifecycle_state
    return default


def worker_failed_lifecycle_state(*, retryable, retry_available):
    return worker_lifecycle_state_for_public_state(
        WORKER_STATUS_FAILED,
        WORKER_ACTION_RETRY if retryable and retry_available else WORKER_ACTION_NONE,
        default=WORKER_LIFECYCLE_FAILED_TERMINAL,
    )


def worker_timeout_lifecycle_state(status, retry_available):
    if status == WORKER_STATUS_BLOCKED:
        return worker_lifecycle_state_for_public_state(
            WORKER_STATUS_BLOCKED,
            WORKER_ACTION_RESOLVE_BLOCKER,
            timeout_origin=True,
            default=WORKER_LIFECYCLE_BLOCKED_TIMEOUT,
        )
    if status == WORKER_STATUS_FAILED:
        return worker_lifecycle_state_for_public_state(
            WORKER_STATUS_FAILED,
            WORKER_ACTION_RETRY if retry_available else WORKER_ACTION_NONE,
            timeout_origin=True,
            default=WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL,
        )
    if status == WORKER_STATUS_ABORTED:
        return worker_lifecycle_state_for_public_state(
            WORKER_STATUS_ABORTED,
            WORKER_ACTION_NONE,
            timeout_origin=True,
            default=WORKER_LIFECYCLE_TIMEOUT_ABORTED,
        )
    return worker_lifecycle_state_for_public_state(
        WORKER_STATUS_TIMEOUT,
        WORKER_ACTION_RETRY if retry_available else WORKER_ACTION_NONE,
        timeout_origin=True,
        default=WORKER_LIFECYCLE_TIMEOUT_TERMINAL,
    )


def worker_result_lifecycle_state(status):
    status = short_status(status)
    if status not in {
        WORKER_STATUS_ABORTED,
        WORKER_STATUS_BLOCKED,
        WORKER_STATUS_DONE,
        WORKER_STATUS_FAILED,
        WORKER_STATUS_TIMEOUT,
    }:
        status = WORKER_STATUS_FAILED
    return worker_lifecycle_state_for_status_alias(status) or WORKER_LIFECYCLE_FAILED_TERMINAL


def _failed_transition_lifecycle_state(transition):
    payload = transition.payload
    return worker_failed_lifecycle_state(retryable=payload.retryable, retry_available=payload.retry_available)


def _timed_out_transition_lifecycle_state(transition):
    payload = transition.payload
    return worker_timeout_lifecycle_state(payload.status, payload.retry_available)


def _result_applied_transition_lifecycle_state(transition):
    return worker_result_lifecycle_state(transition.payload.result["status"])


def _snapshot_transition_is_legal(latest_worker, transition):
    source_state = worker_lifecycle_state(latest_worker)
    target_state = _snapshot_transition_lifecycle_state(transition)
    if target_state is None:
        return True
    return target_state in _WORKER_SNAPSHOT_TARGET_STATES_BY_SOURCE.get(source_state, frozenset())


def _snapshot_transition_lifecycle_state(transition):
    payload = transition.payload
    if "lifecycle_state" not in payload.state_fields or "lifecycle_state" not in payload.worker:
        return None
    lifecycle_state = payload.worker.get("lifecycle_state")
    if lifecycle_state in WORKER_LIFECYCLE_STATES:
        return lifecycle_state
    return WORKER_LIFECYCLE_QUEUED


WORKER_TRANSITION_METADATA = {
    metadata.name: metadata
    for metadata in (
        _transition_metadata(
            WorkerTransitionName.PROVISIONED,
            source_states=(
                WORKER_LIFECYCLE_QUEUED,
                WORKER_LIFECYCLE_ACTIVE_WAIT,
                WORKER_LIFECYCLE_ACTIVE_RETRY,
                WORKER_LIFECYCLE_FAILED_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
            ),
        ),
        _transition_metadata(
            WorkerTransitionName.ACTIVE,
            source_states=(
                WORKER_LIFECYCLE_QUEUED,
                WORKER_LIFECYCLE_ACTIVE_WAIT,
                WORKER_LIFECYCLE_ACTIVE_RETRY,
                WORKER_LIFECYCLE_BLOCKED_DEPENDENCY,
                WORKER_LIFECYCLE_BLOCKED_TIMEOUT,
                WORKER_LIFECYCLE_FAILED_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
            ),
            target_states=(WORKER_LIFECYCLE_ACTIVE_WAIT,),
            target_lifecycle=worker_lifecycle_state_for_status_alias(WORKER_STATUS_ACTIVE),
        ),
        _transition_metadata(
            WorkerTransitionName.ATTEMPT_STARTED,
            source_states=(WORKER_LIFECYCLE_ACTIVE_WAIT,),
        ),
        _transition_metadata(
            WorkerTransitionName.FAILED,
            source_states=(
                WORKER_LIFECYCLE_QUEUED,
                WORKER_LIFECYCLE_ACTIVE_WAIT,
                WORKER_LIFECYCLE_ACTIVE_RETRY,
                WORKER_LIFECYCLE_FAILED_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
            ),
            target_states=(WORKER_LIFECYCLE_FAILED_RETRY, WORKER_LIFECYCLE_FAILED_TERMINAL),
            target_lifecycle=_failed_transition_lifecycle_state,
        ),
        _transition_metadata(
            WorkerTransitionName.DEPENDENCY_BLOCKED,
            source_states=(
                WORKER_LIFECYCLE_QUEUED,
                WORKER_LIFECYCLE_ACTIVE_WAIT,
                WORKER_LIFECYCLE_ACTIVE_RETRY,
            ),
            target_states=(WORKER_LIFECYCLE_BLOCKED_DEPENDENCY,),
            target_lifecycle=worker_lifecycle_state_for_status_alias(WORKER_STATUS_BLOCKED),
        ),
        _transition_metadata(
            WorkerTransitionName.ABORTED,
            source_states=(
                WORKER_LIFECYCLE_QUEUED,
                WORKER_LIFECYCLE_ACTIVE_WAIT,
                WORKER_LIFECYCLE_ACTIVE_RETRY,
                WORKER_LIFECYCLE_BLOCKED_DEPENDENCY,
                WORKER_LIFECYCLE_BLOCKED_TIMEOUT,
                WORKER_LIFECYCLE_FAILED_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_ABORTED,
                WORKER_LIFECYCLE_ABORTED,
            ),
            target_states=(WORKER_LIFECYCLE_ABORTED,),
            target_lifecycle=worker_lifecycle_state_for_status_alias(WORKER_STATUS_ABORTED),
        ),
        _transition_metadata(
            WorkerTransitionName.RETRY_SCHEDULED,
            source_states=(
                WORKER_LIFECYCLE_ACTIVE_WAIT,
                WORKER_LIFECYCLE_FAILED_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
            ),
            target_states=(WORKER_LIFECYCLE_ACTIVE_RETRY,),
            target_lifecycle=worker_lifecycle_state_for_public_state(WORKER_STATUS_ACTIVE, WORKER_ACTION_RETRY),
        ),
        _transition_metadata(
            WorkerTransitionName.TIMED_OUT,
            source_states=(WORKER_LIFECYCLE_ACTIVE_WAIT,),
            target_states=(
                WORKER_LIFECYCLE_BLOCKED_TIMEOUT,
                WORKER_LIFECYCLE_TIMEOUT_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_TERMINAL,
                WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
                WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL,
                WORKER_LIFECYCLE_TIMEOUT_ABORTED,
            ),
            target_lifecycle=_timed_out_transition_lifecycle_state,
        ),
        _transition_metadata(
            WorkerTransitionName.RESULT_APPLIED,
            source_states=(WORKER_LIFECYCLE_ACTIVE_WAIT,),
            target_states=(
                WORKER_LIFECYCLE_BLOCKED_DEPENDENCY,
                WORKER_LIFECYCLE_DONE_COLLECT,
                WORKER_LIFECYCLE_FAILED_TERMINAL,
                WORKER_LIFECYCLE_TIMEOUT_TERMINAL,
                WORKER_LIFECYCLE_ABORTED,
            ),
            target_lifecycle=_result_applied_transition_lifecycle_state,
        ),
        _transition_metadata(
            WorkerTransitionName.CLEANUP_UPDATED,
            source_states=WORKER_LIFECYCLE_STATES,
            public_lifecycle_transition=False,
        ),
        _transition_metadata(
            WorkerTransitionName.SNAPSHOT_APPLIED,
            source_states=WORKER_LIFECYCLE_STATES,
            is_legal_transition=_snapshot_transition_is_legal,
            public_lifecycle_transition=False,
        ),
    )
}


_WORKER_SNAPSHOT_TARGET_STATES_BY_SOURCE = {
    source_state: frozenset(
        {
            source_state,
            *(
                target_state
                for metadata in WORKER_TRANSITION_METADATA.values()
                if source_state in metadata.source_states
                for target_state in metadata.target_states
            ),
        }
    )
    for source_state in WORKER_LIFECYCLE_STATES
}


def _worker_transition_metadata(name):
    metadata = WORKER_TRANSITION_METADATA.get(name)
    if metadata is None:
        raise ValueError(f"unknown worker transition: {name}")
    return metadata


def worker_transition_target_lifecycle_state(transition):
    target_lifecycle = _worker_transition_metadata(transition.name).target_lifecycle
    if callable(target_lifecycle):
        return target_lifecycle(transition)
    return target_lifecycle


def worker_transition_is_legal(latest_worker, transition):
    metadata = _worker_transition_metadata(transition.name)
    if metadata.is_legal_transition is not None:
        return metadata.is_legal_transition(latest_worker, transition)
    return worker_lifecycle_state(latest_worker) in metadata.source_states


def _lifecycle_metadata_with_transition_views(lifecycle_metadata, transition_metadata):
    source_transitions_by_state = {lifecycle_state: set() for lifecycle_state in lifecycle_metadata}
    target_transitions_by_state = {lifecycle_state: set() for lifecycle_state in lifecycle_metadata}
    for metadata in transition_metadata.values():
        if not metadata.public_lifecycle_transition:
            continue
        for lifecycle_state in metadata.source_states:
            if lifecycle_state not in source_transitions_by_state:
                raise ValueError(f"unknown worker transition source lifecycle state: {lifecycle_state}")
            source_transitions_by_state[lifecycle_state].add(metadata.name)
        for lifecycle_state in metadata.target_states:
            if lifecycle_state not in target_transitions_by_state:
                raise ValueError(f"unknown worker transition target lifecycle state: {lifecycle_state}")
            target_transitions_by_state[lifecycle_state].add(metadata.name)
    return {
        lifecycle_state: replace(
            metadata,
            source_transitions=frozenset(source_transitions_by_state[lifecycle_state]),
            target_transitions=frozenset(target_transitions_by_state[lifecycle_state]),
        )
        for lifecycle_state, metadata in lifecycle_metadata.items()
    }


WORKER_LIFECYCLE_METADATA = _lifecycle_metadata_with_transition_views(
    WORKER_LIFECYCLE_METADATA,
    WORKER_TRANSITION_METADATA,
)


def worker_lifecycle_set_fields(worker_id, lifecycle_state):
    return {"id": worker_id, "lifecycle_state": lifecycle_state}


def _is_worker_mapping(worker):
    return isinstance(worker, Mapping)


def is_worker_mapping(worker):
    return _is_worker_mapping(worker)


def worker_retry_available(worker, category=None):
    if not _is_worker_mapping(worker):
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
    lifecycle_state: Optional[str]
    status: Optional[str]
    next_eligible_action: str
    has_prompt: bool

    @classmethod
    def from_worker(cls, worker):
        if not _is_worker_mapping(worker):
            return cls(None, None, WORKER_ACTION_NONE, False)
        lifecycle_state = worker_lifecycle_state(worker)
        status, next_eligible_action = public_worker_state(lifecycle_state)
        return cls(lifecycle_state, status, next_eligible_action, worker_has_prompt(worker))

    def can_execute(self):
        return self.has_prompt and self.next_eligible_action in EXECUTABLE_WORKER_ACTIONS

    def can_block_for_dependency(self):
        return self.has_prompt and is_dependency_blockable_status(self.status)


def worker_lifecycle_state(worker):
    if not _is_worker_mapping(worker):
        return None
    return _canonical_lifecycle_state(worker)


def _canonical_lifecycle_state(worker):
    lifecycle_state = worker.get("lifecycle_state") if _is_worker_mapping(worker) else None
    if lifecycle_state in WORKER_LIFECYCLE_STATES:
        return lifecycle_state
    return WORKER_LIFECYCLE_QUEUED


def _lifecycle_state_from_legacy_public_worker_state(worker):
    """Compatibility boundary for legacy/public records that do not carry lifecycle_state."""
    worker = worker if _is_worker_mapping(worker) else {}
    status = short_status(worker.get("status"))
    if status == WORKER_STATUS_QUEUED:
        return worker_lifecycle_state_for_status_alias(status)
    if status == WORKER_STATUS_ACTIVE:
        if worker.get("next_eligible_action") == WORKER_ACTION_RETRY:
            return worker_lifecycle_state_for_public_state(status, WORKER_ACTION_RETRY)
        return worker_lifecycle_state_for_status_alias(status)
    if is_blocked_status(status):
        timeout_origin = worker.get("failure_category") == WORKER_STATUS_TIMEOUT or WORKER_STATUS_TIMEOUT in set(
            worker.get("blockers") or []
        )
        return worker_lifecycle_state_for_public_state(
            status,
            WORKER_ACTION_RESOLVE_BLOCKER,
            timeout_origin=timeout_origin,
        )
    if status == WORKER_STATUS_DONE:
        return worker_lifecycle_state_for_status_alias(status)
    if status == WORKER_STATUS_FAILED:
        if worker.get("failure_category") == WORKER_STATUS_TIMEOUT:
            return worker_lifecycle_state_for_public_state(
                status,
                WORKER_ACTION_RETRY if worker_retry_available(worker, WORKER_STATUS_TIMEOUT) else WORKER_ACTION_NONE,
                timeout_origin=True,
            )
        return worker_lifecycle_state_for_public_state(
            status,
            WORKER_ACTION_RETRY if worker_retry_available(worker) else WORKER_ACTION_NONE,
        )
    if status == WORKER_STATUS_TIMEOUT:
        return worker_lifecycle_state_for_public_state(
            status,
            WORKER_ACTION_RETRY if worker_retry_available(worker, WORKER_STATUS_TIMEOUT) else WORKER_ACTION_NONE,
            timeout_origin=True,
        )
    if status == WORKER_STATUS_ABORTED:
        if worker.get("failure_category") == WORKER_STATUS_TIMEOUT:
            return worker_lifecycle_state_for_public_state(
                status,
                WORKER_ACTION_NONE,
                timeout_origin=True,
            )
        return worker_lifecycle_state_for_status_alias(status)
    return WORKER_LIFECYCLE_QUEUED


def canonicalize_legacy_worker_record(worker):
    fields = dict(worker) if _is_worker_mapping(worker) else {}
    if fields.get("lifecycle_state") not in WORKER_LIFECYCLE_STATES:
        fields["lifecycle_state"] = _lifecycle_state_from_legacy_public_worker_state(fields)
    return fields


def latest_prompt_ids_are_retry_marker(latest_worker):
    return (
        _is_worker_mapping(latest_worker)
        and worker_lifecycle_state(latest_worker) == WORKER_LIFECYCLE_ACTIVE_RETRY
        and latest_worker.get("last_failure_category") is not None
    )


def next_eligible_worker_action(worker):
    if not _is_worker_mapping(worker):
        return WORKER_ACTION_NONE
    return WorkerSchedulingState.from_worker(worker).next_eligible_action


def worker_has_prompt(worker):
    if not _is_worker_mapping(worker):
        return False
    prompt = worker.get("prompt")
    return prompt is not None and bool(str(prompt))


def is_executable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_execute()


def is_dependency_blockable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_block_for_dependency()


class WorkerRecord(MutableMapping):
    """Hydrated worker domain object.

    Worker records provide mapping-style access for existing orchestration code,
    but mutations are backed by this object rather than by persisted JSON dicts.
    Storage still writes sparse snapshots via to_snapshot().
    """

    def __init__(self, worker_id, fields=None):
        self._fields = deepcopy(dict(fields or {}))
        self._worker_id = self._fields.get("id") or worker_id

    def __getitem__(self, key):
        return self._fields[key]

    def __setitem__(self, key, value):
        self._fields[key] = value

    def __delitem__(self, key):
        del self._fields[key]

    def __iter__(self):
        return iter(self._fields)

    def __len__(self):
        return len(self._fields)

    def __repr__(self):
        return f"{type(self).__name__}({self.worker_id!r}, {self._fields!r})"

    def __eq__(self, other):
        if isinstance(other, WorkerRecord):
            return self._fields == other._fields
        if isinstance(other, Mapping):
            return self._fields == dict(other)
        return NotImplemented

    @classmethod
    def from_worker(cls, worker, worker_id=None):
        fields = dict(worker) if _is_worker_mapping(worker) else {}
        resolved_worker_id = fields.get("id") or worker_id
        return cls(resolved_worker_id, fields)

    @classmethod
    def default_snapshot_fields(cls, worker_id):
        return {
            "id": worker_id,
            "role": None,
            "session_id": None,
            "agent": None,
            "model": None,
            "dependencies": [],
            "prompt_ids": [],
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
            "blockers": [],
            "output_refs": [],
        }

    @classmethod
    def default_fields(cls, worker_id):
        fields = cls.default_snapshot_fields(worker_id)
        fields.update(cls.public_state_fields(fields["lifecycle_state"]))
        return cls.from_worker(require_internal_worker(fields), worker_id)

    @classmethod
    def public_state(cls, lifecycle_state):
        return public_worker_state(lifecycle_state)

    @classmethod
    def public_state_fields(cls, lifecycle_state):
        return public_worker_state_fields(lifecycle_state)

    @classmethod
    def lifecycle_set_fields(cls, worker_id, lifecycle_state):
        return worker_lifecycle_set_fields(worker_id, lifecycle_state)

    @property
    def worker_id(self):
        return self.get("id") or self._worker_id

    @property
    def fields(self):
        return self

    @property
    def lifecycle_state(self):
        return _canonical_lifecycle_state(self.fields)

    @property
    def status(self):
        return self.public_state(self.lifecycle_state)[0]

    @property
    def next_eligible_action(self):
        return self.public_state(self.lifecycle_state)[1]

    @property
    def has_prompt(self):
        return worker_has_prompt(self.fields)

    def scheduling_state(self):
        return WorkerSchedulingState(
            self.lifecycle_state,
            self.status,
            self.next_eligible_action,
            self.has_prompt,
        )

    def to_snapshot(self):
        normalized = self.default_snapshot_fields(self.worker_id)
        fields = deepcopy(dict(self))
        fields.pop("status", None)
        fields.pop("next_eligible_action", None)
        normalized.update(fields)
        normalized["id"] = normalized.get("id") or self.worker_id
        for key in WORKER_LIST_FIELDS:
            value = normalized.get(key)
            normalized[key] = value if isinstance(value, list) else []
        for key in WORKER_OPTIONAL_LIST_FIELDS:
            if key in normalized:
                value = normalized.get(key)
                normalized[key] = value if isinstance(value, list) else []
        if normalized.get("retry_count") is None:
            normalized["retry_count"] = 0
        if normalized.get("retry_limit") is None:
            normalized["retry_limit"] = 0
        if not normalized.get("timeout_policy"):
            normalized["timeout_policy"] = WORKER_STATUS_TIMEOUT
        if normalized.get("lifecycle_state") not in WORKER_LIFECYCLE_STATES:
            normalized["lifecycle_state"] = WORKER_LIFECYCLE_QUEUED
        return normalized

    def to_worker(self):
        normalized = self.to_snapshot()
        normalized.update(self.public_state_fields(normalized["lifecycle_state"]))
        return type(self).from_worker(require_internal_worker(normalized), self.worker_id)

    def serialized_public_state(self):
        return self.public_state_fields(self.lifecycle_state)

    def set_session(self, session_id, *, agent=None, model=None):
        self["session_id"] = deepcopy(session_id)
        if agent is not None:
            self["agent"] = deepcopy(agent)
        if model is not None:
            self["model"] = deepcopy(model)
        return self

    def remember_prompt_id(self, prompt_id):
        prompt_ids = self.get("prompt_ids")
        if not isinstance(prompt_ids, list):
            prompt_ids = []
        if prompt_id not in prompt_ids:
            prompt_ids.append(prompt_id)
        self["prompt_ids"] = prompt_ids
        return self

    def apply_transition(self, transition):
        result = _apply_worker_transition_to_record(self, transition)
        if result.skipped and not result.stale_snapshot_recovery:
            raise WorkerTransitionError(result)
        merged = result.worker
        self.clear()
        self.update(merged)
        self._worker_id = self.get("id") or self._worker_id or transition.worker_id
        return self

    def ensure_cleanup(self):
        cleanup = self.get("cleanup")
        if not isinstance(cleanup, dict):
            cleanup = {"requested": True, "deleted": False}
            self["cleanup"] = cleanup
        return cleanup

    def remember_session_for_cleanup(self, session_id):
        cleanup = self.ensure_cleanup()
        cleanup["requested"] = True
        cleanup["deleted"] = False
        sessions = cleanup.get("sessions")
        if not isinstance(sessions, list):
            sessions = []
        if isinstance(session_id, str) and session_id and session_id not in sessions:
            sessions.append(session_id)
        cleanup["sessions"] = sessions
        return self


def default_worker_record(worker_id):
    return WorkerRecord.default_fields(worker_id)


def deserialize_worker_record(worker, worker_id):
    return WorkerRecord.from_worker(canonicalize_legacy_worker_record(worker), worker_id).to_worker()


def serialize_worker_snapshot(worker, worker_id):
    return WorkerRecord.from_worker(worker, worker_id).to_snapshot()


def worker_record_for_mutation(worker, worker_id=None):
    if isinstance(worker, WorkerRecord):
        return worker
    return WorkerRecord.from_worker(worker, worker_id)


def sync_worker_record(worker, record):
    if worker is not record and isinstance(worker, MutableMapping):
        worker.clear()
        worker.update(record)
        return worker
    return record


def require_internal_worker(worker):
    missing = [field_name for field_name in WORKER_REQUIRED_FIELD_NAMES if field_name not in worker]
    if missing:
        raise ValueError(f"internal worker missing required fields: {', '.join(missing)}")
    return worker


@dataclass(frozen=True)
class _ProvisionedTransition:
    session_id: object = None
    agent: object = None
    model: object = None


@dataclass(frozen=True)
class _ActiveTransition:
    timeout_started_at: object = UNSET_TRANSITION_FIELD
    clear_prompt_ids: bool = False


@dataclass(frozen=True)
class _AttemptStartedTransition:
    attempt: dict


@dataclass(frozen=True)
class _FailedTransition:
    category: str
    reason: str
    retryable: bool = True
    retry_available: bool = False
    timeout_started_at: object = UNSET_TRANSITION_FIELD
    prompt_ids: tuple = ()


@dataclass(frozen=True)
class _DependencyBlockedTransition:
    blockers: tuple


@dataclass(frozen=True)
class _AbortedTransition:
    abort: object


@dataclass(frozen=True)
class _RetryScheduledTransition:
    category: str
    reason: str
    retry_count: int
    timeout_started_at: object = UNSET_TRANSITION_FIELD
    prompt_ids: tuple = ()


@dataclass(frozen=True)
class _TimedOutTransition:
    reason: str
    status: str
    timed_out_at: object
    retry_available: bool = False
    manual_retry_required: bool = False
    timeout_started_at: object = UNSET_TRANSITION_FIELD


@dataclass(frozen=True)
class _ResultAppliedTransition:
    result: dict
    prompt_ids: tuple = ()
    timeout_started_at: object = UNSET_TRANSITION_FIELD


@dataclass(frozen=True)
class _CleanupUpdatedTransition:
    cleanup: object = None


@dataclass(frozen=True)
class _SnapshotAppliedTransition:
    worker: dict
    state_fields: tuple = WORKER_SNAPSHOT_STATE_FIELDS
    set_if_missing_fields: tuple = ("session_id",)
    removable_fields: tuple = REMOVABLE_WORKER_TRANSITION_FIELDS


@dataclass(frozen=True)
class _AttemptFinalization:
    attempt_id: str
    fields: dict


@dataclass(frozen=True)
class WorkerTransition:
    """Named lifecycle transition applied by WorkerLifecycleReducer."""

    worker_id: str
    name: WorkerTransitionName
    payload: object = None
    attempt_finalization: Optional[_AttemptFinalization] = None

    def __post_init__(self):
        if not isinstance(self.name, WorkerTransitionName):
            raise ValueError(f"unknown worker transition: {self.name}")

    def with_finalized_attempt(self, attempt_id, fields):
        return WorkerTransition(
            self.worker_id,
            self.name,
            self.payload,
            _AttemptFinalization(attempt_id, deepcopy(fields or {})),
        )

    @classmethod
    def provisioned(cls, worker):
        worker_id = worker["id"]
        return cls(
            worker_id,
            WorkerTransitionName.PROVISIONED,
            _ProvisionedTransition(
                session_id=deepcopy(worker.get("session_id")),
                agent=_copy_present(worker.get("agent")),
                model=_copy_present(worker.get("model")),
            ),
        )

    @classmethod
    def active(cls, worker_id, *, timeout_started_at=UNSET_TRANSITION_FIELD, clear_prompt_ids=False):
        return cls(
            worker_id,
            WorkerTransitionName.ACTIVE,
            _ActiveTransition(
                timeout_started_at=_copy_transition_value(timeout_started_at),
                clear_prompt_ids=clear_prompt_ids,
            ),
        )

    @classmethod
    def attempt_started(cls, worker_id, attempt):
        return cls(
            worker_id,
            WorkerTransitionName.ATTEMPT_STARTED,
            _AttemptStartedTransition(deepcopy(attempt or {})),
        )

    @classmethod
    def failed(
        cls,
        worker_id,
        category,
        reason,
        *,
        retryable=True,
        retry_available=False,
        timeout_started_at=UNSET_TRANSITION_FIELD,
        prompt_ids=(),
    ):
        return cls(
            worker_id,
            WorkerTransitionName.FAILED,
            _FailedTransition(
                category,
                reason,
                retryable=retryable,
                retry_available=retry_available,
                timeout_started_at=_copy_transition_value(timeout_started_at),
                prompt_ids=_filtered_prompt_ids(prompt_ids),
            ),
        )

    @classmethod
    def dependency_blocked(cls, worker_id, blockers):
        return cls(
            worker_id,
            WorkerTransitionName.DEPENDENCY_BLOCKED,
            _DependencyBlockedTransition(tuple(blockers)),
        )

    @classmethod
    def aborted(cls, worker_id, abort):
        return cls(worker_id, WorkerTransitionName.ABORTED, _AbortedTransition(deepcopy(abort)))

    @classmethod
    def retry_scheduled(
        cls,
        worker_id,
        category,
        reason,
        *,
        retry_count,
        timeout_started_at=UNSET_TRANSITION_FIELD,
        prompt_ids=(),
    ):
        return cls(
            worker_id,
            WorkerTransitionName.RETRY_SCHEDULED,
            _RetryScheduledTransition(
                category,
                reason,
                retry_count=retry_count,
                timeout_started_at=_copy_transition_value(timeout_started_at),
                prompt_ids=_filtered_prompt_ids(prompt_ids),
            ),
        )

    @classmethod
    def timed_out(
        cls,
        worker_id,
        reason,
        *,
        status,
        timed_out_at,
        retry_available=False,
        manual_retry_required=False,
        timeout_started_at=UNSET_TRANSITION_FIELD,
    ):
        return cls(
            worker_id,
            WorkerTransitionName.TIMED_OUT,
            _TimedOutTransition(
                reason,
                status=status,
                timed_out_at=deepcopy(timed_out_at),
                retry_available=retry_available,
                manual_retry_required=manual_retry_required,
                timeout_started_at=_copy_transition_value(timeout_started_at),
            ),
        )

    @classmethod
    def result_applied(cls, worker_id, result, *, prompt_ids=(), timeout_started_at=UNSET_TRANSITION_FIELD):
        return cls(
            worker_id,
            WorkerTransitionName.RESULT_APPLIED,
            _ResultAppliedTransition(
                deepcopy(result or {}),
                prompt_ids=_filtered_prompt_ids(prompt_ids),
                timeout_started_at=_copy_transition_value(timeout_started_at),
            ),
        )

    @classmethod
    def cleanup_updated(cls, worker):
        worker_id = worker["id"]
        return cls(
            worker_id,
            WorkerTransitionName.CLEANUP_UPDATED,
            _CleanupUpdatedTransition(deepcopy(worker.get("cleanup"))),
        )

    @classmethod
    def snapshot_applied(cls, worker):
        worker_id = worker["id"]
        return cls(
            worker_id,
            WorkerTransitionName.SNAPSHOT_APPLIED,
            _SnapshotAppliedTransition(
                deepcopy(worker),
                state_fields=tuple(WORKER_SNAPSHOT_STATE_FIELDS),
                set_if_missing_fields=("session_id",),
                removable_fields=tuple(REMOVABLE_WORKER_TRANSITION_FIELDS),
            ),
        )


def _copy_present(value):
    return None if value is None else deepcopy(value)


def _copy_transition_value(value):
    if value is UNSET_TRANSITION_FIELD:
        return value
    return deepcopy(value)


def _filtered_prompt_ids(prompt_ids):
    return tuple(prompt_id for prompt_id in prompt_ids if prompt_id is not None)


def default_worker(worker_id):
    return deserialize_worker_record({}, worker_id)


def normalize_worker(worker, worker_id):
    return deserialize_worker_record(worker, worker_id)


def normalize_worker_snapshot(worker, worker_id):
    return serialize_worker_snapshot(canonicalize_legacy_worker_record(worker), worker_id)


def _apply_worker_transition_to_record(worker, transition):
    from opencode_session.worker_lifecycle_reducer import apply_worker_transition_to_record

    record = WorkerRecord.from_worker(worker, transition.worker_id)
    return apply_worker_transition_to_record(record, transition)


def apply_worker_transition_to_worker(worker, transition):
    record = worker_record_for_mutation(worker, transition.worker_id)
    record.apply_transition(transition)
    return sync_worker_record(worker, record)


def apply_worker_transition(latest_workers, transition):
    latest_worker = latest_workers.get(transition.worker_id)
    record = worker_record_for_mutation(latest_worker, transition.worker_id)
    record.apply_transition(transition)
    latest_workers[transition.worker_id] = record
    return record


def next_eligible_action(worker):
    if not _is_worker_mapping(worker):
        return WORKER_ACTION_NONE
    return WorkerRecord.from_worker(worker).next_eligible_action


def ensure_worker(run, worker_id, *, role):
    workers = run.setdefault("workers", {})
    worker = normalize_worker(workers.get(worker_id), worker_id)
    if not worker.get("role"):
        worker["role"] = role
    worker["id"] = worker_id
    workers[worker_id] = worker
    return worker


def mark_worker_active(worker, *, now=None):
    timeout_started_at = UNSET_TRANSITION_FIELD
    if now is not None:
        timeout_started_at = now() if worker.get("timeout_seconds") else None
    transition = WorkerTransition.active(
        _worker_id(worker),
        timeout_started_at=timeout_started_at,
        clear_prompt_ids=latest_prompt_ids_are_retry_marker(worker),
    )
    return transition


def mark_worker_failed(worker, category, reason, *, retryable=True, prompt_ids=()):
    transition = WorkerTransition.failed(
        _worker_id(worker),
        category,
        reason,
        retryable=retryable,
        retry_available=worker_retry_available(worker, category),
        timeout_started_at=_existing_or_unset(worker, "timeout_started_at"),
        prompt_ids=prompt_ids,
    )
    return transition


def mark_dependency_blocked(worker, blockers):
    transition = WorkerTransition.dependency_blocked(_worker_id(worker), blockers)
    return transition


def mark_worker_aborted(worker, abort):
    transition = WorkerTransition.aborted(_worker_id(worker), abort)
    return transition


def schedule_worker_retry(worker, category, reason, *, prompt_ids=()):
    if not worker_retry_available(worker, category):
        return False
    transition = WorkerTransition.retry_scheduled(
        _worker_id(worker),
        category,
        reason,
        retry_count=int(worker.get("retry_count") or 0) + 1,
        timeout_started_at=_existing_or_unset(worker, "timeout_started_at"),
        prompt_ids=prompt_ids,
    )
    return transition


def worker_timeout_reason(worker):
    return f"worker timed out after {format_timeout(worker.get('timeout_seconds'))}s"


def mark_worker_timeout(worker, reason, now, *, manual_retry_required=False):
    status = worker.get("timeout_policy") or WORKER_STATUS_TIMEOUT
    transition = WorkerTransition.timed_out(
        _worker_id(worker),
        reason,
        status=status,
        timed_out_at=now(),
        retry_available=worker_retry_available(worker, WORKER_STATUS_TIMEOUT),
        manual_retry_required=manual_retry_required,
        timeout_started_at=_existing_or_unset(worker, "timeout_started_at"),
    )
    return transition


def format_timeout(timeout):
    return str(timeout)


def apply_worker_result(worker, result, *, prompt_ids=()):
    transition = WorkerTransition.result_applied(
        _worker_id(worker),
        result,
        prompt_ids=prompt_ids,
        timeout_started_at=_existing_or_unset(worker, "timeout_started_at"),
    )
    return transition


def _worker_id(worker):
    return worker["id"]


def _existing_or_unset(worker, field_name):
    return worker[field_name] if field_name in worker else UNSET_TRANSITION_FIELD


def refresh_run_summary(run, *, include_unprompted_when_no_prompts=False):
    workers = run.get("workers", {})
    run["output_refs"] = worker_output_refs_in_dependency_order(workers)
    status = run_status_from_workers(
        workers,
        include_unprompted_when_no_prompts=include_unprompted_when_no_prompts,
    )
    if status is not None:
        run["status"] = status


def run_status_from_workers(workers, *, include_unprompted_when_no_prompts=False):
    prompted_workers = [worker for worker in workers.values() if _is_worker_mapping(worker) and worker_prompt(worker)]
    status_workers = prompted_workers
    if include_unprompted_when_no_prompts:
        status_workers = prompted_workers or [worker for worker in workers.values() if _is_worker_mapping(worker)]
    return aggregate_run_status(_worker_status(worker) for worker in status_workers)


def worker_output_refs_in_dependency_order(workers):
    ordered = []
    for worker in workers_in_dependency_order(workers):
        worker_id = worker.get("id")
        if _worker_status(worker) != WORKER_STATUS_DONE:
            continue
        for output_ref in worker.get("output_refs", []):
            if isinstance(output_ref, str) and output_ref.startswith("assistant:"):
                ordered.append(f"{worker_id}:{output_ref.split(':', 1)[1]}")
            else:
                ordered.append(f"{worker_id}:{output_ref}")
    return ordered


def workers_in_dependency_order(workers):
    from opencode_session.worker_dependencies import analyze_worker_dependencies

    analysis = analyze_worker_dependencies(workers)
    return [workers[worker_id] for worker_id in analysis.worker_ids_in_dependency_order]


def exit_code_for_run(run):
    return exit_code_for_status(run.get("status"), partial_success=has_partial_worker_success(run))


def has_partial_worker_success(run):
    workers = [worker for worker in (run.get("workers") or {}).values() if _is_worker_mapping(worker) and worker_prompt(worker)]
    if not workers:
        return False
    statuses = {_worker_status(worker) for worker in workers}
    return WORKER_STATUS_DONE in statuses and any(
        status in {WORKER_STATUS_FAILED, WORKER_STATUS_BLOCKED, WORKER_STATUS_ABORTED, WORKER_STATUS_TIMEOUT}
        for status in statuses
    )


def worker_prompt(worker):
    prompt = worker.get("prompt")
    if prompt is None:
        return None
    return str(prompt)


def _worker_status(worker):
    return WorkerRecord.from_worker(worker).status if _is_worker_mapping(worker) else None
