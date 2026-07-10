import os
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

    def cleanup(self, project_id, directory_prefix, *, apply=False):
        result = self.plan(project_id, directory_prefix)
        result["mode"] = "apply" if apply else "dry-run"
        if not apply:
            result["status"] = "partial" if result["unsupported"] else "planned"
            return result

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

    def plan(self, project_id, directory_prefix):
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
        sandboxes = _stale_paths(project.get("sandboxes") or [], prefix)
        if sandboxes:
            _remember(unsupported, "project_sandbox_remove")
        directories = _stale_paths(
            [record.get("directory") for record in directory_records if record.get("directory")],
            prefix,
        )
        planned_workspaces = [
            _workspace_plan(record)
            for record in workspaces
            if record.get("directory")
            and _is_stale(record.get("directory"), prefix)
            and workspace_project_id(record) == str(project_id)
        ]
        planned_workspaces = [record for record in planned_workspaces if record is not None]
        return {
            "status": "planned",
            "mode": "dry-run",
            "project_id": str(project_id),
            "directory_prefix": prefix,
            "planned_directories": sorted(set(directories + sandboxes)),
            "planned_project_sandboxes": sorted(set(sandboxes)),
            "planned_workspaces": planned_workspaces,
            "refreshed": False,
            "removed_workspaces": [],
            "verified": {"directories": [], "workspaces": []},
            "remaining": {"project_sandboxes": [], "project_directories": [], "workspaces": []},
            "unsupported": unsupported,
            "errors": [],
        }

    def _refresh_project_copies(self, result):
        if not result["planned_directories"]:
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
        for workspace in result["planned_workspaces"]:
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
        current_workspace_ids = set()
        if self.metadata.supports("workspace_collection"):
            current_workspace_ids = {
                str(record.get("id"))
                for record in self.metadata.list_workspaces(project_id=result["project_id"]).records
                if record.get("id") is not None
            }

        result["remaining"] = {
            "project_sandboxes": sorted(planned_paths & current_sandboxes),
            "project_directories": sorted(planned_paths & current_directories),
            "workspaces": sorted(planned_workspace_ids & current_workspace_ids),
        }
        remaining_paths = set(result["remaining"]["project_sandboxes"] + result["remaining"]["project_directories"])
        result["verified"] = {
            "directories": sorted(planned_paths - remaining_paths),
            "workspaces": sorted(planned_workspace_ids - current_workspace_ids),
        }
        if result["remaining"]["project_sandboxes"]:
            _remember(result["unsupported"], "project_sandbox_remove")


def _workspace_plan(workspace):
    workspace_id = workspace.get("id")
    if workspace_id is None:
        return None
    return {
        "id": str(workspace_id),
        "directory": normalized_directory(workspace.get("directory")),
    }


def _stale_paths(paths, prefix):
    return [normalized_directory(path) for path in paths if _is_stale(path, prefix)]


def _is_stale(path, prefix):
    normalized = normalized_directory(path)
    return _matches_directory_prefix(normalized, prefix) and not Path(normalized).exists()


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
