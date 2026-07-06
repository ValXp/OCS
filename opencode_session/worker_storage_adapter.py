from collections.abc import Mapping

from opencode_session.status import short_status
from opencode_session.worker_state import (
    PUBLIC_WORKER_STATE_FIELD_NAMES,
    WORKER_ACTION_NONE,
    WORKER_ACTION_RESOLVE_BLOCKER,
    WORKER_ACTION_RETRY,
    WORKER_LIFECYCLE_QUEUED,
    WORKER_LIFECYCLE_STATES,
    WORKER_STATUS_ABORTED,
    WORKER_STATUS_ACTIVE,
    WORKER_STATUS_DONE,
    WORKER_STATUS_FAILED,
    WORKER_STATUS_QUEUED,
    WORKER_STATUS_TIMEOUT,
    WorkerRecord,
    is_blocked_status,
    serialize_worker_snapshot,
    worker_lifecycle_state_for_public_state,
    worker_lifecycle_state_for_status_alias,
)


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


def canonicalize_legacy_worker_record(worker):
    fields = _worker_fields(worker)
    if fields.get("lifecycle_state") not in WORKER_LIFECYCLE_STATES:
        fields["lifecycle_state"] = _lifecycle_state_from_legacy_public_worker_state(fields)
    for public_field_name in PUBLIC_WORKER_STATE_FIELD_NAMES:
        fields.pop(public_field_name, None)
    return fields


def hydrate_worker_record(worker, worker_id):
    return WorkerRecord.from_worker(canonicalize_legacy_worker_record(worker), worker_id).to_worker()


def normalize_worker_snapshot_for_storage(worker, worker_id):
    return serialize_worker_snapshot(canonicalize_legacy_worker_record(worker), worker_id)
