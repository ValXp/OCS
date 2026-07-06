from dataclasses import dataclass
from collections.abc import Mapping

from opencode_session.worker_storage_adapter import normalize_worker_snapshot_for_storage
from opencode_session.worker_state import (
    WorkerTransition,
    WorkerRecord,
    apply_worker_transition,
)


@dataclass(frozen=True)
class PersistedWorkerTransitions:
    run: dict
    workers: list


def persist_run_mutation(store, run, mutator, *, now):
    name = run["name"]

    def update(latest_run):
        mutator(latest_run)
        latest_run["updated_at"] = now()

    return store.update_run(name, update)


def persist_worker_snapshot_update(store, run, worker, *, refresh_run_summary, now):
    return persist_worker_snapshot_updates(store, run, [worker], refresh_run_summary=refresh_run_summary, now=now)


def persist_worker_snapshot_updates(store, run, workers, *, refresh_run_summary, now):
    updates = [_snapshot_update_transition(worker) for worker in workers]
    updates = [transition for transition in updates if transition is not None]
    return persist_worker_transitions(store, run, updates, refresh_run_summary=refresh_run_summary, now=now)


def _snapshot_update_transition(worker):
    if isinstance(worker, WorkerRecord):
        worker_id = worker.worker_id
    elif isinstance(worker, Mapping):
        worker_id = worker.get("id")
    else:
        return None
    if not worker_id:
        return None
    return WorkerTransition.snapshot_applied(normalize_worker_snapshot_for_storage(worker, worker_id))


def persist_worker_transitions(store, run, transitions, *, refresh_run_summary, now):
    name = run["name"]
    transitions = tuple(transitions)

    def update(latest_run):
        latest_workers = latest_run.setdefault("workers", {})
        for transition in transitions:
            apply_worker_transition(latest_workers, transition)
        refresh_run_summary(latest_run)
        latest_run["updated_at"] = now()

    persisted = store.update_run(name, update)
    return PersistedWorkerTransitions(
        persisted,
        [
            persisted["workers"][transition.worker_id]
            for transition in transitions
            if transition.worker_id in persisted.get("workers", {})
        ],
    )


def persist_run_summary(store, run, *, refresh_run_summary, now):
    name = run["name"]

    def update(latest_run):
        refresh_run_summary(latest_run)
        latest_run["updated_at"] = now()

    return store.update_run(name, update)
