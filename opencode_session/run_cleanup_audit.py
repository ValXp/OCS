from copy import deepcopy

from opencode_session.run_store import RunStoreError


def begin_cleanup_audit(store, name, expected_run, record):
    in_progress = deepcopy(record)
    in_progress["status"] = "in_progress"
    return _update_if_unchanged(store, name, expected_run, in_progress)


def finish_cleanup_audit(store, name, expected_run, record):
    try:
        return _update_if_unchanged(store, name, expected_run, record)
    except RunStoreError as error:
        if error.kind != "conflict":
            raise
        _record_store_error(record, str(error))
        return store.update_run(
            name,
            lambda latest: latest.__setitem__("resource_cleanup", deepcopy(record)),
            allow_cleanup_in_progress=True,
        )


def delete_audited_run(store, name, expected_run, record):
    try:
        store.delete_run(name, expected_run=expected_run)
    except RunStoreError as error:
        _record_store_error(record, str(error))
        if error.kind != "missing":
            store.update_run(
                name,
                lambda latest: latest.__setitem__("resource_cleanup", deepcopy(record)),
                allow_cleanup_in_progress=True,
            )
        return False
    record["run_store_deleted"] = True
    return True


def _update_if_unchanged(store, name, expected_run, cleanup_record):
    def update(latest):
        if latest != expected_run:
            raise RunStoreError(
                "run record changed during cleanup; refusing to use the stale cleanup plan",
                kind="conflict",
            )
        latest["resource_cleanup"] = deepcopy(cleanup_record)

    return store.update_run(name, update, allow_cleanup_in_progress=True)


def _record_store_error(record, message):
    record["errors"].append({"category": "run_store", "error": message})
    record["status"] = "partial" if any(record["completed"].values()) else "failed"
