import tempfile
import unittest

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.project_metadata import ProjectMetadataService

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, load_json, run_ocs
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, load_json, run_ocs


PROJECT_PATHS = {
    "/project": {"get": {}},
    "/project/{projectID}/directories": {"get": {}},
    "/experimental/workspace": {"get": {}},
    "/experimental/workspace/{id}": {"delete": {}},
    "/experimental/project/{projectID}/copy/refresh": {"post": {}},
}


def metadata_server(*, projects=(), directories=None, workspaces=(), paths=None):
    server = FakeOpenCodeServer()
    server.json("GET", "/doc", {"openapi": "3.1.0", "paths": paths if paths is not None else PROJECT_PATHS})
    server.json("GET", "/project", list(projects))
    for project_id, records in (directories or {}).items():
        server.json("GET", f"/project/{project_id}/directories", records)
    server.json("GET", "/experimental/workspace", list(workspaces))
    return server


class ProjectMetadataCommandTest(unittest.TestCase):
    def test_project_list_filters_directory_and_renders_json(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            projects = [
                {"id": "project-a", "name": "A", "worktree": other, "sandboxes": [directory]},
                {"id": "project-b", "name": "B", "worktree": other, "sandboxes": []},
            ]
            with metadata_server(projects=projects) as server:
                result = run_ocs(
                    "project",
                    "list",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                    "--json",
                )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(load_json(self, result), [projects[0]])

    def test_project_inspect_and_directories_render_normalized_records(self):
        projects = [{"id": "project-a", "worktree": "/repo", "sandboxes": []}]
        directories = {"project-a": ["/repo", {"directory": "/repo-copy", "strategy": "worktree"}]}
        with metadata_server(projects=projects, directories=directories) as server:
            inspect_result = run_ocs("project", "inspect", "project-a", "--server", server.url, "--json")
            directories_result = run_ocs(
                "project", "directories", "project-a", "--server", server.url, "--json"
            )

        self.assertEqual(inspect_result.returncode, 0, inspect_result.stderr)
        self.assertEqual(load_json(self, inspect_result), projects[0])
        self.assertEqual(directories_result.returncode, 0, directories_result.stderr)
        self.assertEqual(
            load_json(self, directories_result),
            [{"directory": "/repo"}, {"directory": "/repo-copy", "strategy": "worktree"}],
        )

    def test_workspace_list_filters_project_and_directory(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            workspaces = [
                {"id": "ws-a", "projectID": "project-a", "directory": directory, "name": "A"},
                {"id": "ws-b", "projectID": "project-b", "directory": directory, "name": "B"},
                {"id": "ws-c", "projectID": "project-a", "directory": other, "name": "C"},
            ]
            with metadata_server(workspaces=workspaces) as server:
                result = run_ocs(
                    "workspace",
                    "list",
                    "--project-id",
                    "project-a",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                    "--json",
                )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(load_json(self, result), [workspaces[0]])

    def test_missing_route_returns_unsupported(self):
        with metadata_server(paths={}) as server:
            result = run_ocs("workspace", "list", "--server", server.url, "--json")

        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stdout, "")
        self.assertIn("unsupported OpenCode route: GET /experimental/workspace", result.stderr)


class ProjectMetadataClientTest(unittest.TestCase):
    def test_renders_quoted_project_id(self):
        with metadata_server() as server:
            server.json("GET", "/project/project%2Fone/directories", [])
            metadata = ProjectMetadataService(OpenCodeApiClient(server.url))

            result = metadata.list_project_directories("project/one")

        self.assertEqual(result.records, [])
        self.assertIn(("GET", "/project/project%2Fone/directories", None), server.requests)


if __name__ == "__main__":
    unittest.main()
