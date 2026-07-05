from copy import deepcopy
from pathlib import Path

from opencode_session.status import short_status
from opencode_session.worker_state import (
    default_worker,
    normalize_worker,
    run_status_from_workers,
    worker_output_refs_in_dependency_order,
)


SCHEMA_VERSION = 1
DEFAULT_RUN_STATUS = "queued"
DEFAULT_SERVER_URL = "http://127.0.0.1:4096"
_MISSING = object()
_WORKER_APPEND_ONLY_FIELDS = {"prompt_ids", "output_refs"}
_WORKER_STATUS_OWNED_FIELDS = {
    "abort",
    "error",
    "failure_category",
    "failure_reason",
    "failure_retryable",
    "last_failure_category",
    "last_failure_reason",
    "result",
    "timed_out_at",
}


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
        worker = default_worker(worker_id)
    else:
        worker = normalize_worker(existing, worker_id)

    for key in (
        "role",
        "session_id",
        "agent",
        "model",
        "prompt",
        "status",
        "retry_count",
        "retry_limit",
        "timeout_seconds",
        "timeout_policy",
    ):
        if changes.get(key) is not None:
            worker[key] = changes[key]
    for key in ("dependencies", "prompt_ids", "retryable_failures", "blockers", "output_refs"):
        if changes.get(key) is not None:
            worker[key] = changes[key]

    workers[worker_id] = worker
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
    normalized["workers"] = {worker_id: normalize_worker(worker, worker_id) for worker_id, worker in workers.items()}
    normalized.setdefault("created_at", None)
    normalized.setdefault("updated_at", None)
    return normalized


def merge_run_changes(baseline, incoming, current):
    baseline = baseline if isinstance(baseline, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}
    current = current if isinstance(current, dict) else {}
    merged = {}
    for key in set(baseline) | set(incoming) | set(current):
        if key == "output_refs":
            continue
        if key == "workers":
            workers = _merge_workers(
                baseline.get("workers", {}),
                incoming.get("workers", {}),
                current.get("workers", {}),
            )
            if workers is not _MISSING:
                merged[key] = workers
            continue
        value = _merge_run_field(
            key,
            baseline.get(key, _MISSING),
            incoming.get(key, _MISSING),
            current.get(key, _MISSING),
        )
        if value is not _MISSING:
            merged[key] = value
    merged = normalize_run(merged, fallback_name=incoming.get("name") or current.get("name") or baseline.get("name"))
    workers = merged.get("workers", {})
    merged["output_refs"] = worker_output_refs_in_dependency_order(workers)
    status = run_status_from_workers(workers)
    if status is not None:
        merged["status"] = status
    return merged


def _merge_run_field(key, baseline, incoming, current):
    if incoming is _MISSING:
        if current is _MISSING:
            return _MISSING
        if baseline is not _MISSING and current == baseline:
            return _MISSING
        return deepcopy(current)
    if current is _MISSING:
        if baseline is not _MISSING and incoming == baseline:
            return _MISSING
        return deepcopy(incoming)
    if incoming == baseline:
        return deepcopy(current)
    if current == baseline or incoming == current:
        return deepcopy(incoming)
    if key == "status":
        return _merge_status(incoming, current)
    if key == "blockers" and isinstance(incoming, list) and isinstance(current, list):
        return _merge_lists(current, incoming)
    return deepcopy(incoming)


def _merge_workers(baseline, incoming, current):
    baseline = baseline if isinstance(baseline, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}
    current = current if isinstance(current, dict) else {}
    merged = {}
    for key in set(baseline) | set(incoming) | set(current):
        value = _merge_worker(
            key,
            baseline.get(key, _MISSING),
            incoming.get(key, _MISSING),
            current.get(key, _MISSING),
        )
        if value is not _MISSING:
            merged[key] = value
    return merged


def _merge_worker(worker_id, baseline, incoming, current):
    if incoming is _MISSING:
        if current is _MISSING:
            return _MISSING
        if baseline is not _MISSING and current == baseline:
            return _MISSING
        return deepcopy(current)
    if current is _MISSING:
        if baseline is not _MISSING and incoming == baseline:
            return _MISSING
        return normalize_worker(incoming, worker_id)
    if incoming == baseline:
        return deepcopy(current)
    if current == baseline or incoming == current:
        return normalize_worker(incoming, worker_id)
    return _merge_conflicting_worker(worker_id, baseline, incoming, current)


def _merge_conflicting_worker(worker_id, baseline, incoming, current):
    baseline_worker = normalize_worker(baseline, worker_id)
    incoming_worker = normalize_worker(incoming, worker_id)
    current_worker = normalize_worker(current, worker_id)
    status_owner = _worker_status_owner(incoming_worker, current_worker)
    merged_status = _merge_status(incoming_worker.get("status"), current_worker.get("status"))
    merged = {}
    for key in set(baseline_worker) | set(incoming_worker) | set(current_worker):
        value = _merge_worker_field(
            key,
            baseline_worker.get(key, _MISSING),
            incoming_worker.get(key, _MISSING),
            current_worker.get(key, _MISSING),
            merged_status=merged_status,
            status_owner=status_owner,
        )
        if value is not _MISSING:
            merged[key] = value
    return normalize_worker(merged, worker_id)


def _merge_worker_field(key, baseline, incoming, current, *, merged_status, status_owner):
    if key == "status":
        return merged_status
    if key in _WORKER_STATUS_OWNED_FIELDS:
        return _merge_worker_status_owned_field(incoming, current, status_owner=status_owner)
    if key in _WORKER_APPEND_ONLY_FIELDS:
        return _merge_worker_append_only_field(incoming, current, status_owner=status_owner)
    if key == "blockers":
        return _merge_worker_blockers(incoming, current, merged_status=merged_status, status_owner=status_owner)
    return _merge_independent_worker_field(baseline, incoming, current)


def _merge_worker_status_owned_field(incoming, current, *, status_owner):
    owner_value = current if status_owner == "current" else incoming
    if owner_value is _MISSING:
        return _MISSING
    return deepcopy(owner_value)


def _merge_worker_append_only_field(incoming, current, *, status_owner):
    if incoming is _MISSING:
        return deepcopy(current) if current is not _MISSING else _MISSING
    if current is _MISSING:
        return deepcopy(incoming)
    if status_owner == "current":
        return deepcopy(current)
    if isinstance(incoming, list) and isinstance(current, list):
        return _merge_lists(current, incoming)
    return deepcopy(incoming)


def _merge_worker_blockers(incoming, current, *, merged_status, status_owner):
    if incoming is _MISSING:
        return deepcopy(current) if current is not _MISSING else _MISSING
    if current is _MISSING:
        return deepcopy(incoming)
    if merged_status == "blocked" and isinstance(incoming, list) and isinstance(current, list):
        return _merge_lists(current, incoming)
    owner_value = current if status_owner == "current" else incoming
    return deepcopy(owner_value)


def _merge_independent_worker_field(baseline, incoming, current):
    if incoming is _MISSING:
        if current is _MISSING:
            return _MISSING
        if baseline is not _MISSING and current == baseline:
            return _MISSING
        return deepcopy(current)
    if current is _MISSING:
        if baseline is not _MISSING and incoming == baseline:
            return _MISSING
        return deepcopy(incoming)
    if incoming == baseline:
        return deepcopy(current)
    if current == baseline or incoming == current:
        return deepcopy(incoming)
    return deepcopy(incoming)


def _worker_status_owner(incoming_worker, current_worker):
    incoming_status = incoming_worker.get("status")
    current_status = current_worker.get("status")
    if _status_priority(current_status) > _status_priority(incoming_status):
        return "current"
    return "incoming"


def _merge_lists(current, incoming):
    merged = []
    for value in [*current, *incoming]:
        if value not in merged:
            merged.append(deepcopy(value))
    return merged


def _merge_status(incoming, current):
    if not isinstance(incoming, str) or not isinstance(current, str):
        return deepcopy(incoming)
    return current if _status_priority(current) > _status_priority(incoming) else incoming


def _status_priority(status):
    priority = {"queued": 0, "active": 1, "blocked": 2, "done": 3, "timeout": 4, "failed": 4, "aborted": 5}
    return priority.get(status, 0)
