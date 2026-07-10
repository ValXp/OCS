import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from opencode_session.run_cleanup_audit import begin_cleanup_audit, finish_cleanup_audit
from opencode_session.run_store import RunStore, RunStoreError

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs


class RunCleanupCliTest(unittest.TestCase):
    def test_dry_run_lists_exact_resources_without_changing_them(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "worker.log"
            log.write_text("keep", encoding="utf-8")
            self._create_run_with_worker(store, directory, session_id="ses_owned", owned_log=log)

            result = run_ocs(
                "run", "--store", store, "cleanup", "demo",
                "--sessions", "--logs", "--run-store", "--dry-run", "--json",
            )
            payload = load_json(self, result, "cleanup dry-run")

            self.assertEqual(result.returncode, 0, format_completed_process(result))
            self.assertEqual(payload["mode"], "dry-run")
            self.assertEqual(payload["planned"]["sessions"], ["ses_owned"])
            self.assertEqual([record["path"] for record in payload["planned"]["logs"]], [str(log)])
            self.assertTrue(payload["planned"]["run_store"])
            self.assertTrue(log.exists())
            self.assertTrue((Path(store) / "demo.json").exists())

    def test_apply_deletes_recorded_session_log_and_run_record_last(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "worker.log"
            log.write_text("remove", encoding="utf-8")
            with _cleanup_server(["ses_owned"]) as server:
                self._create_run_with_worker(
                    store,
                    directory,
                    session_id="ses_owned",
                    owned_log=log,
                    server_url=server.url,
                )
                result = run_ocs(
                    "run", "--store", store, "cleanup", "demo",
                    "--sessions", "--logs", "--run-store", "--apply", "--json",
                )
                payload = load_json(self, result, "cleanup apply")
                requests = list(server.requests)

            self.assertEqual(result.returncode, 0, format_completed_process(result))
            self.assertEqual(payload["status"], "done")
            self.assertEqual(payload["completed"]["sessions"], ["ses_owned"])
            self.assertTrue(payload["run_store_deleted"])
            self.assertFalse(log.exists())
            self.assertFalse((Path(store) / "demo.json").exists())
            self.assertIn(("DELETE", "/api/session/ses_owned", None), requests)
            self.assertIn(("GET", "/api/session/ses_owned", None), requests)

    def test_partial_failure_keeps_audited_run_record(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "worker.log"
            log.write_text("remove", encoding="utf-8")
            with _cleanup_server(["ses_owned"], delete_failure=True) as server:
                self._create_run_with_worker(
                    store,
                    directory,
                    session_id="ses_owned",
                    owned_log=log,
                    server_url=server.url,
                )
                result = run_ocs(
                    "run", "--store", store, "cleanup", "demo",
                    "--sessions", "--logs", "--run-store", "--apply", "--json",
                )
                payload = load_json(self, result, "partial cleanup")

            self.assertEqual(result.returncode, 1, format_completed_process(result))
            self.assertEqual(payload["status"], "partial")
            self.assertFalse(payload["run_store_deleted"])
            self.assertFalse(log.exists())
            run = RunStore(store).load_run("demo")
            self.assertEqual(run["resource_cleanup"]["status"], "partial")
            self.assertTrue(run["resource_cleanup"]["errors"])

    def test_registered_clean_worktree_and_merged_branch_are_removed(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store:
            repository = Path(root) / "repo"
            worktree = Path(root) / "owned-worktree"
            _create_repository_with_worktree(repository, worktree, branch="owned-branch")
            self._create_run_with_worker(store, repository, owned_worktree=worktree)

            result = run_ocs(
                "run", "--store", store, "cleanup", "demo",
                "--worktrees", "--branches", "--apply", "--json",
            )

            self.assertEqual(result.returncode, 0, format_completed_process(result))
            self.assertFalse(worktree.exists())
            branches = _git(repository, "branch", "--format=%(refname:short)").splitlines()
            self.assertNotIn("owned-branch", branches)

    def test_dirty_worktree_is_refused_without_force_and_unrelated_path_is_untouched(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store:
            repository = Path(root) / "repo"
            worktree = Path(root) / "owned-worktree"
            unrelated = Path(root) / "unrelated"
            unrelated.mkdir()
            (unrelated / "keep.txt").write_text("keep", encoding="utf-8")
            _create_repository_with_worktree(repository, worktree, branch="owned-branch")
            (worktree / "dirty.txt").write_text("dirty", encoding="utf-8")
            self._create_run_with_worker(store, repository, owned_worktree=worktree)

            refused = run_ocs(
                "run", "--store", store, "cleanup", "demo", "--worktrees", "--apply", "--json",
            )
            self.assertEqual(refused.returncode, 1, format_completed_process(refused))
            self.assertTrue(worktree.exists())
            forced = run_ocs(
                "run", "--store", store, "cleanup", "demo", "--worktrees", "--apply", "--force", "--json",
            )

            self.assertEqual(forced.returncode, 0, format_completed_process(forced))
            self.assertFalse(worktree.exists())
            self.assertTrue((unrelated / "keep.txt").exists())

    def test_active_worker_blocks_all_apply_side_effects_without_force(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "worker.log"
            log.write_text("keep", encoding="utf-8")
            self._create_run_with_worker(store, directory, owned_log=log, worker_status="active")

            blocked = run_ocs(
                "run", "--store", store, "cleanup", "demo", "--logs", "--apply", "--json",
            )
            payload = load_json(self, blocked, "active cleanup")

            self.assertEqual(blocked.returncode, 1, format_completed_process(blocked))
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(payload["preflight"]["active_workers"], ["worker"])
            self.assertTrue(log.exists())
            self.assertNotIn("resource_cleanup", RunStore(store).load_run("demo"))

            forced = run_ocs(
                "run", "--store", store, "cleanup", "demo", "--logs", "--apply", "--force", "--json",
            )
            self.assertEqual(forced.returncode, 0, format_completed_process(forced))
            self.assertFalse(log.exists())

    def test_dry_run_reports_dirty_preflight_and_server_override_without_mutation(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store:
            repository = Path(root) / "repo"
            worktree = Path(root) / "owned-worktree"
            _create_repository_with_worktree(repository, worktree, branch="owned-branch")
            (worktree / "dirty.txt").write_text("dirty", encoding="utf-8")
            self._create_run_with_worker(store, repository, owned_worktree=worktree)

            result = run_ocs(
                "run", "--store", store, "cleanup", "demo", "--worktrees", "--dry-run",
                "--server", "http://override.example", "--json",
            )
            payload = load_json(self, result, "cleanup preflight")

            self.assertEqual(result.returncode, 1, format_completed_process(result))
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(payload["planned"]["server_url"], "http://override.example")
            self.assertEqual(payload["preflight"]["blocked_worktrees"], [str(worktree)])
            self.assertTrue(worktree.exists())
            self.assertNotIn("resource_cleanup", RunStore(store).load_run("demo"))

    def test_force_never_bypasses_registered_log_identity(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "worker.log"
            log.write_text("owned", encoding="utf-8")
            self._create_run_with_worker(store, directory, owned_log=log)
            original = Path(directory) / "original.log"
            log.rename(original)
            log.write_text("replacement", encoding="utf-8")

            result = run_ocs(
                "run", "--store", store, "cleanup", "demo", "--logs", "--apply", "--force", "--json",
            )
            payload = load_json(self, result, "replacement log cleanup")

            self.assertEqual(result.returncode, 1, format_completed_process(result))
            self.assertEqual(payload["preflight"]["blocked_logs"], [str(log)])
            self.assertTrue(log.exists())
            self.assertEqual(log.read_text(encoding="utf-8"), "replacement")

    def test_force_never_bypasses_registered_worktree_identity(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store:
            repository = Path(root) / "repo"
            worktree = Path(root) / "owned-worktree"
            _create_repository_with_worktree(repository, worktree, branch="owned-branch")
            self._create_run_with_worker(store, repository, owned_worktree=worktree)
            _git(worktree, "switch", "-c", "replacement-branch")

            result = run_ocs(
                "run", "--store", store, "cleanup", "demo", "--worktrees", "--apply", "--force", "--json",
            )
            payload = load_json(self, result, "replacement worktree cleanup")

            self.assertEqual(result.returncode, 1, format_completed_process(result))
            self.assertEqual(payload["preflight"]["blocked_worktrees"], [str(worktree)])
            self.assertTrue(worktree.exists())

    def test_branch_cleanup_refuses_a_recreated_branch_after_worktree_disappears(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store:
            repository = Path(root) / "repo"
            worktree = Path(root) / "owned-worktree"
            _create_repository_with_worktree(repository, worktree, branch="owned-branch")
            self._create_run_with_worker(store, repository, owned_worktree=worktree)
            _git(repository, "worktree", "remove", str(worktree))
            _git(repository, "branch", "-D", "owned-branch")
            (repository / "replacement.txt").write_text("replacement\n", encoding="utf-8")
            _git(repository, "add", "replacement.txt")
            _git(repository, "commit", "-m", "replacement branch base")
            _git(repository, "branch", "owned-branch")

            result = run_ocs(
                "run", "--store", store, "cleanup", "demo", "--branches", "--apply", "--force", "--json",
            )
            payload = load_json(self, result, "recreated branch cleanup")

            self.assertEqual(result.returncode, 1, format_completed_process(result))
            self.assertTrue(payload["preflight"]["blocked_branches"])
            self.assertIn("owned-branch", _git(repository, "branch", "--format=%(refname:short)").splitlines())

    def test_retry_treats_already_absent_owned_worktree_and_branch_as_complete(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store:
            repository = Path(root) / "repo"
            worktree = Path(root) / "owned-worktree"
            _create_repository_with_worktree(repository, worktree, branch="owned-branch")
            self._create_run_with_worker(store, repository, owned_worktree=worktree)
            _git(repository, "worktree", "remove", str(worktree))
            _git(repository, "branch", "-D", "owned-branch")

            result = run_ocs(
                "run", "--store", store, "cleanup", "demo", "--all", "--apply", "--json",
            )
            payload = load_json(self, result, "idempotent cleanup retry")

            self.assertEqual(result.returncode, 0, format_completed_process(result))
            self.assertTrue(payload["run_store_deleted"])
            self.assertFalse((Path(store) / "demo.json").exists())

    def test_apply_removes_worktree_before_refreshing_and_verifying_project_metadata(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store:
            repository = Path(root) / "repo"
            worktree = Path(root) / "owned-worktree"
            _create_repository_with_worktree(repository, worktree, branch="owned-branch")
            state = {
                "directories": [{"directory": str(worktree), "strategy": "worktree"}],
                "workspaces": [{"id": "ws-owned", "projectID": "project-a", "directory": str(worktree)}],
            }
            with FakeOpenCodeServer() as server:
                server.json("GET", "/doc", {"paths": _project_cleanup_paths()})
                server.json(
                    "GET", "/project", [{"id": "project-a", "worktree": str(repository), "sandboxes": []}],
                )
                server.json("GET", "/project/project-a/directories", lambda _request: state["directories"])
                server.json("GET", "/experimental/workspace", lambda _request: state["workspaces"])

                def refresh(_request):
                    self.assertFalse(worktree.exists(), "project refresh must follow worktree removal")
                    state["directories"] = []
                    return {}

                def remove_workspace(_request):
                    state["workspaces"] = []
                    return {"id": "ws-owned"}

                server.json("POST", "/experimental/project/project-a/copy/refresh", refresh)
                server.json("DELETE", "/experimental/workspace/ws-owned", remove_workspace)
                self._create_run_with_worker(
                    store,
                    repository,
                    owned_worktree=worktree,
                    owned_project_copy=("project-a", worktree),
                    server_url=server.url,
                )

                result = run_ocs(
                    "run", "--store", store, "cleanup", "demo",
                    "--worktrees", "--project-metadata", "--apply", "--json",
                )
                payload = load_json(self, result, "project cleanup")

            self.assertEqual(result.returncode, 0, format_completed_process(result))
            self.assertEqual(payload["status"], "done")
            self.assertEqual(len(payload["completed"]["project_metadata"]), 1)
            self.assertFalse(worktree.exists())
            self.assertEqual(state, {"directories": [], "workspaces": []})

    def test_dry_run_previews_project_metadata_after_planned_worktree_removal(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store:
            repository = Path(root) / "repo"
            worktree = Path(root) / "owned-worktree"
            _create_repository_with_worktree(repository, worktree, branch="owned-branch")
            with FakeOpenCodeServer() as server:
                server.json("GET", "/doc", {"paths": _project_cleanup_paths()})
                server.json(
                    "GET", "/project", [{"id": "project-a", "worktree": str(repository), "sandboxes": []}],
                )
                server.json("GET", "/project/project-a/directories", [{"directory": str(worktree)}])
                server.json(
                    "GET",
                    "/experimental/workspace",
                    [{"id": "ws-owned", "projectID": "project-a", "directory": str(worktree)}],
                )
                self._create_run_with_worker(
                    store,
                    repository,
                    owned_worktree=worktree,
                    owned_project_copy=("project-a", worktree),
                    server_url=server.url,
                )

                result = run_ocs(
                    "run", "--store", store, "cleanup", "demo",
                    "--worktrees", "--project-metadata", "--dry-run", "--json",
                )
                payload = load_json(self, result, "project cleanup preview")
                requests = list(server.requests)

            self.assertEqual(result.returncode, 0, format_completed_process(result))
            self.assertEqual(payload["status"], "planned")
            self.assertEqual(payload["project_metadata_preview"][0]["planned_directories"], [str(worktree)])
            self.assertTrue(worktree.exists())
            self.assertNotIn("POST", [method for method, _path, _payload in requests])
            self.assertNotIn("DELETE", [method for method, _path, _payload in requests])

    def test_project_only_dry_run_and_apply_do_not_treat_existing_paths_as_stale(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store:
            repository = Path(root) / "repo"
            worktree = Path(root) / "owned-worktree"
            _create_repository_with_worktree(repository, worktree, branch="owned-branch")
            state = {
                "directories": [{"directory": str(worktree)}],
                "workspaces": [{"id": "ws-owned", "projectID": "project-a", "directory": str(worktree)}],
            }
            with FakeOpenCodeServer() as server:
                server.json("GET", "/doc", {"paths": _project_cleanup_paths()})
                server.json(
                    "GET", "/project", [{"id": "project-a", "worktree": str(repository), "sandboxes": []}],
                )
                server.json("GET", "/project/project-a/directories", lambda _request: state["directories"])
                server.json("GET", "/experimental/workspace", lambda _request: state["workspaces"])
                self._create_run_with_worker(
                    store,
                    repository,
                    owned_worktree=worktree,
                    owned_project_copy=("project-a", worktree),
                    server_url=server.url,
                )

                dry_run = run_ocs(
                    "run", "--store", store, "cleanup", "demo", "--project-metadata", "--json",
                )
                apply = run_ocs(
                    "run", "--store", store, "cleanup", "demo", "--project-metadata", "--apply", "--json",
                )
                dry_payload = load_json(self, dry_run, "project-only preview")
                apply_payload = load_json(self, apply, "project-only apply")
                requests = list(server.requests)

            self.assertEqual(dry_run.returncode, 0, format_completed_process(dry_run))
            self.assertEqual(apply.returncode, 0, format_completed_process(apply))
            self.assertEqual(dry_payload["project_metadata_preview"][0]["planned_directories"], [])
            self.assertEqual(apply_payload["project_metadata_results"][0]["planned_directories"], [])
            self.assertTrue(worktree.exists())
            self.assertEqual(state["workspaces"][0]["id"], "ws-owned")
            self.assertNotIn("POST", [method for method, _path, _payload in requests])
            self.assertNotIn("DELETE", [method for method, _path, _payload in requests])

    def test_partial_project_preview_blocks_apply_before_local_or_remote_mutation(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store:
            repository = Path(root) / "repo"
            worktree = Path(root) / "owned-worktree"
            unrelated_stale = Path(root) / "unrelated-stale"
            _create_repository_with_worktree(repository, worktree, branch="owned-branch")
            with FakeOpenCodeServer() as server:
                server.json("GET", "/doc", {"paths": _project_cleanup_paths()})
                server.json(
                    "GET",
                    "/project",
                    [{"id": "project-a", "worktree": str(repository), "sandboxes": [str(worktree)]}],
                )
                server.json(
                    "GET",
                    "/project/project-a/directories",
                    [{"directory": str(worktree)}, {"directory": str(unrelated_stale)}],
                )
                server.json("GET", "/experimental/workspace", [])
                self._create_run_with_worker(
                    store,
                    repository,
                    owned_worktree=worktree,
                    owned_project_copy=("project-a", worktree),
                    server_url=server.url,
                )

                result = run_ocs(
                    "run", "--store", store, "cleanup", "demo",
                    "--worktrees", "--project-metadata", "--apply", "--json",
                )
                payload = load_json(self, result, "partial project preflight")
                requests = list(server.requests)

            self.assertEqual(result.returncode, 1, format_completed_process(result))
            self.assertEqual(payload["mode"], "apply")
            self.assertEqual(payload["status"], "blocked")
            self.assertIn(
                "project_copy_refresh_unscoped",
                payload["project_metadata_preview"][0]["unsupported"],
            )
            self.assertTrue(worktree.exists())
            self.assertNotIn("resource_cleanup", RunStore(store).load_run("demo"))
            self.assertNotIn("POST", [method for method, _path, _payload in requests])
            self.assertNotIn("DELETE", [method for method, _path, _payload in requests])

    def test_same_project_refresh_allows_union_of_run_owned_worktrees(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store:
            repository = Path(root) / "repo"
            worktree_a = Path(root) / "owned-a"
            worktree_b = Path(root) / "owned-b"
            _create_repository_with_worktree(repository, worktree_a, branch="owned-a")
            _git(repository, "worktree", "add", "-b", "owned-b", str(worktree_b), "main")
            state = {
                "directories": [{"directory": str(worktree_a)}, {"directory": str(worktree_b)}],
                "refreshes": 0,
            }
            with FakeOpenCodeServer() as server:
                server.json("GET", "/doc", {"paths": _project_cleanup_paths()})
                server.json(
                    "GET", "/project", [{"id": "project-a", "worktree": str(repository), "sandboxes": []}],
                )
                server.json("GET", "/project/project-a/directories", lambda _request: state["directories"])
                server.json("GET", "/experimental/workspace", [])

                def refresh(_request):
                    self.assertFalse(worktree_a.exists())
                    self.assertFalse(worktree_b.exists())
                    state["refreshes"] += 1
                    state["directories"] = []
                    return {}

                server.json("POST", "/experimental/project/project-a/copy/refresh", refresh)
                init = run_ocs(
                    "run", "--store", store, "init", "demo",
                    "--directory", str(repository), "--server", server.url,
                )
                worker_a = run_ocs(
                    "run", "--store", store, "worker", "demo", "worker-a", "--role", "build",
                    "--owned-worktree", str(worktree_a),
                    "--owned-project-copy", "project-a", str(worktree_a),
                )
                worker_b = run_ocs(
                    "run", "--store", store, "worker", "demo", "worker-b", "--role", "build",
                    "--owned-worktree", str(worktree_b),
                    "--owned-project-copy", "project-a", str(worktree_b),
                )
                for command in (init, worker_a, worker_b):
                    self.assertEqual(command.returncode, 0, format_completed_process(command))

                result = run_ocs(
                    "run", "--store", store, "cleanup", "demo",
                    "--worktrees", "--project-metadata", "--apply", "--json",
                )
                payload = load_json(self, result, "multi-worktree project cleanup")

            self.assertEqual(result.returncode, 0, format_completed_process(result))
            self.assertEqual(payload["status"], "done")
            self.assertEqual(state["refreshes"], 1)
            self.assertEqual(len(payload["completed"]["worktrees"]), 2)
            self.assertEqual(len(payload["completed"]["project_metadata"]), 2)

    def test_retargeted_project_prefix_is_blocked_before_remote_inventory(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store:
            target_a = Path(root) / "target-a"
            target_b = Path(root) / "target-b"
            target_a.mkdir()
            target_b.mkdir()
            link = Path(root) / "project-link"
            link.symlink_to(target_a, target_is_directory=True)
            prefix = link / "ocs-run-"
            with FakeOpenCodeServer() as server:
                server.json("GET", "/doc", {"paths": _project_cleanup_paths()})
                self._create_run_with_worker(
                    store,
                    root,
                    owned_project_copy=("project-a", prefix),
                    server_url=server.url,
                )
                link.unlink()
                link.symlink_to(target_b, target_is_directory=True)

                result = run_ocs(
                    "run", "--store", store, "cleanup", "demo",
                    "--project-metadata", "--apply", "--json",
                )
                payload = load_json(self, result, "retargeted project prefix")
                requests = list(server.requests)

            self.assertEqual(result.returncode, 1, format_completed_process(result))
            self.assertEqual(payload["status"], "blocked")
            self.assertTrue(payload["preflight"]["blocked_project_metadata"])
            self.assertEqual(requests, [])

    def test_cleanup_requires_explicit_category(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            RunStore(store).create_run("demo", directory=directory, server_url="http://opencode.example")
            result = run_ocs("run", "--store", store, "cleanup", "demo")

        self.assertEqual(result.returncode, 64, format_completed_process(result))
        self.assertIn("requires a resource category", result.stderr)

    def _create_run_with_worker(
        self,
        store,
        directory,
        *,
        session_id=None,
        owned_log=None,
        owned_worktree=None,
        owned_project_copy=None,
        worker_status=None,
        server_url="http://opencode.example",
    ):
        init = run_ocs(
            "run", "--store", store, "init", "demo", "--directory", str(directory), "--server", server_url,
        )
        args = ["run", "--store", store, "worker", "demo", "worker", "--role", "build"]
        if session_id is not None:
            args.extend(("--session", session_id))
        if owned_log is not None:
            args.extend(("--owned-log", str(owned_log)))
        if owned_worktree is not None:
            args.extend(("--owned-worktree", str(owned_worktree)))
        if owned_project_copy is not None:
            args.extend(("--owned-project-copy", str(owned_project_copy[0]), str(owned_project_copy[1])))
        if worker_status is not None:
            args.extend(("--status", worker_status))
        worker = run_ocs(*args)
        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker.returncode, 0, format_completed_process(worker))


class RunStoreDeletionTest(unittest.TestCase):
    def test_delete_run_removes_record_and_preserves_missing_error_contract(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(store)
            run_store.create_run("demo", directory=directory, server_url="http://opencode.example")
            run_store.delete_run("demo")

            with self.assertRaisesRegex(RunStoreError, "not found"):
                run_store.load_run("demo")

    def test_delete_run_refuses_a_stale_expected_record(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(store)
            expected = run_store.create_run("demo", directory=directory, server_url="http://opencode.example")
            run_store.upsert_worker("demo", "worker", role="build")

            with self.assertRaisesRegex(RunStoreError, "changed before deletion") as raised:
                run_store.delete_run("demo", expected_run=expected)

            self.assertEqual(raised.exception.kind, "conflict")
            self.assertIn("worker", run_store.load_run("demo")["workers"])

    def test_cleanup_audit_detects_concurrent_run_mutation_and_preserves_it(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(store)
            expected = run_store.create_run("demo", directory=directory, server_url="http://opencode.example")
            record = {
                "status": "done",
                "completed": {name: [] for name in ("sessions", "worktrees", "branches", "project_metadata", "logs")},
                "errors": [],
            }
            baseline = begin_cleanup_audit(run_store, "demo", expected, record)
            run_store.update_run(
                "demo",
                lambda run: run.setdefault("output_refs", []).append("late-change"),
                allow_cleanup_in_progress=True,
            )

            finish_cleanup_audit(run_store, "demo", baseline, record)
            latest = run_store.load_run("demo")

            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["errors"][0]["category"], "run_store")
            self.assertIn("late-change", latest["output_refs"])
            self.assertEqual(latest["resource_cleanup"]["status"], "failed")

    def test_cleanup_audit_fences_normal_run_mutations(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(store)
            expected = run_store.create_run("demo", directory=directory, server_url="http://opencode.example")
            record = {
                "status": "done",
                "completed": {name: [] for name in ("sessions", "worktrees", "branches", "project_metadata", "logs")},
                "errors": [],
            }
            begin_cleanup_audit(run_store, "demo", expected, record)

            with self.assertRaisesRegex(RunStoreError, "cleanup in progress") as raised:
                run_store.upsert_worker("demo", "late-worker", role="build")

            self.assertEqual(raised.exception.kind, "conflict")

    def test_cleanup_lease_rejects_a_second_cleanup_process(self):
        with tempfile.TemporaryDirectory() as store:
            first = RunStore(store)
            second = RunStore(store)

            with first.cleanup_lease("demo"):
                with self.assertRaisesRegex(RunStoreError, "already has a cleanup process") as raised:
                    with second.cleanup_lease("demo"):
                        self.fail("second cleanup lease unexpectedly acquired")

            self.assertEqual(raised.exception.kind, "conflict")


def _cleanup_server(session_ids, *, delete_failure=False):
    server = FakeOpenCodeServer()
    sessions = set(session_ids)
    server.json("GET", "/global/health", {"healthy": True, "version": "test"})
    server.json("GET", "/doc", {"paths": {"/api/session": {"get": {}}, "/api/session/{sessionID}": {"get": {}, "delete": {}}}})

    def get_session(handler, request):
        session_id = request.params["sessionID"]
        if session_id in sessions:
            handler._write_json({"id": session_id})
        else:
            handler._write_json({"error": "missing"}, status=404)

    def delete_session(handler, request):
        session_id = request.params["sessionID"]
        if delete_failure:
            handler._write_json({"error": "delete failed"}, status=500)
            return
        sessions.discard(session_id)
        handler._write_json({"id": session_id, "deleted": True})

    server.route("GET", "/api/session/{sessionID}", get_session)
    server.route("DELETE", "/api/session/{sessionID}", delete_session)
    return server


def _project_cleanup_paths():
    return {
        "/project": {"get": {}},
        "/project/{projectID}/directories": {"get": {}},
        "/experimental/workspace": {"get": {}},
        "/experimental/workspace/{workspaceID}": {"delete": {}},
        "/experimental/project/{projectID}/copy/refresh": {"post": {}},
    }


def _create_repository_with_worktree(repository, worktree, *, branch):
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "OCS Test")
    _git(repository, "config", "user.email", "ocs@example.test")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")
    _git(repository, "worktree", "add", "-b", branch, str(worktree), "main")


def _git(repository, *args):
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
