from copy import deepcopy
from dataclasses import dataclass, field

from opencode_session.status import short_status
from opencode_session.worker_attempt_log import _append_attempt, _finalize_attempt
from opencode_session.worker_lifecycle import (
    WORKER_LIFECYCLE_ABORTED,
    WORKER_LIFECYCLE_ACTIVE_RETRY,
    WORKER_LIFECYCLE_ACTIVE_WAIT,
    WORKER_LIFECYCLE_BLOCKED_DEPENDENCY,
    WORKER_LIFECYCLE_BLOCKED_TIMEOUT,
    WORKER_LIFECYCLE_DONE_COLLECT,
    WORKER_LIFECYCLE_FAILED_RETRY,
    WORKER_LIFECYCLE_FAILED_TERMINAL,
    WORKER_LIFECYCLE_TIMEOUT_ABORTED,
    WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
    WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL,
    WORKER_LIFECYCLE_TIMEOUT_RETRY,
    WORKER_LIFECYCLE_TIMEOUT_TERMINAL,
    WORKER_STATUS_ABORTED,
    WORKER_STATUS_BLOCKED,
    WORKER_STATUS_DONE,
    WORKER_STATUS_FAILED,
    WORKER_STATUS_TIMEOUT,
    latest_prompt_ids_are_retry_marker,
    public_worker_state,
    worker_lifecycle_set_fields,
    worker_lifecycle_state,
)
from opencode_session.worker_snapshot_codec import WORKER_SNAPSHOT_STATE_FIELDS, WorkerRecord


REMOVABLE_WORKER_TRANSITION_FIELDS = ("error", "failure_retryable", "manual_retry_required")
UNSET_TRANSITION_FIELD = object()


@dataclass(frozen=True)
class WorkerTransitionSpec:
    set_fields: dict = field(default_factory=dict)
    delete_fields: tuple = ()
    set_if_missing_fields: dict = field(default_factory=dict)
    merge_unique_fields: dict = field(default_factory=dict)
    append_attempt: dict = field(default_factory=dict)
    finalize_attempt: dict = field(default_factory=dict)
    replace_worker: bool = False
    preserve_accepted_abort: bool = True


@dataclass(frozen=True)
class WorkerTransition:
    """Lifecycle command applied to the latest worker snapshot."""

    worker_id: str
    name: str
    spec: WorkerTransitionSpec

    @property
    def set_fields(self):
        return self.spec.set_fields

    @classmethod
    def _from_spec(cls, worker_id, name, spec):
        return cls(worker_id, name, spec)

    def with_finalized_attempt(self, attempt_id, fields):
        return self._from_spec(
            self.worker_id,
            self.name,
            _transition_spec(
                set_fields=self.spec.set_fields,
                delete_fields=self.spec.delete_fields,
                set_if_missing_fields=self.spec.set_if_missing_fields,
                merge_unique_fields=self.spec.merge_unique_fields,
                append_attempt=self.spec.append_attempt,
                finalize_attempt={"id": attempt_id, "fields": fields},
                replace_worker=self.spec.replace_worker,
                preserve_accepted_abort=self.spec.preserve_accepted_abort,
            ),
        )

    @classmethod
    def provisioned(cls, worker):
        worker_id = worker["id"]
        return cls._from_spec(
            worker_id,
            "provisioned",
            _provisioned_transition_spec(worker_id, worker),
        )

    @classmethod
    def active(cls, worker_id, *, timeout_started_at=UNSET_TRANSITION_FIELD, clear_prompt_ids=False):
        return cls._from_spec(
            worker_id,
            "active",
            _active_transition_spec(
                worker_id,
                timeout_started_at=timeout_started_at,
                clear_prompt_ids=clear_prompt_ids,
            ),
        )

    @classmethod
    def attempt_started(cls, worker_id, attempt):
        return cls._from_spec(
            worker_id,
            "attempt_started",
            _transition_spec(append_attempt=attempt),
        )

    @classmethod
    def failed(
        cls,
        worker_id,
        category,
        reason,
        *,
        retryable=True,
        retry_available=False,
        timeout_started_at=UNSET_TRANSITION_FIELD,
        prompt_ids=(),
    ):
        return cls._from_spec(
            worker_id,
            "failed",
            _failed_transition_spec(
                worker_id,
                category,
                reason,
                retryable=retryable,
                retry_available=retry_available,
                timeout_started_at=timeout_started_at,
                prompt_ids=prompt_ids,
            ),
        )

    @classmethod
    def dependency_blocked(cls, worker_id, blockers):
        return cls._from_spec(
            worker_id,
            "dependency_blocked",
            _dependency_blocked_transition_spec(worker_id, blockers),
        )

    @classmethod
    def aborted(cls, worker_id, abort):
        return cls._from_spec(
            worker_id,
            "aborted",
            _aborted_transition_spec(worker_id, abort),
        )

    @classmethod
    def retry_scheduled(
        cls,
        worker_id,
        category,
        reason,
        *,
        retry_count,
        timeout_started_at=UNSET_TRANSITION_FIELD,
        prompt_ids=(),
    ):
        return cls._from_spec(
            worker_id,
            "retry_scheduled",
            _retry_scheduled_transition_spec(
                worker_id,
                category,
                reason,
                retry_count=retry_count,
                timeout_started_at=timeout_started_at,
                prompt_ids=prompt_ids,
            ),
        )

    @classmethod
    def timed_out(
        cls,
        worker_id,
        reason,
        *,
        status,
        timed_out_at,
        retry_available=False,
        manual_retry_required=False,
        timeout_started_at=UNSET_TRANSITION_FIELD,
    ):
        return cls._from_spec(
            worker_id,
            "timed_out",
            _timed_out_transition_spec(
                worker_id,
                reason,
                status=status,
                timed_out_at=timed_out_at,
                retry_available=retry_available,
                manual_retry_required=manual_retry_required,
                timeout_started_at=timeout_started_at,
            ),
        )

    @classmethod
    def result_applied(cls, worker_id, result, *, prompt_ids=(), timeout_started_at=UNSET_TRANSITION_FIELD):
        return cls._from_spec(
            worker_id,
            "result_applied",
            _result_applied_transition_spec(
                worker_id,
                result,
                prompt_ids=prompt_ids,
                timeout_started_at=timeout_started_at,
            ),
        )

    @classmethod
    def cleanup_updated(cls, worker):
        worker_id = worker["id"]
        return cls._from_spec(
            worker_id,
            "cleanup_updated",
            _cleanup_updated_transition_spec(worker_id, worker),
        )

    @classmethod
    def snapshot_applied(cls, worker):
        worker_id = worker["id"]
        return cls._from_spec(
            worker_id,
            "snapshot_applied",
            _snapshot_applied_transition_spec(worker_id, worker),
        )


def apply_worker_transition_spec(record, spec):
    latest_worker = record.to_snapshot()
    set_fields = deepcopy(spec.set_fields or {})
    set_if_missing_fields = deepcopy(spec.set_if_missing_fields or {})
    merge_unique_fields = deepcopy(spec.merge_unique_fields or {})
    if spec.preserve_accepted_abort and _accepted_abort(latest_worker) and not _accepted_abort(set_fields):
        merged = _transition_for_aborted_worker(latest_worker, set_fields, merge_unique_fields)
    else:
        merged = {} if spec.replace_worker else deepcopy(latest_worker)
        merged.update(set_fields)
        for field_name in spec.delete_fields:
            merged.pop(field_name, None)
        _merge_unique_fields(merged, latest_worker, merge_unique_fields)
        for field_name, value in set_if_missing_fields.items():
            if not merged.get(field_name):
                merged[field_name] = deepcopy(value)
        if "abort" not in set_fields and "abort" in latest_worker:
            merged["abort"] = deepcopy(latest_worker["abort"])
    _append_attempt(merged, spec.append_attempt)
    _finalize_attempt(merged, spec.finalize_attempt)
    return WorkerRecord.from_worker(merged, record.worker_id).to_worker()


def _transition_for_aborted_worker(latest_worker, set_fields, merge_unique_fields):
    merged = deepcopy(latest_worker)
    _merge_unique_fields(merged, latest_worker, merge_unique_fields)
    if "cleanup" in set_fields:
        merged["cleanup"] = deepcopy(set_fields["cleanup"])
    return merged


def _accepted_abort(worker):
    abort = worker.get("abort") if isinstance(worker, dict) else None
    status = public_worker_state(worker_lifecycle_state(worker))[0]
    return isinstance(abort, dict) and abort.get("accepted") and status == WORKER_STATUS_ABORTED


def _merge_unique_fields(target, latest_worker, merge_unique_fields):
    for field_name, values in merge_unique_fields.items():
        source_worker = latest_worker
        if field_name == "prompt_ids" and latest_prompt_ids_are_retry_marker(latest_worker):
            source_worker = {}
        _merge_unique_list_field(target, source_worker, {field_name: list(values)}, field_name)


def _merge_unique_list_field(target, latest_worker, worker_record, field_name):
    merged_values = []
    for source in (latest_worker, worker_record):
        values = source.get(field_name) if isinstance(source, dict) else None
        if not isinstance(values, list):
            continue
        for value in values:
            if value not in merged_values:
                merged_values.append(deepcopy(value))
    target[field_name] = merged_values


def _transition_spec(
    *,
    set_fields=None,
    delete_fields=(),
    set_if_missing_fields=None,
    merge_unique_fields=None,
    append_attempt=None,
    finalize_attempt=None,
    replace_worker=False,
    preserve_accepted_abort=True,
):
    return WorkerTransitionSpec(
        set_fields=deepcopy(set_fields or {}),
        delete_fields=tuple(delete_fields),
        set_if_missing_fields=deepcopy(set_if_missing_fields or {}),
        merge_unique_fields=deepcopy(merge_unique_fields or {}),
        append_attempt=deepcopy(append_attempt or {}),
        finalize_attempt=deepcopy(finalize_attempt or {}),
        replace_worker=replace_worker,
        preserve_accepted_abort=preserve_accepted_abort,
    )


def _provisioned_transition_spec(worker_id, worker):
    set_fields = {"id": worker_id}
    for field_name in ("agent", "model"):
        if worker.get(field_name) is not None:
            set_fields[field_name] = deepcopy(worker[field_name])
    set_if_missing_fields = {}
    if worker.get("session_id"):
        set_if_missing_fields["session_id"] = deepcopy(worker["session_id"])
    return _transition_spec(set_fields=set_fields, set_if_missing_fields=set_if_missing_fields)


def _active_transition_spec(worker_id, *, timeout_started_at=UNSET_TRANSITION_FIELD, clear_prompt_ids=False):
    set_fields, delete_fields = _cleared_current_status_fields()
    set_fields.update(worker_lifecycle_set_fields(worker_id, WORKER_LIFECYCLE_ACTIVE_WAIT))
    _set_if_not_unset(set_fields, "timeout_started_at", timeout_started_at)
    if clear_prompt_ids:
        set_fields["prompt_ids"] = []
    return _transition_spec(set_fields=set_fields, delete_fields=delete_fields)


def _failed_transition_spec(
    worker_id,
    category,
    reason,
    *,
    retryable=True,
    retry_available=False,
    timeout_started_at=UNSET_TRANSITION_FIELD,
    prompt_ids=(),
):
    lifecycle_state = WORKER_LIFECYCLE_FAILED_RETRY if retryable and retry_available else WORKER_LIFECYCLE_FAILED_TERMINAL
    set_fields = worker_lifecycle_set_fields(worker_id, lifecycle_state)
    set_fields.update(
        {
            "error": reason,
            "failure_category": category,
            "failure_reason": reason,
            "last_failure_category": category,
            "last_failure_reason": reason,
        }
    )
    _set_if_not_unset(set_fields, "timeout_started_at", timeout_started_at)
    delete_fields = ["manual_retry_required"]
    if retryable:
        delete_fields.append("failure_retryable")
    else:
        set_fields["failure_retryable"] = False
    return _transition_spec(
        set_fields=set_fields,
        delete_fields=tuple(delete_fields),
        merge_unique_fields=_prompt_ids_merge(prompt_ids),
    )


def _dependency_blocked_transition_spec(worker_id, blockers):
    set_fields = worker_lifecycle_set_fields(worker_id, WORKER_LIFECYCLE_BLOCKED_DEPENDENCY)
    set_fields["blockers"] = list(blockers)
    return _transition_spec(set_fields=set_fields)


def _aborted_transition_spec(worker_id, abort):
    set_fields = {"id": worker_id, "abort": deepcopy(abort)}
    if isinstance(abort, dict) and abort.get("accepted"):
        set_fields.update(worker_lifecycle_set_fields(worker_id, WORKER_LIFECYCLE_ABORTED))
    return _transition_spec(set_fields=set_fields)


def _retry_scheduled_transition_spec(
    worker_id,
    category,
    reason,
    *,
    retry_count,
    timeout_started_at=UNSET_TRANSITION_FIELD,
    prompt_ids=(),
):
    set_fields, delete_fields = _cleared_current_status_fields()
    set_fields.update(worker_lifecycle_set_fields(worker_id, WORKER_LIFECYCLE_ACTIVE_RETRY))
    set_fields.update(
        {
            "retry_count": retry_count,
            "last_failure_category": category,
            "last_failure_reason": reason,
        }
    )
    _set_if_not_unset(set_fields, "timeout_started_at", timeout_started_at)
    return _transition_spec(
        set_fields=set_fields,
        delete_fields=delete_fields,
        merge_unique_fields=_prompt_ids_merge(prompt_ids),
    )


def _timed_out_transition_spec(
    worker_id,
    reason,
    *,
    status,
    timed_out_at,
    retry_available=False,
    manual_retry_required=False,
    timeout_started_at=UNSET_TRANSITION_FIELD,
):
    lifecycle_state = _timeout_lifecycle_state(status, retry_available)
    set_fields = worker_lifecycle_set_fields(worker_id, lifecycle_state)
    set_fields.update(
        {
            "error": reason,
            "failure_category": WORKER_STATUS_TIMEOUT,
            "failure_reason": reason,
            "last_failure_category": WORKER_STATUS_TIMEOUT,
            "last_failure_reason": reason,
            "timed_out_at": timed_out_at,
            "output_refs": [],
        }
    )
    if status == WORKER_STATUS_BLOCKED:
        set_fields["blockers"] = [WORKER_STATUS_TIMEOUT]
    _set_if_not_unset(set_fields, "timeout_started_at", timeout_started_at)
    delete_fields = []
    if manual_retry_required:
        set_fields["manual_retry_required"] = True
    else:
        delete_fields.append("manual_retry_required")
    return _transition_spec(set_fields=set_fields, delete_fields=tuple(delete_fields))


def _result_applied_transition_spec(
    worker_id,
    result,
    *,
    prompt_ids=(),
    timeout_started_at=UNSET_TRANSITION_FIELD,
):
    status = short_status(result["status"])
    set_fields = worker_lifecycle_set_fields(worker_id, _result_lifecycle_state(status))
    set_fields["result"] = deepcopy(result)
    _set_if_not_unset(set_fields, "timeout_started_at", timeout_started_at)
    delete_fields = ()
    if status == WORKER_STATUS_DONE:
        clear_fields, delete_fields = _cleared_current_status_fields()
        set_fields.update(clear_fields)
        assistant_message_id = result["message_ids"].get("assistant")
        set_fields["output_refs"] = [f"assistant:{assistant_message_id}"] if assistant_message_id else []
    else:
        set_fields["failure_category"] = None
        set_fields["failure_reason"] = None
    return _transition_spec(
        set_fields=set_fields,
        delete_fields=delete_fields,
        merge_unique_fields=_prompt_ids_merge(prompt_ids),
    )


def _cleanup_updated_transition_spec(worker_id, worker):
    return _transition_spec(set_fields={"id": worker_id, "cleanup": deepcopy(worker.get("cleanup"))})


def _snapshot_applied_transition_spec(
    worker_id,
    worker,
    *,
    state_fields=None,
    set_if_missing_fields=("session_id",),
    removable_fields=REMOVABLE_WORKER_TRANSITION_FIELDS,
):
    set_fields = {"id": worker_id}
    selected_state_fields = state_fields or WORKER_SNAPSHOT_STATE_FIELDS
    for field_name in selected_state_fields:
        if field_name in worker:
            set_fields[field_name] = deepcopy(worker[field_name])
    prompt_ids = worker.get("prompt_ids")
    return _transition_spec(
        set_fields=set_fields,
        delete_fields=tuple(field_name for field_name in removable_fields if field_name not in worker),
        set_if_missing_fields={
            field_name: deepcopy(worker[field_name])
            for field_name in set_if_missing_fields
            if worker.get(field_name)
        },
        merge_unique_fields={"prompt_ids": tuple(prompt_ids)} if isinstance(prompt_ids, list) else {},
    )


def _timeout_lifecycle_state(status, retry_available):
    if status == WORKER_STATUS_BLOCKED:
        return WORKER_LIFECYCLE_BLOCKED_TIMEOUT
    if status == WORKER_STATUS_FAILED:
        return WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY if retry_available else WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL
    if status == WORKER_STATUS_ABORTED:
        return WORKER_LIFECYCLE_TIMEOUT_ABORTED
    return WORKER_LIFECYCLE_TIMEOUT_RETRY if retry_available else WORKER_LIFECYCLE_TIMEOUT_TERMINAL


def _result_lifecycle_state(status):
    if status == WORKER_STATUS_DONE:
        return WORKER_LIFECYCLE_DONE_COLLECT
    if status == WORKER_STATUS_ABORTED:
        return WORKER_LIFECYCLE_ABORTED
    if status == WORKER_STATUS_TIMEOUT:
        return WORKER_LIFECYCLE_TIMEOUT_TERMINAL
    if status == WORKER_STATUS_BLOCKED:
        return WORKER_LIFECYCLE_BLOCKED_DEPENDENCY
    return WORKER_LIFECYCLE_FAILED_TERMINAL


def _cleared_current_status_fields():
    return {
        "blockers": [],
        "failure_category": None,
        "failure_reason": None,
    }, REMOVABLE_WORKER_TRANSITION_FIELDS


def _prompt_ids_merge(prompt_ids):
    prompt_ids = tuple(prompt_id for prompt_id in prompt_ids if prompt_id is not None)
    return {"prompt_ids": prompt_ids} if prompt_ids else {}


def _set_if_not_unset(fields, name, value):
    if value is not UNSET_TRANSITION_FIELD:
        fields[name] = value
