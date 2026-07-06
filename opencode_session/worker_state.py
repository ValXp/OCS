from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Type, Union

from opencode_session.schema_common import WORKER_REQUIRED_FIELD_NAMES
from opencode_session.status import short_status
from opencode_session.worker_attempt_log import _append_attempt


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
    source_transitions: frozenset = frozenset()
    target_transitions: frozenset = frozenset()

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
        object.__setattr__(self, "source_transitions", _transition_name_set(self.source_transitions))
        object.__setattr__(self, "target_transitions", _transition_name_set(self.target_transitions))
        if self.retryable and self.action != "retry":
            raise ValueError("retryable worker lifecycle rows must use the retry action")


def _transition_name_set(transitions):
    return frozenset(WorkerTransitionName(transition) for transition in transitions)


_WORKER_LIFECYCLE_TABLE = (
    _WorkerLifecycleRow(
        "queued",
        "queued",
        "start",
        status_alias=True,
        executable=True,
        status_priority=0,
        source_transitions=(
            WorkerTransitionName.PROVISIONED,
            WorkerTransitionName.ACTIVE,
            WorkerTransitionName.FAILED,
            WorkerTransitionName.DEPENDENCY_BLOCKED,
            WorkerTransitionName.ABORTED,
        ),
    ),
    _WorkerLifecycleRow(
        "active_wait",
        "active",
        "wait",
        status_alias=True,
        status_priority=1,
        source_transitions=(
            WorkerTransitionName.PROVISIONED,
            WorkerTransitionName.ACTIVE,
            WorkerTransitionName.ATTEMPT_STARTED,
            WorkerTransitionName.FAILED,
            WorkerTransitionName.DEPENDENCY_BLOCKED,
            WorkerTransitionName.ABORTED,
            WorkerTransitionName.RETRY_SCHEDULED,
            WorkerTransitionName.TIMED_OUT,
            WorkerTransitionName.RESULT_APPLIED,
        ),
        target_transitions=(WorkerTransitionName.ACTIVE,),
    ),
    _WorkerLifecycleRow(
        "active_retry",
        "active",
        "retry",
        retryable=True,
        executable=True,
        status_priority=1,
        source_transitions=(
            WorkerTransitionName.PROVISIONED,
            WorkerTransitionName.ACTIVE,
            WorkerTransitionName.FAILED,
            WorkerTransitionName.DEPENDENCY_BLOCKED,
            WorkerTransitionName.ABORTED,
        ),
        target_transitions=(WorkerTransitionName.RETRY_SCHEDULED,),
    ),
    _WorkerLifecycleRow(
        "blocked_dependency",
        "blocked",
        "resolve_blocker",
        status_alias=True,
        failed_dependency_status=True,
        status_priority=2,
        source_transitions=(
            WorkerTransitionName.ACTIVE,
            WorkerTransitionName.ABORTED,
        ),
        target_transitions=(
            WorkerTransitionName.DEPENDENCY_BLOCKED,
            WorkerTransitionName.RESULT_APPLIED,
        ),
    ),
    _WorkerLifecycleRow(
        "blocked_timeout",
        "blocked",
        "resolve_blocker",
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=2,
        source_transitions=(
            WorkerTransitionName.ACTIVE,
            WorkerTransitionName.ABORTED,
        ),
        target_transitions=(WorkerTransitionName.TIMED_OUT,),
    ),
    _WorkerLifecycleRow(
        "done_collect",
        "done",
        "collect",
        status_alias=True,
        terminal_status=True,
        status_priority=3,
        target_transitions=(WorkerTransitionName.RESULT_APPLIED,),
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
        source_transitions=(
            WorkerTransitionName.PROVISIONED,
            WorkerTransitionName.ACTIVE,
            WorkerTransitionName.FAILED,
            WorkerTransitionName.ABORTED,
            WorkerTransitionName.RETRY_SCHEDULED,
        ),
        target_transitions=(WorkerTransitionName.FAILED,),
    ),
    _WorkerLifecycleRow(
        "failed_terminal",
        "failed",
        "retry",
        status_alias=True,
        terminal_status=True,
        failed_dependency_status=True,
        status_priority=6,
        target_transitions=(
            WorkerTransitionName.FAILED,
            WorkerTransitionName.RESULT_APPLIED,
        ),
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
        source_transitions=(
            WorkerTransitionName.PROVISIONED,
            WorkerTransitionName.ACTIVE,
            WorkerTransitionName.FAILED,
            WorkerTransitionName.ABORTED,
            WorkerTransitionName.RETRY_SCHEDULED,
        ),
        target_transitions=(WorkerTransitionName.TIMED_OUT,),
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
        target_transitions=(
            WorkerTransitionName.TIMED_OUT,
            WorkerTransitionName.RESULT_APPLIED,
        ),
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
        source_transitions=(
            WorkerTransitionName.PROVISIONED,
            WorkerTransitionName.ACTIVE,
            WorkerTransitionName.FAILED,
            WorkerTransitionName.ABORTED,
            WorkerTransitionName.RETRY_SCHEDULED,
        ),
        target_transitions=(WorkerTransitionName.TIMED_OUT,),
    ),
    _WorkerLifecycleRow(
        "timeout_failed_terminal",
        "failed",
        "retry",
        terminal_status=True,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=6,
        target_transitions=(WorkerTransitionName.TIMED_OUT,),
    ),
    _WorkerLifecycleRow(
        "timeout_aborted",
        "aborted",
        "none",
        terminal_status=True,
        failed_dependency_status=True,
        timeout_origin=True,
        status_priority=5,
        source_transitions=(WorkerTransitionName.ABORTED,),
        target_transitions=(WorkerTransitionName.TIMED_OUT,),
    ),
    _WorkerLifecycleRow(
        "aborted",
        "aborted",
        "none",
        status_alias=True,
        terminal_status=True,
        failed_dependency_status=True,
        status_priority=5,
        source_transitions=(WorkerTransitionName.ABORTED,),
        target_transitions=(
            WorkerTransitionName.ABORTED,
            WorkerTransitionName.RESULT_APPLIED,
        ),
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


_WorkerTransitionPayloadType = Type[object]
_WorkerTransitionPayloadFactory = Callable[..., "WorkerTransitionPayload"]
_WorkerTransitionApplier = Callable[[object, "WorkerTransition"], dict]
_WorkerTransitionTargetResolver = Callable[["WorkerTransition"], Optional[str]]
_WorkerTransitionLegalityChecker = Callable[[object, "WorkerTransition"], bool]


@dataclass(frozen=True)
class WorkerTransitionSpec:
    name: WorkerTransitionName
    source_states: frozenset
    payload_type: _WorkerTransitionPayloadType
    payload_factory: _WorkerTransitionPayloadFactory
    applier: _WorkerTransitionApplier
    target_states: frozenset = frozenset()
    target_resolver: Optional[_WorkerTransitionTargetResolver] = None
    public_lifecycle_transition: bool = True
    legality_checker: Optional[_WorkerTransitionLegalityChecker] = None

    def __post_init__(self):
        if not isinstance(self.name, WorkerTransitionName):
            raise ValueError(f"unknown worker transition: {self.name}")
        object.__setattr__(self, "source_states", frozenset(self.source_states))
        object.__setattr__(self, "target_states", frozenset(self.target_states))
        if self.payload_type is None or not isinstance(self.payload_type, type):
            raise ValueError(f"worker transition '{self.name.value}' missing payload type")
        if self.payload_factory is None or not callable(self.payload_factory):
            raise ValueError(f"worker transition '{self.name.value}' missing payload factory")
        if self.applier is None or not callable(self.applier):
            raise ValueError(f"worker transition '{self.name.value}' missing applier")
        if self.target_resolver is not None and not callable(self.target_resolver):
            raise ValueError(f"worker transition '{self.name.value}' target resolver must be callable")
        if self.legality_checker is not None and not callable(self.legality_checker):
            raise ValueError(f"worker transition '{self.name.value}' legality checker must be callable")

    @property
    def metadata(self):
        return WorkerTransitionMetadata(
            self.name,
            self.source_states,
            self.target_states,
            public_lifecycle_transition=self.public_lifecycle_transition,
        )

    def build_payload(self, *args, **kwargs):
        return self.payload_factory(*args, **kwargs)

    def target_lifecycle_state(self, transition):
        if self.target_resolver is None:
            return None
        target_state = self.target_resolver(transition)
        if target_state is not None and self.target_states and target_state not in self.target_states:
            raise ValueError(
                f"worker transition '{self.name.value}' resolved unknown target lifecycle state: {target_state}"
            )
        return target_state

    def is_legal(self, latest_worker, transition):
        if self.legality_checker is not None:
            return self.legality_checker(latest_worker, transition)
        return worker_lifecycle_state(latest_worker) in self.source_states

    def apply_payload(self, reducer, transition):
        _require_transition_payload(transition, self.payload_type)
        return self.applier(reducer, transition)


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
        source_transitions=row.source_transitions,
        target_transitions=row.target_transitions,
    )


def _transition_spec(
    name,
    *,
    source_states=(),
    target_states=(),
    payload_type=None,
    payload_factory=None,
    applier=None,
    target_resolver=None,
    public_lifecycle_transition=True,
    legality_checker=None,
):
    return WorkerTransitionSpec(
        name,
        source_states,
        payload_type,
        payload_factory,
        applier,
        target_states=target_states,
        target_resolver=target_resolver,
        public_lifecycle_transition=public_lifecycle_transition,
        legality_checker=legality_checker,
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
WORKER_LIFECYCLE_STATES = frozenset(WORKER_LIFECYCLE_METADATA)
WORKER_TIMEOUT_ORIGIN_LIFECYCLE_STATES = _lifecycle_states_matching(timeout_origin=True)
WORKER_LIFECYCLE_STATE_BY_STATUS_ALIAS = _status_aliases_by_lifecycle_metadata()
WORKER_STATUS_PRIORITY_BY_STATUS = _status_values_by_lifecycle_metadata("status_priority")


def _lifecycle_source_states_for_transition(transition_name):
    transition_name = WorkerTransitionName(transition_name)
    return frozenset(
        lifecycle_state
        for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items()
        if transition_name in metadata.source_transitions
    )


def _lifecycle_target_states_for_transition(transition_name):
    transition_name = WorkerTransitionName(transition_name)
    return frozenset(
        lifecycle_state
        for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items()
        if transition_name in metadata.target_transitions
    )


WORKER_RETRYABLE_LIFECYCLE_STATES = _lifecycle_states_matching(retryable=True)
WORKER_BLOCKED_LIFECYCLE_STATES = _lifecycle_states_matching(status=WORKER_STATUS_BLOCKED)
WORKER_ABORTED_LIFECYCLE_STATES = _lifecycle_states_matching(status=WORKER_STATUS_ABORTED)
WORKER_FAILED_TARGET_LIFECYCLE_STATES = _lifecycle_target_states_for_transition(WorkerTransitionName.FAILED)
WORKER_RETRY_SCHEDULE_SOURCE_LIFECYCLE_STATES = _lifecycle_source_states_for_transition(
    WorkerTransitionName.RETRY_SCHEDULED
)

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
    record = WorkerRecord.from_worker(worker, worker_id).to_worker()
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


def _active_transition_lifecycle_state(transition):
    return worker_lifecycle_state_for_status_alias(WORKER_STATUS_ACTIVE)


def _failed_transition_lifecycle_state(transition):
    payload = _require_transition_payload(transition, _FailedTransition)
    return worker_failed_lifecycle_state(retryable=payload.retryable, retry_available=payload.retry_available)


def _dependency_blocked_transition_lifecycle_state(transition):
    return worker_lifecycle_state_for_status_alias(WORKER_STATUS_BLOCKED)


def _aborted_transition_lifecycle_state(transition):
    return worker_lifecycle_state_for_status_alias(WORKER_STATUS_ABORTED)


def _retry_scheduled_transition_lifecycle_state(transition):
    return worker_lifecycle_state_for_public_state(WORKER_STATUS_ACTIVE, WORKER_ACTION_RETRY)


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


def _snapshot_applied_transition_payload(worker):
    return _SnapshotAppliedTransition(
        deepcopy(_worker_fields(worker)),
        state_fields=tuple(WORKER_SNAPSHOT_STATE_FIELDS),
        set_if_missing_fields=("session_id",),
        removable_fields=tuple(REMOVABLE_WORKER_TRANSITION_FIELDS),
    )


def _snapshot_worker_id(worker):
    if isinstance(worker, WorkerRecord):
        return worker.worker_id
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


WORKER_TRANSITION_DEFINITIONS = {
    spec.name: spec
    for spec in (
        _transition_spec(
            WorkerTransitionName.PROVISIONED,
            source_states=_lifecycle_source_states_for_transition(WorkerTransitionName.PROVISIONED),
            payload_type=_ProvisionedTransition,
            payload_factory=_provisioned_transition_payload,
            applier=_apply_provisioned_transition,
        ),
        _transition_spec(
            WorkerTransitionName.ACTIVE,
            source_states=_lifecycle_source_states_for_transition(WorkerTransitionName.ACTIVE),
            target_states=_lifecycle_target_states_for_transition(WorkerTransitionName.ACTIVE),
            target_resolver=_active_transition_lifecycle_state,
            payload_type=_ActiveTransition,
            payload_factory=_active_transition_payload,
            applier=_apply_active_transition,
        ),
        _transition_spec(
            WorkerTransitionName.ATTEMPT_STARTED,
            source_states=_lifecycle_source_states_for_transition(WorkerTransitionName.ATTEMPT_STARTED),
            payload_type=_AttemptStartedTransition,
            payload_factory=_attempt_started_transition_payload,
            applier=_apply_attempt_started_transition,
        ),
        _transition_spec(
            WorkerTransitionName.FAILED,
            source_states=_lifecycle_source_states_for_transition(WorkerTransitionName.FAILED),
            target_states=_lifecycle_target_states_for_transition(WorkerTransitionName.FAILED),
            target_resolver=_failed_transition_lifecycle_state,
            payload_type=_FailedTransition,
            payload_factory=_failed_transition_payload,
            applier=_apply_failed_transition,
        ),
        _transition_spec(
            WorkerTransitionName.DEPENDENCY_BLOCKED,
            source_states=_lifecycle_source_states_for_transition(WorkerTransitionName.DEPENDENCY_BLOCKED),
            target_states=_lifecycle_target_states_for_transition(WorkerTransitionName.DEPENDENCY_BLOCKED),
            target_resolver=_dependency_blocked_transition_lifecycle_state,
            payload_type=_DependencyBlockedTransition,
            payload_factory=_dependency_blocked_transition_payload,
            applier=_apply_dependency_blocked_transition,
        ),
        _transition_spec(
            WorkerTransitionName.ABORTED,
            source_states=_lifecycle_source_states_for_transition(WorkerTransitionName.ABORTED),
            target_states=_lifecycle_target_states_for_transition(WorkerTransitionName.ABORTED),
            target_resolver=_aborted_transition_lifecycle_state,
            payload_type=_AbortedTransition,
            payload_factory=_aborted_transition_payload,
            applier=_apply_aborted_transition,
        ),
        _transition_spec(
            WorkerTransitionName.RETRY_SCHEDULED,
            source_states=_lifecycle_source_states_for_transition(WorkerTransitionName.RETRY_SCHEDULED),
            target_states=_lifecycle_target_states_for_transition(WorkerTransitionName.RETRY_SCHEDULED),
            target_resolver=_retry_scheduled_transition_lifecycle_state,
            payload_type=_RetryScheduledTransition,
            payload_factory=_retry_scheduled_transition_payload,
            applier=_apply_retry_scheduled_transition,
        ),
        _transition_spec(
            WorkerTransitionName.TIMED_OUT,
            source_states=_lifecycle_source_states_for_transition(WorkerTransitionName.TIMED_OUT),
            target_states=_lifecycle_target_states_for_transition(WorkerTransitionName.TIMED_OUT),
            target_resolver=_timed_out_transition_lifecycle_state,
            payload_type=_TimedOutTransition,
            payload_factory=_timed_out_transition_payload,
            applier=_apply_timed_out_transition,
        ),
        _transition_spec(
            WorkerTransitionName.RESULT_APPLIED,
            source_states=_lifecycle_source_states_for_transition(WorkerTransitionName.RESULT_APPLIED),
            target_states=_lifecycle_target_states_for_transition(WorkerTransitionName.RESULT_APPLIED),
            target_resolver=_result_applied_transition_lifecycle_state,
            payload_type=_ResultAppliedTransition,
            payload_factory=_result_applied_transition_payload,
            applier=_apply_result_applied_transition,
        ),
        _transition_spec(
            WorkerTransitionName.CLEANUP_UPDATED,
            source_states=WORKER_LIFECYCLE_STATES,
            payload_type=_CleanupUpdatedTransition,
            payload_factory=_cleanup_updated_transition_payload,
            applier=_apply_cleanup_updated_transition,
            public_lifecycle_transition=False,
        ),
        _transition_spec(
            WorkerTransitionName.SNAPSHOT_APPLIED,
            source_states=WORKER_LIFECYCLE_STATES,
            payload_type=_SnapshotAppliedTransition,
            payload_factory=_snapshot_applied_transition_payload,
            applier=_apply_snapshot_applied_transition,
            public_lifecycle_transition=False,
            legality_checker=_snapshot_transition_is_legal,
        ),
    )
}

WORKER_TRANSITION_METADATA = {
    transition_name: spec.metadata for transition_name, spec in WORKER_TRANSITION_DEFINITIONS.items()
}


_WORKER_SNAPSHOT_TARGET_STATES_BY_SOURCE = {
    source_state: frozenset(
        {
            source_state,
            *(
                target_state
                for spec in WORKER_TRANSITION_DEFINITIONS.values()
                if source_state in spec.source_states
                for target_state in spec.target_states
            ),
        }
    )
    for source_state in WORKER_LIFECYCLE_STATES
}


def _worker_transition_spec(name):
    if not isinstance(name, WorkerTransitionName):
        raise ValueError(f"unknown worker transition: {name}")
    spec = WORKER_TRANSITION_DEFINITIONS.get(name)
    if spec is None:
        raise ValueError(f"unknown worker transition: {name}")
    return spec


def _worker_transition_metadata(name):
    spec = _worker_transition_spec(name)
    return WORKER_TRANSITION_METADATA[spec.name]


def worker_transition_target_lifecycle_state(transition):
    return _worker_transition_spec(transition.name).target_lifecycle_state(transition)


def worker_transition_is_legal(latest_worker, transition):
    return _worker_transition_spec(transition.name).is_legal(latest_worker, transition)


def apply_worker_transition_payload(reducer, transition):
    return _worker_transition_spec(transition.name).apply_payload(reducer, transition)


def worker_lifecycle_set_fields(worker_id, lifecycle_state):
    return {"id": worker_id, "lifecycle_state": lifecycle_state}


def _is_worker_record(worker):
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


def worker_field(worker, field_name, default=None):
    """Compatibility accessor for dynamic persisted fields; core invariants use WorkerRecord properties."""
    return _require_worker_record(worker).field(field_name, default)


def worker_has_field(worker, field_name):
    return _require_worker_record(worker).has_field(field_name)


def is_worker_record(worker):
    return _is_worker_record(worker)


def worker_retry_available(worker, category=None):
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
    if isinstance(worker, WorkerRecord):
        return worker.lifecycle_state
    if isinstance(worker, Mapping):
        lifecycle_state = worker.get("lifecycle_state")
        if lifecycle_state in WORKER_LIFECYCLE_STATES:
            return lifecycle_state
    return WORKER_LIFECYCLE_QUEUED


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

    def _raw_field(self, field_name: str, default: object = None) -> object:
        return self._fields.get(field_name, default)

    def _list_field(self, field_name: str) -> list:
        value = self._raw_field(field_name)
        return value if isinstance(value, list) else []

    def field(self, field_name: str, default: object = None) -> object:
        if field_name == "id":
            return self.worker_id
        if field_name == "lifecycle_state":
            return self.lifecycle_state
        return self._raw_field(field_name, default)

    def has_field(self, field_name: str) -> bool:
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
    def worker_id(self) -> str:
        return self._fields.get("id") or self._worker_id

    @property
    def lifecycle_state(self) -> str:
        lifecycle_state = self._raw_field("lifecycle_state")
        if lifecycle_state in WORKER_LIFECYCLE_STATES:
            return lifecycle_state
        return WORKER_LIFECYCLE_QUEUED

    @property
    def role(self) -> object:
        return self._raw_field("role")

    @property
    def session_id(self) -> object:
        return self._raw_field("session_id")

    @property
    def agent(self) -> object:
        return self._raw_field("agent")

    @property
    def model(self) -> object:
        return self._raw_field("model")

    @property
    def prompt(self) -> object:
        return self._raw_field("prompt")

    @property
    def prompt_ids(self) -> list:
        return self._list_field("prompt_ids")

    @property
    def retry_count(self) -> int:
        return int(self._raw_field("retry_count") or 0)

    @property
    def retry_limit(self) -> int:
        return int(self._raw_field("retry_limit") or 0)

    @property
    def retryable_failures(self) -> list:
        return self._list_field("retryable_failures")

    @property
    def failure_retryable(self) -> object:
        return self._raw_field("failure_retryable")

    @property
    def failure_category(self) -> object:
        return self._raw_field("failure_category")

    @property
    def last_failure_category(self) -> object:
        return self._raw_field("last_failure_category")

    @property
    def timeout_seconds(self) -> object:
        return self._raw_field("timeout_seconds")

    @property
    def timeout_policy(self) -> object:
        return self._raw_field("timeout_policy") or WORKER_STATUS_TIMEOUT

    @property
    def timeout_started_at(self) -> object:
        return self._raw_field("timeout_started_at")

    @property
    def output_refs(self) -> list:
        return self._list_field("output_refs")

    @property
    def cleanup(self) -> object:
        return self._raw_field("cleanup")

    @property
    def abort(self) -> object:
        return self._raw_field("abort")

    @property
    def has_prompt(self) -> bool:
        prompt = self.prompt
        return prompt is not None and bool(str(prompt))

    def retry_available(self, category=None) -> bool:
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
        prompt_ids = list(self.prompt_ids)
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
        self._worker_id = self.worker_id or self._worker_id or transition.worker_id
        return self

    def ensure_cleanup(self):
        cleanup = self.cleanup
        if not isinstance(cleanup, dict):
            cleanup = {"requested": True, "deleted": False}
            self.set_field("cleanup", cleanup)
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
            _worker_transition_spec(name).build_payload(*payload_args, **payload_kwargs),
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
    def snapshot_applied(cls, worker):
        worker_id = _snapshot_worker_id(worker)
        return cls.create(WorkerTransitionName.SNAPSHOT_APPLIED, worker_id, worker)


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
        abort = worker.abort
        lifecycle_state = worker.lifecycle_state
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
    return default_worker_record(worker_id)


def normalize_worker(worker, worker_id):
    return deserialize_worker_record(worker, worker_id)


def normalize_worker_snapshot(worker, worker_id):
    return serialize_worker_snapshot(worker, worker_id)


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
    if not worker.role:
        worker.set_field("role", role)
    worker.set_field("id", worker_id)
    workers[worker_id] = worker
    return worker


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
    return worker.timeout_started_at if worker.has_field("timeout_started_at") else UNSET_TRANSITION_FIELD


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
