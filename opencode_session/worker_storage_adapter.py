from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass

from opencode_session.status import short_status
from opencode_session.worker_state import (
    PUBLIC_WORKER_STATE_FIELD_NAMES,
    WORKER_FIELD_SPEC_BY_NAME,
    WORKER_RECORD_CANONICAL_FIELD_NAMES,
    WORKER_LIFECYCLE_QUEUED,
    WORKER_LIFECYCLE_STATES,
    WORKER_STORAGE_INT_FIELD_NAMES,
    WORKER_STORAGE_LIST_FIELD_NAMES,
    WORKER_STORAGE_TIMEOUT_SECONDS_FIELD_NAMES,
    WORKER_STORAGE_TIMEOUT_POLICY_FIELD_NAMES,
    WORKER_STATUS_ABORTED,
    WORKER_STATUS_FAILED,
    WORKER_STATUS_TIMEOUT,
    WORKER_TIMEOUT_POLICY_STATUSES,
    WorkerRecord,
    is_blocked_status,
    worker_default_snapshot_fields,
    worker_failed_lifecycle_state,
    worker_lifecycle_state_for_public_state,
    worker_lifecycle_state_for_status_alias,
    worker_timeout_lifecycle_state,
)


PERSISTED_WORKER_SNAPSHOT_SCHEMA_VERSION = 1


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
    if isinstance(worker, Mapping):
        return dict(worker)
    raise TypeError("persisted worker snapshot must be a mapping")


def _raw_worker_field(worker, field_name, default=None):
    return worker.get(field_name, default)


def _lifecycle_state_from_legacy_public_worker_state(worker):
    status = short_status(_raw_worker_field(worker, "status"))
    if _legacy_worker_timeout_origin(worker, status):
        return worker_timeout_lifecycle_state(
            status,
            _legacy_worker_retry_available(worker, WORKER_STATUS_TIMEOUT),
        )
    if status == WORKER_STATUS_FAILED:
        return worker_failed_lifecycle_state(
            retryable=True,
            retry_available=_legacy_worker_retry_available(worker),
        )
    return (
        worker_lifecycle_state_for_public_state(status, _raw_worker_field(worker, "next_eligible_action"))
        or worker_lifecycle_state_for_status_alias(status)
        or WORKER_LIFECYCLE_QUEUED
    )


def _legacy_worker_retry_available(fields, category=None):
    if fields.get("failure_retryable") is False:
        return False
    retryable_failures = _legacy_worker_list_field(fields, "retryable_failures")
    if not retryable_failures:
        return False
    if category is None:
        category = fields.get("failure_category") or fields.get("last_failure_category")
    if category and category not in retryable_failures and "all" not in retryable_failures:
        return False
    try:
        retry_count = int(fields.get("retry_count") or 0)
        retry_limit = int(fields.get("retry_limit") or 0)
    except (TypeError, ValueError):
        return False
    return retry_count < retry_limit


def _legacy_worker_list_field(fields, field_name):
    value = fields.get(field_name)
    return value if isinstance(value, list) else []


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
    _coerce_persisted_worker_timeout_seconds(fields)
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
    for field_name in WORKER_STORAGE_INT_FIELD_NAMES:
        if field_name in fields:
            fields[field_name] = _coerced_storage_int(fields[field_name])


def _coerce_persisted_worker_lists(fields):
    for field_name in WORKER_STORAGE_LIST_FIELD_NAMES:
        if field_name in fields and not isinstance(fields[field_name], list):
            fields[field_name] = []


def _coerce_persisted_worker_timeout_seconds(fields):
    for field_name in WORKER_STORAGE_TIMEOUT_SECONDS_FIELD_NAMES:
        if field_name in fields:
            fields[field_name] = WORKER_FIELD_SPEC_BY_NAME[field_name].storage_value(fields[field_name])


def _coerce_persisted_worker_timeout_policy(fields):
    for field_name in WORKER_STORAGE_TIMEOUT_POLICY_FIELD_NAMES:
        if field_name not in fields:
            continue
        timeout_policy = short_status(fields[field_name])
        fields[field_name] = (
            timeout_policy if timeout_policy in WORKER_TIMEOUT_POLICY_STATUSES else WORKER_STATUS_TIMEOUT
        )


def hydrate_worker_record(worker, worker_id, *, run_schema_version=PERSISTED_WORKER_SNAPSHOT_SCHEMA_VERSION):
    snapshot = _migrated_persisted_worker_snapshot(worker, worker_id, run_schema_version=run_schema_version)
    fields = _canonical_worker_snapshot_fields(snapshot, worker_id)
    return WorkerRecord(fields["id"], fields).to_worker()


def canonical_worker_snapshot_fields(
    worker,
    worker_id,
    *,
    run_schema_version=PERSISTED_WORKER_SNAPSHOT_SCHEMA_VERSION,
):
    if isinstance(worker, WorkerRecord):
        return _worker_record_snapshot_fields(worker, worker_id)
    raise TypeError("worker snapshot fields require WorkerRecord; hydrate raw mappings at the storage boundary")


def normalize_worker_snapshot_for_storage(
    worker,
    worker_id,
    *,
    run_schema_version=PERSISTED_WORKER_SNAPSHOT_SCHEMA_VERSION,
    persisted_worker=None,
):
    if isinstance(worker, WorkerRecord):
        snapshot = None
        runtime_snapshot = _worker_record_snapshot_fields(worker, worker_id)
    else:
        snapshot = _migrated_persisted_worker_snapshot(worker, worker_id, run_schema_version=run_schema_version)
        runtime_snapshot = _canonical_worker_snapshot_fields(snapshot, worker_id)
    if persisted_worker is not None and not isinstance(persisted_worker, WorkerRecord):
        persisted_snapshot = _migrated_persisted_worker_snapshot(
            persisted_worker,
            worker_id,
            run_schema_version=run_schema_version,
        )
        storage_snapshot = deepcopy(runtime_snapshot)
        storage_snapshot.update(persisted_snapshot.unknown_fields)
        return storage_snapshot
    if snapshot is None:
        return runtime_snapshot
    return snapshot.to_storage_fields(runtime_snapshot)


def _worker_record_snapshot_fields(worker, worker_id):
    snapshot = worker.to_snapshot()
    if worker_id:
        snapshot["id"] = str(worker_id)
    return snapshot


def _canonical_worker_snapshot_fields(snapshot, worker_id):
    runtime_fields = snapshot.runtime_fields
    resolved_worker_id = runtime_fields.get("id") or worker_id
    if not resolved_worker_id:
        raise ValueError("worker snapshot requires id")
    normalized = worker_default_snapshot_fields(str(resolved_worker_id))
    normalized.update(runtime_fields)
    return normalized


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
