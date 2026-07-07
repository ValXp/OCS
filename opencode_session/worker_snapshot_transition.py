from copy import deepcopy

from opencode_session.worker_state import (
    WORKER_SNAPSHOT_ACCEPTED_ABORT_PASSTHROUGH_FIELD_NAMES,
    WORKER_SNAPSHOT_PROMPT_ID_FIELD_NAMES,
    WORKER_SNAPSHOT_REMOVE_WHEN_ABSENT_FIELD_NAMES,
    WORKER_SNAPSHOT_REPLAY_FIELD_NAMES,
    WORKER_SNAPSHOT_SET_IF_MISSING_FIELD_NAMES,
    WorkerRecord,
    WorkerSnapshotTransitionPatch,
    WorkerTransition,
)


def worker_snapshot_transition_patch(worker, worker_id=None):
    worker = _snapshot_worker_record(worker)
    worker_id = _snapshot_worker_id(worker, worker_id)
    snapshot = worker.to_snapshot()
    snapshot["id"] = worker_id
    fields = {"id": worker_id}
    for field_name in WORKER_SNAPSHOT_REPLAY_FIELD_NAMES:
        if field_name in snapshot:
            fields[field_name] = deepcopy(snapshot[field_name])
    prompt_ids = _snapshot_prompt_ids(snapshot)
    return WorkerSnapshotTransitionPatch(
        worker_id,
        fields,
        target_lifecycle_state=fields.get("lifecycle_state"),
        prompt_ids=prompt_ids,
        set_if_missing_fields={
            field_name: deepcopy(snapshot[field_name])
            for field_name in WORKER_SNAPSHOT_SET_IF_MISSING_FIELD_NAMES
            if snapshot.get(field_name)
        },
        remove_fields=tuple(
            field_name
            for field_name in WORKER_SNAPSHOT_REMOVE_WHEN_ABSENT_FIELD_NAMES
            if field_name not in snapshot
        ),
        stale_recovery_allowed=True,
        accepted_abort_fields={
            field_name: deepcopy(snapshot[field_name])
            for field_name in WORKER_SNAPSHOT_ACCEPTED_ABORT_PASSTHROUGH_FIELD_NAMES
            if field_name in snapshot
        },
        accepted_abort_prompt_ids=prompt_ids,
    )


def worker_snapshot_transition(worker, worker_id=None):
    return WorkerTransition.snapshot_applied(worker_snapshot_transition_patch(worker, worker_id))


def _snapshot_worker_id(worker, worker_id=None):
    if worker_id:
        return worker_id
    if worker.worker_id:
        return worker.worker_id
    raise ValueError("snapshot worker requires id")


def _snapshot_worker_record(worker):
    if isinstance(worker, WorkerRecord):
        return worker
    raise TypeError("snapshot worker must be WorkerRecord; hydrate raw mappings at the storage boundary")


def _optional_tuple(value):
    if isinstance(value, list):
        return tuple(value)
    return None


def _snapshot_prompt_ids(snapshot):
    for field_name in WORKER_SNAPSHOT_PROMPT_ID_FIELD_NAMES:
        prompt_ids = _optional_tuple(snapshot.get(field_name))
        if prompt_ids is not None:
            return prompt_ids
    return None
