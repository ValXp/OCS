from contextlib import contextmanager
import fcntl
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from opencode_session.run_record import (
    RunRecordError,
    new_run_record,
    normalize_run,
    normalize_run_for_storage,
    upsert_worker_record,
)


_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class RunStoreError(Exception):
    def __init__(self, message, *, kind="data"):
        super().__init__(message)
        self.kind = kind


class RunStore:
    def __init__(self, root):
        self.root = Path(root)

    def create_run(self, name, *, directory, server_url):
        now = _utc_now()
        run = _normalize_for_store(
            new_run_record(name, directory=directory, server_url=server_url, now=now),
            fallback_name=name,
        )
        with self._locked_run(name):
            if self._run_path(name).exists():
                raise RunStoreError(f"run '{name}' already exists in {self.root}", kind="exists")
            self._write_run_unlocked(run)
        return run

    def upsert_worker(self, name, worker_id, **changes):
        def mutate(run):
            try:
                upsert_worker_record(run, worker_id, changes, now=_utc_now())
            except RunRecordError as error:
                raise RunStoreError(str(error), kind=error.kind) from error

        return self.update_run(name, mutate)

    def load_run(self, name):
        return self._read_run_unlocked(name)

    def update_run(self, name, mutator):
        with self._locked_run(name):
            run = self._read_run_unlocked(name)
            replacement = mutator(run)
            if replacement is not None:
                run = replacement
            run = _normalize_for_store(run, fallback_name=name)
            self._write_run_unlocked(run)
        return run

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
        return _normalize_for_store(data, fallback_name=name)

    def _write_run_unlocked(self, run):
        self.root.mkdir(parents=True, exist_ok=True)
        stored_run = _normalize_for_storage(run, fallback_name=run["name"])
        path = self._run_path(stored_run["name"])
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(stored_run, file, sort_keys=True)
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


def _normalize_for_store(run, *, fallback_name):
    try:
        return normalize_run(run, fallback_name=fallback_name)
    except RunRecordError as error:
        raise RunStoreError(str(error), kind=error.kind) from error


def _normalize_for_storage(run, *, fallback_name):
    try:
        return normalize_run_for_storage(run, fallback_name=fallback_name)
    except RunRecordError as error:
        raise RunStoreError(str(error), kind=error.kind) from error


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
