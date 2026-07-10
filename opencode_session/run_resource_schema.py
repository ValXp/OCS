from copy import deepcopy
from pathlib import Path


RESOURCE_LIST_FIELDS = ("worktrees", "logs", "project_copies")

_WORKTREE_FIELDS = {
    "path",
    "git_dir",
    "linked_git_dir",
    "linked_git_dir_device",
    "linked_git_dir_inode",
    "branch",
    "worker_id",
}
_LOG_FIELDS = {
    "path",
    "parent_realpath",
    "device",
    "inode",
    "resource_type",
    "parent_device",
    "parent_inode",
    "worker_id",
}
_PROJECT_COPY_FIELDS = {"project_id", "directory_prefix", "worker_id"}
_LOG_RESOURCE_TYPES = {"file", "directory", "symlink"}


class RunResourceSchemaError(Exception):
    pass


def normalize_run_resources(value, *, allow_missing=True):
    if value is None and allow_missing:
        return _empty_resources()
    if not isinstance(value, dict):
        raise RunResourceSchemaError("resources must be an object")

    missing = sorted(set(RESOURCE_LIST_FIELDS) - set(value))
    unexpected = sorted(set(value) - set(RESOURCE_LIST_FIELDS))
    if missing:
        raise RunResourceSchemaError(f"resources is missing fields: {', '.join(missing)}")
    if unexpected:
        raise RunResourceSchemaError(f"resources has unexpected fields: {', '.join(unexpected)}")

    normalized = _empty_resources()
    for field_name in RESOURCE_LIST_FIELDS:
        records = value.get(field_name, [])
        if not isinstance(records, list):
            raise RunResourceSchemaError(f"resources.{field_name} must be an array")
        normalized[field_name] = [
            _normalize_record(field_name, record, index)
            for index, record in enumerate(records)
        ]
    return normalized


def validate_run_resource_manifest(value):
    """Validate and return a defensive copy of a persisted resource manifest."""
    return normalize_run_resources(value, allow_missing=False)


def ensure_run_resources(run):
    if "resources" in run:
        resources = validate_run_resource_manifest(run.get("resources"))
    else:
        resources = _empty_resources()
    run["resources"] = resources
    return resources


def _empty_resources():
    return {field_name: [] for field_name in RESOURCE_LIST_FIELDS}


def _normalize_record(field_name, record, index):
    label = f"resources.{field_name}[{index}]"
    if not isinstance(record, dict):
        raise RunResourceSchemaError(f"{label} must be an object")
    if field_name == "worktrees":
        _validate_worktree(record, label)
    elif field_name == "logs":
        _validate_log(record, label)
    else:
        _validate_project_copy(record, label)
    return deepcopy(record)


def _validate_worktree(record, label):
    _require_exact_fields(record, _WORKTREE_FIELDS, label)
    _require_path(record, "path", label)
    _require_path(record, "git_dir", label)
    _require_path(record, "linked_git_dir", label)
    _require_identity(record, "linked_git_dir_device", label)
    _require_identity(record, "linked_git_dir_inode", label)
    _require_text(record, "branch", label)
    _require_text(record, "worker_id", label)


def _validate_log(record, label):
    _require_exact_fields(record, _LOG_FIELDS, label)
    _require_path(record, "path", label)
    _require_path(record, "parent_realpath", label, allow_root=True)
    _require_identity(record, "device", label)
    _require_identity(record, "inode", label)
    _require_identity(record, "parent_device", label)
    _require_identity(record, "parent_inode", label)
    resource_type = _require_text(record, "resource_type", label)
    if resource_type not in _LOG_RESOURCE_TYPES:
        expected = ", ".join(sorted(_LOG_RESOURCE_TYPES))
        raise RunResourceSchemaError(f"{label}.resource_type must be one of: {expected}")
    _require_text(record, "worker_id", label)


def _validate_project_copy(record, label):
    _require_exact_fields(record, _PROJECT_COPY_FIELDS, label)
    _require_text(record, "project_id", label)
    _require_path(record, "directory_prefix", label)
    _require_text(record, "worker_id", label)


def _require_exact_fields(record, expected, label):
    missing = sorted(expected - set(record))
    unexpected = sorted(set(record) - expected)
    if missing:
        raise RunResourceSchemaError(f"{label} is missing fields: {', '.join(missing)}")
    if unexpected:
        raise RunResourceSchemaError(f"{label} has unexpected fields: {', '.join(unexpected)}")


def _require_text(record, field_name, label):
    value = record.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise RunResourceSchemaError(f"{label}.{field_name} must be a non-empty string")
    return value


def _require_path(record, field_name, label, *, allow_root=False):
    value = _require_text(record, field_name, label)
    path = Path(value)
    if not path.is_absolute():
        raise RunResourceSchemaError(f"{label}.{field_name} must be an absolute path")
    if not allow_root and path == Path(path.anchor):
        raise RunResourceSchemaError(f"{label}.{field_name} cannot be a filesystem root")


def _require_identity(record, field_name, label):
    value = record.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunResourceSchemaError(f"{label}.{field_name} must be a non-negative integer")
