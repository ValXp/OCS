from typing import List, Optional, Protocol, TypedDict, Union

from opencode_session.schema_helpers import JsonObject, JsonValue


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


# Persisted snapshots are intentionally sparse JSON. The storage boundary hydrates
# them into worker_state.WorkerRecord before core orchestration code mutates them.
class WorkerSnapshotRecord(TypedDict, total=False):
    id: str
    name: str
    title: str
    role: Optional[str]
    session_id: Optional[str]
    agent: Optional[str]
    model: Optional[str]
    prompt: str
    lifecycle_state: str
    dependencies: List[str]
    prompt_ids: List[str]
    retryable_failures: List[str]
    blockers: List[str]
    output_refs: List[str]
    attempts: List[WorkerAttemptRecord]
    retry_count: int
    retry_limit: int
    timeout_seconds: Optional[float]
    timeout_policy: str
    timeout_started_at: JsonValue
    timed_out_at: JsonValue
    failure_category: Optional[str]
    failure_reason: Optional[str]
    last_failure_category: Optional[str]
    last_failure_reason: Optional[str]
    error: str
    failure_retryable: bool
    manual_retry_required: bool
    cleanup: JsonObject
    abort: JsonObject


class WorkerRequiredFields(TypedDict):
    id: str
    role: Optional[str]
    session_id: Optional[str]
    agent: Optional[str]
    model: Optional[str]
    lifecycle_state: str
    dependencies: List[str]
    prompt_ids: List[str]
    retry_count: int
    retry_limit: int
    retryable_failures: List[str]
    timeout_seconds: Optional[float]
    timeout_policy: str
    timeout_started_at: JsonValue
    timed_out_at: JsonValue
    failure_category: Optional[str]
    failure_reason: Optional[str]
    last_failure_category: Optional[str]
    last_failure_reason: Optional[str]
    blockers: List[str]
    output_refs: List[str]


class Worker(WorkerRequiredFields, total=False):
    name: str
    title: str
    prompt: str
    error: str
    failure_retryable: bool
    manual_retry_required: bool
    cleanup: JsonObject
    abort: JsonObject
    attempts: List[WorkerAttemptRecord]
    result: JsonObject


class HydratedWorker(Protocol):
    """Runtime worker record hydrated from a persisted snapshot."""

    @property
    def worker_id(self) -> str: ...

    @property
    def lifecycle_state(self) -> str: ...

    @property
    def role(self) -> object: ...

    @property
    def session_id(self) -> object: ...

    @property
    def agent(self) -> object: ...

    @property
    def model(self) -> object: ...

    @property
    def prompt(self) -> object: ...

    @property
    def prompt_ids(self) -> List[object]: ...

    @property
    def retry_count(self) -> int: ...

    @property
    def retry_limit(self) -> int: ...

    @property
    def retryable_failures(self) -> List[object]: ...

    @property
    def failure_retryable(self) -> object: ...

    @property
    def failure_category(self) -> object: ...

    @property
    def last_failure_category(self) -> object: ...

    @property
    def timeout_seconds(self) -> object: ...

    @property
    def timeout_policy(self) -> object: ...

    @property
    def timeout_started_at(self) -> object: ...

    @property
    def output_refs(self) -> List[object]: ...

    @property
    def cleanup(self) -> object: ...

    @property
    def abort(self) -> object: ...

    @property
    def has_prompt(self) -> bool: ...

    def retry_available(self, category: object = None) -> bool: ...

    def field(self, field_name: str, default: object = None) -> object: ...

    def set_field(self, field_name: str, value: object) -> "HydratedWorker": ...

    def merge_fields(self, fields: Union[WorkerSnapshotRecord, Worker, None] = None, **kwargs: object) -> "HydratedWorker": ...

    def apply_transition(self, transition: object) -> "HydratedWorker": ...

    def remember_prompt_id(self, prompt_id: Optional[str]) -> "HydratedWorker": ...

    def set_session(
        self,
        session_id: Optional[str],
        *,
        agent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> "HydratedWorker": ...

    def to_public_dict(self) -> Worker: ...

    def to_snapshot(self) -> WorkerSnapshotRecord: ...


WORKER_REQUIRED_FIELD_NAMES = tuple(WorkerRequiredFields.__annotations__)


class WorkerOutputRecord(Worker, total=False):
    status: str
    next_eligible_action: str
