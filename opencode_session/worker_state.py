from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional, Union

from opencode_session.schema_common import WORKER_REQUIRED_FIELD_NAMES
from opencode_session.status import short_status
from opencode_session.worker_attempt_log import _append_attempt


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


class WorkerLifecycleStatus(str, Enum):
    QUEUED = WORKER_STATUS_QUEUED
    ACTIVE = WORKER_STATUS_ACTIVE
    BLOCKED = WORKER_STATUS_BLOCKED
    DONE = WORKER_STATUS_DONE
    FAILED = WORKER_STATUS_FAILED
    TIMEOUT = WORKER_STATUS_TIMEOUT
    ABORTED = WORKER_STATUS_ABORTED

    def __str__(self):
        return self.value


class WorkerLifecycleAction(str, Enum):
    START = WORKER_ACTION_START
    WAIT = WORKER_ACTION_WAIT
    RETRY = WORKER_ACTION_RETRY
    RESOLVE_BLOCKER = WORKER_ACTION_RESOLVE_BLOCKER
    COLLECT = WORKER_ACTION_COLLECT
    NONE = WORKER_ACTION_NONE

    def __str__(self):
        return self.value


@dataclass(frozen=True)
class WorkerLifecycleDimensions:
    status: WorkerLifecycleStatus
    action: WorkerLifecycleAction
    retryable: bool = False
    timeout_origin: bool = False

    def __post_init__(self):
        object.__setattr__(self, "status", WorkerLifecycleStatus(short_status(self.status)))
        object.__setattr__(self, "action", WorkerLifecycleAction(self.action))
        object.__setattr__(self, "retryable", bool(self.retryable))
        object.__setattr__(self, "timeout_origin", bool(self.timeout_origin))
        if self.retryable and self.action is not WorkerLifecycleAction.RETRY:
            raise ValueError("retryable worker lifecycle states must use the retry action dimension")

    @classmethod
    def from_public_state(cls, status, action, *, timeout_origin=False):
        status = WorkerLifecycleStatus(short_status(status))
        action = WorkerLifecycleAction(action or WORKER_ACTION_NONE)
        retryable = action is WorkerLifecycleAction.RETRY
        if action is WorkerLifecycleAction.NONE and status in {
            WorkerLifecycleStatus.FAILED,
            WorkerLifecycleStatus.TIMEOUT,
        }:
            action = WorkerLifecycleAction.RETRY
        return cls(status, action, retryable=retryable, timeout_origin=timeout_origin)

    @classmethod
    def timeout(cls, status, *, retry_available):
        try:
            status = WorkerLifecycleStatus(short_status(status))
        except ValueError:
            status = WorkerLifecycleStatus.TIMEOUT
        if status is WorkerLifecycleStatus.BLOCKED:
            return cls(status, WorkerLifecycleAction.RESOLVE_BLOCKER, timeout_origin=True)
        if status is WorkerLifecycleStatus.ABORTED:
            return cls(status, WorkerLifecycleAction.NONE, timeout_origin=True)
        if status is WorkerLifecycleStatus.FAILED:
            return cls(status, WorkerLifecycleAction.RETRY, retryable=retry_available, timeout_origin=True)
        return cls(
            WorkerLifecycleStatus.TIMEOUT,
            WorkerLifecycleAction.RETRY,
            retryable=retry_available,
            timeout_origin=True,
        )

    @property
    def public_status(self):
        return self.status.value

    @property
    def public_action(self):
        if self.action is WorkerLifecycleAction.RETRY:
            return WORKER_ACTION_RETRY if self.retryable else WORKER_ACTION_NONE
        return self.action.value


@dataclass(frozen=True)
class WorkerLifecycleMetadata:
    dimensions: WorkerLifecycleDimensions
    status_alias: bool = False
    terminal_status: bool = False
    failed_dependency_status: bool = False
    executable: bool = False
    status_priority: int = 0
    source_transitions: frozenset = frozenset()
    target_transitions: frozenset = frozenset()

    @property
    def status(self):
        return self.dimensions.public_status

    @property
    def next_eligible_action(self):
        return self.dimensions.public_action

    @property
    def retryable(self):
        return self.dimensions.retryable

    @property
    def timeout_origin(self):
        return self.dimensions.timeout_origin


@dataclass(frozen=True)
class WorkerTransitionMetadata:
    name: WorkerTransitionName
    source_states: frozenset
    target_states: frozenset
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
    action,
    *,
    retryable=False,
    status_alias=False,
    terminal_status=False,
    failed_dependency_status=False,
    executable=False,
    timeout_origin=False,
    status_priority=0,
    source_transitions=(),
    target_transitions=(),
):
    return WorkerLifecycleMetadata(
        WorkerLifecycleDimensions(status, action, retryable=retryable, timeout_origin=timeout_origin),
        status_alias=status_alias,
        terminal_status=terminal_status,
        failed_dependency_status=failed_dependency_status,
        executable=executable,
        status_priority=status_priority,
        source_transitions=frozenset(source_transitions),
        target_transitions=frozenset(target_transitions),
    )


def _transition_metadata(
    name,
    *,
    source_states=(),
    target_states=(),
    public_lifecycle_transition=True,
):
    return WorkerTransitionMetadata(
        name,
        frozenset(source_states),
        frozenset(target_states),
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
        retryable=True,
        executable=True,
        status_priority=1,
    ),
    WORKER_LIFECYCLE_BLOCKED_DEPENDENCY: _lifecycle_metadata(
        WORKER_STATUS_BLOCKED,
        WORKER_ACTION_RESOLVE_BLOCKER,
        status_alias=True,
        failed_dependency_status=True,
        status_priority=2,
    ),
    WORKER_LIFECYCLE_BLOCKED_TIMEOUT: _lifecycle_metadata(
        WORKER_STATUS_BLOCKED,
        WORKER_ACTION_RESOLVE_BLOCKER,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=2,
    ),
    WORKER_LIFECYCLE_DONE_COLLECT: _lifecycle_metadata(
        WORKER_STATUS_DONE,
        WORKER_ACTION_COLLECT,
        status_alias=True,
        terminal_status=True,
        status_priority=3,
    ),
    WORKER_LIFECYCLE_FAILED_RETRY: _lifecycle_metadata(
        WORKER_STATUS_FAILED,
        WORKER_ACTION_RETRY,
        retryable=True,
        terminal_status=True,
        failed_dependency_status=True,
        executable=True,
        status_priority=6,
    ),
    WORKER_LIFECYCLE_FAILED_TERMINAL: _lifecycle_metadata(
        WORKER_STATUS_FAILED,
        WORKER_ACTION_RETRY,
        status_alias=True,
        terminal_status=True,
        failed_dependency_status=True,
        status_priority=6,
    ),
    WORKER_LIFECYCLE_TIMEOUT_RETRY: _lifecycle_metadata(
        WORKER_STATUS_TIMEOUT,
        WORKER_ACTION_RETRY,
        retryable=True,
        terminal_status=True,
        failed_dependency_status=True,
        executable=True,
        timeout_origin=True,
        status_priority=4,
    ),
    WORKER_LIFECYCLE_TIMEOUT_TERMINAL: _lifecycle_metadata(
        WORKER_STATUS_TIMEOUT,
        WORKER_ACTION_RETRY,
        status_alias=True,
        terminal_status=True,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=4,
    ),
    WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY: _lifecycle_metadata(
        WORKER_STATUS_FAILED,
        WORKER_ACTION_RETRY,
        retryable=True,
        terminal_status=True,
        failed_dependency_status=True,
        executable=True,
        timeout_origin=True,
        status_priority=6,
    ),
    WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL: _lifecycle_metadata(
        WORKER_STATUS_FAILED,
        WORKER_ACTION_RETRY,
        terminal_status=True,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=6,
    ),
    WORKER_LIFECYCLE_TIMEOUT_ABORTED: _lifecycle_metadata(
        WORKER_STATUS_ABORTED,
        WORKER_ACTION_NONE,
        terminal_status=True,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=5,
    ),
    WORKER_LIFECYCLE_ABORTED: _lifecycle_metadata(
        WORKER_STATUS_ABORTED,
        WORKER_ACTION_NONE,
        status_alias=True,
        terminal_status=True,
        failed_dependency_status=True,
        status_priority=5,
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


def _worker_lifecycle_state_by_dimensions(lifecycle_metadata):
    states = {}
    for lifecycle_state, metadata in lifecycle_metadata.items():
        if metadata.dimensions in states:
            raise ValueError(f"duplicate worker lifecycle dimensions: {metadata.dimensions}")
        states[metadata.dimensions] = lifecycle_state
    return states


def _dimension_filter_values(value, enum_type, *, short=False):
    if value is None:
        return None
    values = value if isinstance(value, (tuple, list, set, frozenset)) else (value,)
    if short:
        return frozenset(enum_type(short_status(item)) for item in values)
    return frozenset(enum_type(item) for item in values)


def _lifecycle_states_matching(*, status=None, action=None, retryable=None, timeout_origin=None):
    statuses = _dimension_filter_values(status, WorkerLifecycleStatus, short=True)
    actions = _dimension_filter_values(action, WorkerLifecycleAction)
    return frozenset(
        lifecycle_state
        for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items()
        if (statuses is None or metadata.dimensions.status in statuses)
        and (actions is None or metadata.dimensions.action in actions)
        and (retryable is None or metadata.retryable is retryable)
        and (timeout_origin is None or metadata.timeout_origin is timeout_origin)
    )


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
WORKER_LIFECYCLE_DIMENSIONS_BY_STATE = {
    lifecycle_state: metadata.dimensions for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items()
}
WORKER_LIFECYCLE_STATE_BY_DIMENSIONS = _worker_lifecycle_state_by_dimensions(WORKER_LIFECYCLE_METADATA)
WORKER_LIFECYCLE_STATES = frozenset(WORKER_LIFECYCLE_METADATA)
WORKER_RETRYABLE_LIFECYCLE_STATES = _lifecycle_states_matching(retryable=True)
WORKER_BLOCKED_LIFECYCLE_STATES = _lifecycle_states_matching(status=WORKER_STATUS_BLOCKED)
WORKER_ABORTED_LIFECYCLE_STATES = _lifecycle_states_matching(status=WORKER_STATUS_ABORTED)
WORKER_FAILED_TARGET_LIFECYCLE_STATES = _lifecycle_states_matching(
    status=WORKER_STATUS_FAILED,
    timeout_origin=False,
)
WORKER_RETRY_SCHEDULE_SOURCE_LIFECYCLE_STATES = frozenset(
    {WORKER_LIFECYCLE_ACTIVE_WAIT}
    | _lifecycle_states_matching(
        status=(WORKER_STATUS_FAILED, WORKER_STATUS_TIMEOUT),
        retryable=True,
    )
)
WORKER_TIMEOUT_ORIGIN_LIFECYCLE_STATES = _lifecycle_states_matching(timeout_origin=True)
WORKER_LIFECYCLE_STATE_BY_STATUS_ALIAS = _status_aliases_by_lifecycle_metadata()
WORKER_STATUS_PRIORITY_BY_STATUS = _status_values_by_lifecycle_metadata("status_priority")

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
PUBLIC_WORKER_STATE_FIELD_NAMES = frozenset(("status", "next_eligible_action"))


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


def public_worker_state(lifecycle_state):
    return PUBLIC_WORKER_STATE_BY_LIFECYCLE.get(lifecycle_state, (None, WORKER_ACTION_NONE))


def public_worker_state_fields(lifecycle_state):
    status, action = public_worker_state(lifecycle_state)
    return {
        "lifecycle_state": lifecycle_state,
        "status": status,
        "next_eligible_action": action,
    }


def worker_output_dict(worker, worker_id=None):
    record = WorkerRecord.from_worker(canonicalize_legacy_worker_record(worker), worker_id).to_worker()
    fields = record.to_snapshot()
    fields.update(public_worker_state_fields(fields["lifecycle_state"]))
    return fields


def worker_output_field(worker, field_name, default=None):
    return worker_output_dict(worker).get(field_name, default)


def worker_lifecycle_source_states(transition_name):
    return _worker_transition_metadata(transition_name).source_states


def worker_lifecycle_target_states(transition_name):
    return _worker_transition_metadata(transition_name).target_states


def worker_lifecycle_state_for_status_alias(status):
    return WORKER_LIFECYCLE_STATE_BY_STATUS_ALIAS.get(short_status(status))


def worker_lifecycle_state_for_dimensions(dimensions, *, default=None):
    if not isinstance(dimensions, WorkerLifecycleDimensions):
        return default
    return WORKER_LIFECYCLE_STATE_BY_DIMENSIONS.get(dimensions, default)


def worker_lifecycle_state_for_public_state(status, action, *, timeout_origin=False, default=None):
    try:
        dimensions = WorkerLifecycleDimensions.from_public_state(status, action, timeout_origin=timeout_origin)
    except ValueError:
        return default
    lifecycle_state = worker_lifecycle_state_for_dimensions(dimensions)
    if lifecycle_state is not None:
        return lifecycle_state
    return default


def worker_failed_lifecycle_state(*, retryable, retry_available):
    return worker_lifecycle_state_for_dimensions(
        WorkerLifecycleDimensions(
            WorkerLifecycleStatus.FAILED,
            WorkerLifecycleAction.RETRY,
            retryable=retryable and retry_available,
        ),
        default=WORKER_LIFECYCLE_FAILED_TERMINAL,
    )


def worker_timeout_lifecycle_state(status, retry_available):
    return worker_lifecycle_state_for_dimensions(
        WorkerLifecycleDimensions.timeout(status, retry_available=retry_available),
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
    payload = _require_transition_payload(transition, _FailedTransition)
    return worker_failed_lifecycle_state(retryable=payload.retryable, retry_available=payload.retry_available)


def _timed_out_transition_lifecycle_state(transition):
    payload = _require_transition_payload(transition, _TimedOutTransition)
    return worker_timeout_lifecycle_state(payload.status, payload.retry_available)


def _result_applied_transition_lifecycle_state(transition):
    payload = _require_transition_payload(transition, _ResultAppliedTransition)
    return worker_result_lifecycle_state(payload.result["status"])


def _snapshot_transition_is_legal(latest_worker, transition):
    source_state = worker_lifecycle_state(latest_worker)
    target_state = _snapshot_transition_lifecycle_state(transition)
    if target_state is None:
        return True
    return target_state in _WORKER_SNAPSHOT_TARGET_STATES_BY_SOURCE.get(source_state, frozenset())


def _snapshot_transition_lifecycle_state(transition):
    payload = _require_transition_payload(transition, _SnapshotAppliedTransition)
    if "lifecycle_state" not in payload.state_fields or "lifecycle_state" not in payload.worker:
        return None
    lifecycle_state = payload.worker.get("lifecycle_state")
    if lifecycle_state in WORKER_LIFECYCLE_STATES:
        return lifecycle_state
    return WORKER_LIFECYCLE_QUEUED


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


WorkerTransitionPayload = Union[
    _ProvisionedTransition,
    _ActiveTransition,
    _AttemptStartedTransition,
    _FailedTransition,
    _DependencyBlockedTransition,
    _AbortedTransition,
    _RetryScheduledTransition,
    _TimedOutTransition,
    _ResultAppliedTransition,
    _CleanupUpdatedTransition,
    _SnapshotAppliedTransition,
]


def _provisioned_transition_payload(worker):
    return _ProvisionedTransition(
        session_id=deepcopy(worker_field(worker, "session_id")),
        agent=_copy_present(worker_field(worker, "agent")),
        model=_copy_present(worker_field(worker, "model")),
    )


def _active_transition_payload(*, timeout_started_at=UNSET_TRANSITION_FIELD, clear_prompt_ids=False):
    return _ActiveTransition(
        timeout_started_at=_copy_transition_value(timeout_started_at),
        clear_prompt_ids=clear_prompt_ids,
    )


def _attempt_started_transition_payload(attempt):
    return _AttemptStartedTransition(deepcopy(attempt or {}))


def _failed_transition_payload(
    category,
    reason,
    *,
    retryable=True,
    retry_available=False,
    timeout_started_at=UNSET_TRANSITION_FIELD,
    prompt_ids=(),
):
    return _FailedTransition(
        category,
        reason,
        retryable=retryable,
        retry_available=retry_available,
        timeout_started_at=_copy_transition_value(timeout_started_at),
        prompt_ids=_filtered_prompt_ids(prompt_ids),
    )


def _dependency_blocked_transition_payload(blockers):
    return _DependencyBlockedTransition(tuple(blockers))


def _aborted_transition_payload(abort):
    return _AbortedTransition(deepcopy(abort))


def _retry_scheduled_transition_payload(
    category,
    reason,
    *,
    retry_count,
    timeout_started_at=UNSET_TRANSITION_FIELD,
    prompt_ids=(),
):
    return _RetryScheduledTransition(
        category,
        reason,
        retry_count=retry_count,
        timeout_started_at=_copy_transition_value(timeout_started_at),
        prompt_ids=_filtered_prompt_ids(prompt_ids),
    )


def _timed_out_transition_payload(
    reason,
    *,
    status,
    timed_out_at,
    retry_available=False,
    manual_retry_required=False,
    timeout_started_at=UNSET_TRANSITION_FIELD,
):
    return _TimedOutTransition(
        reason,
        status=status,
        timed_out_at=deepcopy(timed_out_at),
        retry_available=retry_available,
        manual_retry_required=manual_retry_required,
        timeout_started_at=_copy_transition_value(timeout_started_at),
    )


def _result_applied_transition_payload(result, *, prompt_ids=(), timeout_started_at=UNSET_TRANSITION_FIELD):
    return _ResultAppliedTransition(
        deepcopy(result or {}),
        prompt_ids=_filtered_prompt_ids(prompt_ids),
        timeout_started_at=_copy_transition_value(timeout_started_at),
    )


def _cleanup_updated_transition_payload(worker):
    return _CleanupUpdatedTransition(deepcopy(worker_field(worker, "cleanup")))


def _snapshot_applied_transition_payload(worker):
    return _SnapshotAppliedTransition(
        deepcopy(_worker_fields(worker)),
        state_fields=tuple(WORKER_SNAPSHOT_STATE_FIELDS),
        set_if_missing_fields=("session_id",),
        removable_fields=tuple(REMOVABLE_WORKER_TRANSITION_FIELDS),
    )


def _snapshot_worker_id(worker):
    if isinstance(worker, WorkerRecord):
        return worker.field("id")
    if isinstance(worker, Mapping):
        return worker.get("id")
    raise TypeError("snapshot worker must be WorkerRecord or persisted worker mapping")


def _require_transition_payload(transition, payload_type):
    payload = transition.payload
    if not isinstance(payload, payload_type):
        raise TypeError(f"worker transition '{transition.name.value}' has incompatible payload")
    return payload


def _transition_lifecycle_set_fields(transition):
    return worker_lifecycle_set_fields(
        transition.worker_id,
        worker_transition_target_lifecycle_state(transition),
    )


def _apply_provisioned_transition(reducer, transition):
    payload = _require_transition_payload(transition, _ProvisionedTransition)
    worker = reducer._copy_latest()
    if reducer._has_accepted_abort():
        return worker
    worker["id"] = transition.worker_id
    if payload.agent is not None:
        worker["agent"] = deepcopy(payload.agent)
    if payload.model is not None:
        worker["model"] = deepcopy(payload.model)
    if payload.session_id and not worker.get("session_id"):
        worker["session_id"] = deepcopy(payload.session_id)
    return worker


def _apply_active_transition(reducer, transition):
    payload = _require_transition_payload(transition, _ActiveTransition)
    worker = reducer._copy_latest()
    if reducer._has_accepted_abort():
        return worker
    _clear_current_status_fields(worker)
    worker.update(_transition_lifecycle_set_fields(transition))
    _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
    if payload.clear_prompt_ids:
        worker["prompt_ids"] = []
    return worker


def _apply_attempt_started_transition(reducer, transition):
    payload = _require_transition_payload(transition, _AttemptStartedTransition)
    worker = reducer._copy_latest()
    _append_attempt(worker, payload.attempt)
    return worker


def _apply_failed_transition(reducer, transition):
    payload = _require_transition_payload(transition, _FailedTransition)
    worker = reducer._copy_latest()
    if reducer._has_accepted_abort():
        reducer._merge_prompt_ids(worker, payload.prompt_ids)
        return worker
    worker.update(_transition_lifecycle_set_fields(transition))
    worker.update(
        {
            "error": payload.reason,
            "failure_category": payload.category,
            "failure_reason": payload.reason,
            "last_failure_category": payload.category,
            "last_failure_reason": payload.reason,
        }
    )
    _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
    worker.pop("manual_retry_required", None)
    if payload.retryable:
        worker.pop("failure_retryable", None)
    else:
        worker["failure_retryable"] = False
    reducer._merge_prompt_ids(worker, payload.prompt_ids)
    return worker


def _apply_dependency_blocked_transition(reducer, transition):
    payload = _require_transition_payload(transition, _DependencyBlockedTransition)
    worker = reducer._copy_latest()
    if reducer._has_accepted_abort():
        return worker
    worker.update(_transition_lifecycle_set_fields(transition))
    worker["blockers"] = list(payload.blockers)
    return worker


def _apply_aborted_transition(reducer, transition):
    payload = _require_transition_payload(transition, _AbortedTransition)
    worker = reducer._copy_latest()
    if reducer._has_accepted_abort() and not _abort_is_accepted(payload.abort):
        return worker
    worker["id"] = transition.worker_id
    worker["abort"] = deepcopy(payload.abort)
    if _abort_is_accepted(payload.abort):
        worker.update(_transition_lifecycle_set_fields(transition))
    return worker


def _apply_retry_scheduled_transition(reducer, transition):
    payload = _require_transition_payload(transition, _RetryScheduledTransition)
    worker = reducer._copy_latest()
    if reducer._has_accepted_abort():
        reducer._merge_prompt_ids(worker, payload.prompt_ids)
        return worker
    _clear_current_status_fields(worker)
    worker.update(_transition_lifecycle_set_fields(transition))
    worker.update(
        {
            "retry_count": payload.retry_count,
            "last_failure_category": payload.category,
            "last_failure_reason": payload.reason,
        }
    )
    _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
    reducer._merge_prompt_ids(worker, payload.prompt_ids)
    return worker


def _apply_timed_out_transition(reducer, transition):
    payload = _require_transition_payload(transition, _TimedOutTransition)
    worker = reducer._copy_latest()
    if reducer._has_accepted_abort():
        return worker
    worker.update(_transition_lifecycle_set_fields(transition))
    worker.update(
        {
            "error": payload.reason,
            "failure_category": WORKER_STATUS_TIMEOUT,
            "failure_reason": payload.reason,
            "last_failure_category": WORKER_STATUS_TIMEOUT,
            "last_failure_reason": payload.reason,
            "timed_out_at": payload.timed_out_at,
            "output_refs": [],
        }
    )
    if payload.status == WORKER_STATUS_BLOCKED:
        worker["blockers"] = [WORKER_STATUS_TIMEOUT]
    _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
    if payload.manual_retry_required:
        worker["manual_retry_required"] = True
    else:
        worker.pop("manual_retry_required", None)
    return worker


def _apply_result_applied_transition(reducer, transition):
    payload = _require_transition_payload(transition, _ResultAppliedTransition)
    worker = reducer._copy_latest()
    if reducer._has_accepted_abort():
        reducer._merge_prompt_ids(worker, payload.prompt_ids)
        return worker
    status = short_status(payload.result["status"])
    worker.update(_transition_lifecycle_set_fields(transition))
    worker["result"] = deepcopy(payload.result)
    _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
    if status == WORKER_STATUS_DONE:
        _clear_current_status_fields(worker)
        assistant_message_id = payload.result["message_ids"].get("assistant")
        worker["output_refs"] = [f"assistant:{assistant_message_id}"] if assistant_message_id else []
    else:
        worker["failure_category"] = None
        worker["failure_reason"] = None
    reducer._merge_prompt_ids(worker, payload.prompt_ids)
    return worker


def _apply_cleanup_updated_transition(reducer, transition):
    payload = _require_transition_payload(transition, _CleanupUpdatedTransition)
    worker = reducer._copy_latest()
    worker["id"] = transition.worker_id
    worker["cleanup"] = deepcopy(payload.cleanup)
    return worker


def _apply_snapshot_applied_transition(reducer, transition):
    payload = _require_transition_payload(transition, _SnapshotAppliedTransition)
    if reducer._has_accepted_abort() and not _accepted_abort(_snapshot_transition_fields(transition)):
        worker = reducer._copy_latest()
        if "cleanup" in payload.state_fields and "cleanup" in payload.worker:
            worker["cleanup"] = deepcopy(payload.worker.get("cleanup"))
        prompt_ids = payload.worker.get("prompt_ids")
        if isinstance(prompt_ids, list):
            reducer._merge_prompt_ids(worker, tuple(prompt_ids), merge_empty=True)
        return worker
    worker = reducer._copy_latest()
    worker["id"] = transition.worker_id
    for field_name in payload.state_fields:
        if field_name in payload.worker:
            worker[field_name] = deepcopy(payload.worker[field_name])
    for field_name in payload.removable_fields:
        if field_name not in payload.worker:
            worker.pop(field_name, None)
    prompt_ids = payload.worker.get("prompt_ids")
    if isinstance(prompt_ids, list):
        reducer._merge_prompt_ids(worker, tuple(prompt_ids), merge_empty=True)
    for field_name in payload.set_if_missing_fields:
        if payload.worker.get(field_name) and not worker.get(field_name):
            worker[field_name] = deepcopy(payload.worker[field_name])
    return worker


WORKER_TRANSITION_METADATA = {
    metadata.name: metadata
    for metadata in (
        _transition_metadata(
            WorkerTransitionName.PROVISIONED,
            source_states=frozenset({WORKER_LIFECYCLE_QUEUED, WORKER_LIFECYCLE_ACTIVE_WAIT})
            | WORKER_RETRYABLE_LIFECYCLE_STATES,
        ),
        _transition_metadata(
            WorkerTransitionName.ACTIVE,
            source_states=frozenset({WORKER_LIFECYCLE_QUEUED, WORKER_LIFECYCLE_ACTIVE_WAIT})
            | WORKER_RETRYABLE_LIFECYCLE_STATES
            | WORKER_BLOCKED_LIFECYCLE_STATES,
            target_states=(WORKER_LIFECYCLE_ACTIVE_WAIT,),
        ),
        _transition_metadata(
            WorkerTransitionName.ATTEMPT_STARTED,
            source_states=(WORKER_LIFECYCLE_ACTIVE_WAIT,),
        ),
        _transition_metadata(
            WorkerTransitionName.FAILED,
            source_states=frozenset({WORKER_LIFECYCLE_QUEUED, WORKER_LIFECYCLE_ACTIVE_WAIT})
            | WORKER_RETRYABLE_LIFECYCLE_STATES,
            target_states=WORKER_FAILED_TARGET_LIFECYCLE_STATES,
        ),
        _transition_metadata(
            WorkerTransitionName.DEPENDENCY_BLOCKED,
            source_states=(
                WORKER_LIFECYCLE_QUEUED,
                WORKER_LIFECYCLE_ACTIVE_WAIT,
                WORKER_LIFECYCLE_ACTIVE_RETRY,
            ),
            target_states=(WORKER_LIFECYCLE_BLOCKED_DEPENDENCY,),
        ),
        _transition_metadata(
            WorkerTransitionName.ABORTED,
            source_states=frozenset({WORKER_LIFECYCLE_QUEUED, WORKER_LIFECYCLE_ACTIVE_WAIT})
            | WORKER_RETRYABLE_LIFECYCLE_STATES
            | WORKER_BLOCKED_LIFECYCLE_STATES
            | WORKER_ABORTED_LIFECYCLE_STATES,
            target_states=(WORKER_LIFECYCLE_ABORTED,),
        ),
        _transition_metadata(
            WorkerTransitionName.RETRY_SCHEDULED,
            source_states=WORKER_RETRY_SCHEDULE_SOURCE_LIFECYCLE_STATES,
            target_states=(WORKER_LIFECYCLE_ACTIVE_RETRY,),
        ),
        _transition_metadata(
            WorkerTransitionName.TIMED_OUT,
            source_states=(WORKER_LIFECYCLE_ACTIVE_WAIT,),
            target_states=WORKER_TIMEOUT_ORIGIN_LIFECYCLE_STATES,
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
        ),
        _transition_metadata(
            WorkerTransitionName.CLEANUP_UPDATED,
            source_states=WORKER_LIFECYCLE_STATES,
            public_lifecycle_transition=False,
        ),
        _transition_metadata(
            WorkerTransitionName.SNAPSHOT_APPLIED,
            source_states=WORKER_LIFECYCLE_STATES,
            public_lifecycle_transition=False,
        ),
    )
}

WORKER_TRANSITION_DEFINITIONS = WORKER_TRANSITION_METADATA


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
    if not isinstance(name, WorkerTransitionName):
        raise ValueError(f"unknown worker transition: {name}")
    metadata = WORKER_TRANSITION_METADATA.get(name)
    if metadata is None:
        raise ValueError(f"unknown worker transition: {name}")
    return metadata


def worker_transition_target_lifecycle_state(transition):
    _worker_transition_metadata(transition.name)
    if transition.name is WorkerTransitionName.ACTIVE:
        return worker_lifecycle_state_for_status_alias(WORKER_STATUS_ACTIVE)
    if transition.name is WorkerTransitionName.FAILED:
        return _failed_transition_lifecycle_state(transition)
    if transition.name is WorkerTransitionName.DEPENDENCY_BLOCKED:
        return worker_lifecycle_state_for_status_alias(WORKER_STATUS_BLOCKED)
    if transition.name is WorkerTransitionName.ABORTED:
        return worker_lifecycle_state_for_status_alias(WORKER_STATUS_ABORTED)
    if transition.name is WorkerTransitionName.RETRY_SCHEDULED:
        return worker_lifecycle_state_for_public_state(WORKER_STATUS_ACTIVE, WORKER_ACTION_RETRY)
    if transition.name is WorkerTransitionName.TIMED_OUT:
        return _timed_out_transition_lifecycle_state(transition)
    if transition.name is WorkerTransitionName.RESULT_APPLIED:
        return _result_applied_transition_lifecycle_state(transition)
    return None


def worker_transition_is_legal(latest_worker, transition):
    if transition.name is WorkerTransitionName.SNAPSHOT_APPLIED:
        return _snapshot_transition_is_legal(latest_worker, transition)
    return worker_lifecycle_state(latest_worker) in _worker_transition_metadata(transition.name).source_states


def apply_worker_transition_payload(reducer, transition):
    _worker_transition_metadata(transition.name)
    if transition.name is WorkerTransitionName.PROVISIONED:
        return _apply_provisioned_transition(reducer, transition)
    if transition.name is WorkerTransitionName.ACTIVE:
        return _apply_active_transition(reducer, transition)
    if transition.name is WorkerTransitionName.ATTEMPT_STARTED:
        return _apply_attempt_started_transition(reducer, transition)
    if transition.name is WorkerTransitionName.FAILED:
        return _apply_failed_transition(reducer, transition)
    if transition.name is WorkerTransitionName.DEPENDENCY_BLOCKED:
        return _apply_dependency_blocked_transition(reducer, transition)
    if transition.name is WorkerTransitionName.ABORTED:
        return _apply_aborted_transition(reducer, transition)
    if transition.name is WorkerTransitionName.RETRY_SCHEDULED:
        return _apply_retry_scheduled_transition(reducer, transition)
    if transition.name is WorkerTransitionName.TIMED_OUT:
        return _apply_timed_out_transition(reducer, transition)
    if transition.name is WorkerTransitionName.RESULT_APPLIED:
        return _apply_result_applied_transition(reducer, transition)
    if transition.name is WorkerTransitionName.CLEANUP_UPDATED:
        return _apply_cleanup_updated_transition(reducer, transition)
    if transition.name is WorkerTransitionName.SNAPSHOT_APPLIED:
        return _apply_snapshot_applied_transition(reducer, transition)
    raise ValueError(f"unknown worker transition: {transition.name}")


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
    return isinstance(worker, WorkerRecord)


def _require_worker_record(worker):
    if isinstance(worker, WorkerRecord):
        return worker
    raise TypeError("internal worker must be WorkerRecord; hydrate raw mappings at the storage boundary")


def _worker_fields(worker):
    if isinstance(worker, WorkerRecord):
        return worker.to_snapshot()
    if isinstance(worker, Mapping):
        return dict(worker)
    return {}


def _raw_worker_field(worker, field_name, default=None):
    if isinstance(worker, WorkerRecord):
        return worker.field(field_name, default)
    if isinstance(worker, Mapping):
        return worker.get(field_name, default)
    return default


def worker_field(worker, field_name, default=None):
    return _require_worker_record(worker).field(field_name, default)


def worker_has_field(worker, field_name):
    return _require_worker_record(worker).has_field(field_name)


def is_worker_mapping(worker):
    return _is_worker_mapping(worker)


def worker_retry_available(worker, category=None):
    worker = _require_worker_record(worker)
    if worker_field(worker, "failure_retryable") is False:
        return False
    retryable = set(worker_field(worker, "retryable_failures") or [])
    if not retryable:
        return False
    if category is None:
        category = worker_field(worker, "failure_category") or worker_field(worker, "last_failure_category")
    if category and category not in retryable and "all" not in retryable:
        return False
    try:
        retry_count = int(worker_field(worker, "retry_count") or 0)
        retry_limit = int(worker_field(worker, "retry_limit") or 0)
    except (TypeError, ValueError):
        return False
    return retry_count < retry_limit


def _legacy_worker_retry_available(worker, category=None):
    if not isinstance(worker, Mapping):
        return False
    if _raw_worker_field(worker, "failure_retryable") is False:
        return False
    retryable = set(_raw_worker_field(worker, "retryable_failures") or [])
    if not retryable:
        return False
    if category is None:
        category = _raw_worker_field(worker, "failure_category") or _raw_worker_field(worker, "last_failure_category")
    if category and category not in retryable and "all" not in retryable:
        return False
    try:
        retry_count = int(_raw_worker_field(worker, "retry_count") or 0)
        retry_limit = int(_raw_worker_field(worker, "retry_limit") or 0)
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
    has_prompt: bool

    @classmethod
    def from_worker(cls, worker):
        worker = _require_worker_record(worker)
        return cls(worker_lifecycle_state(worker), worker_has_prompt(worker))

    def can_execute(self):
        metadata = WORKER_LIFECYCLE_METADATA.get(self.lifecycle_state)
        return self.has_prompt and metadata is not None and metadata.executable

    def can_block_for_dependency(self):
        metadata = WORKER_LIFECYCLE_METADATA.get(self.lifecycle_state)
        return self.has_prompt and metadata is not None and is_dependency_blockable_status(metadata.status)


def worker_lifecycle_state(worker):
    worker = _require_worker_record(worker)
    return _canonical_lifecycle_state(worker)


def _canonical_lifecycle_state(worker):
    if isinstance(worker, WorkerRecord):
        return worker.lifecycle_state
    lifecycle_state = _raw_worker_field(worker, "lifecycle_state") if isinstance(worker, Mapping) else None
    if lifecycle_state in WORKER_LIFECYCLE_STATES:
        return lifecycle_state
    return _lifecycle_state_from_legacy_public_worker_state(worker)


def _lifecycle_state_from_legacy_public_worker_state(worker):
    """Compatibility boundary for legacy/public records that do not carry lifecycle_state."""
    worker = worker if isinstance(worker, Mapping) else {}
    status = short_status(_raw_worker_field(worker, "status"))
    if status == WORKER_STATUS_QUEUED:
        return worker_lifecycle_state_for_status_alias(status)
    if status == WORKER_STATUS_ACTIVE:
        if _raw_worker_field(worker, "next_eligible_action") == WORKER_ACTION_RETRY:
            return worker_lifecycle_state_for_public_state(status, WORKER_ACTION_RETRY)
        return worker_lifecycle_state_for_status_alias(status)
    if is_blocked_status(status):
        timeout_origin = _raw_worker_field(
            worker,
            "failure_category",
        ) == WORKER_STATUS_TIMEOUT or WORKER_STATUS_TIMEOUT in set(
            _raw_worker_field(worker, "blockers") or []
        )
        return worker_lifecycle_state_for_public_state(
            status,
            WORKER_ACTION_RESOLVE_BLOCKER,
            timeout_origin=timeout_origin,
        )
    if status == WORKER_STATUS_DONE:
        return worker_lifecycle_state_for_status_alias(status)
    if status == WORKER_STATUS_FAILED:
        if _raw_worker_field(worker, "failure_category") == WORKER_STATUS_TIMEOUT:
            return worker_lifecycle_state_for_public_state(
                status,
                WORKER_ACTION_RETRY if _legacy_worker_retry_available(worker, WORKER_STATUS_TIMEOUT) else WORKER_ACTION_NONE,
                timeout_origin=True,
            )
        return worker_lifecycle_state_for_public_state(
            status,
            WORKER_ACTION_RETRY if _legacy_worker_retry_available(worker) else WORKER_ACTION_NONE,
        )
    if status == WORKER_STATUS_TIMEOUT:
        return worker_lifecycle_state_for_public_state(
            status,
            WORKER_ACTION_RETRY if _legacy_worker_retry_available(worker, WORKER_STATUS_TIMEOUT) else WORKER_ACTION_NONE,
            timeout_origin=True,
        )
    if status == WORKER_STATUS_ABORTED:
        if _raw_worker_field(worker, "failure_category") == WORKER_STATUS_TIMEOUT:
            return worker_lifecycle_state_for_public_state(
                status,
                WORKER_ACTION_NONE,
                timeout_origin=True,
            )
        return worker_lifecycle_state_for_status_alias(status)
    return WORKER_LIFECYCLE_QUEUED


def canonicalize_legacy_worker_record(worker):
    fields = _worker_fields(worker)
    if fields.get("lifecycle_state") not in WORKER_LIFECYCLE_STATES:
        fields["lifecycle_state"] = _lifecycle_state_from_legacy_public_worker_state(fields)
    for public_field_name in PUBLIC_WORKER_STATE_FIELD_NAMES:
        fields.pop(public_field_name, None)
    return fields


def latest_prompt_ids_are_retry_marker(latest_worker):
    return (
        worker_lifecycle_state(latest_worker) == WORKER_LIFECYCLE_ACTIVE_RETRY
        and worker_field(latest_worker, "last_failure_category") is not None
    )


def next_eligible_worker_action(worker):
    return public_worker_state(worker_lifecycle_state(worker))[1]


def worker_has_prompt(worker):
    prompt = worker_field(worker, "prompt")
    return prompt is not None and bool(str(prompt))


def is_executable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_execute()


def is_dependency_blockable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_block_for_dependency()


class WorkerRecord:
    """Hydrated worker domain object with explicit serialization boundaries."""

    __iter__ = None

    def __init__(self, worker_id, fields=None):
        self._fields = deepcopy(dict(fields or {}))
        self._worker_id = self._fields.get("id") or worker_id

    def __repr__(self):
        return f"{type(self).__name__}({self.worker_id!r}, {self.to_snapshot()!r})"

    def __eq__(self, other):
        if isinstance(other, WorkerRecord):
            return self.to_snapshot() == other.to_snapshot()
        return NotImplemented

    def _raw_field(self, field_name, default=None):
        return self._fields.get(field_name, default)

    def field(self, field_name, default=None):
        if field_name == "id":
            return self.worker_id
        if field_name == "lifecycle_state":
            return self.lifecycle_state
        return self._raw_field(field_name, default)

    def has_field(self, field_name):
        return field_name in self._fields or field_name in {"id", "lifecycle_state"}

    def set_field(self, field_name, value):
        self._fields[field_name] = deepcopy(value)
        if field_name == "id":
            self._worker_id = self._fields.get("id") or self._worker_id
        return self

    def remove_field(self, field_name):
        self._fields.pop(field_name, None)
        return self

    def merge_fields(self, fields=None, **kwargs):
        if fields is not None:
            self._fields.update(deepcopy(_worker_fields(fields)))
        if kwargs:
            self._fields.update(deepcopy(kwargs))
        self._worker_id = self._fields.get("id") or self._worker_id
        return self

    def replace_fields(self, fields):
        self._fields = deepcopy(_worker_fields(fields))
        self._worker_id = self._fields.get("id") or self._worker_id
        return self

    @classmethod
    def from_worker(cls, worker, worker_id=None):
        fields = _worker_fields(worker)
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
        return cls.from_worker(require_internal_worker(fields), worker_id)

    @classmethod
    def lifecycle_set_fields(cls, worker_id, lifecycle_state):
        return worker_lifecycle_set_fields(worker_id, lifecycle_state)

    @property
    def worker_id(self):
        return self._fields.get("id") or self._worker_id

    @property
    def lifecycle_state(self):
        lifecycle_state = self._raw_field("lifecycle_state")
        if lifecycle_state in WORKER_LIFECYCLE_STATES:
            return lifecycle_state
        return WORKER_LIFECYCLE_QUEUED

    @property
    def has_prompt(self):
        return worker_has_prompt(self)

    def scheduling_state(self):
        return WorkerSchedulingState(
            self.lifecycle_state,
            self.has_prompt,
        )

    def to_public_dict(self):
        return self.to_snapshot()

    def to_snapshot(self):
        normalized = self.default_snapshot_fields(self.worker_id)
        fields = deepcopy(self._fields)
        fields["id"] = fields.get("id") or self.worker_id
        fields["lifecycle_state"] = self.lifecycle_state
        for public_field_name in PUBLIC_WORKER_STATE_FIELD_NAMES:
            fields.pop(public_field_name, None)
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
        return type(self).from_worker(require_internal_worker(normalized), self.worker_id)

    def set_session(self, session_id, *, agent=None, model=None):
        self.set_field("session_id", session_id)
        if agent is not None:
            self.set_field("agent", agent)
        if model is not None:
            self.set_field("model", model)
        return self

    def remember_prompt_id(self, prompt_id):
        prompt_ids = self.field("prompt_ids")
        if not isinstance(prompt_ids, list):
            prompt_ids = []
        if prompt_id not in prompt_ids:
            prompt_ids.append(prompt_id)
        self.set_field("prompt_ids", prompt_ids)
        return self

    def apply_transition(self, transition):
        result = _apply_worker_transition_to_record(self, transition)
        if result.skipped and not result.stale_snapshot_recovery:
            raise WorkerTransitionError(result)
        merged = result.worker
        self.replace_fields(merged)
        self._worker_id = self.field("id") or self._worker_id or transition.worker_id
        return self

    def ensure_cleanup(self):
        cleanup = self.field("cleanup")
        if not isinstance(cleanup, dict):
            cleanup = {"requested": True, "deleted": False}
            self.set_field("cleanup", cleanup)
            cleanup = self.field("cleanup")
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
    if worker is None and worker_id is not None:
        return default_worker_record(worker_id)
    raise TypeError("internal worker mutation requires WorkerRecord")


def require_internal_worker(worker):
    missing = [field_name for field_name in WORKER_REQUIRED_FIELD_NAMES if field_name not in worker]
    if missing:
        raise ValueError(f"internal worker missing required fields: {', '.join(missing)}")
    return worker


@dataclass(frozen=True)
class WorkerTransition:
    """Named lifecycle transition applied by WorkerLifecycleReducer."""

    worker_id: str
    name: WorkerTransitionName
    payload: Optional[WorkerTransitionPayload] = None
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
        worker_id = worker_field(worker, "id")
        return cls(worker_id, WorkerTransitionName.PROVISIONED, _provisioned_transition_payload(worker))

    @classmethod
    def active(cls, worker_id, *, timeout_started_at=UNSET_TRANSITION_FIELD, clear_prompt_ids=False):
        return cls(
            worker_id,
            WorkerTransitionName.ACTIVE,
            _active_transition_payload(
                timeout_started_at=timeout_started_at,
                clear_prompt_ids=clear_prompt_ids,
            ),
        )

    @classmethod
    def attempt_started(cls, worker_id, attempt):
        return cls(
            worker_id,
            WorkerTransitionName.ATTEMPT_STARTED,
            _attempt_started_transition_payload(attempt),
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
            _failed_transition_payload(
                category,
                reason,
                retryable=retryable,
                retry_available=retry_available,
                timeout_started_at=timeout_started_at,
                prompt_ids=prompt_ids,
            ),
        )

    @classmethod
    def dependency_blocked(cls, worker_id, blockers):
        return cls(
            worker_id,
            WorkerTransitionName.DEPENDENCY_BLOCKED,
            _dependency_blocked_transition_payload(blockers),
        )

    @classmethod
    def aborted(cls, worker_id, abort):
        return cls(worker_id, WorkerTransitionName.ABORTED, _aborted_transition_payload(abort))

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
            _retry_scheduled_transition_payload(
                category,
                reason,
                retry_count=retry_count,
                timeout_started_at=timeout_started_at,
                prompt_ids=prompt_ids,
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
            _timed_out_transition_payload(
                reason,
                status=status,
                timed_out_at=timed_out_at,
                retry_available=retry_available,
                manual_retry_required=manual_retry_required,
                timeout_started_at=timeout_started_at,
            ),
        )

    @classmethod
    def result_applied(cls, worker_id, result, *, prompt_ids=(), timeout_started_at=UNSET_TRANSITION_FIELD):
        return cls(
            worker_id,
            WorkerTransitionName.RESULT_APPLIED,
            _result_applied_transition_payload(
                result,
                prompt_ids=prompt_ids,
                timeout_started_at=timeout_started_at,
            ),
        )

    @classmethod
    def cleanup_updated(cls, worker):
        worker_id = worker_field(worker, "id")
        return cls(worker_id, WorkerTransitionName.CLEANUP_UPDATED, _cleanup_updated_transition_payload(worker))

    @classmethod
    def snapshot_applied(cls, worker):
        worker_id = _snapshot_worker_id(worker)
        return cls(worker_id, WorkerTransitionName.SNAPSHOT_APPLIED, _snapshot_applied_transition_payload(worker))


def _copy_present(value):
    return None if value is None else deepcopy(value)


def _copy_transition_value(value):
    if value is UNSET_TRANSITION_FIELD:
        return value
    return deepcopy(value)


def _filtered_prompt_ids(prompt_ids):
    return tuple(prompt_id for prompt_id in prompt_ids if prompt_id is not None)


def _clear_current_status_fields(worker):
    worker["blockers"] = []
    worker["failure_category"] = None
    worker["failure_reason"] = None
    for field_name in REMOVABLE_WORKER_TRANSITION_FIELDS:
        worker.pop(field_name, None)


def _set_if_not_unset(fields, name, value):
    if value is not UNSET_TRANSITION_FIELD:
        fields[name] = deepcopy(value)


def _snapshot_transition_fields(transition):
    payload = transition.payload
    fields = {"id": transition.worker_id}
    for field_name in payload.state_fields:
        if field_name in payload.worker:
            fields[field_name] = deepcopy(payload.worker[field_name])
    return fields


def _accepted_abort(worker):
    if isinstance(worker, WorkerRecord):
        abort = worker.field("abort")
        lifecycle_state = worker_lifecycle_state(worker)
    elif isinstance(worker, Mapping):
        abort = worker.get("abort")
        lifecycle_state = _canonical_lifecycle_state(worker)
    else:
        abort = None
        lifecycle_state = None
    status = public_worker_state(lifecycle_state)[0]
    return isinstance(abort, dict) and abort.get("accepted") and status == WORKER_STATUS_ABORTED


def _abort_is_accepted(abort):
    return isinstance(abort, dict) and abort.get("accepted")


def default_worker(worker_id):
    return deserialize_worker_record({}, worker_id)


def normalize_worker(worker, worker_id):
    return deserialize_worker_record(worker, worker_id)


def normalize_worker_snapshot(worker, worker_id):
    return serialize_worker_snapshot(canonicalize_legacy_worker_record(worker), worker_id)


def _apply_worker_transition_to_record(worker, transition):
    from opencode_session.worker_lifecycle_reducer import apply_worker_transition_to_record

    record = worker_record_for_mutation(worker, transition.worker_id)
    return apply_worker_transition_to_record(record, transition)


def apply_worker_transition_to_worker(worker, transition):
    record = worker_record_for_mutation(worker, transition.worker_id)
    record.apply_transition(transition)
    return record


def apply_worker_transition(latest_workers, transition):
    latest_worker = latest_workers.get(transition.worker_id)
    record = worker_record_for_mutation(latest_worker, transition.worker_id)
    record.apply_transition(transition)
    latest_workers[transition.worker_id] = record
    return record


def next_eligible_action(worker):
    return next_eligible_worker_action(worker)


def ensure_worker(run, worker_id, *, role):
    workers = run.setdefault("workers", {})
    worker = normalize_worker(workers.get(worker_id), worker_id)
    if not worker.field("role"):
        worker.set_field("role", role)
    worker.set_field("id", worker_id)
    workers[worker_id] = worker
    return worker


def mark_worker_active(worker, *, now=None):
    timeout_started_at = UNSET_TRANSITION_FIELD
    if now is not None:
        timeout_started_at = now() if worker_field(worker, "timeout_seconds") else None
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
        retry_count=int(worker_field(worker, "retry_count") or 0) + 1,
        timeout_started_at=_existing_or_unset(worker, "timeout_started_at"),
        prompt_ids=prompt_ids,
    )
    return transition


def worker_timeout_reason(worker):
    return f"worker timed out after {format_timeout(worker_field(worker, 'timeout_seconds'))}s"


def mark_worker_timeout(worker, reason, now, *, manual_retry_required=False):
    status = worker_field(worker, "timeout_policy") or WORKER_STATUS_TIMEOUT
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
    return worker_field(worker, "id")


def _existing_or_unset(worker, field_name):
    return worker_field(worker, field_name) if worker_has_field(worker, field_name) else UNSET_TRANSITION_FIELD


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
        worker_id = worker_field(worker, "id")
        if _worker_status(worker) != WORKER_STATUS_DONE:
            continue
        for output_ref in worker_field(worker, "output_refs", []):
            if isinstance(output_ref, str) and output_ref.startswith("assistant:"):
                ordered.append(f"{worker_id}:{output_ref.split(':', 1)[1]}")
            else:
                ordered.append(f"{worker_id}:{output_ref}")
    return ordered


def workers_in_dependency_order(workers):
    from opencode_session.worker_dependencies import analyze_worker_dependencies

    analysis = analyze_worker_dependencies(workers)
    return [workers[worker_id] for worker_id in analysis.worker_ids_in_dependency_order]


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
    prompt = worker_field(worker, "prompt")
    if prompt is None:
        return None
    return str(prompt)


def _worker_status(worker):
    return public_worker_state(worker_lifecycle_state(worker))[0] if isinstance(worker, WorkerRecord) else None
