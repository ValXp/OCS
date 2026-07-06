from copy import deepcopy
from dataclasses import dataclass

from opencode_session.status import short_status
from opencode_session.worker_lifecycle import (
    WORKER_ACTION_NONE,
    WORKER_ACTION_RETRY,
    WORKER_ACTION_START,
    WORKER_LIFECYCLE_ABORTED,
    WORKER_LIFECYCLE_ACTIVE_RETRY,
    WORKER_LIFECYCLE_ACTIVE_WAIT,
    WORKER_LIFECYCLE_BLOCKED_DEPENDENCY,
    WORKER_LIFECYCLE_BLOCKED_TIMEOUT,
    WORKER_LIFECYCLE_DONE_COLLECT,
    WORKER_LIFECYCLE_FAILED_RETRY,
    WORKER_LIFECYCLE_FAILED_TERMINAL,
    WORKER_LIFECYCLE_QUEUED,
    WORKER_LIFECYCLE_STATES,
    WORKER_LIFECYCLE_TIMEOUT_ABORTED,
    WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
    WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL,
    WORKER_LIFECYCLE_TIMEOUT_RETRY,
    WORKER_LIFECYCLE_TIMEOUT_TERMINAL,
    WORKER_LIST_FIELDS,
    WORKER_STATUS_ABORTED,
    WORKER_STATUS_ACTIVE,
    WORKER_STATUS_BLOCKED,
    WORKER_STATUS_DONE,
    WORKER_STATUS_FAILED,
    WORKER_STATUS_QUEUED,
    WORKER_STATUS_TIMEOUT,
    is_blocked_status,
    public_worker_state,
    public_worker_state_fields,
    worker_lifecycle_set_fields,
    worker_retry_available,
)


@dataclass(frozen=True)
class WorkerRecord:
    worker_id: str
    fields: dict
    has_explicit_lifecycle: bool = False

    @classmethod
    def from_worker(cls, worker, worker_id=None):
        fields = dict(worker) if isinstance(worker, dict) else {}
        resolved_worker_id = fields.get("id") or worker_id
        has_explicit_lifecycle = isinstance(worker, dict) and bool(worker.get("lifecycle_state"))
        return cls(resolved_worker_id, fields, has_explicit_lifecycle)

    @classmethod
    def default_fields(cls, worker_id):
        return {
            "id": worker_id,
            "role": None,
            "session_id": None,
            "agent": None,
            "model": None,
            "dependencies": [],
            "prompt_ids": [],
            "status": WORKER_STATUS_QUEUED,
            "retry_count": 0,
            "retry_limit": 0,
            "retryable_failures": [],
            "timeout_seconds": None,
            "timeout_policy": WORKER_STATUS_TIMEOUT,
            "timeout_started_at": None,
            "timed_out_at": None,
            "lifecycle_state": WORKER_LIFECYCLE_QUEUED,
            "failure_category": None,
            "failure_reason": None,
            "last_failure_category": None,
            "last_failure_reason": None,
            "next_eligible_action": WORKER_ACTION_START,
            "blockers": [],
            "output_refs": [],
        }

    @classmethod
    def public_state(cls, lifecycle_state):
        return public_worker_state(lifecycle_state)

    @classmethod
    def public_state_fields(cls, lifecycle_state):
        return public_worker_state_fields(lifecycle_state)

    @classmethod
    def lifecycle_set_fields(cls, worker_id, lifecycle_state):
        return worker_lifecycle_set_fields(worker_id, lifecycle_state)

    @property
    def lifecycle_state(self):
        lifecycle_state = self.fields.get("lifecycle_state")
        if lifecycle_state in WORKER_LIFECYCLE_STATES:
            return lifecycle_state
        return self._infer_lifecycle_state(self.fields)

    @property
    def status(self):
        return self.public_state(self.lifecycle_state)[0]

    @property
    def next_eligible_action(self):
        return self.public_state(self.lifecycle_state)[1]

    def scheduling_state(self):
        from opencode_session.worker_scheduling import WorkerSchedulingState, worker_has_prompt

        return WorkerSchedulingState(
            self.lifecycle_state,
            self.status,
            self.next_eligible_action,
            worker_has_prompt(self.fields),
        )

    def to_worker(self):
        normalized = self.default_fields(self.worker_id)
        normalized.update(self.fields)
        normalized["id"] = normalized.get("id") or self.worker_id
        for key in WORKER_LIST_FIELDS:
            value = normalized.get(key)
            normalized[key] = value if isinstance(value, list) else []
        if normalized.get("retry_count") is None:
            normalized["retry_count"] = 0
        if normalized.get("retry_limit") is None:
            normalized["retry_limit"] = 0
        if not normalized.get("timeout_policy"):
            normalized["timeout_policy"] = WORKER_STATUS_TIMEOUT
        if not normalized.get("status"):
            normalized["status"] = WORKER_STATUS_QUEUED
        else:
            normalized["status"] = short_status(normalized["status"])
        lifecycle_source = dict(normalized)
        if not self.has_explicit_lifecycle:
            lifecycle_source.pop("lifecycle_state", None)
        lifecycle_record = WorkerRecord.from_worker(lifecycle_source, normalized["id"])
        normalized.update(lifecycle_record.serialized_public_state())
        return normalized

    def serialized_public_state(self):
        return self.public_state_fields(self.lifecycle_state)

    def apply_transition_spec(self, spec):
        latest_worker = self.to_worker()
        set_fields = deepcopy(spec.set_fields or {})
        set_if_missing_fields = deepcopy(spec.set_if_missing_fields or {})
        merge_unique_fields = deepcopy(spec.merge_unique_fields or {})
        if spec.preserve_accepted_abort and _accepted_abort(latest_worker) and not _accepted_abort(set_fields):
            merged = self._transition_for_aborted_worker(latest_worker, set_fields, merge_unique_fields)
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
        return WorkerRecord.from_worker(merged, self.worker_id).to_worker()

    @staticmethod
    def _transition_for_aborted_worker(latest_worker, set_fields, merge_unique_fields):
        merged = deepcopy(latest_worker)
        _merge_unique_fields(merged, latest_worker, merge_unique_fields)
        if "cleanup" in set_fields:
            merged["cleanup"] = deepcopy(set_fields["cleanup"])
        return merged

    @staticmethod
    def _infer_lifecycle_state(worker):
        status = short_status(worker.get("status") if isinstance(worker, dict) else None)
        if status == WORKER_STATUS_QUEUED:
            return WORKER_LIFECYCLE_QUEUED
        if status == WORKER_STATUS_ACTIVE:
            if worker.get("next_eligible_action") == WORKER_ACTION_RETRY:
                return WORKER_LIFECYCLE_ACTIVE_RETRY
            return WORKER_LIFECYCLE_ACTIVE_WAIT
        if is_blocked_status(status):
            if worker.get("failure_category") == WORKER_STATUS_TIMEOUT or WORKER_STATUS_TIMEOUT in set(
                worker.get("blockers") or []
            ):
                return WORKER_LIFECYCLE_BLOCKED_TIMEOUT
            return WORKER_LIFECYCLE_BLOCKED_DEPENDENCY
        if status == WORKER_STATUS_DONE:
            return WORKER_LIFECYCLE_DONE_COLLECT
        if status == WORKER_STATUS_FAILED:
            if worker.get("failure_category") == WORKER_STATUS_TIMEOUT:
                if worker_retry_available(worker, WORKER_STATUS_TIMEOUT):
                    return WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY
                return WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL
            if worker_retry_available(worker):
                return WORKER_LIFECYCLE_FAILED_RETRY
            return WORKER_LIFECYCLE_FAILED_TERMINAL
        if status == WORKER_STATUS_TIMEOUT:
            if worker_retry_available(worker, WORKER_STATUS_TIMEOUT):
                return WORKER_LIFECYCLE_TIMEOUT_RETRY
            return WORKER_LIFECYCLE_TIMEOUT_TERMINAL
        if status == WORKER_STATUS_ABORTED:
            if worker.get("failure_category") == WORKER_STATUS_TIMEOUT:
                return WORKER_LIFECYCLE_TIMEOUT_ABORTED
            return WORKER_LIFECYCLE_ABORTED
        return WORKER_LIFECYCLE_QUEUED


def worker_lifecycle_state(worker):
    if not isinstance(worker, dict):
        return None
    return WorkerRecord.from_worker(worker).lifecycle_state


def infer_worker_lifecycle_state(worker):
    return WorkerRecord._infer_lifecycle_state(worker)


def latest_prompt_ids_are_retry_marker(latest_worker):
    return (
        isinstance(latest_worker, dict)
        and WorkerRecord.from_worker(latest_worker).lifecycle_state == WORKER_LIFECYCLE_ACTIVE_RETRY
        and latest_worker.get("last_failure_category") is not None
    )


def snapshot_state_source(worker):
    source = deepcopy(worker)
    source.pop("lifecycle_state", None)
    return source


def _accepted_abort(worker):
    abort = worker.get("abort") if isinstance(worker, dict) else None
    return isinstance(abort, dict) and abort.get("accepted") and worker.get("status") == WORKER_STATUS_ABORTED


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
