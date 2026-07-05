from copy import deepcopy

from opencode_session.worker_state import WorkerTransition


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
    transitions = [
        worker if isinstance(worker, WorkerTransition) else WorkerTransition.from_worker_snapshot(worker)
        for worker in workers
        if isinstance(worker, WorkerTransition) or isinstance(worker, dict) and worker.get("id")
    ]
    return persist_worker_transitions(store, run, transitions, refresh_run_summary=refresh_run_summary, now=now)


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
