from collections.abc import Mapping
from copy import deepcopy

from opencode_session.worker_attempt_log import _finalize_attempt
from opencode_session.worker_state import (
    WORKER_TRANSITION_METADATA as _WORKER_TRANSITION_METADATA,
    WorkerRecord,
    WorkerTransition,
    WorkerTransitionName,
    WorkerTransitionResult,
    _accepted_abort,
    apply_worker_transition_payload,
    latest_prompt_ids_are_retry_marker,
    worker_lifecycle_state,
    worker_transition_is_legal,
    worker_transition_target_lifecycle_state,
)


class WorkerLifecycleReducer:
    def __init__(self, record):
        self.record = record
        self.latest_worker = record.to_worker()

    def apply(self, transition):
        if not isinstance(transition.name, WorkerTransitionName):
            raise ValueError(f"unknown worker transition: {transition.name}")
        metadata = _worker_transition_metadata(transition.name)
        if not self._has_accepted_abort() and not worker_transition_is_legal(self.latest_worker, transition):
            return WorkerTransitionResult(
                applied=False,
                worker=self._unchanged_worker(transition),
                reason=_illegal_transition_reason(self.latest_worker, transition, metadata),
                stale_snapshot_recovery=_is_stale_snapshot_recovery(transition),
            )
        worker = apply_worker_transition_payload(self, transition)
        _finalize_worker_attempt(worker, transition.attempt_finalization)
        return WorkerTransitionResult(
            applied=True,
            worker=WorkerRecord.from_worker(
                worker,
                self.record.worker_id or transition.worker_id,
                allow_extra_fields=True,
            ).to_worker(),
        )

    def _copy_latest(self):
        return deepcopy(self.latest_worker.to_snapshot())

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


def _worker_transition_metadata(name):
    if not isinstance(name, WorkerTransitionName):
        raise ValueError(f"unknown worker transition: {name}")
    metadata = _WORKER_TRANSITION_METADATA.get(name)
    if metadata is None:
        raise ValueError(f"unknown worker transition: {name}")
    return metadata


def _illegal_transition_reason(latest_worker, transition, metadata):
    source_state = worker_lifecycle_state(latest_worker)
    transition_name = transition.name.value
    target_state = _transition_target_lifecycle_state_for_reason(transition)
    target = f" to lifecycle_state '{target_state}'" if target_state is not None else ""
    if _is_stale_snapshot_recovery(transition):
        return (
            f"stale snapshot ignored for worker '{transition.worker_id}': transition "
            f"'{transition_name}' cannot move from lifecycle_state '{source_state}'{target}"
        )
    allowed = ", ".join(sorted(metadata.source_states)) or "none"
    return (
        f"illegal worker transition '{transition_name}' for worker '{transition.worker_id}' "
        f"from lifecycle_state '{source_state}'{target}; allowed source states: {allowed}"
    )


def _transition_target_lifecycle_state_for_reason(transition):
    if _is_stale_snapshot_recovery(transition):
        return _snapshot_target_lifecycle_state(transition)
    try:
        return worker_transition_target_lifecycle_state(transition)
    except (KeyError, TypeError, AttributeError):
        return None


def _snapshot_target_lifecycle_state(transition):
    payload = getattr(transition, "payload", None)
    patch = getattr(payload, "patch", None)
    return getattr(patch, "target_lifecycle_state", None)


def _is_stale_snapshot_recovery(transition):
    if transition.name is not WorkerTransitionName.SNAPSHOT_APPLIED:
        return False
    patch = getattr(getattr(transition, "payload", None), "patch", None)
    return bool(getattr(patch, "stale_recovery_allowed", False))


def _merge_unique_list_field(target, latest_worker, worker_record, field_name):
    merged_values = []
    for source in (latest_worker, worker_record):
        if isinstance(source, WorkerRecord):
            values = source.field(field_name)
        else:
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
