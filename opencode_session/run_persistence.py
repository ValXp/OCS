from copy import deepcopy

from opencode_session.worker_state import WorkerTransition


def persist_run_mutation(store, run, mutator, *, now):
    name = run["name"]

    def update(latest_run):
        mutator(latest_run)
        latest_run["updated_at"] = now()

    persisted = store.update_run(name, update)
    replace_run_snapshot(run, persisted)
    return run


def persist_worker_snapshot_update(store, run, worker, *, refresh_run_summary, now):
    return persist_worker_snapshot_updates(store, run, [worker], refresh_run_summary=refresh_run_summary, now=now)


def persist_worker_snapshot_updates(store, run, workers, *, refresh_run_summary, now):
    updates = [WorkerTransition.snapshot_applied(worker) for worker in workers if isinstance(worker, dict) and worker.get("id")]
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
    replace_run_snapshot(run, persisted)
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
    replace_run_snapshot(run, persisted)
    return run


def replace_run_snapshot(target, source):
    target.clear()
    target.update(deepcopy(source))
