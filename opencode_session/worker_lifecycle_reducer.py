from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass

from opencode_session.status import short_status
from opencode_session.worker_attempt_log import _append_attempt, _finalize_attempt
from opencode_session.worker_state import (
    REMOVABLE_WORKER_TRANSITION_FIELDS,
    UNSET_TRANSITION_FIELD,
    WORKER_STATUS_ABORTED,
    WORKER_STATUS_BLOCKED,
    WORKER_STATUS_DONE,
    WORKER_STATUS_TIMEOUT,
    WORKER_TRANSITION_METADATA,
    WorkerRecord,
    WorkerTransition,
    WorkerTransitionName,
    WorkerTransitionResult,
    latest_prompt_ids_are_retry_marker,
    public_worker_state,
    worker_lifecycle_set_fields,
    worker_lifecycle_state,
    worker_transition_is_legal,
    worker_transition_target_lifecycle_state,
)


@dataclass(frozen=True)
class WorkerTransitionDefinition:
    metadata: object
    apply_transition: object

    @property
    def name(self):
        return self.metadata.name

    @property
    def source_states(self):
        return self.metadata.source_states

    @property
    def target_states(self):
        return self.metadata.target_states

    @property
    def target_lifecycle(self):
        return self.metadata.target_lifecycle

    def is_legal(self, latest_worker, transition):
        return worker_transition_is_legal(latest_worker, transition)

    def apply(self, reducer, transition):
        return self.apply_transition(reducer, transition)

    def target_lifecycle_state(self, transition):
        return worker_transition_target_lifecycle_state(transition)


class WorkerLifecycleReducer:
    def __init__(self, record):
        self.record = record
        self.latest_worker = record.to_snapshot()

    def apply(self, transition):
        if not isinstance(transition.name, WorkerTransitionName):
            raise ValueError(f"unknown worker transition: {transition.name}")
        definition = _worker_transition_definition(transition.name)
        if not self._has_accepted_abort() and not definition.is_legal(self.latest_worker, transition):
            return WorkerTransitionResult(
                applied=False,
                worker=self._unchanged_worker(transition),
                reason=_illegal_transition_reason(self.latest_worker, transition, definition),
                stale_snapshot_recovery=_is_stale_snapshot_recovery(transition),
            )
        worker = definition.apply(self, transition)
        _finalize_worker_attempt(worker, transition.attempt_finalization)
        return WorkerTransitionResult(
            applied=True,
            worker=WorkerRecord.from_worker(worker, self.record.worker_id or transition.worker_id).to_worker(),
        )

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
        worker.update(worker_lifecycle_set_fields(transition.worker_id, _transition_target_lifecycle_state(transition)))
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
        worker.update(worker_lifecycle_set_fields(transition.worker_id, _transition_target_lifecycle_state(transition)))
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
        worker.update(worker_lifecycle_set_fields(transition.worker_id, _transition_target_lifecycle_state(transition)))
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
            worker.update(worker_lifecycle_set_fields(transition.worker_id, _transition_target_lifecycle_state(transition)))
        return worker

    def retry_scheduled(self, transition):
        payload = transition.payload
        worker = self._copy_latest()
        if self._has_accepted_abort():
            self._merge_prompt_ids(worker, payload.prompt_ids)
            return worker
        _clear_current_status_fields(worker)
        worker.update(worker_lifecycle_set_fields(transition.worker_id, _transition_target_lifecycle_state(transition)))
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
        worker.update(worker_lifecycle_set_fields(transition.worker_id, _transition_target_lifecycle_state(transition)))
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
        worker.update(worker_lifecycle_set_fields(transition.worker_id, _transition_target_lifecycle_state(transition)))
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

    def _unchanged_worker(self, transition):
        return WorkerRecord.from_worker(self.latest_worker, self.record.worker_id or transition.worker_id).to_worker()

    def _has_accepted_abort(self):
        return _accepted_abort(self.latest_worker)

    def _merge_prompt_ids(self, worker, prompt_ids, *, merge_empty=False):
        if not prompt_ids and not merge_empty:
            return
        source_worker = {} if latest_prompt_ids_are_retry_marker(self.latest_worker) else self.latest_worker
        _merge_unique_list_field(worker, source_worker, {"prompt_ids": list(prompt_ids)}, "prompt_ids")


def apply_worker_transition_to_record(record, transition):
    return WorkerLifecycleReducer(record).apply(transition)


_WORKER_TRANSITION_DEFINITIONS = {
    name: WorkerTransitionDefinition(
        metadata,
        getattr(WorkerLifecycleReducer, metadata.apply_method),
    )
    for name, metadata in WORKER_TRANSITION_METADATA.items()
}


def _worker_transition_definition(name):
    definition = _WORKER_TRANSITION_DEFINITIONS.get(name)
    if definition is None:
        raise ValueError(f"unknown worker transition: {name}")
    return definition


def _illegal_transition_reason(latest_worker, transition, definition):
    source_state = worker_lifecycle_state(latest_worker)
    transition_name = transition.name.value
    target_state = _transition_target_lifecycle_state_for_reason(transition, definition)
    target = f" to lifecycle_state '{target_state}'" if target_state is not None else ""
    if _is_stale_snapshot_recovery(transition):
        return (
            f"stale snapshot ignored for worker '{transition.worker_id}': transition "
            f"'{transition_name}' cannot move from lifecycle_state '{source_state}'{target}"
        )
    allowed = ", ".join(sorted(definition.source_states)) or "none"
    return (
        f"illegal worker transition '{transition_name}' for worker '{transition.worker_id}' "
        f"from lifecycle_state '{source_state}'{target}; allowed source states: {allowed}"
    )


def _transition_target_lifecycle_state_for_reason(transition, definition):
    if _is_stale_snapshot_recovery(transition):
        return _snapshot_target_lifecycle_state(transition)
    try:
        return definition.target_lifecycle_state(transition)
    except (KeyError, TypeError, AttributeError):
        return None


def _snapshot_target_lifecycle_state(transition):
    payload = getattr(transition, "payload", None)
    state_fields = getattr(payload, "state_fields", ())
    worker = getattr(payload, "worker", {})
    if "lifecycle_state" not in state_fields or not isinstance(worker, Mapping):
        return None
    return worker.get("lifecycle_state")


def _is_stale_snapshot_recovery(transition):
    return transition.name is WorkerTransitionName.SNAPSHOT_APPLIED


def _transition_target_lifecycle_state(transition):
    return worker_transition_target_lifecycle_state(transition)


def _snapshot_transition_fields(transition):
    payload = transition.payload
    fields = {"id": transition.worker_id}
    for field_name in payload.state_fields:
        if field_name in payload.worker:
            fields[field_name] = deepcopy(payload.worker[field_name])
    return fields


def _accepted_abort(worker):
    abort = worker.get("abort") if isinstance(worker, Mapping) else None
    status = public_worker_state(worker_lifecycle_state(worker))[0]
    return isinstance(abort, dict) and abort.get("accepted") and status == WORKER_STATUS_ABORTED


def _abort_is_accepted(abort):
    return isinstance(abort, dict) and abort.get("accepted")


def _merge_unique_list_field(target, latest_worker, worker_record, field_name):
    merged_values = []
    for source in (latest_worker, worker_record):
        values = source.get(field_name) if isinstance(source, Mapping) else None
        if not isinstance(values, list):
            continue
        for value in values:
            if value not in merged_values:
                merged_values.append(deepcopy(value))
    target[field_name] = merged_values


def _clear_current_status_fields(worker):
    worker["blockers"] = []
    worker["failure_category"] = None
    worker["failure_reason"] = None
    for field_name in REMOVABLE_WORKER_TRANSITION_FIELDS:
        worker.pop(field_name, None)


def _set_if_not_unset(fields, name, value):
    if value is not UNSET_TRANSITION_FIELD:
        fields[name] = deepcopy(value)


def _finalize_worker_attempt(worker, finalization):
    if finalization is None:
        return
    _finalize_attempt(worker, {"id": finalization.attempt_id, "fields": finalization.fields})
