from collections.abc import Mapping
from copy import deepcopy

from opencode_session.worker_state import WorkerRecord, WorkerSnapshotTransitionPatch, WorkerTransition
from opencode_session.worker_storage_adapter import canonical_worker_snapshot_fields


STORAGE_WORKER_SNAPSHOT_STATE_FIELDS = (
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
STORAGE_WORKER_SNAPSHOT_SET_IF_MISSING_FIELDS = ("session_id",)
STORAGE_WORKER_SNAPSHOT_REMOVE_WHEN_ABSENT_FIELDS = (
    "error",
    "failure_retryable",
    "manual_retry_required",
)
STORAGE_WORKER_ACCEPTED_ABORT_PASSTHROUGH_FIELDS = ("cleanup",)


def worker_snapshot_transition_patch(worker, worker_id=None):
    worker_id = _snapshot_worker_id(worker, worker_id)
    snapshot = canonical_worker_snapshot_fields(worker, worker_id)
    fields = {"id": worker_id}
    for field_name in STORAGE_WORKER_SNAPSHOT_STATE_FIELDS:
        if field_name in snapshot:
            fields[field_name] = deepcopy(snapshot[field_name])
    return WorkerSnapshotTransitionPatch(
        worker_id,
        fields,
        target_lifecycle_state=fields.get("lifecycle_state"),
        prompt_ids=_optional_tuple(snapshot.get("prompt_ids")),
        set_if_missing_fields={
            field_name: deepcopy(snapshot[field_name])
            for field_name in STORAGE_WORKER_SNAPSHOT_SET_IF_MISSING_FIELDS
            if snapshot.get(field_name)
        },
        remove_fields=tuple(
            field_name
            for field_name in STORAGE_WORKER_SNAPSHOT_REMOVE_WHEN_ABSENT_FIELDS
            if field_name not in snapshot
        ),
        stale_recovery_allowed=True,
        accepted_abort_fields={
            field_name: deepcopy(snapshot[field_name])
            for field_name in STORAGE_WORKER_ACCEPTED_ABORT_PASSTHROUGH_FIELDS
            if field_name in snapshot
        },
        accepted_abort_prompt_ids=_optional_tuple(snapshot.get("prompt_ids")),
    )


def worker_snapshot_transition(worker, worker_id=None):
    return WorkerTransition.snapshot_applied(worker_snapshot_transition_patch(worker, worker_id))


def _snapshot_worker_id(worker, worker_id=None):
    if worker_id:
        return worker_id
    if isinstance(worker, WorkerRecord):
        if worker.worker_id:
            return worker.worker_id
        raise ValueError("snapshot worker requires id")
    if isinstance(worker, Mapping):
        worker_id = worker.get("id")
        if worker_id:
            return worker_id
        raise ValueError("snapshot worker requires id")
    raise TypeError("snapshot worker must be WorkerRecord or persisted worker mapping")


def _optional_tuple(value):
    if isinstance(value, list):
        return tuple(value)
    return None
