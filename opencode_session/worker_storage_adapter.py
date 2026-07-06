from collections.abc import Mapping
from copy import deepcopy

from opencode_session.status import short_status
from opencode_session.worker_state import (
    PUBLIC_WORKER_STATE_FIELD_NAMES,
    WORKER_ACTION_NONE,
    WORKER_ACTION_RESOLVE_BLOCKER,
    WORKER_ACTION_RETRY,
    WORKER_LIFECYCLE_QUEUED,
    WORKER_LIFECYCLE_STATES,
    WORKER_LIST_FIELDS,
    WORKER_OPTIONAL_LIST_FIELDS,
    WORKER_STATUS_ABORTED,
    WORKER_STATUS_ACTIVE,
    WORKER_STATUS_DONE,
    WORKER_STATUS_FAILED,
    WORKER_STATUS_QUEUED,
    WORKER_STATUS_TIMEOUT,
    WORKER_TIMEOUT_POLICY_STATUSES,
    WorkerSnapshotTransitionPatch,
    WorkerRecord,
    WorkerTransition,
    is_blocked_status,
    worker_lifecycle_state_for_public_state,
    worker_lifecycle_state_for_status_alias,
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


def _worker_fields(worker):
    if isinstance(worker, WorkerRecord):
        return worker.to_snapshot()
    if isinstance(worker, Mapping):
        return dict(worker)
    return {}


def _raw_worker_field(worker, field_name, default=None):
    if isinstance(worker, WorkerRecord):
        return worker.field(field_name, default)
    if isinstance(worker, Mapping):
        return worker.get(field_name, default)
    return default


def _legacy_worker_retry_available(worker, category=None):
    if not isinstance(worker, Mapping):
        return False
    if _raw_worker_field(worker, "failure_retryable") is False:
        return False
    retryable = set(_raw_worker_field(worker, "retryable_failures") or [])
    if not retryable:
        return False
    if category is None:
        category = _raw_worker_field(worker, "failure_category") or _raw_worker_field(worker, "last_failure_category")
    if category and category not in retryable and "all" not in retryable:
        return False
    try:
        retry_count = int(_raw_worker_field(worker, "retry_count") or 0)
        retry_limit = int(_raw_worker_field(worker, "retry_limit") or 0)
    except (TypeError, ValueError):
        return False
    return retry_count < retry_limit


def _lifecycle_state_from_legacy_public_worker_state(worker):
    worker = worker if isinstance(worker, Mapping) else {}
    status = short_status(_raw_worker_field(worker, "status"))
    if status == WORKER_STATUS_QUEUED:
        return worker_lifecycle_state_for_status_alias(status)
    if status == WORKER_STATUS_ACTIVE:
        if _raw_worker_field(worker, "next_eligible_action") == WORKER_ACTION_RETRY:
            return worker_lifecycle_state_for_public_state(status, WORKER_ACTION_RETRY)
        return worker_lifecycle_state_for_status_alias(status)
    if is_blocked_status(status):
        timeout_origin = _raw_worker_field(
            worker,
            "failure_category",
        ) == WORKER_STATUS_TIMEOUT or WORKER_STATUS_TIMEOUT in set(
            _raw_worker_field(worker, "blockers") or []
        )
        return worker_lifecycle_state_for_public_state(
            status,
            WORKER_ACTION_RESOLVE_BLOCKER,
            timeout_origin=timeout_origin,
        )
    if status == WORKER_STATUS_DONE:
        return worker_lifecycle_state_for_status_alias(status)
    if status == WORKER_STATUS_FAILED:
        if _raw_worker_field(worker, "failure_category") == WORKER_STATUS_TIMEOUT:
            return worker_lifecycle_state_for_public_state(
                status,
                (
                    WORKER_ACTION_RETRY
                    if _legacy_worker_retry_available(worker, WORKER_STATUS_TIMEOUT)
                    else WORKER_ACTION_NONE
                ),
                timeout_origin=True,
            )
        return worker_lifecycle_state_for_public_state(
            status,
            WORKER_ACTION_RETRY if _legacy_worker_retry_available(worker) else WORKER_ACTION_NONE,
        )
    if status == WORKER_STATUS_TIMEOUT:
        return worker_lifecycle_state_for_public_state(
            status,
            (
                WORKER_ACTION_RETRY
                if _legacy_worker_retry_available(worker, WORKER_STATUS_TIMEOUT)
                else WORKER_ACTION_NONE
            ),
            timeout_origin=True,
        )
    if status == WORKER_STATUS_ABORTED:
        if _raw_worker_field(worker, "failure_category") == WORKER_STATUS_TIMEOUT:
            return worker_lifecycle_state_for_public_state(
                status,
                WORKER_ACTION_NONE,
                timeout_origin=True,
            )
        return worker_lifecycle_state_for_status_alias(status)
    return WORKER_LIFECYCLE_QUEUED


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
