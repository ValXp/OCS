from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from typing import Literal

from opencode_session.schema_helpers import JsonValue


WorkerFieldValidatorName = Literal[
    "any",
    "id",
    "int",
    "lifecycle_state",
    "list",
    "timeout_seconds",
    "timeout_policy",
]
WORKER_FIELD_VALIDATOR_NAMES = frozenset(
    (
        "any",
        "id",
        "int",
        "lifecycle_state",
        "list",
        "timeout_seconds",
        "timeout_policy",
    )
)
WORKER_FIELD_LIFECYCLE_STATES = (
    "queued",
    "active_wait",
    "active_retry",
    "blocked_dependency",
    "blocked_timeout",
    "done_collect",
    "failed_retry",
    "failed_terminal",
    "timeout_retry",
    "timeout_terminal",
    "timeout_failed_retry",
    "timeout_failed_terminal",
    "timeout_aborted",
    "aborted",
)
WORKER_FIELD_TIMEOUT_POLICY_STATUSES = (
    "timeout",
    "blocked",
    "failed",
    "aborted",
)
_ACCEPTED_VALUE_VALIDATOR_NAMES = frozenset(("lifecycle_state", "timeout_policy"))


@dataclass(frozen=True)
class WorkerFieldSpec:
    """Canonical metadata for worker fields and their boundary projections."""

    name: str
    schema_annotation: str
    default: JsonValue = None
    required: bool = True
    validator: WorkerFieldValidatorName = "any"
    accepted_values: tuple = ()
    default_from_worker_id: bool = False
    record_update: bool = False
    run_upsert: bool = False
    removable_transition_field: bool = False
    snapshot_replay_field: bool = False
    snapshot_set_if_missing: bool = False
    snapshot_accepted_abort_passthrough: bool = False
    snapshot_prompt_ids: bool = False

    def __post_init__(self):
        if self.validator not in WORKER_FIELD_VALIDATOR_NAMES:
            raise ValueError(f"unknown worker field validator: {self.validator}")
        if self.validator in _ACCEPTED_VALUE_VALIDATOR_NAMES and not self.accepted_values:
            raise ValueError(f"worker field {self.name} requires accepted_values")

    def default_value(self, worker_id):
        if self.default_from_worker_id:
            return worker_id
        return deepcopy(self.default)

    def canonical_value(self, value):
        return _WORKER_FIELD_VALIDATORS[self.validator](self, deepcopy(value))

    def storage_value(self, value):
        coercer = _WORKER_FIELD_STORAGE_COERCERS.get(self.validator)
        if coercer is None:
            return self.canonical_value(value)
        return coercer(self, deepcopy(value))


def _canonical_any(spec, value):
    return value


def _canonical_id(spec, value):
    if not isinstance(value, str) or not value:
        raise ValueError("worker id must be a non-empty string")
    return value


def _canonical_int(spec, value):
    if type(value) is not int:
        raise TypeError(f"worker {spec.name} must be an int")
    return value


def _canonical_list(spec, value):
    if not isinstance(value, list):
        raise TypeError(f"worker {spec.name} must be a list")
    return value


def _canonical_accepted_value(spec, value):
    if value not in spec.accepted_values:
        raise ValueError(f"worker {spec.name} must be canonical: {value}")
    return value


def _canonical_timeout_seconds(spec, value):
    if value is None:
        return None
    if type(value) not in (int, float):
        raise TypeError(f"worker {spec.name} must be a positive number or None")
    if not isfinite(value) or value <= 0:
        raise ValueError(f"worker {spec.name} must be a positive number or None")
    if type(value) is float and value.is_integer():
        return int(value)
    return value


def _coerce_storage_timeout_seconds(spec, value):
    if value is None or isinstance(value, bool):
        return None
    if type(value) in (int, float):
        timeout_seconds = value
    else:
        try:
            timeout_seconds = float(value)
        except (TypeError, ValueError):
            return None
    try:
        return _canonical_timeout_seconds(spec, timeout_seconds)
    except (TypeError, ValueError):
        return None


_WORKER_FIELD_VALIDATORS = {
    "any": _canonical_any,
    "id": _canonical_id,
    "int": _canonical_int,
    "lifecycle_state": _canonical_accepted_value,
    "list": _canonical_list,
    "timeout_seconds": _canonical_timeout_seconds,
    "timeout_policy": _canonical_accepted_value,
}
_WORKER_FIELD_STORAGE_COERCERS = {
    "timeout_seconds": _coerce_storage_timeout_seconds,
}


WORKER_FIELD_SPECS = (
    WorkerFieldSpec("id", "str", default_from_worker_id=True, validator="id"),
    WorkerFieldSpec("role", "Optional[str]", record_update=True, run_upsert=True),
    WorkerFieldSpec(
        "session_id",
        "Optional[str]",
        record_update=True,
        run_upsert=True,
        snapshot_set_if_missing=True,
    ),
    WorkerFieldSpec("agent", "Optional[str]", record_update=True, run_upsert=True),
    WorkerFieldSpec("model", "Optional[str]", record_update=True, run_upsert=True),
    WorkerFieldSpec(
        "dependencies",
        "List[str]",
        default=[],
        validator="list",
        record_update=True,
        run_upsert=True,
    ),
    WorkerFieldSpec(
        "prompt_ids",
        "List[str]",
        default=[],
        validator="list",
        record_update=True,
        run_upsert=True,
        snapshot_prompt_ids=True,
    ),
    WorkerFieldSpec(
        "retry_count",
        "int",
        default=0,
        validator="int",
        record_update=True,
        run_upsert=True,
        snapshot_replay_field=True,
    ),
    WorkerFieldSpec(
        "retry_limit",
        "int",
        default=0,
        validator="int",
        record_update=True,
        run_upsert=True,
    ),
    WorkerFieldSpec(
        "retryable_failures",
        "List[str]",
        default=[],
        validator="list",
        record_update=True,
        run_upsert=True,
    ),
    WorkerFieldSpec(
        "timeout_seconds",
        "Optional[float]",
        validator="timeout_seconds",
        record_update=True,
        run_upsert=True,
    ),
    WorkerFieldSpec(
        "timeout_policy",
        "str",
        default="timeout",
        validator="timeout_policy",
        accepted_values=WORKER_FIELD_TIMEOUT_POLICY_STATUSES,
        record_update=True,
        run_upsert=True,
    ),
    WorkerFieldSpec("timeout_started_at", "JsonValue", record_update=True, snapshot_replay_field=True),
    WorkerFieldSpec("timed_out_at", "JsonValue", record_update=True, snapshot_replay_field=True),
    WorkerFieldSpec(
        "lifecycle_state",
        "str",
        default="queued",
        validator="lifecycle_state",
        accepted_values=WORKER_FIELD_LIFECYCLE_STATES,
        record_update=True,
        run_upsert=True,
        snapshot_replay_field=True,
    ),
    WorkerFieldSpec("failure_category", "Optional[str]", record_update=True, snapshot_replay_field=True),
    WorkerFieldSpec("failure_reason", "Optional[str]", record_update=True, snapshot_replay_field=True),
    WorkerFieldSpec("last_failure_category", "Optional[str]", record_update=True, snapshot_replay_field=True),
    WorkerFieldSpec("last_failure_reason", "Optional[str]", record_update=True, snapshot_replay_field=True),
    WorkerFieldSpec(
        "blockers",
        "List[str]",
        default=[],
        validator="list",
        record_update=True,
        run_upsert=True,
        snapshot_replay_field=True,
    ),
    WorkerFieldSpec(
        "output_refs",
        "List[str]",
        default=[],
        validator="list",
        record_update=True,
        run_upsert=True,
        snapshot_replay_field=True,
    ),
    WorkerFieldSpec("name", "str", required=False),
    WorkerFieldSpec("title", "str", required=False),
    WorkerFieldSpec("prompt", "str", required=False, record_update=True, run_upsert=True),
    WorkerFieldSpec(
        "error",
        "str",
        required=False,
        record_update=True,
        removable_transition_field=True,
        snapshot_replay_field=True,
    ),
    WorkerFieldSpec(
        "failure_retryable",
        "bool",
        required=False,
        record_update=True,
        removable_transition_field=True,
        snapshot_replay_field=True,
    ),
    WorkerFieldSpec(
        "manual_retry_required",
        "bool",
        required=False,
        record_update=True,
        removable_transition_field=True,
        snapshot_replay_field=True,
    ),
    WorkerFieldSpec(
        "cleanup",
        "JsonObject",
        required=False,
        record_update=True,
        snapshot_replay_field=True,
        snapshot_accepted_abort_passthrough=True,
    ),
    WorkerFieldSpec("abort", "JsonObject", required=False, record_update=True, snapshot_replay_field=True),
    WorkerFieldSpec(
        "attempts",
        "List[WorkerAttemptRecord]",
        default=[],
        required=False,
        validator="list",
        record_update=True,
        snapshot_replay_field=True,
    ),
    WorkerFieldSpec("result", "JsonObject", required=False, record_update=True, snapshot_replay_field=True),
)
WORKER_FIELD_SPEC_BY_NAME = {spec.name: spec for spec in WORKER_FIELD_SPECS}
WORKER_RECORD_FIELD_NAMES = tuple(spec.name for spec in WORKER_FIELD_SPECS)
WORKER_REQUIRED_FIELD_NAMES = tuple(spec.name for spec in WORKER_FIELD_SPECS if spec.required)
WORKER_RECORD_OPTIONAL_FIELD_NAMES = tuple(spec.name for spec in WORKER_FIELD_SPECS if not spec.required)
WORKER_RECORD_CANONICAL_FIELD_NAMES = frozenset(WORKER_RECORD_FIELD_NAMES)
WORKER_LIST_FIELDS = tuple(
    spec.name for spec in WORKER_FIELD_SPECS if spec.required and spec.validator == "list"
)
WORKER_OPTIONAL_LIST_FIELDS = tuple(
    spec.name for spec in WORKER_FIELD_SPECS if not spec.required and spec.validator == "list"
)
WORKER_RECORD_UPDATE_FIELD_NAMES = tuple(spec.name for spec in WORKER_FIELD_SPECS if spec.record_update)
WORKER_RUN_UPSERT_FIELD_NAMES = tuple(spec.name for spec in WORKER_FIELD_SPECS if spec.run_upsert)
REMOVABLE_WORKER_TRANSITION_FIELDS = tuple(
    spec.name for spec in WORKER_FIELD_SPECS if spec.removable_transition_field
)
WORKER_STORAGE_INT_FIELD_NAMES = tuple(spec.name for spec in WORKER_FIELD_SPECS if spec.validator == "int")
WORKER_STORAGE_LIST_FIELD_NAMES = tuple(spec.name for spec in WORKER_FIELD_SPECS if spec.validator == "list")
WORKER_STORAGE_TIMEOUT_SECONDS_FIELD_NAMES = tuple(
    spec.name for spec in WORKER_FIELD_SPECS if spec.validator == "timeout_seconds"
)
WORKER_STORAGE_TIMEOUT_POLICY_FIELD_NAMES = tuple(
    spec.name for spec in WORKER_FIELD_SPECS if spec.validator == "timeout_policy"
)
WORKER_SNAPSHOT_REPLAY_FIELD_NAMES = tuple(
    spec.name for spec in WORKER_FIELD_SPECS if spec.snapshot_replay_field
)
WORKER_SNAPSHOT_SET_IF_MISSING_FIELD_NAMES = tuple(
    spec.name for spec in WORKER_FIELD_SPECS if spec.snapshot_set_if_missing
)
WORKER_SNAPSHOT_REMOVE_WHEN_ABSENT_FIELD_NAMES = REMOVABLE_WORKER_TRANSITION_FIELDS
WORKER_SNAPSHOT_ACCEPTED_ABORT_PASSTHROUGH_FIELD_NAMES = tuple(
    spec.name for spec in WORKER_FIELD_SPECS if spec.snapshot_accepted_abort_passthrough
)
WORKER_SNAPSHOT_PROMPT_ID_FIELD_NAMES = tuple(
    spec.name for spec in WORKER_FIELD_SPECS if spec.snapshot_prompt_ids
)


def worker_default_snapshot_fields(worker_id):
    return {
        spec.name: spec.default_value(worker_id)
        for spec in WORKER_FIELD_SPECS
        if spec.required
    }


def _worker_schema_annotations(specs):
    return {spec.name: spec.schema_annotation for spec in specs}


def worker_required_schema_annotations():
    return _worker_schema_annotations(spec for spec in WORKER_FIELD_SPECS if spec.required)


def worker_optional_schema_annotations():
    return _worker_schema_annotations(spec for spec in WORKER_FIELD_SPECS if not spec.required)


def worker_snapshot_schema_annotations():
    return _worker_schema_annotations(WORKER_FIELD_SPECS)
