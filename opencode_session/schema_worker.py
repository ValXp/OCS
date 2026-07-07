from typing import List, Optional, Protocol, TypedDict

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
    dependencies: List[str]
    prompt_ids: List[str]
    retry_count: int
    retry_limit: int
    retryable_failures: List[str]
    timeout_seconds: Optional[float]
    timeout_policy: str
    timeout_started_at: JsonValue
    timed_out_at: JsonValue
    lifecycle_state: str
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
    def role(self) -> Optional[str]: ...

    @property
    def name(self) -> Optional[str]: ...

    @property
    def title(self) -> Optional[str]: ...

    @property
    def session_id(self) -> Optional[str]: ...

    @property
    def agent(self) -> Optional[str]: ...

    @property
    def model(self) -> Optional[str]: ...

    @property
    def prompt(self) -> Optional[str]: ...

    @property
    def dependencies(self) -> List[str]: ...

    @property
    def prompt_ids(self) -> List[str]: ...

    @property
    def retry_count(self) -> int: ...

    @property
    def retry_limit(self) -> int: ...

    @property
    def retryable_failures(self) -> List[str]: ...

    @property
    def failure_retryable(self) -> Optional[bool]: ...

    @property
    def failure_category(self) -> Optional[str]: ...

    @property
    def failure_reason(self) -> Optional[str]: ...

    @property
    def last_failure_category(self) -> Optional[str]: ...

    @property
    def last_failure_reason(self) -> Optional[str]: ...

    @property
    def blockers(self) -> List[str]: ...

    @property
    def timeout_seconds(self) -> Optional[float]: ...

    @property
    def timeout_policy(self) -> str: ...

    @property
    def timeout_started_at(self) -> JsonValue: ...

    @property
    def timed_out_at(self) -> JsonValue: ...

    @property
    def output_refs(self) -> List[str]: ...

    @property
    def error(self) -> Optional[str]: ...

    @property
    def manual_retry_required(self) -> Optional[bool]: ...

    @property
    def result(self) -> Optional[JsonObject]: ...

    @property
    def attempts(self) -> List[WorkerAttemptRecord]: ...

    @property
    def cleanup(self) -> Optional[JsonObject]: ...

    @property
    def abort(self) -> Optional[JsonObject]: ...

    @property
    def has_prompt(self) -> bool: ...

    def retry_available(self, category: object = None) -> bool: ...

    def apply_transition(self, transition: object) -> "HydratedWorker": ...

    def remember_prompt_id(self, prompt_id: Optional[str]) -> "HydratedWorker": ...

    def set_session(
        self,
        session_id: Optional[str],
        *,
        agent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> "HydratedWorker": ...

    def to_snapshot(self) -> WorkerSnapshotRecord: ...

    def to_output_dict(self) -> "WorkerOutputRecord": ...


WORKER_REQUIRED_FIELD_NAMES = tuple(WorkerRequiredFields.__annotations__)


class WorkerOutputRecord(Worker, total=False):
    status: str
    next_eligible_action: str
