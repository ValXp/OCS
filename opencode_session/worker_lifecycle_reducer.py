from collections.abc import Mapping
from copy import deepcopy

from opencode_session.worker_attempt_log import _finalize_attempt
from opencode_session.worker_state import (
    WORKER_TRANSITION_DEFINITIONS as _WORKER_TRANSITION_DEFINITIONS,
    WorkerRecord,
    WorkerTransition,
    WorkerTransitionName,
    WorkerTransitionResult,
    _accepted_abort,
    latest_prompt_ids_are_retry_marker,
    worker_lifecycle_state,
)


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


def _finalize_worker_attempt(worker, finalization):
    if finalization is None:
        return
    _finalize_attempt(worker, {"id": finalization.attempt_id, "fields": finalization.fields})
