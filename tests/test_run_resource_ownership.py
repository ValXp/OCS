import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from opencode_session.run_resources import (
    RunResourceError,
    register_run_resources,
    verify_owned_log,
    verify_owned_worktree,
)
from opencode_session.run_store import RunStore, RunStoreError


class RunResourceManifestTest(unittest.TestCase):
    def test_load_rejects_malformed_resource_record_with_run_store_error(self):
        with tempfile.TemporaryDirectory() as root:
            store = _new_store(root)
            _replace_manifest(
                root,
                {
                    "worktrees": [],
                    "logs": [{"path": "/tmp/incomplete.log"}],
                    "project_copies": [],
                },
            )

            with self.assertRaises(RunStoreError) as caught:
                store.load_run("demo")

        self.assertIn("run record for 'demo' is corrupted", str(caught.exception))
        self.assertIn("resources.logs[0] is missing fields", str(caught.exception))

    def test_load_rejects_non_object_record_instead_of_silently_dropping_it(self):
        with tempfile.TemporaryDirectory() as root:
            store = _new_store(root)
            _replace_manifest(
                root,
                {"worktrees": [], "logs": ["not-a-record"], "project_copies": []},
            )

            with self.assertRaisesRegex(RunStoreError, r"resources\.logs\[0\] must be an object"):
                store.load_run("demo")

    def test_load_rejects_root_owned_path(self):
        with tempfile.TemporaryDirectory() as root:
            store = _new_store(root)
            record = _log_record_stub("/")
            _replace_manifest(
                root,
                {"worktrees": [], "logs": [record], "project_copies": []},
            )

            with self.assertRaisesRegex(RunStoreError, "cannot be a filesystem root"):
                store.load_run("demo")

    def test_load_rejects_null_and_incomplete_manifests(self):
        for manifest, expected in (
            (None, "resources must be an object"),
            ({"logs": []}, "resources is missing fields"),
        ):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as root:
                store = _new_store(root)
                _replace_manifest(root, manifest)
                with self.assertRaisesRegex(RunStoreError, expected):
                    store.load_run("demo")


class OwnedLogIdentityTest(unittest.TestCase):
    def test_registration_rejects_blank_relative_root_and_missing_paths(self):
        with tempfile.TemporaryDirectory() as root:
            store = _new_store(root)
            for value, expected in (
                ("", "cannot be blank"),
                ("relative.log", "must be absolute"),
                ("/", "cannot be a filesystem root"),
                (str(Path(root) / "missing.log"), "cannot inspect owned log"),
            ):
                with self.subTest(value=value), self.assertRaisesRegex(RunResourceError, expected):
                    register_run_resources(store, "demo", "worker", log_paths=[value])

    def test_registration_records_identity_and_verifier_detects_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "worker.log"
            log.write_text("original", encoding="utf-8")
            store = _new_store(root)
            run = register_run_resources(store, "demo", "worker", log_paths=[log])
            record = run["resources"]["logs"][0]

            self.assertIsNone(verify_owned_log(record))
            self.assertEqual(record["resource_type"], "file")
            original = Path(root) / "original.log"
            log.rename(original)
            log.write_text("replacement", encoding="utf-8")

            self.assertIn("identity or type has changed", verify_owned_log(record))

    def test_verifier_detects_replaced_ancestor_directory(self):
        with tempfile.TemporaryDirectory() as root:
            parent = Path(root) / "logs"
            parent.mkdir()
            log = parent / "worker.log"
            log.write_text("original", encoding="utf-8")
            store = _new_store(root)
            record = register_run_resources(
                store, "demo", "worker", log_paths=[log]
            )["resources"]["logs"][0]

            original_parent = Path(root) / "original-logs"
            parent.rename(original_parent)
            parent.mkdir()
            log.write_text("replacement", encoding="utf-8")

            self.assertIn("parent directory identity has changed", verify_owned_log(record))

    def test_broken_symlink_is_a_valid_exact_log_resource(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "worker.log"
            log.symlink_to(Path(root) / "missing-target")
            store = _new_store(root)
            record = register_run_resources(
                store, "demo", "worker", log_paths=[log]
            )["resources"]["logs"][0]

            self.assertEqual(record["resource_type"], "symlink")
            self.assertIsNone(verify_owned_log(record))


class OwnedWorktreeIdentityTest(unittest.TestCase):
    def test_verifier_detects_linked_git_directory_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            repository, worktree = _repository_with_worktree(root)
            store = _new_store(root)
            record = register_run_resources(
                store, "demo", "worker", worktree_paths=[worktree]
            )["resources"]["worktrees"][0]

            self.assertIsNone(verify_owned_worktree(record))
            linked_git_dir = Path(record["linked_git_dir"])
            original_git_dir = linked_git_dir.with_name(linked_git_dir.name + "-original")
            linked_git_dir.rename(original_git_dir)
            shutil.copytree(original_git_dir, linked_git_dir)

            self.assertIn("identity has changed", verify_owned_worktree(record))
            self.assertTrue(repository.exists())

    def test_verifier_detects_branch_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            _, worktree = _repository_with_worktree(root)
            store = _new_store(root)
            record = register_run_resources(
                store, "demo", "worker", worktree_paths=[worktree]
            )["resources"]["worktrees"][0]

            _git(worktree, "switch", "-c", "replacement-branch")

            self.assertIn("branch changed", verify_owned_worktree(record))

    def test_registration_rejects_detached_worktree(self):
        with tempfile.TemporaryDirectory() as root:
            _, worktree = _repository_with_worktree(root)
            _git(worktree, "checkout", "--detach")
            store = _new_store(root)

            with self.assertRaisesRegex(RunResourceError, "must have an attached branch"):
                register_run_resources(store, "demo", "worker", worktree_paths=[worktree])


def _new_store(root):
    store = RunStore(Path(root) / "runs")
    store.create_run("demo", directory=root, server_url="http://opencode.example")
    return store


def _replace_manifest(root, manifest):
    path = Path(root) / "runs" / "demo.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["resources"] = manifest
    path.write_text(json.dumps(payload), encoding="utf-8")


def _log_record_stub(path):
    return {
        "path": path,
        "parent_realpath": "/",
        "device": 1,
        "inode": 1,
        "resource_type": "file",
        "parent_device": 1,
        "parent_inode": 1,
        "worker_id": "worker",
    }


def _repository_with_worktree(root):
    repository = Path(root) / "repository"
    worktree = Path(root) / "owned-worktree"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "OCS Test")
    _git(repository, "config", "user.email", "ocs@example.test")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")
    _git(repository, "worktree", "add", "-b", "owned-branch", str(worktree), "main")
    return repository, worktree


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
