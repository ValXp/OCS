from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass

from opencode_session.status import short_status
from opencode_session.worker_state import (
    PUBLIC_WORKER_STATE_FIELD_NAMES,
    WORKER_RECORD_CANONICAL_FIELD_NAMES,
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
PERSISTED_WORKER_SNAPSHOT_SCHEMA_VERSION = 1
_LEGACY_LIFECYCLE_PROJECTION_WORKER_ID = "__legacy_worker__"


@dataclass(frozen=True)
class _PersistedWorkerSnapshot:
    fields: dict

    @property
    def runtime_fields(self):
        return {
            field_name: deepcopy(value)
            for field_name, value in self.fields.items()
            if field_name in WORKER_RECORD_CANONICAL_FIELD_NAMES
        }

    @property
    def unknown_fields(self):
        return {
            field_name: deepcopy(value)
            for field_name, value in self.fields.items()
            if field_name not in WORKER_RECORD_CANONICAL_FIELD_NAMES
        }

    def to_storage_fields(self, runtime_fields=None):
        fields = deepcopy(runtime_fields) if runtime_fields is not None else self.runtime_fields
        fields.update(self.unknown_fields)
        return fields


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
    return WorkerRecord.from_worker(_PersistedWorkerSnapshot(fields).runtime_fields, fields["id"])


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


def migrate_persisted_worker_snapshot(
    worker,
    worker_id=None,
    *,
    run_schema_version=PERSISTED_WORKER_SNAPSHOT_SCHEMA_VERSION,
):
    return _migrated_persisted_worker_snapshot(
        worker,
        worker_id,
        run_schema_version=run_schema_version,
    ).to_storage_fields()


def _migrated_persisted_worker_snapshot(
    worker,
    worker_id=None,
    *,
    run_schema_version=PERSISTED_WORKER_SNAPSHOT_SCHEMA_VERSION,
):
    fields = _worker_fields(worker)
    schema_version = _coerced_schema_version(run_schema_version)
    if schema_version <= PERSISTED_WORKER_SNAPSHOT_SCHEMA_VERSION:
        return _migrate_v1_persisted_worker_snapshot(fields, worker_id)
    # No newer worker snapshot schema exists yet; keep public/legacy fields out of WorkerRecord.
    return _migrate_v1_persisted_worker_snapshot(fields, worker_id)


def canonicalize_legacy_worker_record(worker, worker_id=None):
    return migrate_persisted_worker_snapshot(worker, worker_id, run_schema_version=1)


def _migrate_v1_persisted_worker_snapshot(fields, worker_id=None):
    fields = dict(fields)
    _repair_persisted_worker_identity(fields, worker_id)
    _repair_persisted_worker_lifecycle(fields)
    _remove_legacy_public_worker_state(fields)
    _coerce_persisted_worker_retry_budget(fields)
    _coerce_persisted_worker_lists(fields)
    _coerce_persisted_worker_timeout_policy(fields)
    return _PersistedWorkerSnapshot(fields)


def _repair_persisted_worker_identity(fields, worker_id):
    if worker_id:
        fields["id"] = str(worker_id)
    elif "id" in fields and not fields["id"]:
        fields.pop("id", None)
    elif "id" in fields and not isinstance(fields["id"], str):
        fields["id"] = str(fields["id"])


def _repair_persisted_worker_lifecycle(fields):
    if fields.get("lifecycle_state") not in WORKER_LIFECYCLE_STATES:
        fields["lifecycle_state"] = _lifecycle_state_from_legacy_public_worker_state(fields)


def _remove_legacy_public_worker_state(fields):
    for public_field_name in PUBLIC_WORKER_STATE_FIELD_NAMES:
        fields.pop(public_field_name, None)


def _coerce_persisted_worker_retry_budget(fields):
    for field_name in ("retry_count", "retry_limit"):
        if field_name in fields:
            fields[field_name] = _coerced_storage_int(fields[field_name])


def _coerce_persisted_worker_lists(fields):
    for field_name in (*WORKER_LIST_FIELDS, *WORKER_OPTIONAL_LIST_FIELDS):
        if field_name in fields and not isinstance(fields[field_name], list):
            fields[field_name] = []


def _coerce_persisted_worker_timeout_policy(fields):
    if "timeout_policy" in fields:
        timeout_policy = short_status(fields["timeout_policy"])
        fields["timeout_policy"] = (
            timeout_policy if timeout_policy in WORKER_TIMEOUT_POLICY_STATUSES else WORKER_STATUS_TIMEOUT
        )


def hydrate_worker_record(worker, worker_id, *, run_schema_version=PERSISTED_WORKER_SNAPSHOT_SCHEMA_VERSION):
    snapshot = _migrated_persisted_worker_snapshot(worker, worker_id, run_schema_version=run_schema_version)
    return WorkerRecord.from_worker(
        snapshot.runtime_fields,
        worker_id,
    ).to_worker()


def normalize_worker_snapshot_for_storage(
    worker,
    worker_id,
    *,
    run_schema_version=PERSISTED_WORKER_SNAPSHOT_SCHEMA_VERSION,
    persisted_worker=None,
):
    snapshot = _migrated_persisted_worker_snapshot(worker, worker_id, run_schema_version=run_schema_version)
    runtime_snapshot = WorkerRecord.from_worker(
        snapshot.runtime_fields,
        worker_id,
    ).to_snapshot()
    if persisted_worker is not None and not isinstance(persisted_worker, WorkerRecord):
        persisted_snapshot = _migrated_persisted_worker_snapshot(
            persisted_worker,
            worker_id,
            run_schema_version=run_schema_version,
        )
        storage_snapshot = deepcopy(runtime_snapshot)
        storage_snapshot.update(persisted_snapshot.unknown_fields)
        return storage_snapshot
    if isinstance(worker, WorkerRecord):
        return runtime_snapshot
    return snapshot.to_storage_fields(runtime_snapshot)


def _coerced_schema_version(value):
    try:
        return int(value or PERSISTED_WORKER_SNAPSHOT_SCHEMA_VERSION)
    except (TypeError, ValueError):
        return PERSISTED_WORKER_SNAPSHOT_SCHEMA_VERSION


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
