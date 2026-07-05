from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from opencode_session.formatting import (
    compact_list as _compact_list,
    compact_value as _compact_value,
    format_table as _format_table,
)
from opencode_session.status import short_status
from opencode_session.worker_state import (
    default_worker as _default_worker,
    normalize_worker as _normalize_worker,
)


SCHEMA_VERSION = 1
DEFAULT_RUN_STATUS = "queued"
DEFAULT_SERVER_URL = "http://127.0.0.1:4096"
_MISSING = object()
_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class RunStoreError(Exception):
    def __init__(self, message, *, kind="data"):
        super().__init__(message)
        self.kind = kind


class _StoredRun(dict):
    pass


class RunStore:
    def __init__(self, root):
        self.root = Path(root)

    def create_run(self, name, *, directory, server_url):
        now = _utc_now()
        run = _StoredRun(
            {
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
        )
        self.save_run(run)
        return run

    def upsert_worker(self, name, worker_id, **changes):
        def mutate(run):
            workers = run.setdefault("workers", {})
            existing = workers.get(worker_id)
            if existing is None:
                if not changes.get("role"):
                    raise RunStoreError(f"worker '{worker_id}' does not exist; --role is required to create it")
                worker = _default_worker(worker_id)
            else:
                worker = _normalize_worker(existing, worker_id)

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
            run["updated_at"] = _utc_now()

        return self.update_run(name, mutate)

    def load_run(self, name):
        return _track_run(self._read_run_unlocked(name))

    def save_run(self, run):
        name = run["name"]
        incoming = _normalize_run(dict(run), fallback_name=name)
        baseline = _run_snapshot(run)
        with self._locked_run(name):
            merged = incoming
            if baseline is not None:
                try:
                    current = self._read_run_unlocked(name)
                except RunStoreError as error:
                    if error.kind != "missing":
                        raise
                else:
                    merged = _merge_run_changes(baseline, incoming, current)
            self._write_run_unlocked(merged)
        _replace_mapping_in_place(run, merged)
        _remember_run_snapshot(run, merged)

    def update_run(self, name, mutator):
        with self._locked_run(name):
            run = self._read_run_unlocked(name)
            replacement = mutator(run)
            if replacement is not None:
                run = replacement
            run = _normalize_run(run, fallback_name=name)
            self._write_run_unlocked(run)
        return _track_run(run)

    def _read_run_unlocked(self, name):
        path = self._run_path(name)
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError as error:
            raise RunStoreError(f"run '{name}' not found in {self.root}", kind="missing") from error
        except json.JSONDecodeError as error:
            raise RunStoreError(f"run record for '{name}' is corrupted: invalid JSON in {path}: {error}") from error
        if not isinstance(data, dict):
            raise RunStoreError(f"run record for '{name}' is corrupted: expected JSON object in {path}")
        return _normalize_run(data, fallback_name=name)

    def _write_run_unlocked(self, run):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._run_path(run["name"])
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(run, file, sort_keys=True)
            file.write("\n")
        os.replace(temporary_path, path)

    @contextmanager
    def _locked_run(self, name):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path(name)
        thread_lock = _thread_lock_for(lock_path)
        with thread_lock:
            with lock_path.open("a", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _run_path(self, name):
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise RunStoreError(f"invalid run name '{name}'")
        return self.root / f"{name}.json"

    def _lock_path(self, name):
        return self._run_path(name).with_suffix(".json.lock")


def default_store_root():
    return os.environ.get("OCS_RUN_STORE") or str(Path.cwd() / ".ocs" / "runs")


def _thread_lock_for(path):
    key = str(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


def _track_run(run):
    stored = _StoredRun(run)
    _remember_run_snapshot(stored, run)
    return stored


def _run_snapshot(run):
    snapshot = getattr(run, "_run_store_snapshot", None)
    return deepcopy(snapshot) if snapshot is not None else None


def _remember_run_snapshot(run, snapshot):
    if isinstance(run, _StoredRun):
        run._run_store_snapshot = deepcopy(snapshot)


def _replace_mapping_in_place(target, source):
    for key in list(target):
        if key not in source:
            del target[key]
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            _replace_mapping_in_place(existing, value)
        elif isinstance(existing, list) and isinstance(value, list):
            existing[:] = deepcopy(value)
        else:
            target[key] = deepcopy(value)


def _merge_run_changes(baseline, incoming, current):
    baseline = baseline if isinstance(baseline, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}
    current = current if isinstance(current, dict) else {}
    merged = {}
    for key in set(baseline) | set(incoming) | set(current):
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
    return _normalize_run(merged, fallback_name=incoming.get("name") or current.get("name") or baseline.get("name"))


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
    if key in {"blockers", "output_refs"} and isinstance(incoming, list) and isinstance(current, list):
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
        return _normalize_worker(incoming, worker_id)
    if incoming == baseline:
        return deepcopy(current)
    if current == baseline or incoming == current:
        return _normalize_worker(incoming, worker_id)
    return _merge_conflicting_worker(worker_id, incoming, current)


def _merge_conflicting_worker(worker_id, incoming, current):
    incoming_worker = _normalize_worker(incoming, worker_id)
    current_worker = _normalize_worker(current, worker_id)
    incoming_status = incoming_worker.get("status")
    current_status = current_worker.get("status")
    if _status_priority(current_status) > _status_priority(incoming_status):
        return deepcopy(current_worker)
    return deepcopy(incoming_worker)


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


def format_run_compact(run):
    workers = run.get("workers") or {}
    counts = _worker_status_counts(workers)
    fields = [
        ("run", run.get("name")),
        ("status", run.get("status")),
        ("dir", run.get("directory")),
        ("server", run.get("server_url")),
        ("workers", len(workers)),
        ("queued", counts["queued"]),
        ("active", counts["active"]),
        ("done", counts["done"]),
        ("blocked", counts["blocked"]),
        ("failed", counts["failed"]),
        ("aborted", counts["aborted"]),
        ("timeout", counts["timeout"]),
        ("retries", run.get("retry_count")),
        ("timeout_s", run.get("timeout_seconds")),
        ("blockers", _compact_list(run.get("blockers"))),
        ("outputs", _compact_list(run.get("output_refs"))),
    ]
    lines = [" ".join(f"{key}={_compact_value(value)}" for key, value in fields)]
    worker_records = [_normalize_worker(workers[worker_id], worker_id) for worker_id in sorted(workers)]
    if len(worker_records) > 1:
        lines.append(_format_worker_table(worker_records))
    elif worker_records:
        lines.append(_format_worker_compact(worker_records[0]))
    return "\n".join(lines)


def _normalize_run(run, *, fallback_name):
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
        raise RunStoreError(f"run record for '{fallback_name}' is corrupted: workers must be an object")
    normalized["workers"] = {worker_id: _normalize_worker(worker, worker_id) for worker_id, worker in workers.items()}
    normalized.setdefault("created_at", None)
    normalized.setdefault("updated_at", None)
    return normalized


def _format_worker_compact(worker):
    fields = [
        ("worker", worker.get("id")),
        ("role", worker.get("role")),
        ("status", worker.get("status")),
        ("session", worker.get("session_id")),
        ("agent", worker.get("agent")),
        ("model", worker.get("model")),
        ("deps", _compact_list(worker.get("dependencies"))),
        ("prompts", _compact_list(worker.get("prompt_ids"))),
        ("retries", worker.get("retry_count")),
        ("timeout", worker.get("timeout_seconds")),
        ("blockers", _compact_list(worker.get("blockers"))),
        ("outputs", _compact_list(worker.get("output_refs"))),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_worker_table(workers):
    rows = []
    for worker in workers:
        rows.append(
            [
                worker.get("id"),
                worker.get("role"),
                worker.get("status"),
                worker.get("session_id"),
                worker.get("agent"),
                worker.get("model"),
                _compact_list(worker.get("dependencies")),
                _compact_list(worker.get("prompt_ids")),
                worker.get("retry_count"),
                worker.get("timeout_seconds"),
                _compact_list(worker.get("blockers")),
                _compact_list(worker.get("output_refs")),
            ]
        )
    return _format_table(
        ["worker", "role", "status", "session", "agent", "model", "deps", "prompts", "retries", "timeout", "blockers", "outputs"],
        rows,
    )


def _worker_status_counts(workers):
    counts = {"queued": 0, "active": 0, "done": 0, "blocked": 0, "failed": 0, "aborted": 0, "timeout": 0}
    for worker in workers.values():
        status = short_status(worker.get("status")) if isinstance(worker, dict) else None
        if status in counts:
            counts[status] += 1
    return counts


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
