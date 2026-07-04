from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from opencode_session.status import short_status


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
        run = self.load_run(name)
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
                worker[key] = short_status(changes[key]) if key == "status" else changes[key]
        for key in ("dependencies", "prompt_ids", "retryable_failures", "blockers", "output_refs"):
            if changes.get(key) is not None:
                worker[key] = changes[key]

        workers[worker_id] = worker
        run["updated_at"] = _utc_now()
        self.save_run(run)
        return run

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
    return _merge_value(baseline, incoming, current, ())


def _merge_value(baseline, incoming, current, path):
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
    if isinstance(incoming, dict) and isinstance(current, dict):
        base_dict = baseline if isinstance(baseline, dict) else {}
        return _merge_dict(base_dict, incoming, current, path)
    if incoming == baseline:
        return deepcopy(current)
    if current == baseline or incoming == current:
        return deepcopy(incoming)
    return _merge_conflicting_values(incoming, current, path)


def _merge_dict(baseline, incoming, current, path):
    merged = {}
    for key in set(baseline) | set(incoming) | set(current):
        value = _merge_value(
            baseline.get(key, _MISSING),
            incoming.get(key, _MISSING),
            current.get(key, _MISSING),
            path + (key,),
        )
        if value is not _MISSING:
            merged[key] = value
    return merged


def _merge_conflicting_values(incoming, current, path):
    if isinstance(incoming, list) and isinstance(current, list):
        return _merge_lists(current, incoming)
    if path and path[-1] == "status":
        return _merge_status(incoming, current)
    return deepcopy(incoming)


def _merge_lists(current, incoming):
    merged = []
    for value in [*current, *incoming]:
        if value not in merged:
            merged.append(deepcopy(value))
    return merged


def _merge_status(incoming, current):
    priority = {"queued": 0, "active": 1, "blocked": 2, "done": 3, "timeout": 4, "failed": 4, "aborted": 5}
    if not isinstance(incoming, str) or not isinstance(current, str):
        return deepcopy(incoming)
    return current if priority.get(current, 0) > priority.get(incoming, 0) else incoming


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


def _default_worker(worker_id):
    return {
        "id": worker_id,
        "role": None,
        "session_id": None,
        "agent": None,
        "model": None,
        "dependencies": [],
        "prompt_ids": [],
        "status": "queued",
        "retry_count": 0,
        "retry_limit": 0,
        "retryable_failures": [],
        "timeout_seconds": None,
        "timeout_policy": "timeout",
        "timeout_started_at": None,
        "timed_out_at": None,
        "failure_category": None,
        "failure_reason": None,
        "last_failure_category": None,
        "last_failure_reason": None,
        "next_eligible_action": "start",
        "blockers": [],
        "output_refs": [],
    }


def _normalize_worker(worker, worker_id):
    normalized = _default_worker(worker_id)
    if isinstance(worker, dict):
        normalized.update(worker)
    normalized["id"] = normalized.get("id") or worker_id
    for key in ("dependencies", "prompt_ids", "retryable_failures", "blockers", "output_refs"):
        value = normalized.get(key)
        normalized[key] = value if isinstance(value, list) else []
    if normalized.get("retry_count") is None:
        normalized["retry_count"] = 0
    if normalized.get("retry_limit") is None:
        normalized["retry_limit"] = 0
    if not normalized.get("timeout_policy"):
        normalized["timeout_policy"] = "timeout"
    if not normalized.get("status"):
        normalized["status"] = "queued"
    else:
        normalized["status"] = short_status(normalized["status"])
    normalized["next_eligible_action"] = _next_eligible_action(normalized)
    return normalized


def _next_eligible_action(worker):
    status = worker.get("status")
    if status == "queued":
        return "start"
    if status == "active":
        return "retry" if worker.get("next_eligible_action") == "retry" else "wait"
    if status == "blocked":
        return "resolve_blocker"
    if status == "done":
        return "collect"
    if status == "failed" and _retry_available(worker):
        return "retry"
    return "none"


def _retry_available(worker):
    retryable = set(worker.get("retryable_failures") or [])
    if not retryable:
        return False
    category = worker.get("failure_category") or worker.get("last_failure_category")
    if category and category not in retryable and "all" not in retryable:
        return False
    try:
        retry_count = int(worker.get("retry_count") or 0)
        retry_limit = int(worker.get("retry_limit") or 0)
    except (TypeError, ValueError):
        return False
    return retry_count < retry_limit


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


def _compact_list(values):
    if not values:
        return None
    return ",".join(str(value) for value in values)


def _compact_value(value):
    if value is None or value == "":
        return "-"
    text = str(value)
    if any(character.isspace() for character in text):
        return json.dumps(text)
    return text


def _format_table(headers, rows):
    lines = ["\t".join(headers)]
    lines.extend("\t".join(_compact_value(value) for value in row) for row in rows)
    return "\n".join(lines)


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
