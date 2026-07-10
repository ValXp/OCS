import os
import stat
import subprocess
from copy import deepcopy
from pathlib import Path

from opencode_session.domain_helpers import append_unique_string, string_list
from opencode_session.run_resource_schema import (
    RunResourceSchemaError,
    ensure_run_resources,
    validate_run_resource_manifest,
)
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
    linked_git_dir = Path(
        _git_output(path, "rev-parse", "--path-format=absolute", "--absolute-git-dir")
    ).resolve()
    linked_identity = _directory_identity(linked_git_dir, label="linked Git directory")
    branch = _git_output(path, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
    if not branch:
        raise RunResourceError(f"owned worktree must have an attached branch: {path}")
    return {
        "path": str(path),
        "git_dir": str(git_dir),
        "linked_git_dir": str(linked_git_dir),
        "linked_git_dir_device": linked_identity[0],
        "linked_git_dir_inode": linked_identity[1],
        "branch": branch,
        "worker_id": _required_text(worker_id, label="worker ID"),
    }


def _log_record(value, worker_id):
    path = _safe_owned_path(value, label="log")
    identity = _log_identity(path)
    parent_realpath = Path(os.path.realpath(path.parent))
    parent_identity = _directory_identity(parent_realpath, label="log parent directory")
    return {
        "path": str(path),
        "parent_realpath": str(parent_realpath),
        "device": identity[0],
        "inode": identity[1],
        "resource_type": identity[2],
        "parent_device": parent_identity[0],
        "parent_inode": parent_identity[1],
        "worker_id": _required_text(worker_id, label="worker ID"),
    }


def _project_copy_record(value, worker_id):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise RunResourceError("owned project copy requires PROJECT_ID and DIRECTORY_PREFIX")
    project_id = _required_text(value[0], label="project ID")
    directory_prefix = _safe_owned_path(value[1], label="project-copy directory prefix")
    return {
        "project_id": project_id,
        "directory_prefix": str(directory_prefix),
        "worker_id": _required_text(worker_id, label="worker ID"),
    }


def _safe_owned_path(value, *, label):
    try:
        raw_value = os.fspath(value)
    except TypeError as error:
        raise RunResourceError(f"owned {label} path must be a string or path") from error
    if isinstance(raw_value, bytes):
        raise RunResourceError(f"owned {label} path must be text")
    if not raw_value.strip():
        raise RunResourceError(f"owned {label} path cannot be blank")
    path = Path(os.path.normpath(raw_value))
    if not path.is_absolute():
        raise RunResourceError(f"owned {label} path must be absolute: {raw_value}")
    if path == Path(path.anchor):
        raise RunResourceError(f"owned {label} path cannot be a filesystem root: {path}")
    return path


def verify_owned_worktree(record):
    """Return None only while the exact registered worktree identity still matches."""
    error = _record_schema_error("worktrees", record)
    if error is not None:
        return error
    path = Path(record["path"])
    if not path.is_dir():
        return "owned worktree path no longer exists as a directory"
    current_top = _git_output(path, "rev-parse", "--show-toplevel", allow_failure=True)
    if not current_top or Path(current_top).resolve() != path:
        return "owned worktree path is no longer the recorded Git top-level directory"
    current_common = _git_output(
        path, "rev-parse", "--path-format=absolute", "--git-common-dir", allow_failure=True
    )
    if not current_common or Path(current_common).resolve() != Path(record["git_dir"]):
        return "owned worktree no longer belongs to the recorded repository"
    current_linked = _git_output(
        path, "rev-parse", "--path-format=absolute", "--absolute-git-dir", allow_failure=True
    )
    if not current_linked or Path(current_linked).resolve() != Path(record["linked_git_dir"]):
        return "owned worktree linked Git directory has changed"
    try:
        linked_identity = _directory_identity(Path(current_linked).resolve(), label="linked Git directory")
    except RunResourceError as error:
        return str(error)
    if linked_identity != (record["linked_git_dir_device"], record["linked_git_dir_inode"]):
        return "owned worktree linked Git directory identity has changed"
    branch = _git_output(path, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
    if not branch:
        return "owned worktree is now detached"
    if branch != record["branch"]:
        return f"owned worktree branch changed from {record['branch']} to {branch}"
    return None


def verify_owned_log(record):
    """Return None only while the exact registered log and its parent still match."""
    error = _record_schema_error("logs", record)
    if error is not None:
        return error
    path = Path(record["path"])
    current_parent = Path(os.path.realpath(path.parent))
    if current_parent != Path(record["parent_realpath"]):
        return "owned log parent real path has changed"
    try:
        parent_identity = _directory_identity(current_parent, label="log parent directory")
    except RunResourceError as error:
        return str(error)
    if parent_identity != (record["parent_device"], record["parent_inode"]):
        return "owned log parent directory identity has changed"
    try:
        identity = _log_identity(path)
    except RunResourceError as error:
        return str(error)
    if identity != (record["device"], record["inode"], record["resource_type"]):
        return "owned log identity or type has changed"
    return None


def _record_schema_error(field_name, record):
    manifest = {name: [] for name in ("worktrees", "logs", "project_copies")}
    manifest[field_name] = [record]
    try:
        validate_run_resource_manifest(manifest)
    except RunResourceSchemaError as error:
        return f"invalid owned resource record: {error}"
    return None


def _log_identity(path):
    try:
        details = os.lstat(path)
    except OSError as error:
        raise RunResourceError(f"cannot inspect owned log {path}: {error}") from error
    resource_type = _resource_type(details.st_mode)
    if resource_type is None:
        raise RunResourceError(f"owned log must be a regular file, directory, or symlink: {path}")
    return details.st_dev, details.st_ino, resource_type


def _resource_type(mode):
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return None


def _directory_identity(path, *, label):
    try:
        details = os.stat(path)
    except OSError as error:
        raise RunResourceError(f"cannot inspect {label} {path}: {error}") from error
    if not stat.S_ISDIR(details.st_mode):
        raise RunResourceError(f"{label} is not a directory: {path}")
    return details.st_dev, details.st_ino


def _required_text(value, *, label):
    if not isinstance(value, str) or not value.strip():
        raise RunResourceError(f"owned resource requires a non-empty {label}")
    return value.strip()


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
