from typing import List, Optional, Protocol, TypedDict

from opencode_session.schema_helpers import JsonObject, JsonValue
from opencode_session.worker_field_spec import (
    WORKER_REQUIRED_FIELD_NAMES as _WORKER_REQUIRED_FIELD_NAMES,
    worker_required_schema_annotations,
    worker_snapshot_schema_annotations,
)


class WorkerAttemptRecord(TypedDict, total=False):
    id: str
    session_id: Optional[str]
    created_session_ids: List[str]
    status: str
    started_at: JsonValue
    finished_at: JsonValue
    failure_category: Optional[str]
    error: str
    result_status: str
    user_message_id: str
    assistant_message_id: str


def _worker_typed_dict(name, annotations, *, total=False, required_field_names=()):
    typed_dict = TypedDict(name, annotations, total=total)
    if required_field_names:
        required_keys = frozenset(required_field_names)
        typed_dict.__required_keys__ = required_keys
        typed_dict.__optional_keys__ = frozenset(
            field_name for field_name in typed_dict.__annotations__ if field_name not in required_keys
        )
    return typed_dict


# Persisted snapshots are intentionally sparse JSON. The storage boundary hydrates
# them into worker_state.WorkerRecord before core orchestration code mutates them.
WorkerSnapshotRecord = TypedDict(
    "WorkerSnapshotRecord",
    worker_snapshot_schema_annotations(),
    total=False,
)
WorkerRequiredFields = TypedDict(
    "WorkerRequiredFields",
    worker_required_schema_annotations(),
)
Worker = _worker_typed_dict(
    "Worker",
    worker_snapshot_schema_annotations(),
    total=False,
    required_field_names=_WORKER_REQUIRED_FIELD_NAMES,
)


class WorkerTransitionRecord(Protocol):
    """Schema-side transition shape; avoids importing worker_state at this boundary."""

    @property
    def worker_id(self) -> str: ...

    @property
    def name(self) -> str: ...


class HydratedWorker(Protocol):
    """Runtime worker record hydrated from a persisted snapshot."""

    @property
    def worker_id(self) -> str: ...

    @property
    def has_prompt(self) -> bool: ...

    def retry_available(self, category: Optional[str] = None) -> bool: ...

    def apply_transition(self, transition: WorkerTransitionRecord) -> "HydratedWorker": ...

    def remember_prompt_id(self, prompt_id: str) -> "HydratedWorker": ...

    def set_session(
        self,
        session_id: Optional[str],
        *,
        agent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> "HydratedWorker": ...

    def to_snapshot(self) -> WorkerSnapshotRecord: ...

    def to_output_dict(self) -> "WorkerOutputRecord": ...


WORKER_REQUIRED_FIELD_NAMES = _WORKER_REQUIRED_FIELD_NAMES


WorkerOutputRecord = _worker_typed_dict(
    "WorkerOutputRecord",
    {
        **worker_snapshot_schema_annotations(),
        "status": "str",
        "next_eligible_action": "str",
    },
    total=False,
    required_field_names=WORKER_REQUIRED_FIELD_NAMES,
)
