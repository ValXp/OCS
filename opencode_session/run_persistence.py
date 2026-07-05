from copy import deepcopy
from dataclasses import dataclass, field

from opencode_session.worker_state import WorkerTransition, normalize_worker


WORKER_SNAPSHOT_STATE_FIELDS = (
    "lifecycle_state",
    "status",
    "retry_count",
    "timeout_started_at",
    "timed_out_at",
    "failure_category",
    "failure_reason",
    "last_failure_category",
    "last_failure_reason",
    "next_eligible_action",
    "blockers",
    "output_refs",
    "error",
    "failure_retryable",
    "manual_retry_required",
    "result",
    "cleanup",
    "abort",
)
WORKER_SNAPSHOT_SET_IF_MISSING_FIELDS = ("session_id",)
REMOVABLE_WORKER_SNAPSHOT_FIELDS = ("error", "failure_retryable", "manual_retry_required")


@dataclass(frozen=True)
class WorkerSnapshotMerge:
    """Named stale-snapshot merge policy; not a lifecycle transition."""

    worker_id: str
    set_fields: dict = field(default_factory=dict)
    delete_fields: tuple = ()
    set_if_missing_fields: dict = field(default_factory=dict)
    merge_unique_fields: dict = field(default_factory=dict)

    @classmethod
    def from_worker_snapshot(cls, worker):
        worker_id = worker["id"]
        normalized = normalize_worker(_snapshot_state_source(worker), worker_id)
        set_fields = {"id": worker_id}
        for field_name in WORKER_SNAPSHOT_STATE_FIELDS:
            if field_name in normalized:
                set_fields[field_name] = deepcopy(normalized[field_name])
        prompt_ids = normalized.get("prompt_ids")
        merge_unique_fields = {"prompt_ids": tuple(prompt_ids)} if isinstance(prompt_ids, list) else {}
        set_if_missing_fields = {
            field_name: deepcopy(normalized[field_name])
            for field_name in WORKER_SNAPSHOT_SET_IF_MISSING_FIELDS
            if normalized.get(field_name)
        }
        return cls(
            worker_id,
            set_fields=set_fields,
            delete_fields=tuple(
                field_name for field_name in REMOVABLE_WORKER_SNAPSHOT_FIELDS if field_name not in normalized
            ),
            set_if_missing_fields=set_if_missing_fields,
            merge_unique_fields=merge_unique_fields,
        )

    def apply_to(self, latest_workers):
        return WorkerTransition(
            self.worker_id,
            set_fields=self.set_fields,
            delete_fields=self.delete_fields,
            set_if_missing_fields=self.set_if_missing_fields,
            merge_unique_fields=self.merge_unique_fields,
        ).apply_to(latest_workers)


def _snapshot_state_source(worker):
    source = deepcopy(worker)
    source.pop("lifecycle_state", None)
    return source


def persist_run_mutation(store, run, mutator, *, now):
    name = run["name"]

    def update(latest_run):
        mutator(latest_run)
        latest_run["updated_at"] = now()

    persisted = store.update_run(name, update)
    replace_mapping_in_place(run, persisted)
    return run


def persist_worker_update(store, run, worker, *, refresh_run_summary, now):
    return persist_worker_updates(store, run, [worker], refresh_run_summary=refresh_run_summary, now=now)


def persist_worker_updates(store, run, workers, *, refresh_run_summary, now):
    updates = [worker for worker in workers if isinstance(worker, (WorkerTransition, WorkerSnapshotMerge))]
    return persist_worker_transitions(store, run, updates, refresh_run_summary=refresh_run_summary, now=now)


def persist_worker_snapshot_update(store, run, worker, *, refresh_run_summary, now):
    return persist_worker_snapshot_updates(store, run, [worker], refresh_run_summary=refresh_run_summary, now=now)


def persist_worker_snapshot_updates(store, run, workers, *, refresh_run_summary, now):
    updates = [WorkerSnapshotMerge.from_worker_snapshot(worker) for worker in workers if isinstance(worker, dict) and worker.get("id")]
    return persist_worker_transitions(store, run, updates, refresh_run_summary=refresh_run_summary, now=now)


def persist_worker_transitions(store, run, transitions, *, refresh_run_summary, now):
    name = run["name"]
    transitions = tuple(transitions)

    def update(latest_run):
        latest_workers = latest_run.setdefault("workers", {})
        for transition in transitions:
            transition.apply_to(latest_workers)
        refresh_run_summary(latest_run)
        latest_run["updated_at"] = now()

    persisted = store.update_run(name, update)
    replace_mapping_in_place(run, persisted)
    return [
        run["workers"][transition.worker_id]
        for transition in transitions
        if transition.worker_id in run.get("workers", {})
    ]


def persist_run_summary(store, run, *, refresh_run_summary, now):
    name = run["name"]

    def update(latest_run):
        refresh_run_summary(latest_run)
        latest_run["updated_at"] = now()

    persisted = store.update_run(name, update)
    replace_mapping_in_place(run, persisted)
    return run


def replace_mapping_in_place(target, source):
    for key in list(target):
        if key not in source:
            del target[key]
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            replace_mapping_in_place(existing, value)
        elif isinstance(existing, list) and isinstance(value, list):
            existing[:] = deepcopy(value)
        else:
            target[key] = deepcopy(value)
