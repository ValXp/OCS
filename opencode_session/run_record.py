from copy import deepcopy
from pathlib import Path

from opencode_session.run_resource_schema import normalize_run_resources
from opencode_session.status import short_status
from opencode_session.worker_storage_adapter import (
    hydrate_worker_record,
    normalize_worker_snapshot_for_storage,
)
from opencode_session.worker_state import (
    WORKER_LIFECYCLE_STATES,
    WORKER_RUN_UPSERT_FIELD_NAMES,
    default_worker_record,
    is_worker_record,
    worker_record_for_mutation,
    worker_output_dict,
)


SCHEMA_VERSION = 1
DEFAULT_RUN_STATUS = "queued"
DEFAULT_SERVER_URL = "http://127.0.0.1:4096"


class RunRecordError(Exception):
    def __init__(self, message, *, kind="data"):
        super().__init__(message)
        self.kind = kind


def new_run_record(name, *, directory, server_url, now):
    return {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "run_id": name,
        "directory": str(Path(directory).resolve()),
        "server_url": server_url,
        "status": DEFAULT_RUN_STATUS,
        "retry_count": 0,
        "timeout_seconds": None,
        "blockers": [],
        "output_refs": [],
        "workers": {},
        "created_at": now,
        "updated_at": now,
    }


def run_name(run):
    return run["name"]


def run_optional_name(run):
    return run.get("name")


def run_directory(run):
    return run["directory"]


def run_optional_directory(run):
    return run.get("directory")


def run_server_url(run):
    return run["server_url"]


def run_workers(run):
    workers = run.get("workers")
    if workers is None:
        return {}
    if not isinstance(workers, dict):
        raise RunRecordError("run record is corrupted: workers must be an object")
    return workers


def ensure_run_workers(run):
    workers = run.get("workers")
    if workers is None:
        workers = {}
        run["workers"] = workers
    if not isinstance(workers, dict):
        raise RunRecordError("run record is corrupted: workers must be an object")
    return workers


def run_worker(run, worker_id, default=None):
    return run_workers(run).get(worker_id, default)


def set_run_worker(run, worker_id, worker):
    ensure_run_workers(run)[worker_id] = worker


def ensure_run_worker(run, worker_id, *, role):
    workers = ensure_run_workers(run)
    existing = workers.get(worker_id)
    if existing is None:
        worker = default_worker_record(worker_id)
    else:
        worker = worker_record_for_mutation(existing, worker_id).to_worker()
    if not worker.role:
        worker.role = deepcopy(role)
    worker.id = worker_id
    set_run_worker(run, worker_id, worker)
    return worker


def set_run_directory(run, directory):
    run["directory"] = str(Path(directory).resolve())


def set_run_server_url(run, server_url):
    run["server_url"] = server_url


def set_run_status(run, status):
    run["status"] = status


def set_run_updated_at(run, updated_at):
    run["updated_at"] = updated_at


def upsert_worker_record(run, worker_id, changes, *, now):
    workers = ensure_run_workers(run)
    existing = workers.get(worker_id)
    if existing is None:
        if not changes.get("role"):
            raise RunRecordError(f"worker '{worker_id}' does not exist; --role is required to create it")
        worker = default_worker_record(worker_id)
    else:
        worker = worker_record_for_mutation(existing, worker_id).to_worker()

    for public_field_name in ("status", "next_eligible_action"):
        if changes.get(public_field_name) is not None:
            raise RunRecordError(
                f"worker '{worker_id}' updates must use lifecycle_state, not {public_field_name}"
            )
    if changes.get("lifecycle_state") is not None:
        lifecycle_state = changes["lifecycle_state"]
        if lifecycle_state not in WORKER_LIFECYCLE_STATES:
            raise RunRecordError(f"worker '{worker_id}' has invalid lifecycle_state: {lifecycle_state}")
    worker.update_canonical_fields_from_mapping(
        changes,
        skip_none=True,
        field_names=WORKER_RUN_UPSERT_FIELD_NAMES,
    )

    workers[worker_id] = worker.to_worker()
    set_run_updated_at(run, now)


def normalize_run(run, *, fallback_name):
    normalized = _normalize_run_fields(run, fallback_name=fallback_name)
    run_schema_version = _worker_snapshot_schema_version(normalized)
    workers = _run_worker_snapshots(normalized, fallback_name=fallback_name)
    normalized["workers"] = {
        worker_id: _normalize_worker_for_run(worker, worker_id, run_schema_version=run_schema_version)
        for worker_id, worker in workers.items()
    }
    return normalized


def _normalize_worker_for_run(worker, worker_id, *, run_schema_version):
    if is_worker_record(worker):
        return worker.to_worker()
    return hydrate_worker_record(worker, worker_id, run_schema_version=run_schema_version)


def normalize_run_for_storage(run, *, fallback_name, persisted_run=None):
    persisted_workers = _worker_snapshots(persisted_run if persisted_run is not None else run)
    normalized = _normalize_run_fields(run, fallback_name=fallback_name)
    run_schema_version = _worker_snapshot_schema_version(normalized)
    workers = _run_worker_snapshots(normalized, fallback_name=fallback_name)
    normalized["workers"] = {
        worker_id: normalize_worker_snapshot_for_storage(
            worker,
            worker_id,
            run_schema_version=run_schema_version,
            persisted_worker=persisted_workers.get(worker_id),
        )
        for worker_id, worker in workers.items()
    }
    return normalized


def _normalize_run_fields(run, *, fallback_name):
    normalized = dict(run)
    normalized.setdefault("schema_version", SCHEMA_VERSION)
    if not normalized.get("name"):
        normalized["name"] = fallback_name
    if not normalized.get("run_id"):
        normalized["run_id"] = normalized["name"]
    normalized.setdefault("directory", str(Path.cwd()))
    if not normalized.get("server_url"):
        normalized["server_url"] = DEFAULT_SERVER_URL
    normalized.setdefault("status", DEFAULT_RUN_STATUS)
    normalized["status"] = short_status(normalized["status"])
    normalized.setdefault("retry_count", 0)
    normalized.setdefault("timeout_seconds", None)
    normalized.setdefault("blockers", [])
    normalized.setdefault("output_refs", [])
    if "resources" in normalized:
        normalized["resources"] = normalize_run_resources(normalized.get("resources"))
    if "resource_cleanup" in normalized and not isinstance(normalized.get("resource_cleanup"), dict):
        normalized["resource_cleanup"] = {}
    normalized.setdefault("created_at", None)
    normalized.setdefault("updated_at", None)
    return normalized


def _run_worker_snapshots(run, *, fallback_name):
    workers = run.get("workers")
    if workers is None:
        return {}
    if not isinstance(workers, dict):
        raise RunRecordError(f"run record for '{fallback_name}' is corrupted: workers must be an object")
    return workers


def _worker_snapshots(run):
    workers = run.get("workers") if isinstance(run, dict) else None
    return workers if isinstance(workers, dict) else {}


def _worker_snapshot_schema_version(run):
    return run.get("schema_version", SCHEMA_VERSION) if isinstance(run, dict) else SCHEMA_VERSION


def run_record_for_output(run):
    output = deepcopy(run)
    workers = output.get("workers") or {}
    if not isinstance(workers, dict):
        workers = {}
    output["workers"] = {
        worker_id: worker_output_dict(worker, worker_id)
        for worker_id, worker in workers.items()
    }
    return output
