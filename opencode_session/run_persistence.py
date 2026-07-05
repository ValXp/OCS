from copy import deepcopy


REMOVABLE_WORKER_TRANSITION_FIELDS = ("error", "failure_retryable", "manual_retry_required")


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
    name = run["name"]
    worker_records = {worker["id"]: deepcopy(worker) for worker in workers if isinstance(worker, dict) and worker.get("id")}

    def update(latest_run):
        latest_workers = latest_run.setdefault("workers", {})
        for worker_id, worker_record in worker_records.items():
            latest_workers[worker_id] = merge_worker_transition(latest_workers.get(worker_id), worker_record)
        refresh_run_summary(latest_run)
        latest_run["updated_at"] = now()

    persisted = store.update_run(name, update)
    replace_mapping_in_place(run, persisted)
    return [run["workers"][worker_id] for worker_id in worker_records if worker_id in run.get("workers", {})]


def merge_worker_transition(latest_worker, worker_record):
    if not isinstance(latest_worker, dict):
        return deepcopy(worker_record)
    if _accepted_abort(latest_worker) and not _accepted_abort(worker_record):
        return _merge_into_aborted_worker(latest_worker, worker_record)

    merged = deepcopy(latest_worker)
    merged.update(deepcopy(worker_record))
    _delete_removed_transition_fields(merged, worker_record)
    _merge_unique_list_field(merged, latest_worker, worker_record, "prompt_ids")
    if "abort" not in worker_record and "abort" in latest_worker:
        merged["abort"] = deepcopy(latest_worker["abort"])
    return merged


def _delete_removed_transition_fields(target, worker_record):
    for field in REMOVABLE_WORKER_TRANSITION_FIELDS:
        if field not in worker_record:
            target.pop(field, None)


def _merge_into_aborted_worker(latest_worker, worker_record):
    merged = deepcopy(latest_worker)
    _merge_unique_list_field(merged, latest_worker, worker_record, "prompt_ids")
    if "cleanup" in worker_record:
        merged["cleanup"] = deepcopy(worker_record["cleanup"])
    return merged


def _accepted_abort(worker):
    abort = worker.get("abort") if isinstance(worker, dict) else None
    return isinstance(abort, dict) and abort.get("accepted") and worker.get("status") == "aborted"


def _merge_unique_list_field(target, latest_worker, worker_record, field):
    merged_values = []
    for source in (latest_worker, worker_record):
        values = source.get(field) if isinstance(source, dict) else None
        if not isinstance(values, list):
            continue
        for value in values:
            if value not in merged_values:
                merged_values.append(deepcopy(value))
    target[field] = merged_values


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
