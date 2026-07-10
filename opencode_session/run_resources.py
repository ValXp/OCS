import os
import subprocess
from copy import deepcopy
from pathlib import Path

from opencode_session.domain_helpers import append_unique_string, string_list
from opencode_session.run_resource_schema import ensure_run_resources
from opencode_session.worker_session_provisioning import WorkerSessionCreationJournalEntry


class RunResourceError(Exception):
    pass


def register_run_resources(
    store,
    name,
    worker_id,
    *,
    worktree_paths=(),
    log_paths=(),
    project_copies=(),
):
    worktrees = [_worktree_record(path, worker_id) for path in worktree_paths or ()]
    logs = [_log_record(path, worker_id) for path in log_paths or ()]
    projects = [_project_copy_record(value, worker_id) for value in project_copies or ()]

    def register(run):
        resources = ensure_run_resources(run)
        for field_name, records, identity_fields in (
            ("worktrees", worktrees, ("path",)),
            ("logs", logs, ("path",)),
            ("project_copies", projects, ("project_id", "directory_prefix")),
        ):
            for record in records:
                _append_unique_record(resources[field_name], record, identity_fields)

    return store.update_run(name, register)


def run_owned_session_ids(run):
    session_ids = []
    workers = run.get("workers") if isinstance(run, dict) else None
    if isinstance(workers, dict):
        for worker in workers.values():
            snapshot = _worker_snapshot(worker)
            append_unique_string(session_ids, snapshot.get("session_id"))
            for attempt in snapshot.get("attempts") or ():
                if not isinstance(attempt, dict):
                    continue
                append_unique_string(session_ids, attempt.get("session_id"))
                for session_id in string_list(attempt.get("created_session_ids")):
                    append_unique_string(session_ids, session_id)
            cleanup = snapshot.get("cleanup")
            if isinstance(cleanup, dict):
                for session_id in string_list(cleanup.get("sessions")):
                    append_unique_string(session_ids, session_id)

    journal = run.get("worker_session_journal") if isinstance(run, dict) else None
    if isinstance(journal, list):
        for entry in journal:
            creation = WorkerSessionCreationJournalEntry.from_journal_entry(entry)
            if creation is None:
                continue
            for session_id in creation.session_ids:
                append_unique_string(session_ids, session_id)
    return session_ids


def _worker_snapshot(worker):
    to_snapshot = getattr(worker, "to_snapshot", None)
    if callable(to_snapshot):
        return to_snapshot()
    return worker if isinstance(worker, dict) else {}


def _worktree_record(value, worker_id):
    path = _safe_owned_path(value, label="worktree")
    top_level = Path(_git_output(path, "rev-parse", "--show-toplevel")).resolve()
    if top_level != path:
        raise RunResourceError(f"owned worktree must be its Git top-level directory: {path}")
    git_dir = Path(_git_output(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    branch = _git_output(path, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True) or None
    return {
        "path": str(path),
        "git_dir": str(git_dir),
        "branch": branch,
        "worker_id": worker_id,
    }


def _log_record(value, worker_id):
    path = _safe_owned_path(value, label="log")
    return {"path": str(path), "worker_id": worker_id}


def _project_copy_record(value, worker_id):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise RunResourceError("owned project copy requires PROJECT_ID and DIRECTORY_PREFIX")
    project_id = str(value[0]).strip()
    if not project_id:
        raise RunResourceError("owned project copy requires a non-empty project ID")
    directory_prefix = _safe_owned_path(value[1], label="project-copy directory prefix")
    return {"project_id": project_id, "directory_prefix": str(directory_prefix), "worker_id": worker_id}


def _safe_owned_path(value, *, label):
    path = Path(os.path.abspath(os.path.expanduser(str(value))))
    if path == Path(path.anchor):
        raise RunResourceError(f"owned {label} path cannot be a filesystem root: {path}")
    return path


def _git_output(path, *args, allow_failure=False):
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        if allow_failure:
            return ""
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RunResourceError(f"cannot register owned worktree {path}: {detail}")
    return result.stdout.strip()


def _append_unique_record(records, record, identity_fields):
    identity = tuple(record.get(field_name) for field_name in identity_fields)
    for index, existing in enumerate(records):
        if tuple(existing.get(field_name) for field_name in identity_fields) == identity:
            records[index] = deepcopy(record)
            return
    records.append(deepcopy(record))
