from collections.abc import Mapping
from copy import deepcopy

from opencode_session.status import short_status
from opencode_session.worker_state import (
    PUBLIC_WORKER_STATE_FIELD_NAMES,
    WORKER_LIFECYCLE_QUEUED,
    WORKER_LIFECYCLE_STATES,
    WORKER_LIST_FIELDS,
    WORKER_OPTIONAL_LIST_FIELDS,
    WORKER_STATUS_ABORTED,
    WORKER_STATUS_FAILED,
    WORKER_STATUS_TIMEOUT,
    WORKER_TIMEOUT_POLICY_STATUSES,
    WorkerSnapshotTransitionPatch,
    WorkerRecord,
    WorkerTransition,
    is_blocked_status,
    worker_failed_lifecycle_state,
    worker_lifecycle_state_for_public_state,
    worker_lifecycle_state_for_status_alias,
    worker_retry_available,
    worker_timeout_lifecycle_state,
)


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
_LEGACY_LIFECYCLE_PROJECTION_WORKER_ID = "__legacy_worker__"


def _worker_fields(worker):
    if isinstance(worker, WorkerRecord):
        return worker.to_snapshot()
    if isinstance(worker, Mapping):
        return dict(worker)
    return {}


def _raw_worker_field(worker, field_name, default=None):
    if isinstance(worker, WorkerRecord):
        return worker._compat_field(field_name, default)
    if isinstance(worker, Mapping):
        return worker.get(field_name, default)
    return default


def _legacy_worker_record_for_lifecycle_projection(worker):
    fields = _worker_fields(worker)
    for public_field_name in PUBLIC_WORKER_STATE_FIELD_NAMES:
        fields.pop(public_field_name, None)
    worker_id = fields.get("id") or _LEGACY_LIFECYCLE_PROJECTION_WORKER_ID
    fields["id"] = str(worker_id)
    if fields.get("lifecycle_state") not in WORKER_LIFECYCLE_STATES:
        fields["lifecycle_state"] = WORKER_LIFECYCLE_QUEUED
    _coerce_legacy_retry_budget_for_lifecycle_projection(fields)
    for field_name in (*WORKER_LIST_FIELDS, *WORKER_OPTIONAL_LIST_FIELDS):
        if field_name in fields and not isinstance(fields[field_name], list):
            fields[field_name] = []
    if "timeout_policy" in fields:
        timeout_policy = short_status(fields["timeout_policy"])
        fields["timeout_policy"] = (
            timeout_policy if timeout_policy in WORKER_TIMEOUT_POLICY_STATUSES else WORKER_STATUS_TIMEOUT
        )
    return WorkerRecord.from_worker(fields, fields["id"], allow_extra_fields=True)


def _coerce_legacy_retry_budget_for_lifecycle_projection(fields):
    try:
        fields["retry_count"] = int(fields.get("retry_count") or 0)
        fields["retry_limit"] = int(fields.get("retry_limit") or 0)
    except (TypeError, ValueError):
        fields["retry_count"] = 0
        fields["retry_limit"] = 0


def _lifecycle_state_from_legacy_public_worker_state(worker):
    worker = worker if isinstance(worker, Mapping) else {}
    status = short_status(_raw_worker_field(worker, "status"))
    projected_worker = _legacy_worker_record_for_lifecycle_projection(worker)
    if _legacy_worker_timeout_origin(worker, status):
        return worker_timeout_lifecycle_state(
            status,
            worker_retry_available(projected_worker, WORKER_STATUS_TIMEOUT),
        )
    if status == WORKER_STATUS_FAILED:
        return worker_failed_lifecycle_state(
            retryable=True,
            retry_available=worker_retry_available(projected_worker),
        )
    return (
        worker_lifecycle_state_for_public_state(status, _raw_worker_field(worker, "next_eligible_action"))
        or worker_lifecycle_state_for_status_alias(status)
        or WORKER_LIFECYCLE_QUEUED
    )


def _legacy_worker_timeout_origin(worker, status):
    if status == WORKER_STATUS_TIMEOUT:
        return True
    if status in {WORKER_STATUS_ABORTED, WORKER_STATUS_FAILED}:
        return _raw_worker_field(worker, "failure_category") == WORKER_STATUS_TIMEOUT
    return is_blocked_status(status) and (
        _raw_worker_field(worker, "failure_category") == WORKER_STATUS_TIMEOUT
        or WORKER_STATUS_TIMEOUT in set(_raw_worker_field(worker, "blockers") or [])
    )


def canonicalize_legacy_worker_record(worker, worker_id=None):
    fields = _worker_fields(worker)
    if worker_id:
        fields["id"] = str(worker_id)
    elif "id" in fields and not fields["id"]:
        fields.pop("id", None)
    elif "id" in fields and not isinstance(fields["id"], str):
        fields["id"] = str(fields["id"])
    if fields.get("lifecycle_state") not in WORKER_LIFECYCLE_STATES:
        fields["lifecycle_state"] = _lifecycle_state_from_legacy_public_worker_state(fields)
    for public_field_name in PUBLIC_WORKER_STATE_FIELD_NAMES:
        fields.pop(public_field_name, None)
    for field_name in ("retry_count", "retry_limit"):
        if field_name in fields:
            fields[field_name] = _coerced_storage_int(fields[field_name])
    for field_name in (*WORKER_LIST_FIELDS, *WORKER_OPTIONAL_LIST_FIELDS):
        if field_name in fields and not isinstance(fields[field_name], list):
            fields[field_name] = []
    if "timeout_policy" in fields:
        timeout_policy = short_status(fields["timeout_policy"])
        fields["timeout_policy"] = (
            timeout_policy if timeout_policy in WORKER_TIMEOUT_POLICY_STATUSES else WORKER_STATUS_TIMEOUT
        )
    return fields


def hydrate_worker_record(worker, worker_id):
    return WorkerRecord.from_worker(
        canonicalize_legacy_worker_record(worker, worker_id),
        worker_id,
        allow_extra_fields=True,
    ).to_worker()


def normalize_worker_snapshot_for_storage(worker, worker_id):
    return WorkerRecord.from_worker(
        canonicalize_legacy_worker_record(worker, worker_id),
        worker_id,
        allow_extra_fields=True,
    ).to_snapshot()


def _coerced_storage_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def worker_snapshot_transition_patch(worker, worker_id=None):
    worker_id = _snapshot_worker_id(worker, worker_id)
    snapshot = normalize_worker_snapshot_for_storage(worker, worker_id)
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
