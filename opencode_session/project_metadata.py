from dataclasses import dataclass
from pathlib import Path

from opencode_session.api_profile import OpenCodeServerProfile
from opencode_session.api_transport import OpenCodeApiError


@dataclass(frozen=True)
class ProjectMetadataResult:
    records: list
    raw_body: str


class ProjectMetadataUnsupported(Exception):
    def __init__(self, route_name, route):
        super().__init__(f"unsupported OpenCode route: {route['method']} {route['path']}")
        self.route_name = route_name
        self.route = route


class ProjectMetadataNotFound(Exception):
    pass


class ProjectMetadataService:
    def __init__(self, client):
        self.client = client
        self.profile = OpenCodeServerProfile.from_openapi_doc(client.require_openapi_doc())
        client.configure_server_profile(self.profile)

    def supports(self, route_name):
        return self.profile.route_available(route_name)

    def require_route(self, route_name):
        if self.supports(route_name):
            return
        route = self.profile.route_availability.get(route_name) or {
            "method": "UNKNOWN",
            "path": self.profile.route_plan.get(route_name, route_name),
        }
        raise ProjectMetadataUnsupported(route_name, route)

    def list_projects(self, *, directory=None):
        self.require_route("project_collection")
        response = self.client.list_projects_response()
        projects = _records(response.data, "projects")
        if directory is not None:
            target = normalized_directory(directory)
            projects = [project for project in projects if target in project_directories(project)]
        return ProjectMetadataResult(projects, response.body)

    def inspect_project(self, project_id):
        result = self.list_projects()
        project = next((project for project in result.records if str(project.get("id")) == str(project_id)), None)
        if project is None:
            raise ProjectMetadataNotFound(f"project '{project_id}' was not found")
        return ProjectMetadataResult([project], result.raw_body)

    def list_project_directories(self, project_id, *, directory=None):
        self.require_route("project_directories")
        response = self.client.list_project_directories_response(project_id)
        records = [_directory_record(record) for record in _records(response.data, "directories", allow_strings=True)]
        if directory is not None:
            target = normalized_directory(directory)
            records = [record for record in records if normalized_directory(record.get("directory")) == target]
        return ProjectMetadataResult(records, response.body)

    def list_workspaces(self, *, project_id=None, directory=None):
        self.require_route("workspace_collection")
        response = self.client.list_workspaces_response()
        workspaces = _records(response.data, "workspaces")
        if project_id is not None:
            workspaces = [record for record in workspaces if workspace_project_id(record) == str(project_id)]
        if directory is not None:
            target = normalized_directory(directory)
            workspaces = [
                record for record in workspaces if normalized_directory(record.get("directory")) == target
            ]
        return ProjectMetadataResult(workspaces, response.body)


def project_directories(project):
    values = []
    for value in (project.get("worktree"), project.get("directory")):
        if value:
            values.append(normalized_directory(value))
    for value in project.get("sandboxes") or []:
        if value:
            values.append(normalized_directory(value))
    return values


def workspace_project_id(workspace):
    value = workspace.get("projectID")
    if value is None:
        value = workspace.get("project_id")
    return None if value is None else str(value)


def normalized_directory(directory):
    if directory is None:
        return None
    return str(Path(str(directory)).expanduser().resolve(strict=False))


def _records(payload, collection_name, *, allow_strings=False):
    records = payload
    if isinstance(payload, dict):
        records = payload.get(collection_name)
    if not isinstance(records, list):
        raise OpenCodeApiError(
            f"OpenCode {collection_name} response has invalid schema",
            data={"kind": "invalid_schema", "collection": collection_name},
        )
    if not allow_strings and not all(isinstance(record, dict) for record in records):
        raise OpenCodeApiError(
            f"OpenCode {collection_name} response has invalid records",
            data={"kind": "invalid_schema", "collection": collection_name},
        )
    return records


def _directory_record(record):
    if isinstance(record, str):
        return {"directory": record}
    if isinstance(record, dict) and record.get("directory"):
        return dict(record)
    raise OpenCodeApiError(
        "OpenCode directories response has invalid record",
        data={"kind": "invalid_schema", "collection": "directories"},
    )
