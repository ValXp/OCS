import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opencode_session.run_branch_transaction import BranchDeleteTransaction
from opencode_session.run_cleanup_local import (
    preflight_branches,
    preflight_worktrees,
    remove_branches,
    remove_worktrees,
)
from opencode_session.run_resources import register_run_resources
from opencode_session.run_store import RunStore

try:
    from tests.test_opencode_session_run_cleanup import _create_repository_with_worktree, _git
except ModuleNotFoundError:
    from test_opencode_session_run_cleanup import _create_repository_with_worktree, _git


class AtomicBranchCleanupTest(unittest.TestCase):
    def test_same_tip_recreation_after_atomic_delete_is_not_deleted_again(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store_root:
            repository = Path(root) / "repo"
            worktree = Path(root) / "owned-worktree"
            _create_repository_with_worktree(repository, worktree, branch="owned-branch")
            store = RunStore(store_root)
            store.create_run("demo", directory=repository, server_url="http://opencode.example")
            record = register_run_resources(
                store,
                "demo",
                "worker",
                worktree_paths=[worktree],
            )["resources"]["worktrees"][0]
            errors = []
            blocked_worktrees = preflight_worktrees([record], force=False, errors=errors)
            blocked_branches, tips = preflight_branches(
                [record],
                force=False,
                errors=errors,
                blocked_worktrees=blocked_worktrees,
                selected_worktrees=[record["path"]],
            )
            expected_tip = tips[(record["git_dir"], record["branch"])]
            result = {
                "completed": {name: [] for name in ("sessions", "worktrees", "branches", "project_metadata", "logs")},
                "errors": [],
            }
            original_commit = BranchDeleteTransaction.commit

            def commit_then_recreate(transaction):
                error = original_commit(transaction)
                if error is None:
                    _git(repository, "update-ref", "refs/heads/owned-branch", expected_tip)
                return error

            with patch.object(BranchDeleteTransaction, "commit", new=commit_then_recreate):
                remove_worktrees(
                    [record],
                    blocked_worktrees,
                    False,
                    result,
                    branch_records=[record],
                    blocked_branches=blocked_branches,
                    expected_branch_tips=tips,
                )
                remove_branches([record], blocked_branches, tips, False, result)

            self.assertEqual(result["errors"], [])
            self.assertFalse(worktree.exists())
            self.assertIn("owned-branch", _git(repository, "branch", "--format=%(refname:short)").splitlines())
            self.assertEqual(len(result["completed"]["branches"]), 1)

    def test_worktree_disappearance_after_preflight_blocks_recreated_branch(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as store_root:
            repository = Path(root) / "repo"
            worktree = Path(root) / "owned-worktree"
            _create_repository_with_worktree(repository, worktree, branch="owned-branch")
            store = RunStore(store_root)
            store.create_run("demo", directory=repository, server_url="http://opencode.example")
            record = register_run_resources(
                store,
                "demo",
                "worker",
                worktree_paths=[worktree],
            )["resources"]["worktrees"][0]
            errors = []
            blocked_worktrees = preflight_worktrees([record], force=False, errors=errors)
            blocked_branches, tips = preflight_branches(
                [record],
                force=False,
                errors=errors,
                blocked_worktrees=blocked_worktrees,
                selected_worktrees=[record["path"]],
            )
            expected_tip = tips[(record["git_dir"], record["branch"])]
            _git(repository, "worktree", "remove", str(worktree))
            _git(repository, "branch", "-D", "owned-branch")
            _git(repository, "branch", "owned-branch", expected_tip)
            result = {
                "completed": {name: [] for name in ("sessions", "worktrees", "branches", "project_metadata", "logs")},
                "errors": [],
            }

            remove_worktrees(
                [record],
                blocked_worktrees,
                False,
                result,
                branch_records=[record],
                blocked_branches=blocked_branches,
                expected_branch_tips=tips,
            )
            remove_branches([record], blocked_branches, tips, False, result)

            self.assertTrue(result["errors"])
            self.assertEqual(result["completed"]["branches"], [])
            self.assertIn("owned-branch", _git(repository, "branch", "--format=%(refname:short)").splitlines())


if __name__ == "__main__":
    unittest.main()
