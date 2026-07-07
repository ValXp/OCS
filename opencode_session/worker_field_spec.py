from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from opencode_session.schema_helpers import JsonValue


WorkerFieldValidatorName = Literal[
    "any",
    "id",
    "int",
    "lifecycle_state",
    "list",
    "timeout_policy",
]
WORKER_FIELD_VALIDATOR_NAMES = frozenset(
    (
        "any",
        "id",
        "int",
        "lifecycle_state",
        "list",
        "timeout_policy",
    )
)


@dataclass(frozen=True)
class WorkerFieldSpec:
    """Canonical metadata for worker fields and their boundary projections."""

    name: str
    schema_annotation: str
    default: JsonValue = None
    required: bool = True
    validator: WorkerFieldValidatorName = "any"
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

    def default_value(self, worker_id):
        if self.default_from_worker_id:
            return worker_id
        return deepcopy(self.default)


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
    WorkerFieldSpec("timeout_seconds", "Optional[float]", record_update=True, run_upsert=True),
    WorkerFieldSpec(
        "timeout_policy",
        "str",
        default="timeout",
        validator="timeout_policy",
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
