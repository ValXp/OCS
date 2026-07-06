from copy import deepcopy
from pathlib import Path

from opencode_session.status import short_status
from opencode_session.worker_state import (
    WORKER_LIFECYCLE_STATES,
    default_worker_record,
    deserialize_worker_record,
    serialize_worker_snapshot,
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


def upsert_worker_record(run, worker_id, changes, *, now):
    workers = run.setdefault("workers", {})
    existing = workers.get(worker_id)
    if existing is None:
        if not changes.get("role"):
            raise RunRecordError(f"worker '{worker_id}' does not exist; --role is required to create it")
        worker = default_worker_record(worker_id)
    else:
        worker = deserialize_worker_record(existing, worker_id)

    for public_field_name in ("status", "next_eligible_action"):
        if changes.get(public_field_name) is not None:
            raise RunRecordError(
                f"worker '{worker_id}' updates must use lifecycle_state, not {public_field_name}"
            )
    for key in (
        "role",
        "session_id",
        "agent",
        "model",
        "prompt",
        "retry_count",
        "retry_limit",
        "timeout_seconds",
        "timeout_policy",
    ):
        if changes.get(key) is not None:
            worker.set_field(key, changes[key])
    if changes.get("lifecycle_state") is not None:
        lifecycle_state = changes["lifecycle_state"]
        if lifecycle_state not in WORKER_LIFECYCLE_STATES:
            raise RunRecordError(f"worker '{worker_id}' has invalid lifecycle_state: {lifecycle_state}")
        worker.set_field("lifecycle_state", lifecycle_state)
    for key in ("dependencies", "prompt_ids", "retryable_failures", "blockers", "output_refs"):
        if changes.get(key) is not None:
            worker.set_field(key, changes[key])

    workers[worker_id] = deserialize_worker_record(worker, worker_id)
    run["updated_at"] = now


def normalize_run(run, *, fallback_name):
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
    workers = normalized.get("workers")
    if workers is None:
        workers = {}
    elif not isinstance(workers, dict):
        raise RunRecordError(f"run record for '{fallback_name}' is corrupted: workers must be an object")
    normalized["workers"] = {worker_id: deserialize_worker_record(worker, worker_id) for worker_id, worker in workers.items()}
    normalized.setdefault("created_at", None)
    normalized.setdefault("updated_at", None)
    return normalized


def normalize_run_for_storage(run, *, fallback_name):
    normalized = normalize_run(run, fallback_name=fallback_name)
    normalized["workers"] = {
        worker_id: serialize_worker_snapshot(worker, worker_id)
        for worker_id, worker in normalized["workers"].items()
    }
    return normalized


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
