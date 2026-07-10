import os
import stat
from pathlib import Path

from opencode_session.run_resource_schema import RunResourceSchemaError, validate_run_resource_manifest


class ProjectCopyOwnershipError(Exception):
    pass


def project_copy_identity(path):
    path = Path(path)
    parent_realpath = Path(os.path.realpath(path.parent))
    try:
        parent_details = os.stat(parent_realpath)
    except OSError as error:
        raise ProjectCopyOwnershipError(
            f"cannot inspect project-copy prefix parent {parent_realpath}: {error}"
        ) from error
    if not stat.S_ISDIR(parent_details.st_mode):
        raise ProjectCopyOwnershipError(
            f"project-copy prefix parent is not a directory: {parent_realpath}"
        )
    return {
        "resolved_directory_prefix": os.path.realpath(path),
        "directory_prefix_parent_realpath": str(parent_realpath),
        "directory_prefix_parent_device": parent_details.st_dev,
        "directory_prefix_parent_inode": parent_details.st_ino,
    }


def verify_owned_project_copy(record):
    manifest = {"worktrees": [], "logs": [], "project_copies": [record]}
    try:
        validate_run_resource_manifest(manifest)
    except RunResourceSchemaError as error:
        return f"invalid owned project-copy record: {error}"
    try:
        current = project_copy_identity(record["directory_prefix"])
    except ProjectCopyOwnershipError as error:
        return str(error)
    for field_name, value in current.items():
        if record.get(field_name) != value:
            return "owned project-copy directory prefix identity has changed"
    return None
