from dataclasses import dataclass

from opencode_session.run_record import ensure_run_workers, run_name, run_worker, set_run_updated_at
from opencode_session.worker_snapshot_transition import worker_snapshot_transition
from opencode_session.worker_state import (
    WorkerRecord,
    apply_worker_transition,
)


@dataclass(frozen=True)
class PersistedWorkerTransitions:
    run: dict
    workers: list


def persist_run_mutation(store, run, mutator, *, now):
    name = run_name(run)

    def update(latest_run):
        mutator(latest_run)
        set_run_updated_at(latest_run, now())

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
    elif worker is None:
        return None
    else:
        raise TypeError("worker snapshot updates require WorkerRecord; hydrate raw mappings at the storage boundary")
    if not worker_id:
        return None
    return worker_snapshot_transition(worker, worker_id)


def persist_worker_transitions(store, run, transitions, *, refresh_run_summary, now):
    name = run_name(run)
    transitions = tuple(transitions)

    def update(latest_run):
        latest_workers = ensure_run_workers(latest_run)
        for transition in transitions:
            apply_worker_transition(latest_workers, transition)
        refresh_run_summary(latest_run)
        set_run_updated_at(latest_run, now())

    persisted = store.update_run(name, update)
    persisted_workers = []
    for transition in transitions:
        worker = run_worker(persisted, transition.worker_id)
        if worker is not None:
            persisted_workers.append(worker)
    return PersistedWorkerTransitions(persisted, persisted_workers)


def persist_run_summary(store, run, *, refresh_run_summary, now):
    name = run_name(run)

    def update(latest_run):
        refresh_run_summary(latest_run)
        set_run_updated_at(latest_run, now())

    return store.update_run(name, update)
