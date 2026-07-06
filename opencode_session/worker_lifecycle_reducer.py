from copy import deepcopy
from dataclasses import dataclass

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
class _ProvisionedTransition:
    session_id: object = None
    agent: object = None
    model: object = None


@dataclass(frozen=True)
class _ActiveTransition:
    timeout_started_at: object = UNSET_TRANSITION_FIELD
    clear_prompt_ids: bool = False


@dataclass(frozen=True)
class _AttemptStartedTransition:
    attempt: dict


@dataclass(frozen=True)
class _FailedTransition:
    category: str
    reason: str
    retryable: bool = True
    retry_available: bool = False
    timeout_started_at: object = UNSET_TRANSITION_FIELD
    prompt_ids: tuple = ()


@dataclass(frozen=True)
class _DependencyBlockedTransition:
    blockers: tuple


@dataclass(frozen=True)
class _AbortedTransition:
    abort: object


@dataclass(frozen=True)
class _RetryScheduledTransition:
    category: str
    reason: str
    retry_count: int
    timeout_started_at: object = UNSET_TRANSITION_FIELD
    prompt_ids: tuple = ()


@dataclass(frozen=True)
class _TimedOutTransition:
    reason: str
    status: str
    timed_out_at: object
    retry_available: bool = False
    manual_retry_required: bool = False
    timeout_started_at: object = UNSET_TRANSITION_FIELD


@dataclass(frozen=True)
class _ResultAppliedTransition:
    result: dict
    prompt_ids: tuple = ()
    timeout_started_at: object = UNSET_TRANSITION_FIELD


@dataclass(frozen=True)
class _CleanupUpdatedTransition:
    cleanup: object = None


@dataclass(frozen=True)
class _SnapshotAppliedTransition:
    worker: dict
    state_fields: tuple = WORKER_SNAPSHOT_STATE_FIELDS
    set_if_missing_fields: tuple = ("session_id",)
    removable_fields: tuple = REMOVABLE_WORKER_TRANSITION_FIELDS


@dataclass(frozen=True)
class _AttemptFinalization:
    attempt_id: str
    fields: dict


@dataclass(frozen=True)
class WorkerTransition:
    """Named lifecycle transition applied by WorkerLifecycleReducer."""

    worker_id: str
    name: str
    payload: object = None
    attempt_finalization: _AttemptFinalization | None = None

    def with_finalized_attempt(self, attempt_id, fields):
        return WorkerTransition(
            self.worker_id,
            self.name,
            self.payload,
            _AttemptFinalization(attempt_id, deepcopy(fields or {})),
        )

    @classmethod
    def provisioned(cls, worker):
        worker_id = worker["id"]
        return cls(
            worker_id,
            "provisioned",
            _ProvisionedTransition(
                session_id=deepcopy(worker.get("session_id")),
                agent=_copy_present(worker.get("agent")),
                model=_copy_present(worker.get("model")),
            ),
        )

    @classmethod
    def active(cls, worker_id, *, timeout_started_at=UNSET_TRANSITION_FIELD, clear_prompt_ids=False):
        return cls(
            worker_id,
            "active",
            _ActiveTransition(
                timeout_started_at=_copy_transition_value(timeout_started_at),
                clear_prompt_ids=clear_prompt_ids,
            ),
        )

    @classmethod
    def attempt_started(cls, worker_id, attempt):
        return cls(worker_id, "attempt_started", _AttemptStartedTransition(deepcopy(attempt or {})))

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
        return cls(
            worker_id,
            "failed",
            _FailedTransition(
                category,
                reason,
                retryable=retryable,
                retry_available=retry_available,
                timeout_started_at=_copy_transition_value(timeout_started_at),
                prompt_ids=_filtered_prompt_ids(prompt_ids),
            ),
        )

    @classmethod
    def dependency_blocked(cls, worker_id, blockers):
        return cls(worker_id, "dependency_blocked", _DependencyBlockedTransition(tuple(blockers)))

    @classmethod
    def aborted(cls, worker_id, abort):
        return cls(worker_id, "aborted", _AbortedTransition(deepcopy(abort)))

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
        return cls(
            worker_id,
            "retry_scheduled",
            _RetryScheduledTransition(
                category,
                reason,
                retry_count=retry_count,
                timeout_started_at=_copy_transition_value(timeout_started_at),
                prompt_ids=_filtered_prompt_ids(prompt_ids),
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
        return cls(
            worker_id,
            "timed_out",
            _TimedOutTransition(
                reason,
                status=status,
                timed_out_at=deepcopy(timed_out_at),
                retry_available=retry_available,
                manual_retry_required=manual_retry_required,
                timeout_started_at=_copy_transition_value(timeout_started_at),
            ),
        )

    @classmethod
    def result_applied(cls, worker_id, result, *, prompt_ids=(), timeout_started_at=UNSET_TRANSITION_FIELD):
        return cls(
            worker_id,
            "result_applied",
            _ResultAppliedTransition(
                deepcopy(result or {}),
                prompt_ids=_filtered_prompt_ids(prompt_ids),
                timeout_started_at=_copy_transition_value(timeout_started_at),
            ),
        )

    @classmethod
    def cleanup_updated(cls, worker):
        worker_id = worker["id"]
        return cls(worker_id, "cleanup_updated", _CleanupUpdatedTransition(deepcopy(worker.get("cleanup"))))

    @classmethod
    def snapshot_applied(cls, worker):
        worker_id = worker["id"]
        return cls(
            worker_id,
            "snapshot_applied",
            _SnapshotAppliedTransition(
                deepcopy(worker),
                state_fields=tuple(WORKER_SNAPSHOT_STATE_FIELDS),
                set_if_missing_fields=("session_id",),
                removable_fields=tuple(REMOVABLE_WORKER_TRANSITION_FIELDS),
            ),
        )


class WorkerLifecycleReducer:
    def __init__(self, record):
        self.record = record
        self.latest_worker = record.to_snapshot()

    def apply(self, transition):
        if transition.name == "provisioned":
            worker = self.provisioned(transition)
        elif transition.name == "active":
            worker = self.active(transition)
        elif transition.name == "attempt_started":
            worker = self.attempt_started(transition)
        elif transition.name == "failed":
            worker = self.failed(transition)
        elif transition.name == "dependency_blocked":
            worker = self.dependency_blocked(transition)
        elif transition.name == "aborted":
            worker = self.aborted(transition)
        elif transition.name == "retry_scheduled":
            worker = self.retry_scheduled(transition)
        elif transition.name == "timed_out":
            worker = self.timed_out(transition)
        elif transition.name == "result_applied":
            worker = self.result_applied(transition)
        elif transition.name == "cleanup_updated":
            worker = self.cleanup_updated(transition)
        elif transition.name == "snapshot_applied":
            worker = self.snapshot_applied(transition)
        else:
            raise ValueError(f"unknown worker transition: {transition.name}")
        _finalize_worker_attempt(worker, transition.attempt_finalization)
        return WorkerRecord.from_worker(worker, self.record.worker_id or transition.worker_id).to_worker()

    def provisioned(self, transition):
        payload = transition.payload
        worker = self._copy_latest()
        if self._has_accepted_abort():
            return worker
        worker["id"] = transition.worker_id
        if payload.agent is not None:
            worker["agent"] = deepcopy(payload.agent)
        if payload.model is not None:
            worker["model"] = deepcopy(payload.model)
        if payload.session_id and not worker.get("session_id"):
            worker["session_id"] = deepcopy(payload.session_id)
        return worker

    def active(self, transition):
        payload = transition.payload
        worker = self._copy_latest()
        if self._has_accepted_abort():
            return worker
        _clear_current_status_fields(worker)
        worker.update(worker_lifecycle_set_fields(transition.worker_id, WORKER_LIFECYCLE_ACTIVE_WAIT))
        _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
        if payload.clear_prompt_ids:
            worker["prompt_ids"] = []
        return worker

    def attempt_started(self, transition):
        worker = self._copy_latest()
        _append_attempt(worker, transition.payload.attempt)
        return worker

    def failed(self, transition):
        payload = transition.payload
        worker = self._copy_latest()
        if self._has_accepted_abort():
            self._merge_prompt_ids(worker, payload.prompt_ids)
            return worker
        lifecycle_state = (
            WORKER_LIFECYCLE_FAILED_RETRY
            if payload.retryable and payload.retry_available
            else WORKER_LIFECYCLE_FAILED_TERMINAL
        )
        worker.update(worker_lifecycle_set_fields(transition.worker_id, lifecycle_state))
        worker.update(
            {
                "error": payload.reason,
                "failure_category": payload.category,
                "failure_reason": payload.reason,
                "last_failure_category": payload.category,
                "last_failure_reason": payload.reason,
            }
        )
        _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
        worker.pop("manual_retry_required", None)
        if payload.retryable:
            worker.pop("failure_retryable", None)
        else:
            worker["failure_retryable"] = False
        self._merge_prompt_ids(worker, payload.prompt_ids)
        return worker

    def dependency_blocked(self, transition):
        worker = self._copy_latest()
        if self._has_accepted_abort():
            return worker
        worker.update(worker_lifecycle_set_fields(transition.worker_id, WORKER_LIFECYCLE_BLOCKED_DEPENDENCY))
        worker["blockers"] = list(transition.payload.blockers)
        return worker

    def aborted(self, transition):
        payload = transition.payload
        worker = self._copy_latest()
        if self._has_accepted_abort() and not _abort_is_accepted(payload.abort):
            return worker
        worker["id"] = transition.worker_id
        worker["abort"] = deepcopy(payload.abort)
        if _abort_is_accepted(payload.abort):
            worker.update(worker_lifecycle_set_fields(transition.worker_id, WORKER_LIFECYCLE_ABORTED))
        return worker

    def retry_scheduled(self, transition):
        payload = transition.payload
        worker = self._copy_latest()
        if self._has_accepted_abort():
            self._merge_prompt_ids(worker, payload.prompt_ids)
            return worker
        _clear_current_status_fields(worker)
        worker.update(worker_lifecycle_set_fields(transition.worker_id, WORKER_LIFECYCLE_ACTIVE_RETRY))
        worker.update(
            {
                "retry_count": payload.retry_count,
                "last_failure_category": payload.category,
                "last_failure_reason": payload.reason,
            }
        )
        _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
        self._merge_prompt_ids(worker, payload.prompt_ids)
        return worker

    def timed_out(self, transition):
        payload = transition.payload
        worker = self._copy_latest()
        if self._has_accepted_abort():
            return worker
        worker.update(
            worker_lifecycle_set_fields(
                transition.worker_id,
                _timeout_lifecycle_state(payload.status, payload.retry_available),
            )
        )
        worker.update(
            {
                "error": payload.reason,
                "failure_category": WORKER_STATUS_TIMEOUT,
                "failure_reason": payload.reason,
                "last_failure_category": WORKER_STATUS_TIMEOUT,
                "last_failure_reason": payload.reason,
                "timed_out_at": payload.timed_out_at,
                "output_refs": [],
            }
        )
        if payload.status == WORKER_STATUS_BLOCKED:
            worker["blockers"] = [WORKER_STATUS_TIMEOUT]
        _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
        if payload.manual_retry_required:
            worker["manual_retry_required"] = True
        else:
            worker.pop("manual_retry_required", None)
        return worker

    def result_applied(self, transition):
        payload = transition.payload
        worker = self._copy_latest()
        if self._has_accepted_abort():
            self._merge_prompt_ids(worker, payload.prompt_ids)
            return worker
        status = short_status(payload.result["status"])
        worker.update(worker_lifecycle_set_fields(transition.worker_id, _result_lifecycle_state(status)))
        worker["result"] = deepcopy(payload.result)
        _set_if_not_unset(worker, "timeout_started_at", payload.timeout_started_at)
        if status == WORKER_STATUS_DONE:
            _clear_current_status_fields(worker)
            assistant_message_id = payload.result["message_ids"].get("assistant")
            worker["output_refs"] = [f"assistant:{assistant_message_id}"] if assistant_message_id else []
        else:
            worker["failure_category"] = None
            worker["failure_reason"] = None
        self._merge_prompt_ids(worker, payload.prompt_ids)
        return worker

    def cleanup_updated(self, transition):
        worker = self._copy_latest()
        worker["id"] = transition.worker_id
        worker["cleanup"] = deepcopy(transition.payload.cleanup)
        return worker

    def snapshot_applied(self, transition):
        payload = transition.payload
        if self._has_accepted_abort() and not _accepted_abort(_snapshot_transition_fields(transition)):
            worker = self._copy_latest()
            if "cleanup" in payload.state_fields and "cleanup" in payload.worker:
                worker["cleanup"] = deepcopy(payload.worker.get("cleanup"))
            prompt_ids = payload.worker.get("prompt_ids")
            if isinstance(prompt_ids, list):
                self._merge_prompt_ids(worker, tuple(prompt_ids), merge_empty=True)
            return worker
        worker = self._copy_latest()
        worker["id"] = transition.worker_id
        for field_name in payload.state_fields:
            if field_name in payload.worker:
                worker[field_name] = deepcopy(payload.worker[field_name])
        for field_name in payload.removable_fields:
            if field_name not in payload.worker:
                worker.pop(field_name, None)
        prompt_ids = payload.worker.get("prompt_ids")
        if isinstance(prompt_ids, list):
            self._merge_prompt_ids(worker, tuple(prompt_ids), merge_empty=True)
        for field_name in payload.set_if_missing_fields:
            if payload.worker.get(field_name) and not worker.get(field_name):
                worker[field_name] = deepcopy(payload.worker[field_name])
        return worker

    def _copy_latest(self):
        return deepcopy(self.latest_worker)

    def _has_accepted_abort(self):
        return _accepted_abort(self.latest_worker)

    def _merge_prompt_ids(self, worker, prompt_ids, *, merge_empty=False):
        if not prompt_ids and not merge_empty:
            return
        source_worker = {} if latest_prompt_ids_are_retry_marker(self.latest_worker) else self.latest_worker
        _merge_unique_list_field(worker, source_worker, {"prompt_ids": list(prompt_ids)}, "prompt_ids")


def apply_worker_transition_to_record(record, transition):
    return WorkerLifecycleReducer(record).apply(transition)


def _snapshot_transition_fields(transition):
    payload = transition.payload
    fields = {"id": transition.worker_id}
    for field_name in payload.state_fields:
        if field_name in payload.worker:
            fields[field_name] = deepcopy(payload.worker[field_name])
    return fields


def _accepted_abort(worker):
    abort = worker.get("abort") if isinstance(worker, dict) else None
    status = public_worker_state(worker_lifecycle_state(worker))[0]
    return isinstance(abort, dict) and abort.get("accepted") and status == WORKER_STATUS_ABORTED


def _abort_is_accepted(abort):
    return isinstance(abort, dict) and abort.get("accepted")


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


def _clear_current_status_fields(worker):
    worker["blockers"] = []
    worker["failure_category"] = None
    worker["failure_reason"] = None
    for field_name in REMOVABLE_WORKER_TRANSITION_FIELDS:
        worker.pop(field_name, None)


def _set_if_not_unset(fields, name, value):
    if value is not UNSET_TRANSITION_FIELD:
        fields[name] = deepcopy(value)


def _copy_present(value):
    return None if value is None else deepcopy(value)


def _copy_transition_value(value):
    if value is UNSET_TRANSITION_FIELD:
        return value
    return deepcopy(value)


def _filtered_prompt_ids(prompt_ids):
    return tuple(prompt_id for prompt_id in prompt_ids if prompt_id is not None)


def _finalize_worker_attempt(worker, finalization):
    if finalization is None:
        return
    _finalize_attempt(worker, {"id": finalization.attempt_id, "fields": finalization.fields})
