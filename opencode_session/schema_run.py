from typing import Dict, List, Optional, TypedDict

from opencode_session.schema_helpers import JsonValue
from opencode_session.schema_worker import HydratedWorker, WorkerSnapshotRecord


class PersistedRunRecord(TypedDict, total=False):
    schema_version: int
    name: str
    run_id: str
    directory: str
    server_url: str
    status: str
    retry_count: int
    timeout_seconds: Optional[float]
    blockers: List[str]
    output_refs: List[str]
    workers: Dict[str, WorkerSnapshotRecord]
    resources: Dict[str, JsonValue]
    resource_cleanup: Dict[str, JsonValue]
    created_at: JsonValue
    updated_at: JsonValue


class HydratedRunRecord(TypedDict, total=False):
    schema_version: int
    name: str
    run_id: str
    directory: str
    server_url: str
    status: str
    retry_count: int
    timeout_seconds: Optional[float]
    blockers: List[str]
    output_refs: List[str]
    workers: Dict[str, HydratedWorker]
    resources: Dict[str, JsonValue]
    resource_cleanup: Dict[str, JsonValue]
    created_at: JsonValue
    updated_at: JsonValue


RunRecord = HydratedRunRecord
