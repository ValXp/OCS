import os
from copy import deepcopy
from pathlib import Path

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.project_metadata import (
    ProjectMetadataUnsupported,
    normalized_directory,
    workspace_project_id,
)


class ProjectCopyCleanupError(Exception):
    pass


class ProjectCopyCleanupService:
    def __init__(self, metadata):
        self.metadata = metadata

    def cleanup(self, project_id, directory_prefix, *, apply=False, assumed_missing_paths=()):
        result = self.plan(
            project_id,
            directory_prefix,
            assumed_missing_paths=assumed_missing_paths,
        )
        if not apply:
            result["status"] = "partial" if result["unsupported"] else "planned"
            return result
        return self.apply_plan(result)

    def apply_plan(self, plan):
        result = deepcopy(plan)
        result["mode"] = "apply"
        self._refresh_project_copies(result)
        self._remove_workspaces(result)
        try:
            self._verify(result)
        except OpenCodeApiError as error:
            result["errors"].append({"operation": "verify", "error": str(error)})
        if result["errors"]:
            result["status"] = "failed"
        elif result["unsupported"] or any(result["remaining"].values()):
            result["status"] = "partial"
        else:
            result["status"] = "done"
        return result

    def plan(self, project_id, directory_prefix, *, assumed_missing_paths=()):
        project = self.metadata.inspect_project(project_id).records[0]
        directory_records = self.metadata.list_project_directories(project_id).records
        unsupported = []
        try:
            workspaces = self.metadata.list_workspaces(project_id=project_id).records
        except ProjectMetadataUnsupported as error:
            workspaces = []
            unsupported.append(error.route_name)

        prefix = normalized_directory(directory_prefix)
        if os.path.dirname(prefix) == prefix:
            raise ProjectCopyCleanupError("directory prefix cannot be a filesystem root")
        assumed_missing = {normalized_directory(path) for path in assumed_missing_paths}
        sandboxes = _stale_paths(project.get("sandboxes") or [], prefix, assumed_missing=assumed_missing)
        if sandboxes:
            _remember(unsupported, "project_sandbox_remove")
        directory_paths = [record.get("directory") for record in directory_records if record.get("directory")]
        directories = _stale_paths(
            directory_paths,
            prefix,
            assumed_missing=assumed_missing,
        )
        unrelated_stale_directories = sorted(set(_missing_paths(directory_paths)) - set(directories))
        if directories and unrelated_stale_directories:
            _remember(unsupported, "project_copy_refresh_unscoped")
        if directories and not self.metadata.supports("project_copy_refresh"):
            _remember(unsupported, "project_copy_refresh")
        matching_workspaces = [
            record for record in workspaces
            if record.get("directory")
            and _is_stale(record.get("directory"), prefix, assumed_missing=assumed_missing)
            and workspace_project_id(record) == str(project_id)
        ]
        workspace_plans = [(record, _workspace_plan(record)) for record in matching_workspaces]
        planned_workspaces = [planned for _record, planned in workspace_plans if planned is not None]
        invalid_workspaces = [
            {"directory": normalized_directory(record.get("directory")), "reason": "invalid workspace id"}
            for record, planned in workspace_plans
            if planned is None
        ]
        if invalid_workspaces:
            _remember(unsupported, "workspace_identity")
        if planned_workspaces and not self.metadata.supports("workspace_item"):
            _remember(unsupported, "workspace_item")
        return {
            "status": "planned",
            "mode": "dry-run",
            "project_id": str(project_id),
            "directory_prefix": prefix,
            "planned_directories": sorted(set(directories + sandboxes)),
            "planned_project_directories": sorted(set(directories)),
            "planned_project_sandboxes": sorted(set(sandboxes)),
            "unrelated_stale_directories": unrelated_stale_directories,
            "planned_workspaces": planned_workspaces,
            "invalid_workspaces": invalid_workspaces,
            "refreshed": False,
            "removed_workspaces": [],
            "verified": {"directories": [], "workspaces": []},
            "remaining": {"project_sandboxes": [], "project_directories": [], "workspaces": []},
            "unsupported": unsupported,
            "errors": [],
        }

    def _refresh_project_copies(self, result):
        current_paths = [
            record.get("directory")
            for record in self.metadata.list_project_directories(result["project_id"]).records
            if record.get("directory")
        ]
        current_missing = set(_missing_paths(current_paths))
        missing_planned = set(result["planned_project_directories"]) & current_missing
        if not missing_planned:
            return
        allowed_refresh = set(
            result.get("allowed_refresh_directories") or result["planned_project_directories"]
        )
        unrelated = sorted(current_missing - allowed_refresh)
        result["unrelated_stale_directories"] = unrelated
        if unrelated:
            _remember(result["unsupported"], "project_copy_refresh_unscoped")
            return
        if not self.metadata.supports("project_copy_refresh"):
            _remember(result["unsupported"], "project_copy_refresh")
            return
        try:
            self.metadata.client.refresh_project_copies_response(result["project_id"])
            result["refreshed"] = True
        except OpenCodeApiError as error:
            result["errors"].append({"operation": "project_copy_refresh", "error": str(error)})

    def _remove_workspaces(self, result):
        if not result["planned_workspaces"]:
            return
        if not self.metadata.supports("workspace_item"):
            _remember(result["unsupported"], "workspace_item")
            return
        current = self.metadata.list_workspaces(project_id=result["project_id"]).records
        current_by_id = {str(record.get("id")): record for record in current if record.get("id") is not None}
        for workspace in result["planned_workspaces"]:
            current_workspace = current_by_id.get(workspace["id"])
            if current_workspace is None:
                continue
            current_directory = normalized_directory(current_workspace.get("directory"))
            if (
                workspace_project_id(current_workspace) != result["project_id"]
                or current_directory != workspace["directory"]
            ):
                result["errors"].append(
                    {"operation": "workspace_remove", "workspace_id": workspace["id"], "error": "workspace identity changed after preflight"}
                )
                continue
            if Path(current_directory).exists():
                result["errors"].append(
                    {"operation": "workspace_remove", "workspace_id": workspace["id"], "error": "workspace directory is still present"}
                )
                continue
            try:
                self.metadata.client.delete_workspace_response(workspace["id"])
                result["removed_workspaces"].append(workspace["id"])
            except OpenCodeApiError as error:
                result["errors"].append(
                    {"operation": "workspace_remove", "workspace_id": workspace["id"], "error": str(error)}
                )

    def _verify(self, result):
        planned_paths = set(result["planned_directories"])
        project = self.metadata.inspect_project(result["project_id"]).records[0]
        current_sandboxes = {normalized_directory(path) for path in project.get("sandboxes") or []}
        current_directories = {
            normalized_directory(record.get("directory"))
            for record in self.metadata.list_project_directories(result["project_id"]).records
            if record.get("directory")
        }
        planned_workspace_ids = {record["id"] for record in result["planned_workspaces"]}
        remaining_workspace_ids = set()
        remaining_workspaces = []
        if self.metadata.supports("workspace_collection"):
            for workspace in self.metadata.list_workspaces(project_id=result["project_id"]).records:
                directory = workspace.get("directory")
                if not directory or not _is_stale(directory, result["directory_prefix"]):
                    continue
                workspace_id = workspace.get("id")
                if not _valid_workspace_id(workspace_id):
                    remaining_workspaces.append(f"invalid-id:{normalized_directory(directory)}")
                else:
                    workspace_id = str(workspace_id)
                    remaining_workspace_ids.add(workspace_id)
                    remaining_workspaces.append(workspace_id)

        result["remaining"] = {
            "project_sandboxes": sorted(
                (planned_paths & current_sandboxes)
                | {path for path in current_sandboxes if _is_stale(path, result["directory_prefix"])}
            ),
            "project_directories": sorted(
                (planned_paths & current_directories)
                | {path for path in current_directories if _is_stale(path, result["directory_prefix"])}
            ),
            "workspaces": sorted(remaining_workspaces),
        }
        remaining_paths = set(result["remaining"]["project_sandboxes"] + result["remaining"]["project_directories"])
        result["verified"] = {
            "directories": sorted(planned_paths - remaining_paths),
            "workspaces": sorted(planned_workspace_ids - remaining_workspace_ids),
        }
        if result["remaining"]["project_sandboxes"]:
            _remember(result["unsupported"], "project_sandbox_remove")


def _workspace_plan(workspace):
    workspace_id = workspace.get("id")
    if not _valid_workspace_id(workspace_id):
        return None
    return {
        "id": str(workspace_id),
        "directory": normalized_directory(workspace.get("directory")),
    }


def _valid_workspace_id(value):
    if not isinstance(value, str) or not value or value in {".", ".."}:
        return False
    return not any(character.isspace() or character in "/\\?#" for character in value)


def _stale_paths(paths, prefix, *, assumed_missing=frozenset()):
    return [
        normalized_directory(path)
        for path in paths
        if _is_stale(path, prefix, assumed_missing=assumed_missing)
    ]


def _missing_paths(paths):
    return [normalized_directory(path) for path in paths if not Path(normalized_directory(path)).exists()]


def _is_stale(path, prefix, *, assumed_missing=frozenset()):
    normalized = normalized_directory(path)
    return _matches_directory_prefix(normalized, prefix) and (
        normalized in assumed_missing or not Path(normalized).exists()
    )


def _matches_directory_prefix(path, prefix):
    parent = os.path.dirname(prefix)
    try:
        if os.path.commonpath((path, parent)) != parent:
            return False
    except ValueError:
        return False
    relative = os.path.relpath(path, parent)
    first_component = relative.split(os.sep, 1)[0]
    return first_component.startswith(os.path.basename(prefix))


def _remember(values, value):
    if value not in values:
        values.append(value)
