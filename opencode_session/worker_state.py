from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Callable, FrozenSet, Optional, Type, Union

from opencode_session.schema_helpers import JsonValue
from opencode_session.status import short_status
from opencode_session.worker_field_spec import (
    REMOVABLE_WORKER_TRANSITION_FIELDS,
    WORKER_FIELD_SPEC_BY_NAME,
    WORKER_FIELD_SPECS,
    WORKER_FIELD_VALIDATOR_NAMES,
    WORKER_FIELD_TIMEOUT_POLICY_STATUSES,
    WORKER_LIST_FIELDS,
    WORKER_OPTIONAL_LIST_FIELDS,
    WORKER_RECORD_CANONICAL_FIELD_NAMES,
    WORKER_RECORD_FIELD_NAMES,
    WORKER_RECORD_OPTIONAL_FIELD_NAMES,
    WORKER_RECORD_UPDATE_FIELD_NAMES,
    WORKER_REQUIRED_FIELD_NAMES,
    WORKER_RUN_UPSERT_FIELD_NAMES,
    WORKER_SNAPSHOT_ACCEPTED_ABORT_PASSTHROUGH_FIELD_NAMES,
    WORKER_SNAPSHOT_PROMPT_ID_FIELD_NAMES,
    WORKER_SNAPSHOT_REMOVE_WHEN_ABSENT_FIELD_NAMES,
    WORKER_SNAPSHOT_REPLAY_FIELD_NAMES,
    WORKER_SNAPSHOT_SET_IF_MISSING_FIELD_NAMES,
    WORKER_STORAGE_INT_FIELD_NAMES,
    WORKER_STORAGE_LIST_FIELD_NAMES,
    WORKER_STORAGE_TIMEOUT_SECONDS_FIELD_NAMES,
    WORKER_STORAGE_TIMEOUT_POLICY_FIELD_NAMES,
    WorkerFieldSpec,
    WorkerFieldValidatorName,
    worker_default_snapshot_fields,
    worker_optional_schema_annotations,
    worker_required_schema_annotations,
    worker_snapshot_schema_annotations,
)

_WorkerTransitionPayloadType = Type["WorkerTransitionPayload"]
_WorkerTransitionPayloadFactory = Callable[..., "WorkerTransitionPayload"]
_WorkerTransitionReducer = Callable[
    ["WorkerRecord", "WorkerTransition", "WorkerTransitionPayload", Optional[str]],
    "WorkerRecord",
]
_WorkerTransitionTargetResolver = Callable[
    ["WorkerTransition", "WorkerTransitionPayload"],
    Optional[str],
]


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


class _WorkerLifecycleEnum(str, Enum):
    def __str__(self):
        return self.value


@dataclass(frozen=True)
class _WorkerLifecycleRow:
    state: str
    status: str
    action: str
    retryable: bool = False
    timeout_origin: bool = False
    status_alias: bool = False
    terminal_status: bool = False
    failed_dependency_status: bool = False
    executable: bool = False
    status_priority: int = 0

    def __post_init__(self):
        object.__setattr__(self, "state", str(self.state))
        object.__setattr__(self, "status", short_status(self.status))
        object.__setattr__(self, "action", str(self.action))
        object.__setattr__(self, "retryable", bool(self.retryable))
        object.__setattr__(self, "timeout_origin", bool(self.timeout_origin))
        object.__setattr__(self, "status_alias", bool(self.status_alias))
        object.__setattr__(self, "terminal_status", bool(self.terminal_status))
        object.__setattr__(self, "failed_dependency_status", bool(self.failed_dependency_status))
        object.__setattr__(self, "executable", bool(self.executable))
        if self.retryable and self.action != "retry":
            raise ValueError("retryable worker lifecycle rows must use the retry action")


_WORKER_LIFECYCLE_TABLE = (
    _WorkerLifecycleRow(
        "queued",
        "queued",
        "start",
        status_alias=True,
        executable=True,
        status_priority=0,
    ),
    _WorkerLifecycleRow(
        "active_wait",
        "active",
        "wait",
        status_alias=True,
        status_priority=1,
    ),
    _WorkerLifecycleRow(
        "active_retry",
        "active",
        "retry",
        retryable=True,
        executable=True,
        status_priority=1,
    ),
    _WorkerLifecycleRow(
        "blocked_dependency",
        "blocked",
        "resolve_blocker",
        status_alias=True,
        failed_dependency_status=True,
        status_priority=2,
    ),
    _WorkerLifecycleRow(
        "blocked_timeout",
        "blocked",
        "resolve_blocker",
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=2,
    ),
    _WorkerLifecycleRow(
        "done_collect",
        "done",
        "collect",
        status_alias=True,
        terminal_status=True,
        status_priority=3,
    ),
    _WorkerLifecycleRow(
        "failed_retry",
        "failed",
        "retry",
        retryable=True,
        terminal_status=True,
        failed_dependency_status=True,
        executable=True,
        status_priority=6,
    ),
    _WorkerLifecycleRow(
        "failed_terminal",
        "failed",
        "retry",
        status_alias=True,
        terminal_status=True,
        failed_dependency_status=True,
        status_priority=6,
    ),
    _WorkerLifecycleRow(
        "timeout_retry",
        "timeout",
        "retry",
        retryable=True,
        terminal_status=True,
        failed_dependency_status=True,
        executable=True,
        timeout_origin=True,
        status_priority=4,
    ),
    _WorkerLifecycleRow(
        "timeout_terminal",
        "timeout",
        "retry",
        status_alias=True,
        terminal_status=True,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=4,
    ),
    _WorkerLifecycleRow(
        "timeout_failed_retry",
        "failed",
        "retry",
        retryable=True,
        terminal_status=True,
        failed_dependency_status=True,
        executable=True,
        timeout_origin=True,
        status_priority=6,
    ),
    _WorkerLifecycleRow(
        "timeout_failed_terminal",
        "failed",
        "retry",
        terminal_status=True,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=6,
    ),
    _WorkerLifecycleRow(
        "timeout_aborted",
        "aborted",
        "none",
        terminal_status=True,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=5,
    ),
    _WorkerLifecycleRow(
        "aborted",
        "aborted",
        "none",
        status_alias=True,
        terminal_status=True,
        failed_dependency_status=True,
        status_priority=5,
    ),
)


def _lifecycle_table_values(field_name):
    values = []
    for row in _WORKER_LIFECYCLE_TABLE:
        value = getattr(row, field_name)
        if value not in values:
            values.append(value)
    return tuple(values)


def _lifecycle_table_value(field_name, value):
    for row in _WORKER_LIFECYCLE_TABLE:
        row_value = getattr(row, field_name)
        if row_value == value:
            return row_value
    raise ValueError(f"unknown worker lifecycle {field_name}: {value}")


def _enum_members(values):
    return {value.upper(): value for value in values}


def _require_worker_field_name(field_name: str) -> str:
    if not isinstance(field_name, str) or not field_name:
        raise TypeError("worker field name must be a non-empty string")
    return field_name


WORKER_STATUS_QUEUED = _lifecycle_table_value("status", "queued")
WORKER_STATUS_ACTIVE = _lifecycle_table_value("status", "active")
WORKER_STATUS_BLOCKED = _lifecycle_table_value("status", "blocked")
WORKER_STATUS_DONE = _lifecycle_table_value("status", "done")
WORKER_STATUS_FAILED = _lifecycle_table_value("status", "failed")
WORKER_STATUS_ABORTED = _lifecycle_table_value("status", "aborted")
WORKER_STATUS_TIMEOUT = _lifecycle_table_value("status", "timeout")

WORKER_ACTION_START = _lifecycle_table_value("action", "start")
WORKER_ACTION_WAIT = _lifecycle_table_value("action", "wait")
WORKER_ACTION_RETRY = _lifecycle_table_value("action", "retry")
WORKER_ACTION_RESOLVE_BLOCKER = _lifecycle_table_value("action", "resolve_blocker")
WORKER_ACTION_COLLECT = _lifecycle_table_value("action", "collect")
WORKER_ACTION_NONE = _lifecycle_table_value("action", "none")

WORKER_LIFECYCLE_QUEUED = _lifecycle_table_value("state", "queued")
WORKER_LIFECYCLE_ACTIVE_WAIT = _lifecycle_table_value("state", "active_wait")
WORKER_LIFECYCLE_ACTIVE_RETRY = _lifecycle_table_value("state", "active_retry")
WORKER_LIFECYCLE_BLOCKED_DEPENDENCY = _lifecycle_table_value("state", "blocked_dependency")
WORKER_LIFECYCLE_BLOCKED_TIMEOUT = _lifecycle_table_value("state", "blocked_timeout")
WORKER_LIFECYCLE_DONE_COLLECT = _lifecycle_table_value("state", "done_collect")
WORKER_LIFECYCLE_FAILED_RETRY = _lifecycle_table_value("state", "failed_retry")
WORKER_LIFECYCLE_FAILED_TERMINAL = _lifecycle_table_value("state", "failed_terminal")
WORKER_LIFECYCLE_TIMEOUT_RETRY = _lifecycle_table_value("state", "timeout_retry")
WORKER_LIFECYCLE_TIMEOUT_TERMINAL = _lifecycle_table_value("state", "timeout_terminal")
WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY = _lifecycle_table_value("state", "timeout_failed_retry")
WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL = _lifecycle_table_value("state", "timeout_failed_terminal")
WORKER_LIFECYCLE_TIMEOUT_ABORTED = _lifecycle_table_value("state", "timeout_aborted")
WORKER_LIFECYCLE_ABORTED = _lifecycle_table_value("state", "aborted")
WORKER_LIFECYCLE_STATE_VALUES = _lifecycle_table_values("state")

WorkerLifecycleStatus = _WorkerLifecycleEnum(
    "WorkerLifecycleStatus",
    _enum_members(_lifecycle_table_values("status")),
    module=__name__,
)
WorkerLifecycleAction = _WorkerLifecycleEnum(
    "WorkerLifecycleAction",
    _enum_members(_lifecycle_table_values("action")),
    module=__name__,
)


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
    source_states: FrozenSet[str]
    target_states: FrozenSet[str]
    public_lifecycle_transition: bool = True

    def __post_init__(self):
        if not isinstance(self.name, WorkerTransitionName):
            raise ValueError(f"unknown worker transition: {self.name}")
        object.__setattr__(self, "source_states", frozenset(self.source_states))
        object.__setattr__(self, "target_states", frozenset(self.target_states))

    @property
    def metadata(self):
        return self


@dataclass(frozen=True)
class _WorkerTransitionSpec:
    name: WorkerTransitionName
    payload_type: _WorkerTransitionPayloadType
    payload_factory: _WorkerTransitionPayloadFactory
    reducer: _WorkerTransitionReducer
    target_resolver: _WorkerTransitionTargetResolver
    source_states: FrozenSet[str]
    target_states: FrozenSet[str] = dataclass_field(default_factory=frozenset)
    public_lifecycle_transition: bool = True
    target_uses_payload: bool = False

    def __post_init__(self):
        if not isinstance(self.name, WorkerTransitionName):
            raise ValueError(f"unknown worker transition: {self.name}")
        if not isinstance(self.payload_type, type):
            raise TypeError("worker transition payload_type must be a type")
        for field_name in ("payload_factory", "reducer", "target_resolver"):
            if not callable(getattr(self, field_name)):
                raise TypeError(f"worker transition {field_name} must be callable")
        object.__setattr__(self, "source_states", frozenset(self.source_states))
        object.__setattr__(self, "target_states", frozenset(self.target_states))

    @property
    def metadata(self):
        return WorkerTransitionMetadata(
            self.name,
            self.source_states,
            self.target_states,
            public_lifecycle_transition=self.public_lifecycle_transition,
        )


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


def _lifecycle_metadata_from_row(row):
    return WorkerLifecycleMetadata(
        WorkerLifecycleDimensions(
            row.status,
            row.action,
            retryable=row.retryable,
            timeout_origin=row.timeout_origin,
        ),
        status_alias=row.status_alias,
        terminal_status=row.terminal_status,
        failed_dependency_status=row.failed_dependency_status,
        executable=row.executable,
        status_priority=row.status_priority,
    )


def _transition_spec(
    name: WorkerTransitionName,
    *,
    payload_type: _WorkerTransitionPayloadType,
    payload_factory: _WorkerTransitionPayloadFactory,
    reducer: _WorkerTransitionReducer,
    target_resolver: _WorkerTransitionTargetResolver,
    source_states=(),
    target_states=(),
    public_lifecycle_transition: bool = True,
    target_uses_payload: bool = False,
) -> _WorkerTransitionSpec:
    return _WorkerTransitionSpec(
        name,
        payload_type,
        payload_factory,
        reducer,
        target_resolver,
        source_states,
        target_states=target_states,
        public_lifecycle_transition=public_lifecycle_transition,
        target_uses_payload=target_uses_payload,
    )


WORKER_LIFECYCLE_METADATA = {row.state: _lifecycle_metadata_from_row(row) for row in _WORKER_LIFECYCLE_TABLE}


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
WORKER_LIFECYCLE_STATES = frozenset(WORKER_LIFECYCLE_STATE_VALUES)
WORKER_TIMEOUT_ORIGIN_LIFECYCLE_STATES = _lifecycle_states_matching(timeout_origin=True)
WORKER_LIFECYCLE_STATE_BY_STATUS_ALIAS = _status_aliases_by_lifecycle_metadata()
WORKER_STATUS_PRIORITY_BY_STATUS = _status_values_by_lifecycle_metadata("status_priority")


WORKER_RETRYABLE_LIFECYCLE_STATES = _lifecycle_states_matching(retryable=True)
WORKER_BLOCKED_LIFECYCLE_STATES = _lifecycle_states_matching(status=WORKER_STATUS_BLOCKED)
WORKER_ABORTED_LIFECYCLE_STATES = _lifecycle_states_matching(status=WORKER_STATUS_ABORTED)

WORKER_TIMEOUT_POLICY_STATUSES = frozenset(WORKER_FIELD_TIMEOUT_POLICY_STATUSES)
UNSET_TRANSITION_FIELD = object()
PUBLIC_WORKER_STATE_FIELD_NAMES = frozenset(("status", "next_eligible_action"))


@dataclass(frozen=True)
class WorkerSnapshotTransitionPatch:
    """Storage-boundary patch for replaying a normalized worker snapshot."""

    worker_id: str
    fields: dict
    target_lifecycle_state: Optional[str] = None
    prompt_ids: Optional[tuple] = None
    set_if_missing_fields: dict = dataclass_field(default_factory=dict)
    remove_fields: tuple = ()
    stale_recovery_allowed: bool = False
    accepted_abort_fields: dict = dataclass_field(default_factory=dict)
    accepted_abort_prompt_ids: Optional[tuple] = None

    def __post_init__(self):
        fields = deepcopy(dict(self.fields or {}))
        worker_id = fields.get("id") or self.worker_id
        if not worker_id:
            raise ValueError("snapshot patch requires worker_id")
        fields["id"] = worker_id
        target_lifecycle_state = self.target_lifecycle_state
        patch_lifecycle_state = fields.get("lifecycle_state")
        if target_lifecycle_state is None and patch_lifecycle_state in WORKER_LIFECYCLE_STATES:
            target_lifecycle_state = patch_lifecycle_state
        if target_lifecycle_state is not None and target_lifecycle_state not in WORKER_LIFECYCLE_STATES:
            raise ValueError(
                f"snapshot patch target lifecycle_state must be normalized: {target_lifecycle_state}"
            )
        if patch_lifecycle_state is not None and patch_lifecycle_state not in WORKER_LIFECYCLE_STATES:
            raise ValueError(f"snapshot patch lifecycle_state must be normalized: {patch_lifecycle_state}")
        object.__setattr__(self, "worker_id", worker_id)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "target_lifecycle_state", target_lifecycle_state)
        object.__setattr__(self, "prompt_ids", _transition_prompt_ids_or_none(self.prompt_ids))
        object.__setattr__(self, "set_if_missing_fields", deepcopy(dict(self.set_if_missing_fields or {})))
        object.__setattr__(self, "remove_fields", tuple(self.remove_fields or ()))
        object.__setattr__(self, "stale_recovery_allowed", bool(self.stale_recovery_allowed))
        object.__setattr__(self, "accepted_abort_fields", deepcopy(dict(self.accepted_abort_fields or {})))
        object.__setattr__(
            self,
            "accepted_abort_prompt_ids",
            _transition_prompt_ids_or_none(self.accepted_abort_prompt_ids),
        )


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
    return _require_worker_record(worker).to_output_dict()


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
    patch: WorkerSnapshotTransitionPatch


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
    worker = _require_worker_record(worker)
    return _ProvisionedTransition(
        session_id=deepcopy(worker.session_id),
        agent=_copy_present(worker.agent),
        model=_copy_present(worker.model),
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
    worker = _require_worker_record(worker)
    return _CleanupUpdatedTransition(deepcopy(worker.cleanup))


def _snapshot_applied_transition_payload(patch):
    if not isinstance(patch, WorkerSnapshotTransitionPatch):
        raise TypeError("snapshot transitions require WorkerSnapshotTransitionPatch from the storage boundary")
    return _SnapshotAppliedTransition(patch)


def _snapshot_worker_id(patch):
    if isinstance(patch, WorkerSnapshotTransitionPatch):
        return patch.worker_id
    raise TypeError("snapshot transitions require WorkerSnapshotTransitionPatch from the storage boundary")


def _require_transition_payload(transition, payload_type):
    payload = transition.payload
    if not isinstance(payload, payload_type):
        raise TypeError(f"worker transition '{transition.name.value}' has incompatible payload")
    return payload


def _reduce_worker_transition_payload(latest_worker, transition):
    latest_worker = _require_worker_record(latest_worker)
    spec = _worker_transition_spec(transition.name)
    payload = _require_transition_payload(transition, spec.payload_type)
    target_state = _resolve_transition_target_lifecycle_state(spec, transition, payload)
    reduced_worker = spec.reducer(latest_worker, transition, payload, target_state)
    return _require_worker_record(reduced_worker)


def _transition_worker_copy(latest_worker):
    return latest_worker.to_worker()


def _set_transition_lifecycle_state(worker, transition, lifecycle_state):
    worker._set_canonical_field("id", transition.worker_id)
    worker._set_canonical_field("lifecycle_state", lifecycle_state)


def _reduce_provisioned_transition(latest_worker, transition, payload, target_state):
    worker = _transition_worker_copy(latest_worker)
    if _accepted_abort(latest_worker):
        return worker
    worker._set_canonical_field("id", transition.worker_id)
    if payload.agent is not None:
        worker._set_canonical_field("agent", payload.agent)
    if payload.model is not None:
        worker._set_canonical_field("model", payload.model)
    if payload.session_id and not worker.session_id:
        worker._set_canonical_field("session_id", payload.session_id)
    return worker


def _reduce_active_transition(latest_worker, transition, payload, target_state):
    worker = _transition_worker_copy(latest_worker)
    if _accepted_abort(latest_worker):
        return worker
    _clear_current_status_fields(worker)
    _set_transition_lifecycle_state(worker, transition, target_state)
    _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
    if payload.clear_prompt_ids:
        worker._set_canonical_field("prompt_ids", [])
    return worker


def _reduce_attempt_started_transition(latest_worker, transition, payload, target_state):
    worker = _transition_worker_copy(latest_worker)
    worker.append_attempt(payload.attempt)
    return worker


def _reduce_failed_transition(latest_worker, transition, payload, target_state):
    worker = _transition_worker_copy(latest_worker)
    if _accepted_abort(latest_worker):
        _merge_worker_prompt_ids(worker, latest_worker, payload.prompt_ids)
        return worker
    _set_transition_lifecycle_state(worker, transition, target_state)
    worker.update_canonical_fields(
        error=payload.reason,
        failure_category=payload.category,
        failure_reason=payload.reason,
        last_failure_category=payload.category,
        last_failure_reason=payload.reason,
    )
    _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
    worker._remove_field("manual_retry_required")
    if payload.retryable:
        worker._remove_field("failure_retryable")
    else:
        worker._set_canonical_field("failure_retryable", False)
    _merge_worker_prompt_ids(worker, latest_worker, payload.prompt_ids)
    return worker


def _reduce_dependency_blocked_transition(latest_worker, transition, payload, target_state):
    worker = _transition_worker_copy(latest_worker)
    if _accepted_abort(latest_worker):
        return worker
    _set_transition_lifecycle_state(worker, transition, target_state)
    worker._set_canonical_field("blockers", list(payload.blockers))
    return worker


def _reduce_aborted_transition(latest_worker, transition, payload, target_state):
    worker = _transition_worker_copy(latest_worker)
    if _accepted_abort(latest_worker) and not _abort_is_accepted(payload.abort):
        return worker
    worker._set_canonical_field("id", transition.worker_id)
    worker._set_canonical_field("abort", payload.abort)
    if _abort_is_accepted(payload.abort):
        _set_transition_lifecycle_state(worker, transition, target_state)
    return worker


def _reduce_retry_scheduled_transition(latest_worker, transition, payload, target_state):
    worker = _transition_worker_copy(latest_worker)
    if _accepted_abort(latest_worker):
        _merge_worker_prompt_ids(worker, latest_worker, payload.prompt_ids)
        return worker
    _clear_current_status_fields(worker)
    _set_transition_lifecycle_state(worker, transition, target_state)
    worker.update_canonical_fields(
        retry_count=payload.retry_count,
        last_failure_category=payload.category,
        last_failure_reason=payload.reason,
    )
    _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
    _merge_worker_prompt_ids(worker, latest_worker, payload.prompt_ids)
    return worker


def _reduce_timed_out_transition(latest_worker, transition, payload, target_state):
    worker = _transition_worker_copy(latest_worker)
    if _accepted_abort(latest_worker):
        return worker
    _set_transition_lifecycle_state(worker, transition, target_state)
    worker.update_canonical_fields(
        error=payload.reason,
        failure_category=WORKER_STATUS_TIMEOUT,
        failure_reason=payload.reason,
        last_failure_category=WORKER_STATUS_TIMEOUT,
        last_failure_reason=payload.reason,
        timed_out_at=payload.timed_out_at,
        output_refs=[],
    )
    if payload.status == WORKER_STATUS_BLOCKED:
        worker._set_canonical_field("blockers", [WORKER_STATUS_TIMEOUT])
    _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
    if payload.manual_retry_required:
        worker._set_canonical_field("manual_retry_required", True)
    else:
        worker._remove_field("manual_retry_required")
    return worker


def _reduce_result_applied_transition(latest_worker, transition, payload, target_state):
    worker = _transition_worker_copy(latest_worker)
    if _accepted_abort(latest_worker):
        _merge_worker_prompt_ids(worker, latest_worker, payload.prompt_ids)
        return worker
    status = short_status(payload.result["status"])
    _set_transition_lifecycle_state(worker, transition, target_state)
    worker._set_canonical_field("result", payload.result)
    _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
    if status == WORKER_STATUS_DONE:
        _clear_current_status_fields(worker)
        assistant_message_id = payload.result["message_ids"].get("assistant")
        output_refs = [f"assistant:{assistant_message_id}"] if assistant_message_id else []
        worker._set_canonical_field("output_refs", output_refs)
    else:
        worker.update_canonical_fields(failure_category=None, failure_reason=None)
    _merge_worker_prompt_ids(worker, latest_worker, payload.prompt_ids)
    return worker


def _reduce_cleanup_updated_transition(latest_worker, transition, payload, target_state):
    worker = _transition_worker_copy(latest_worker)
    worker._set_canonical_field("id", transition.worker_id)
    worker._set_canonical_field("cleanup", payload.cleanup)
    return worker


def _reduce_snapshot_applied_transition(latest_worker, transition, payload, target_state):
    patch = payload.patch
    worker = _transition_worker_copy(latest_worker)
    if _accepted_abort(latest_worker) and not _accepted_abort_fields(
        patch.fields.get("abort"),
        patch.fields.get("lifecycle_state"),
    ):
        for field_name, value in patch.accepted_abort_fields.items():
            worker._set_field_value(field_name, value)
        if patch.accepted_abort_prompt_ids is not None:
            _merge_worker_prompt_ids(worker, latest_worker, patch.accepted_abort_prompt_ids, merge_empty=True)
        return worker
    worker._set_canonical_field("id", transition.worker_id)
    for field_name, value in patch.fields.items():
        worker._set_field_value(field_name, value)
    for field_name in patch.remove_fields:
        worker._remove_field(field_name)
    if patch.prompt_ids is not None:
        _merge_worker_prompt_ids(worker, latest_worker, patch.prompt_ids, merge_empty=True)
    for field_name, value in patch.set_if_missing_fields.items():
        if value and not getattr(worker, field_name, None):
            worker._set_field_value(field_name, value)
    return worker


def _lifecycle_state_set(*states):
    return frozenset(states)


def _no_transition_target(_transition, _payload):
    return None


def _constant_transition_target(target_state):
    def _resolve_transition_target(_transition, _payload):
        return target_state

    return _resolve_transition_target


def _failed_transition_target(_transition, payload):
    return worker_failed_lifecycle_state(retryable=payload.retryable, retry_available=payload.retry_available)


def _timed_out_transition_target(_transition, payload):
    return worker_timeout_lifecycle_state(payload.status, payload.retry_available)


def _result_applied_transition_target(_transition, payload):
    return worker_result_lifecycle_state(payload.result["status"])


def _snapshot_applied_transition_target(_transition, payload):
    return payload.patch.target_lifecycle_state


_WORKER_TRANSITION_SPECS = (
    _transition_spec(
        WorkerTransitionName.PROVISIONED,
        payload_type=_ProvisionedTransition,
        payload_factory=_provisioned_transition_payload,
        reducer=_reduce_provisioned_transition,
        target_resolver=_no_transition_target,
        source_states=_lifecycle_state_set(
            WORKER_LIFECYCLE_QUEUED,
            WORKER_LIFECYCLE_ACTIVE_WAIT,
            WORKER_LIFECYCLE_ACTIVE_RETRY,
            WORKER_LIFECYCLE_FAILED_RETRY,
            WORKER_LIFECYCLE_TIMEOUT_RETRY,
            WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
        ),
    ),
    _transition_spec(
        WorkerTransitionName.ACTIVE,
        payload_type=_ActiveTransition,
        payload_factory=_active_transition_payload,
        reducer=_reduce_active_transition,
        target_resolver=_constant_transition_target(WORKER_LIFECYCLE_ACTIVE_WAIT),
        source_states=_lifecycle_state_set(
            WORKER_LIFECYCLE_QUEUED,
            WORKER_LIFECYCLE_ACTIVE_WAIT,
            WORKER_LIFECYCLE_ACTIVE_RETRY,
            WORKER_LIFECYCLE_BLOCKED_DEPENDENCY,
            WORKER_LIFECYCLE_BLOCKED_TIMEOUT,
            WORKER_LIFECYCLE_FAILED_RETRY,
            WORKER_LIFECYCLE_TIMEOUT_RETRY,
            WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
        ),
        target_states=_lifecycle_state_set(WORKER_LIFECYCLE_ACTIVE_WAIT),
    ),
    _transition_spec(
        WorkerTransitionName.ATTEMPT_STARTED,
        payload_type=_AttemptStartedTransition,
        payload_factory=_attempt_started_transition_payload,
        reducer=_reduce_attempt_started_transition,
        target_resolver=_no_transition_target,
        source_states=_lifecycle_state_set(WORKER_LIFECYCLE_ACTIVE_WAIT),
    ),
    _transition_spec(
        WorkerTransitionName.FAILED,
        payload_type=_FailedTransition,
        payload_factory=_failed_transition_payload,
        reducer=_reduce_failed_transition,
        target_resolver=_failed_transition_target,
        source_states=_lifecycle_state_set(
            WORKER_LIFECYCLE_QUEUED,
            WORKER_LIFECYCLE_ACTIVE_WAIT,
            WORKER_LIFECYCLE_ACTIVE_RETRY,
            WORKER_LIFECYCLE_FAILED_RETRY,
            WORKER_LIFECYCLE_TIMEOUT_RETRY,
            WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
        ),
        target_states=_lifecycle_state_set(WORKER_LIFECYCLE_FAILED_RETRY, WORKER_LIFECYCLE_FAILED_TERMINAL),
        target_uses_payload=True,
    ),
    _transition_spec(
        WorkerTransitionName.DEPENDENCY_BLOCKED,
        payload_type=_DependencyBlockedTransition,
        payload_factory=_dependency_blocked_transition_payload,
        reducer=_reduce_dependency_blocked_transition,
        target_resolver=_constant_transition_target(WORKER_LIFECYCLE_BLOCKED_DEPENDENCY),
        source_states=_lifecycle_state_set(
            WORKER_LIFECYCLE_QUEUED,
            WORKER_LIFECYCLE_ACTIVE_WAIT,
            WORKER_LIFECYCLE_ACTIVE_RETRY,
        ),
        target_states=_lifecycle_state_set(WORKER_LIFECYCLE_BLOCKED_DEPENDENCY),
    ),
    _transition_spec(
        WorkerTransitionName.ABORTED,
        payload_type=_AbortedTransition,
        payload_factory=_aborted_transition_payload,
        reducer=_reduce_aborted_transition,
        target_resolver=_constant_transition_target(WORKER_LIFECYCLE_ABORTED),
        source_states=_lifecycle_state_set(
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
        target_states=_lifecycle_state_set(WORKER_LIFECYCLE_ABORTED),
    ),
    _transition_spec(
        WorkerTransitionName.RETRY_SCHEDULED,
        payload_type=_RetryScheduledTransition,
        payload_factory=_retry_scheduled_transition_payload,
        reducer=_reduce_retry_scheduled_transition,
        target_resolver=_constant_transition_target(WORKER_LIFECYCLE_ACTIVE_RETRY),
        source_states=_lifecycle_state_set(
            WORKER_LIFECYCLE_ACTIVE_WAIT,
            WORKER_LIFECYCLE_FAILED_RETRY,
            WORKER_LIFECYCLE_TIMEOUT_RETRY,
            WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
        ),
        target_states=_lifecycle_state_set(WORKER_LIFECYCLE_ACTIVE_RETRY),
    ),
    _transition_spec(
        WorkerTransitionName.TIMED_OUT,
        payload_type=_TimedOutTransition,
        payload_factory=_timed_out_transition_payload,
        reducer=_reduce_timed_out_transition,
        target_resolver=_timed_out_transition_target,
        source_states=_lifecycle_state_set(WORKER_LIFECYCLE_ACTIVE_WAIT),
        target_states=_lifecycle_state_set(
            WORKER_LIFECYCLE_BLOCKED_TIMEOUT,
            WORKER_LIFECYCLE_TIMEOUT_RETRY,
            WORKER_LIFECYCLE_TIMEOUT_TERMINAL,
            WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
            WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL,
            WORKER_LIFECYCLE_TIMEOUT_ABORTED,
        ),
        target_uses_payload=True,
    ),
    _transition_spec(
        WorkerTransitionName.RESULT_APPLIED,
        payload_type=_ResultAppliedTransition,
        payload_factory=_result_applied_transition_payload,
        reducer=_reduce_result_applied_transition,
        target_resolver=_result_applied_transition_target,
        source_states=_lifecycle_state_set(WORKER_LIFECYCLE_ACTIVE_WAIT),
        target_states=_lifecycle_state_set(
            WORKER_LIFECYCLE_BLOCKED_DEPENDENCY,
            WORKER_LIFECYCLE_DONE_COLLECT,
            WORKER_LIFECYCLE_FAILED_TERMINAL,
            WORKER_LIFECYCLE_TIMEOUT_TERMINAL,
            WORKER_LIFECYCLE_ABORTED,
        ),
        target_uses_payload=True,
    ),
    _transition_spec(
        WorkerTransitionName.CLEANUP_UPDATED,
        payload_type=_CleanupUpdatedTransition,
        payload_factory=_cleanup_updated_transition_payload,
        reducer=_reduce_cleanup_updated_transition,
        target_resolver=_no_transition_target,
        source_states=WORKER_LIFECYCLE_STATES,
        public_lifecycle_transition=False,
    ),
    _transition_spec(
        WorkerTransitionName.SNAPSHOT_APPLIED,
        payload_type=_SnapshotAppliedTransition,
        payload_factory=_snapshot_applied_transition_payload,
        reducer=_reduce_snapshot_applied_transition,
        target_resolver=_snapshot_applied_transition_target,
        source_states=WORKER_LIFECYCLE_STATES,
        public_lifecycle_transition=False,
        target_uses_payload=True,
    ),
)


def _worker_transition_specs_by_name(specs):
    specs_by_name = {}
    for spec in specs:
        if spec.name in specs_by_name:
            raise ValueError(f"duplicate worker transition spec: {spec.name.value}")
        specs_by_name[spec.name] = spec
    missing_names = tuple(name.value for name in WorkerTransitionName if name not in specs_by_name)
    if missing_names:
        raise ValueError(f"missing worker transition spec: {', '.join(missing_names)}")
    return specs_by_name


_WORKER_TRANSITION_SPEC_BY_NAME = _worker_transition_specs_by_name(_WORKER_TRANSITION_SPECS)

WORKER_TRANSITION_METADATA = {spec.name: spec.metadata for spec in _WORKER_TRANSITION_SPECS}

WORKER_FAILED_TARGET_LIFECYCLE_STATES = WORKER_TRANSITION_METADATA[WorkerTransitionName.FAILED].target_states
WORKER_RETRY_SCHEDULE_SOURCE_LIFECYCLE_STATES = WORKER_TRANSITION_METADATA[
    WorkerTransitionName.RETRY_SCHEDULED
].source_states


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


def _worker_transition_spec(name):
    if not isinstance(name, WorkerTransitionName):
        raise ValueError(f"unknown worker transition: {name}")
    spec = _WORKER_TRANSITION_SPEC_BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"unknown worker transition: {name}")
    return spec


def _worker_transition_metadata(name):
    _worker_transition_spec(name)
    return WORKER_TRANSITION_METADATA[name]


def _build_worker_transition_payload(name, *args, **kwargs):
    spec = _worker_transition_spec(name)
    return _built_transition_payload(name, spec.payload_type, spec.payload_factory(*args, **kwargs))


def _built_transition_payload(name, payload_type, payload):
    if not isinstance(payload, payload_type):
        raise TypeError(f"worker transition '{name.value}' built incompatible payload")
    return payload


def _resolve_transition_target_lifecycle_state(spec, transition, payload=UNSET_TRANSITION_FIELD):
    if spec.target_uses_payload and payload is UNSET_TRANSITION_FIELD:
        payload = _require_transition_payload(transition, spec.payload_type)
    target_state = spec.target_resolver(transition, payload)
    if target_state is not None and not isinstance(target_state, str):
        raise TypeError(f"worker transition '{spec.name.value}' resolved a non-string lifecycle target")
    _validate_transition_target_lifecycle_state(spec.name, target_state)
    return target_state


def worker_transition_target_lifecycle_state(transition):
    spec = _worker_transition_spec(transition.name)
    return _resolve_transition_target_lifecycle_state(spec, transition)


def _validate_transition_target_lifecycle_state(name, target_state):
    metadata = _worker_transition_metadata(name)
    if target_state is not None and metadata.target_states and target_state not in metadata.target_states:
        raise ValueError(
            f"worker transition '{name.value}' resolved unknown target lifecycle state: {target_state}"
        )


def worker_transition_is_legal(latest_worker, transition):
    metadata = _worker_transition_metadata(transition.name)
    source_state = worker_lifecycle_state(latest_worker)
    if source_state not in metadata.source_states:
        return False
    if transition.name is WorkerTransitionName.SNAPSHOT_APPLIED:
        target_state = worker_transition_target_lifecycle_state(transition)
        if target_state is None:
            return True
        return target_state in _WORKER_SNAPSHOT_TARGET_STATES_BY_SOURCE.get(source_state, frozenset())
    return True


def apply_worker_transition_payload(reducer, transition):
    latest_worker = _require_worker_record(reducer.latest_worker)
    return _reduce_worker_transition_payload(latest_worker, transition).to_snapshot()


def worker_lifecycle_set_fields(worker_id, lifecycle_state):
    return {"id": worker_id, "lifecycle_state": lifecycle_state}


def _is_worker_record(worker):
    return isinstance(worker, WorkerRecord)


def _require_worker_record(worker):
    if isinstance(worker, WorkerRecord):
        return worker
    raise TypeError("internal worker must be WorkerRecord; hydrate raw mappings at the storage boundary")


def _worker_fields(worker):
    return _require_worker_record(worker).to_snapshot()


def _worker_init_fields(fields):
    if fields is None:
        return {}
    if isinstance(fields, WorkerRecord):
        return _worker_fields(fields)
    if isinstance(fields, Mapping):
        return dict(fields)
    raise TypeError("worker record fields must be a mapping or WorkerRecord")


def worker_field(worker, field_name, default=None):
    """Boundary accessor for dynamic persisted fields; core code uses WorkerRecord properties."""
    return _require_worker_record(worker)._compat_field(field_name, default)


def worker_has_field(worker, field_name):
    return _require_worker_record(worker)._has_compat_field(field_name)


def is_worker_record(worker):
    return _is_worker_record(worker)


def worker_retry_available(worker, category: Optional[str] = None) -> bool:
    worker = _require_worker_record(worker)
    return worker.retry_available(category)


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
        return cls(worker.lifecycle_state, worker.has_prompt)

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
    return _require_worker_record(worker).lifecycle_state


def latest_prompt_ids_are_retry_marker(latest_worker):
    latest_worker = _require_worker_record(latest_worker)
    return (
        latest_worker.lifecycle_state == WORKER_LIFECYCLE_ACTIVE_RETRY
        and latest_worker.last_failure_category is not None
    )


def next_eligible_worker_action(worker):
    return public_worker_state(worker_lifecycle_state(worker))[1]


def worker_has_prompt(worker):
    return _require_worker_record(worker).has_prompt


def is_executable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_execute()


def is_dependency_blockable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_block_for_dependency()


_UNSET_WORKER_UPDATE = object()


class WorkerRecord:
    """Hydrated worker domain object with explicit serialization boundaries."""

    __slots__ = (*WORKER_RECORD_FIELD_NAMES, "_present_optional_fields")
    __annotations__ = {"_present_optional_fields": "set"}

    __iter__ = None

    def __init__(self, worker_id, fields=None):
        self._reset_fields(worker_id)
        if fields is not None:
            self._merge_fields(fields)
        if not self.id:
            self._set_canonical_field("id", worker_id)

    def __repr__(self):
        return f"{type(self).__name__}({self.worker_id!r}, {self.to_snapshot()!r})"

    def __eq__(self, other):
        if isinstance(other, WorkerRecord):
            return self.to_snapshot() == other.to_snapshot()
        return NotImplemented

    def _reset_fields(self, worker_id):
        defaults = self.default_snapshot_fields(worker_id)
        self._present_optional_fields = set()
        for field_name in WORKER_REQUIRED_FIELD_NAMES:
            setattr(self, field_name, deepcopy(defaults[field_name]))
        for field_name in WORKER_RECORD_OPTIONAL_FIELD_NAMES:
            setattr(self, field_name, None)

    def _raw_field(self, field_name: str, default: object = None) -> object:
        if field_name in WORKER_REQUIRED_FIELD_NAMES:
            return getattr(self, field_name)
        if field_name in WORKER_RECORD_OPTIONAL_FIELD_NAMES:
            if field_name not in self._present_optional_fields:
                return default
            return getattr(self, field_name)
        return default

    def _list_field(self, field_name: str) -> list:
        value = self._raw_field(field_name)
        return value if isinstance(value, list) else []

    def _canonical_field_value(self, field_name: str, value: JsonValue) -> JsonValue:
        field_name = _require_worker_field_name(field_name)
        spec = WORKER_FIELD_SPEC_BY_NAME.get(field_name)
        if spec is None:
            return deepcopy(value)
        return spec.canonical_value(value)

    def _set_canonical_field(self, field_name: str, value: JsonValue) -> None:
        field_name = _require_worker_field_name(field_name)
        setattr(self, field_name, self._canonical_field_value(field_name, value))
        if field_name in WORKER_RECORD_OPTIONAL_FIELD_NAMES:
            self._present_optional_fields.add(field_name)

    def _set_field_value(self, field_name: str, value: JsonValue) -> None:
        field_name = _require_worker_field_name(field_name)
        if field_name in PUBLIC_WORKER_STATE_FIELD_NAMES:
            raise ValueError(f"worker public field '{field_name}' is output-only; use lifecycle_state")
        if field_name in WORKER_RECORD_CANONICAL_FIELD_NAMES:
            self._set_canonical_field(field_name, value)
            return
        raise ValueError(f"unknown worker field: {field_name}")

    def _compat_field(self, field_name: str, default: object = None) -> object:
        return self._raw_field(field_name, default)

    def _has_compat_field(self, field_name: str) -> bool:
        if field_name in WORKER_REQUIRED_FIELD_NAMES:
            return True
        if field_name in WORKER_RECORD_OPTIONAL_FIELD_NAMES:
            return field_name in self._present_optional_fields
        return False

    def _remove_field(self, field_name):
        if field_name in WORKER_RECORD_OPTIONAL_FIELD_NAMES:
            setattr(self, field_name, None)
            self._present_optional_fields.discard(field_name)
        elif field_name in WORKER_REQUIRED_FIELD_NAMES:
            default_value = self.default_snapshot_fields(self.worker_id)[field_name]
            self._set_canonical_field(field_name, default_value)
        return self

    def _merge_fields(self, fields=None, **kwargs):
        if fields is not None:
            for field_name, value in _worker_init_fields(fields).items():
                self._set_field_value(field_name, value)
        if kwargs:
            for field_name, value in kwargs.items():
                self._set_field_value(field_name, value)
        return self

    def replace_fields(self, fields):
        worker_id = self.worker_id
        self._reset_fields(worker_id)
        self._merge_fields(fields)
        return self

    @classmethod
    def from_worker(cls, worker, worker_id=None):
        worker = _require_worker_record(worker)
        resolved_worker_id = worker.worker_id or worker_id
        return cls(resolved_worker_id, worker.to_snapshot())

    @classmethod
    def default_snapshot_fields(cls, worker_id):
        return worker_default_snapshot_fields(worker_id)

    @classmethod
    def default_fields(cls, worker_id):
        return cls(worker_id, cls.default_snapshot_fields(worker_id))

    @classmethod
    def lifecycle_set_fields(cls, worker_id, lifecycle_state):
        return worker_lifecycle_set_fields(worker_id, lifecycle_state)

    @property
    def worker_id(self) -> str:
        return self.id

    @property
    def has_prompt(self) -> bool:
        prompt = self.prompt
        return prompt is not None and bool(str(prompt))

    def update_canonical_fields(
        self,
        *,
        skip_none=False,
        **fields,
    ):
        unexpected_field = next(
            (
                field_name
                for field_name in fields
                if field_name not in WORKER_RECORD_UPDATE_FIELD_NAMES
            ),
            None,
        )
        if unexpected_field is not None:
            raise TypeError(
                "update_canonical_fields() got an unexpected keyword "
                f"argument '{unexpected_field}'"
            )
        return self.update_canonical_fields_from_mapping(
            fields,
            skip_none=skip_none,
            field_names=WORKER_RECORD_UPDATE_FIELD_NAMES,
        )

    def update_canonical_fields_from_mapping(self, fields, *, skip_none=False, field_names=None):
        field_names = (
            WORKER_RECORD_UPDATE_FIELD_NAMES
            if field_names is None
            else tuple(field_names)
        )
        for field_name in field_names:
            value = fields.get(field_name, _UNSET_WORKER_UPDATE)
            if value is _UNSET_WORKER_UPDATE or (skip_none and value is None):
                continue
            self._set_canonical_field(field_name, value)
        return self

    def new_attempt_record(self, *, started_at, created_session_ids=()):
        attempts = self.attempts if isinstance(self.attempts, list) else []
        return {
            "id": f"attempt-{len(attempts) + 1}",
            "session_id": self.session_id,
            "created_session_ids": list(created_session_ids),
            "status": "active",
            "started_at": started_at,
            "finished_at": None,
        }

    def append_attempt(self, attempt):
        if not attempt:
            return self
        attempts = self.attempts if isinstance(self.attempts, list) else []
        attempt = deepcopy(attempt)
        if any(isinstance(existing, dict) and existing.get("id") == attempt.get("id") for existing in attempts):
            return self
        self._set_canonical_field("attempts", [*deepcopy(attempts), attempt])
        return self

    def finalize_attempt(self, attempt_id, fields):
        if not attempt_id:
            return self
        fields = deepcopy(fields) if isinstance(fields, dict) else {}
        attempts = self.attempts if isinstance(self.attempts, list) else []
        finalized = []
        found = False
        for attempt in attempts:
            if isinstance(attempt, dict) and attempt.get("id") == attempt_id:
                updated = deepcopy(attempt)
                updated.update(fields)
                finalized.append(updated)
                found = True
            else:
                finalized.append(deepcopy(attempt))
        if found:
            self._set_canonical_field("attempts", finalized)
        return self

    def retry_available(self, category: Optional[str] = None) -> bool:
        if self.failure_retryable is False:
            return False
        retryable = set(self.retryable_failures or [])
        if not retryable:
            return False
        if category is None:
            category = self.failure_category or self.last_failure_category
        if category and category not in retryable and "all" not in retryable:
            return False
        try:
            retry_count = self.retry_count
            retry_limit = self.retry_limit
        except (TypeError, ValueError):
            return False
        return retry_count < retry_limit

    def scheduling_state(self):
        return WorkerSchedulingState(
            self.lifecycle_state,
            self.has_prompt,
        )

    def to_snapshot(self):
        normalized = self.default_snapshot_fields(self.worker_id)
        for field_name in WORKER_REQUIRED_FIELD_NAMES:
            normalized[field_name] = self._canonical_field_value(field_name, getattr(self, field_name))
        for field_name in WORKER_RECORD_OPTIONAL_FIELD_NAMES:
            if field_name in self._present_optional_fields:
                normalized[field_name] = self._canonical_field_value(field_name, getattr(self, field_name))
        return normalized

    def to_output_dict(self):
        fields = self.to_snapshot()
        fields.update(public_worker_state_fields(fields["lifecycle_state"]))
        return fields

    def to_worker(self):
        normalized = self.to_snapshot()
        return type(self)(self.worker_id, require_internal_worker(normalized))

    def set_session(
        self,
        session_id: Optional[str],
        *,
        agent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> "WorkerRecord":
        self._set_canonical_field("session_id", session_id)
        if agent is not None:
            self._set_canonical_field("agent", agent)
        if model is not None:
            self._set_canonical_field("model", model)
        return self

    def remember_prompt_id(self, prompt_id: str) -> "WorkerRecord":
        if not isinstance(prompt_id, str) or not prompt_id:
            raise TypeError("worker prompt_id must be a non-empty string")
        prompt_ids = list(self.prompt_ids)
        if prompt_id not in prompt_ids:
            prompt_ids.append(prompt_id)
        self._set_canonical_field("prompt_ids", prompt_ids)
        return self

    def apply_transition(self, transition: "WorkerTransition") -> "WorkerRecord":
        result = _apply_worker_transition_to_record(self, transition)
        if result.skipped and not result.stale_snapshot_recovery:
            raise WorkerTransitionError(result)
        merged = result.worker
        self.replace_fields(merged)
        if not self.id:
            self.id = transition.worker_id
        return self

    def ensure_cleanup(self):
        cleanup = self.cleanup
        if not isinstance(cleanup, dict):
            cleanup = {"requested": True, "deleted": False}
            self.cleanup = cleanup
            self._present_optional_fields.add("cleanup")
            cleanup = self.cleanup
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
    return WorkerRecord.from_worker(worker, worker_id).to_worker()


def serialize_worker_snapshot(worker, worker_id):
    return WorkerRecord.from_worker(worker, worker_id).to_snapshot()


def worker_record_for_mutation(worker, worker_id=None):
    if isinstance(worker, WorkerRecord):
        return worker
    if worker is None and worker_id is not None:
        return default_worker_record(worker_id)
    raise TypeError("internal worker mutation requires WorkerRecord")


def require_internal_worker(worker):
    if not isinstance(worker, Mapping):
        raise TypeError("internal worker fields must be a mapping")
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
    def create(cls, name, worker_id, *payload_args, **payload_kwargs):
        if not isinstance(name, WorkerTransitionName):
            raise ValueError(f"unknown worker transition: {name}")
        return cls(
            worker_id,
            name,
            _build_worker_transition_payload(name, *payload_args, **payload_kwargs),
        )

    @classmethod
    def provisioned(cls, worker):
        worker_id = _worker_id(worker)
        return cls.create(WorkerTransitionName.PROVISIONED, worker_id, worker)

    @classmethod
    def active(cls, worker_id, *, timeout_started_at=UNSET_TRANSITION_FIELD, clear_prompt_ids=False):
        return cls.create(
            WorkerTransitionName.ACTIVE,
            worker_id,
            timeout_started_at=timeout_started_at,
            clear_prompt_ids=clear_prompt_ids,
        )

    @classmethod
    def attempt_started(cls, worker_id, attempt):
        return cls.create(WorkerTransitionName.ATTEMPT_STARTED, worker_id, attempt)

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
        return cls.create(
            WorkerTransitionName.FAILED,
            worker_id,
            category,
            reason,
            retryable=retryable,
            retry_available=retry_available,
            timeout_started_at=timeout_started_at,
            prompt_ids=prompt_ids,
        )

    @classmethod
    def dependency_blocked(cls, worker_id, blockers):
        return cls.create(WorkerTransitionName.DEPENDENCY_BLOCKED, worker_id, blockers)

    @classmethod
    def aborted(cls, worker_id, abort):
        return cls.create(WorkerTransitionName.ABORTED, worker_id, abort)

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
        return cls.create(
            WorkerTransitionName.RETRY_SCHEDULED,
            worker_id,
            category,
            reason,
            retry_count=retry_count,
            timeout_started_at=timeout_started_at,
            prompt_ids=prompt_ids,
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
        return cls.create(
            WorkerTransitionName.TIMED_OUT,
            worker_id,
            reason,
            status=status,
            timed_out_at=timed_out_at,
            retry_available=retry_available,
            manual_retry_required=manual_retry_required,
            timeout_started_at=timeout_started_at,
        )

    @classmethod
    def result_applied(cls, worker_id, result, *, prompt_ids=(), timeout_started_at=UNSET_TRANSITION_FIELD):
        return cls.create(
            WorkerTransitionName.RESULT_APPLIED,
            worker_id,
            result,
            prompt_ids=prompt_ids,
            timeout_started_at=timeout_started_at,
        )

    @classmethod
    def cleanup_updated(cls, worker):
        worker_id = _worker_id(worker)
        return cls.create(WorkerTransitionName.CLEANUP_UPDATED, worker_id, worker)

    @classmethod
    def snapshot_applied(cls, patch):
        worker_id = _snapshot_worker_id(patch)
        return cls.create(WorkerTransitionName.SNAPSHOT_APPLIED, worker_id, patch)


def _copy_present(value):
    return None if value is None else deepcopy(value)


def _copy_transition_value(value):
    if value is UNSET_TRANSITION_FIELD:
        return value
    return deepcopy(value)


def _filtered_prompt_ids(prompt_ids):
    return tuple(prompt_id for prompt_id in prompt_ids if prompt_id is not None)


def _transition_prompt_ids_or_none(prompt_ids):
    if prompt_ids is None:
        return None
    if not isinstance(prompt_ids, (list, tuple)):
        return None
    return _filtered_prompt_ids(prompt_ids)


def _clear_current_status_fields(worker):
    worker._set_canonical_field("blockers", [])
    worker.update_canonical_fields(failure_category=None, failure_reason=None)
    for field_name in REMOVABLE_WORKER_TRANSITION_FIELDS:
        worker._remove_field(field_name)


def _set_if_not_unset(worker, name, value):
    if value is not UNSET_TRANSITION_FIELD:
        worker._set_canonical_field(name, value)


def _merge_worker_prompt_ids(worker, latest_worker, prompt_ids, *, merge_empty=False):
    if not prompt_ids and not merge_empty:
        return
    source_prompt_ids = [] if latest_prompt_ids_are_retry_marker(latest_worker) else latest_worker.prompt_ids
    merged_values = []
    for values in (source_prompt_ids, list(prompt_ids or ())):
        for value in values:
            if value not in merged_values:
                merged_values.append(deepcopy(value))
    worker._set_canonical_field("prompt_ids", merged_values)


def _accepted_abort(worker):
    worker = _require_worker_record(worker)
    return _accepted_abort_fields(worker.abort, worker.lifecycle_state)


def _accepted_abort_fields(abort, lifecycle_state):
    status = public_worker_state(lifecycle_state)[0]
    return isinstance(abort, dict) and abort.get("accepted") and status == WORKER_STATUS_ABORTED


def _abort_is_accepted(abort):
    return isinstance(abort, dict) and abort.get("accepted")


class WorkerLifecycleReducer:
    def __init__(self, record):
        self.record = worker_record_for_mutation(record)
        self.latest_worker = self.record.to_worker()

    def apply(self, transition):
        return reduce_worker_transition(self.record, transition)


def reduce_worker_transition(worker, transition):
    record = worker_record_for_mutation(worker, transition.worker_id)
    return _reduce_worker_transition_to_result(record, transition)


def apply_worker_transition_to_record(record, transition):
    return reduce_worker_transition(record, transition)


def _reduce_worker_transition_to_result(record, transition):
    if not isinstance(transition.name, WorkerTransitionName):
        raise ValueError(f"unknown worker transition: {transition.name}")
    latest_worker = record.to_worker()
    metadata = _worker_transition_metadata(transition.name)
    if not _accepted_abort(latest_worker) and not worker_transition_is_legal(latest_worker, transition):
        return WorkerTransitionResult(
            applied=False,
            worker=_unchanged_transition_worker(latest_worker, record, transition),
            reason=_illegal_transition_reason(latest_worker, transition, metadata),
            stale_snapshot_recovery=_is_stale_snapshot_recovery(transition),
        )
    worker = _reduce_worker_transition_payload(latest_worker, transition)
    _finalize_worker_attempt(worker, transition.attempt_finalization)
    return WorkerTransitionResult(
        applied=True,
        worker=_require_worker_record(worker).to_worker(),
    )


def _unchanged_transition_worker(latest_worker, record, transition):
    return _require_worker_record(latest_worker).to_worker()


def _illegal_transition_reason(latest_worker, transition, metadata):
    source_state = worker_lifecycle_state(latest_worker)
    transition_name = transition.name.value
    target_state = _transition_target_lifecycle_state_for_reason(transition)
    target = f" to lifecycle_state '{target_state}'" if target_state is not None else ""
    if _is_stale_snapshot_recovery(transition):
        return (
            f"stale snapshot ignored for worker '{transition.worker_id}': transition "
            f"'{transition_name}' cannot move from lifecycle_state '{source_state}'{target}"
        )
    allowed = ", ".join(sorted(metadata.source_states)) or "none"
    return (
        f"illegal worker transition '{transition_name}' for worker '{transition.worker_id}' "
        f"from lifecycle_state '{source_state}'{target}; allowed source states: {allowed}"
    )


def _transition_target_lifecycle_state_for_reason(transition):
    if _is_stale_snapshot_recovery(transition):
        return _snapshot_target_lifecycle_state(transition)
    try:
        return worker_transition_target_lifecycle_state(transition)
    except (KeyError, TypeError, AttributeError):
        return None


def _snapshot_target_lifecycle_state(transition):
    payload = getattr(transition, "payload", None)
    patch = getattr(payload, "patch", None)
    return getattr(patch, "target_lifecycle_state", None)


def _is_stale_snapshot_recovery(transition):
    if transition.name is not WorkerTransitionName.SNAPSHOT_APPLIED:
        return False
    patch = getattr(getattr(transition, "payload", None), "patch", None)
    return bool(getattr(patch, "stale_recovery_allowed", False))


def _finalize_worker_attempt(worker, finalization):
    if finalization is None:
        return
    worker.finalize_attempt(finalization.attempt_id, finalization.fields)


def _apply_worker_transition_to_record(worker, transition):
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


def mark_worker_active(worker, *, now=None):
    worker = _require_worker_record(worker)
    timeout_started_at = UNSET_TRANSITION_FIELD
    if now is not None:
        timeout_started_at = now() if worker.timeout_seconds else None
    transition = WorkerTransition.active(
        worker.worker_id,
        timeout_started_at=timeout_started_at,
        clear_prompt_ids=latest_prompt_ids_are_retry_marker(worker),
    )
    return transition


def mark_worker_failed(worker, category, reason, *, retryable=True, prompt_ids=()):
    worker = _require_worker_record(worker)
    transition = WorkerTransition.failed(
        worker.worker_id,
        category,
        reason,
        retryable=retryable,
        retry_available=worker_retry_available(worker, category),
        timeout_started_at=_timeout_started_at_or_unset(worker),
        prompt_ids=prompt_ids,
    )
    return transition


def mark_dependency_blocked(worker, blockers):
    worker = _require_worker_record(worker)
    transition = WorkerTransition.dependency_blocked(worker.worker_id, blockers)
    return transition


def mark_worker_aborted(worker, abort):
    worker = _require_worker_record(worker)
    transition = WorkerTransition.aborted(worker.worker_id, abort)
    return transition


def schedule_worker_retry(worker, category, reason, *, prompt_ids=()):
    worker = _require_worker_record(worker)
    if not worker_retry_available(worker, category):
        return False
    transition = WorkerTransition.retry_scheduled(
        worker.worker_id,
        category,
        reason,
        retry_count=worker.retry_count + 1,
        timeout_started_at=_timeout_started_at_or_unset(worker),
        prompt_ids=prompt_ids,
    )
    return transition


def worker_timeout_reason(worker):
    worker = _require_worker_record(worker)
    return f"worker timed out after {format_timeout(worker.timeout_seconds)}s"


def mark_worker_timeout(worker, reason, now, *, manual_retry_required=False):
    worker = _require_worker_record(worker)
    status = worker.timeout_policy
    transition = WorkerTransition.timed_out(
        worker.worker_id,
        reason,
        status=status,
        timed_out_at=now(),
        retry_available=worker_retry_available(worker, WORKER_STATUS_TIMEOUT),
        manual_retry_required=manual_retry_required,
        timeout_started_at=_timeout_started_at_or_unset(worker),
    )
    return transition


def format_timeout(timeout):
    return str(timeout)


def apply_worker_result(worker, result, *, prompt_ids=()):
    worker = _require_worker_record(worker)
    transition = WorkerTransition.result_applied(
        worker.worker_id,
        result,
        prompt_ids=prompt_ids,
        timeout_started_at=_timeout_started_at_or_unset(worker),
    )
    return transition


def _worker_id(worker):
    return _require_worker_record(worker).worker_id


def _timeout_started_at_or_unset(worker):
    worker = _require_worker_record(worker)
    return worker.timeout_started_at


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
    prompted_workers = [worker for worker in workers.values() if _is_worker_record(worker) and worker_prompt(worker)]
    status_workers = prompted_workers
    if include_unprompted_when_no_prompts:
        status_workers = prompted_workers or [worker for worker in workers.values() if _is_worker_record(worker)]
    return aggregate_run_status(_worker_status(worker) for worker in status_workers)


def worker_output_refs_in_dependency_order(workers):
    ordered = []
    for worker in workers_in_dependency_order(workers):
        worker = _require_worker_record(worker)
        worker_id = worker.worker_id
        if _worker_status(worker) != WORKER_STATUS_DONE:
            continue
        for output_ref in worker.output_refs:
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
    workers = [worker for worker in (run.get("workers") or {}).values() if _is_worker_record(worker) and worker_prompt(worker)]
    if not workers:
        return False
    statuses = {_worker_status(worker) for worker in workers}
    return WORKER_STATUS_DONE in statuses and any(
        status in {WORKER_STATUS_FAILED, WORKER_STATUS_BLOCKED, WORKER_STATUS_ABORTED, WORKER_STATUS_TIMEOUT}
        for status in statuses
    )


def worker_prompt(worker):
    prompt = _require_worker_record(worker).prompt
    if prompt is None:
        return None
    return str(prompt)


def _worker_status(worker):
    return public_worker_state(worker_lifecycle_state(worker))[0] if isinstance(worker, WorkerRecord) else None
