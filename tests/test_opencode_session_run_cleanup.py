import json
import subprocess
import tempfile
import unittest
from pathlib import Path

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
