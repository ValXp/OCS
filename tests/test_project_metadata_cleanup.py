import os
import tempfile
import unittest

from opencode_session.project_metadata import ProjectMetadataResult, workspace_project_id
from opencode_session.project_metadata_cleanup import ProjectCopyCleanupError, ProjectCopyCleanupService
from opencode_session.run_cleanup_projects import freeze_project_refresh_scope

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, load_json, run_ocs
    from tests.test_project_metadata import PROJECT_PATHS
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, load_json, run_ocs
    from test_project_metadata import PROJECT_PATHS


SUPPORTED_ROUTES = {
    "project_collection",
    "project_directories",
    "workspace_collection",
    "workspace_item",
    "project_copy_refresh",
}


class FakeMetadata:
    def __init__(self, project, directories, workspaces, *, supported=None):
        self.project = dict(project)
        self.directories = [dict(record) for record in directories]
        self.workspaces = [dict(record) for record in workspaces]
        self.supported = set(SUPPORTED_ROUTES if supported is None else supported)
        self.client = self
        self.calls = []

    def supports(self, route_name):
        return route_name in self.supported

    def inspect_project(self, project_id):
        if str(self.project.get("id")) != str(project_id):
            raise AssertionError("unexpected project")
        return ProjectMetadataResult([dict(self.project)], "{}")

    def list_project_directories(self, project_id):
        return ProjectMetadataResult([dict(record) for record in self.directories], "{}")

    def list_workspaces(self, *, project_id=None):
        records = self.workspaces
        if project_id is not None:
            records = [record for record in records if workspace_project_id(record) == str(project_id)]
        return ProjectMetadataResult([dict(record) for record in records], "{}")

    def refresh_project_copies_response(self, project_id):
        self.calls.append(("POST", "project_copy_refresh", str(project_id)))
        self.directories = [record for record in self.directories if os.path.exists(record["directory"])]

    def delete_workspace_response(self, workspace_id):
        self.calls.append(("DELETE", "workspace", str(workspace_id)))
        self.workspaces = [record for record in self.workspaces if str(record.get("id")) != str(workspace_id)]


class ProjectMetadataCleanupServiceTest(unittest.TestCase):
    def test_filesystem_root_prefix_is_rejected(self):
        metadata = FakeMetadata({"id": "project-a", "sandboxes": []}, [], [])

        with self.assertRaisesRegex(ProjectCopyCleanupError, "cannot be a filesystem root"):
            ProjectCopyCleanupService(metadata).cleanup("project-a", os.path.abspath(os.sep))

    def test_dry_run_has_no_mutating_calls(self):
        with tempfile.TemporaryDirectory() as parent:
            prefix = os.path.join(parent, "ocs-run-")
            stale = prefix + "worker"
            metadata = FakeMetadata(
                {"id": "project-a", "sandboxes": []},
                [{"directory": stale, "strategy": "worktree"}],
                [{"id": "ws-a", "projectID": "project-a", "directory": stale}],
            )

            result = ProjectCopyCleanupService(metadata).cleanup("project-a", prefix)

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["planned_directories"], [stale])
        self.assertEqual(result["planned_workspaces"], [{"id": "ws-a", "directory": stale}])
        self.assertEqual(metadata.calls, [])

    def test_run_cleanup_preview_can_plan_paths_that_will_be_removed_first(self):
        with tempfile.TemporaryDirectory() as parent:
            prefix = os.path.join(parent, "ocs-run-")
            existing = prefix + "worker"
            os.mkdir(existing)
            metadata = FakeMetadata(
                {"id": "project-a", "sandboxes": []},
                [{"directory": existing, "strategy": "worktree"}],
                [{"id": "ws-a", "projectID": "project-a", "directory": existing}],
            )

            result = ProjectCopyCleanupService(metadata).cleanup(
                "project-a",
                prefix,
                assumed_missing_paths=[existing],
            )

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["planned_directories"], [existing])
        self.assertEqual(result["planned_workspaces"], [{"id": "ws-a", "directory": existing}])
        self.assertEqual(metadata.calls, [])

    def test_apply_refreshes_then_removes_matching_workspace_and_verifies(self):
        with tempfile.TemporaryDirectory() as parent:
            prefix = os.path.join(parent, "ocs-run-")
            stale = prefix + "worker"
            metadata = FakeMetadata(
                {"id": "project-a", "sandboxes": []},
                [{"directory": stale, "strategy": "worktree"}],
                [{"id": "ws-a", "projectID": "project-a", "directory": stale}],
            )

            result = ProjectCopyCleanupService(metadata).cleanup("project-a", prefix, apply=True)

        self.assertEqual(result["status"], "done")
        self.assertTrue(result["refreshed"])
        self.assertEqual(result["removed_workspaces"], ["ws-a"])
        self.assertEqual(result["verified"], {"directories": [stale], "workspaces": ["ws-a"]})
        self.assertEqual(result["remaining"], {"project_sandboxes": [], "project_directories": [], "workspaces": []})
        self.assertEqual(
            metadata.calls,
            [("POST", "project_copy_refresh", "project-a"), ("DELETE", "workspace", "ws-a")],
        )

    def test_missing_directory_never_calls_project_copy_delete(self):
        with tempfile.TemporaryDirectory() as parent:
            prefix = os.path.join(parent, "ocs-run-")
            stale = prefix + "worker"
            metadata = FakeMetadata(
                {"id": "project-a", "sandboxes": []},
                [{"directory": stale}],
                [],
            )

            ProjectCopyCleanupService(metadata).cleanup("project-a", prefix, apply=True)

        self.assertEqual(metadata.calls, [("POST", "project_copy_refresh", "project-a")])

    def test_sibling_prefix_and_other_project_are_untouched(self):
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as sibling_parent:
            prefix = os.path.join(parent, "ocs-run-")
            sibling = os.path.join(sibling_parent, "ocs-run-worker")
            matching = prefix + "worker"
            metadata = FakeMetadata(
                {"id": "project-a", "sandboxes": [sibling]},
                [{"directory": sibling}],
                [
                    {"id": "ws-other", "projectID": "project-b", "directory": matching},
                    {"id": "ws-sibling", "projectID": "project-a", "directory": sibling},
                ],
            )

            result = ProjectCopyCleanupService(metadata).cleanup("project-a", prefix)

        self.assertEqual(result["planned_directories"], [])
        self.assertEqual(result["planned_workspaces"], [])

    def test_project_wide_refresh_is_blocked_when_it_would_remove_unrelated_stale_metadata(self):
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as other_parent:
            prefix = os.path.join(parent, "ocs-run-")
            matching = prefix + "worker"
            unrelated = os.path.join(other_parent, "other-worker")
            metadata = FakeMetadata(
                {"id": "project-a", "sandboxes": []},
                [{"directory": matching}, {"directory": unrelated}],
                [],
            )

            dry_run = ProjectCopyCleanupService(metadata).cleanup("project-a", prefix)
            result = ProjectCopyCleanupService(metadata).cleanup("project-a", prefix, apply=True)

        self.assertEqual(dry_run["status"], "partial")
        self.assertIn("project_copy_refresh_unscoped", dry_run["unsupported"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unrelated_stale_directories"], [unrelated])
        self.assertIn("project_copy_refresh_unscoped", result["unsupported"])
        self.assertEqual(metadata.calls, [])
        self.assertEqual(result["remaining"]["project_directories"], [matching])

    def test_same_project_missing_prefixes_are_frozen_as_one_allowed_refresh_scope(self):
        with tempfile.TemporaryDirectory() as parent:
            stale_a = os.path.join(parent, "ocs-a-worker")
            stale_b = os.path.join(parent, "ocs-b-worker")
            metadata = FakeMetadata(
                {"id": "project-a", "sandboxes": []},
                [{"directory": stale_a}, {"directory": stale_b}],
                [],
            )
            service = ProjectCopyCleanupService(metadata)
            project_plans = [
                {"outcome": service.cleanup("project-a", os.path.join(parent, "ocs-a-"))},
                {"outcome": service.cleanup("project-a", os.path.join(parent, "ocs-b-"))},
            ]

            freeze_project_refresh_scope(project_plans)
            result_a = service.apply_plan(project_plans[0]["outcome"])
            result_b = service.apply_plan(project_plans[1]["outcome"])

        for project_plan in project_plans:
            self.assertEqual(project_plan["outcome"]["status"], "planned")
            self.assertEqual(project_plan["outcome"]["unrelated_stale_directories"], [])
            self.assertEqual(
                project_plan["outcome"]["allowed_refresh_directories"],
                [stale_a, stale_b],
            )
        self.assertEqual(result_a["status"], "done")
        self.assertEqual(result_b["status"], "done")
        self.assertEqual(metadata.calls, [("POST", "project_copy_refresh", "project-a")])

    def test_dry_run_reports_missing_mutation_routes_before_apply(self):
        with tempfile.TemporaryDirectory() as parent:
            prefix = os.path.join(parent, "ocs-run-")
            stale = prefix + "worker"
            cases = (
                (
                    SUPPORTED_ROUTES - {"project_copy_refresh"},
                    [{"directory": stale}],
                    [],
                    "project_copy_refresh",
                ),
                (
                    SUPPORTED_ROUTES - {"workspace_item"},
                    [],
                    [{"id": "ws-a", "projectID": "project-a", "directory": stale}],
                    "workspace_item",
                ),
            )
            for supported, directories, workspaces, expected in cases:
                with self.subTest(expected=expected):
                    metadata = FakeMetadata(
                        {"id": "project-a", "sandboxes": []},
                        directories,
                        workspaces,
                        supported=supported,
                    )

                    result = ProjectCopyCleanupService(metadata).cleanup("project-a", prefix)

                    self.assertEqual(result["status"], "partial")
                    self.assertIn(expected, result["unsupported"])
                    self.assertEqual(metadata.calls, [])

    def test_residual_project_sandbox_is_reported_unsupported(self):
        with tempfile.TemporaryDirectory() as parent:
            prefix = os.path.join(parent, "ocs-run-")
            stale = prefix + "worker"
            metadata = FakeMetadata(
                {"id": "project-a", "sandboxes": [stale]},
                [{"directory": stale}],
                [],
            )

            dry_run = ProjectCopyCleanupService(metadata).cleanup("project-a", prefix)
            result = ProjectCopyCleanupService(metadata).cleanup("project-a", prefix, apply=True)

        self.assertEqual(dry_run["status"], "partial")
        self.assertIn("project_sandbox_remove", dry_run["unsupported"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["remaining"]["project_sandboxes"], [stale])
        self.assertIn("project_sandbox_remove", result["unsupported"])
        self.assertNotIn(stale, result["verified"]["directories"])

    def test_matching_workspace_without_id_is_reported_and_remains_visible(self):
        with tempfile.TemporaryDirectory() as parent:
            prefix = os.path.join(parent, "ocs-run-")
            stale = prefix + "worker"
            metadata = FakeMetadata(
                {"id": "project-a", "sandboxes": []},
                [],
                [{"projectID": "project-a", "directory": stale}],
            )

            dry_run = ProjectCopyCleanupService(metadata).cleanup("project-a", prefix)
            result = ProjectCopyCleanupService(metadata).cleanup("project-a", prefix, apply=True)

        self.assertEqual(dry_run["status"], "partial")
        self.assertIn("workspace_identity", dry_run["unsupported"])
        self.assertEqual(dry_run["invalid_workspaces"][0]["directory"], stale)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["remaining"]["workspaces"], [f"invalid-id:{stale}"])

    def test_unsafe_workspace_ids_never_reach_delete_routes(self):
        with tempfile.TemporaryDirectory() as parent:
            prefix = os.path.join(parent, "ocs-run-")
            stale = prefix + "worker"
            for workspace_id in ("", ".", ".."):
                with self.subTest(workspace_id=workspace_id):
                    metadata = FakeMetadata(
                        {"id": "project-a", "sandboxes": []},
                        [],
                        [{"id": workspace_id, "projectID": "project-a", "directory": stale}],
                    )

                    dry_run = ProjectCopyCleanupService(metadata).cleanup("project-a", prefix)
                    result = ProjectCopyCleanupService(metadata).cleanup("project-a", prefix, apply=True)

                    self.assertEqual(dry_run["status"], "partial")
                    self.assertIn("workspace_identity", dry_run["unsupported"])
                    self.assertEqual(result["status"], "partial")
                    self.assertEqual(metadata.calls, [])

    def test_frozen_plan_does_not_delete_workspace_that_appears_after_preflight(self):
        with tempfile.TemporaryDirectory() as parent:
            prefix = os.path.join(parent, "ocs-run-")
            stale = prefix + "worker"
            metadata = FakeMetadata(
                {"id": "project-a", "sandboxes": []},
                [],
                [{"id": "ws-planned", "projectID": "project-a", "directory": stale}],
            )
            service = ProjectCopyCleanupService(metadata)
            plan = service.cleanup("project-a", prefix)
            metadata.workspaces.append(
                {"id": "ws-new", "projectID": "project-a", "directory": stale}
            )

            result = service.apply_plan(plan)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["removed_workspaces"], ["ws-planned"])
        self.assertEqual(result["remaining"]["workspaces"], ["ws-new"])
        self.assertIn({"id": "ws-new", "projectID": "project-a", "directory": stale}, metadata.workspaces)

    def test_verification_reports_new_matching_project_sandbox_after_preflight(self):
        with tempfile.TemporaryDirectory() as parent:
            prefix = os.path.join(parent, "ocs-run-")
            planned = prefix + "worker-a"
            appeared = prefix + "worker-b"
            metadata = FakeMetadata(
                {"id": "project-a", "sandboxes": []},
                [{"directory": planned}],
                [],
            )
            service = ProjectCopyCleanupService(metadata)
            plan = service.cleanup("project-a", prefix)
            metadata.project["sandboxes"] = [appeared]

            result = service.apply_plan(plan)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["remaining"]["project_sandboxes"], [appeared])
        self.assertIn("project_sandbox_remove", result["unsupported"])


class ProjectMetadataCleanupCommandTest(unittest.TestCase):
    def test_json_reports_exact_removed_records(self):
        with tempfile.TemporaryDirectory() as parent:
            prefix = os.path.join(parent, "ocs-run-")
            stale = prefix + "worker"
            state = {
                "directories": [{"directory": stale, "strategy": "worktree"}],
                "workspaces": [{"id": "ws-a", "projectID": "project-a", "directory": stale}],
            }
            with FakeOpenCodeServer() as server:
                server.json("GET", "/doc", {"openapi": "3.1.0", "paths": PROJECT_PATHS})
                server.json("GET", "/project", [{"id": "project-a", "worktree": "/repo", "sandboxes": []}])
                server.json("GET", "/project/project-a/directories", lambda _request: state["directories"])
                server.json("GET", "/experimental/workspace", lambda _request: state["workspaces"])

                def refresh(_request):
                    state["directories"] = []
                    return {}

                def remove_workspace(_request):
                    state["workspaces"] = []
                    return {"id": "ws-a"}

                server.json("POST", "/experimental/project/project-a/copy/refresh", refresh)
                server.json("DELETE", "/experimental/workspace/ws-a", remove_workspace)

                command = run_ocs(
                    "project-copy",
                    "cleanup",
                    "project-a",
                    "--directory-prefix",
                    prefix,
                    "--apply",
                    "--server",
                    server.url,
                    "--json",
                )

        self.assertEqual(command.returncode, 0, command.stderr)
        result = load_json(self, command)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["planned_directories"], [stale])
        self.assertEqual(result["removed_workspaces"], ["ws-a"])
        self.assertEqual(result["verified"], {"directories": [stale], "workspaces": ["ws-a"]})

    def test_residual_project_sandbox_returns_partial_json_and_unsupported_exit(self):
        with tempfile.TemporaryDirectory() as parent:
            prefix = os.path.join(parent, "ocs-run-")
            stale = prefix + "worker"
            state = {"directories": [{"directory": stale}]}
            with FakeOpenCodeServer() as server:
                server.json("GET", "/doc", {"openapi": "3.1.0", "paths": PROJECT_PATHS})
                server.json(
                    "GET",
                    "/project",
                    [{"id": "project-a", "worktree": "/repo", "sandboxes": [stale]}],
                )
                server.json("GET", "/project/project-a/directories", lambda _request: state["directories"])
                server.json("GET", "/experimental/workspace", [])

                def refresh(_request):
                    state["directories"] = []
                    return {}

                server.json("POST", "/experimental/project/project-a/copy/refresh", refresh)

                command = run_ocs(
                    "project-copy",
                    "cleanup",
                    "project-a",
                    "--directory-prefix",
                    prefix,
                    "--apply",
                    "--server",
                    server.url,
                    "--json",
                )

        self.assertEqual(command.returncode, 70)
        result = load_json(self, command)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["remaining"]["project_sandboxes"], [stale])
        self.assertIn("project_sandbox_remove", result["unsupported"])
        self.assertIn("unsupported metadata remains", command.stderr)


if __name__ == "__main__":
    unittest.main()
