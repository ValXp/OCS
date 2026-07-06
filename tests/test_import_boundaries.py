import ast
import importlib
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path


class BlockedModuleFinder:
    def __init__(self, blocked_modules):
        self.blocked_modules = set(blocked_modules)

    def find_spec(self, fullname, path, target=None):
        if fullname in self.blocked_modules:
            raise AssertionError(f"unexpected import of {fullname}")
        return None


@contextmanager
def temporarily_unimported(*module_names):
    saved = {}
    for module_name in module_names:
        if module_name in sys.modules:
            saved[module_name] = sys.modules.pop(module_name)
    try:
        yield
    finally:
        for module_name in module_names:
            sys.modules.pop(module_name, None)
        sys.modules.update(saved)


class ImportBoundaryTest(unittest.TestCase):
    def test_api_transport_import_does_not_require_session_domain_modules(self):
        blocked = BlockedModuleFinder(
            {
                "opencode_session.api_domain",
                "opencode_session.schema_session_adapter",
            }
        )
        with temporarily_unimported(
            "opencode_session.api_domain",
            "opencode_session.api_transport",
            "opencode_session.schema_session_adapter",
        ):
            sys.meta_path.insert(0, blocked)
            try:
                api_transport = importlib.import_module("opencode_session.api_transport")
            finally:
                sys.meta_path.remove(blocked)

        self.assertTrue(hasattr(api_transport, "OpenCodeApiTransport"))

    def test_run_record_import_does_not_require_execution_api_or_session_parsing(self):
        blocked = BlockedModuleFinder(
            {
                "opencode_session.api_client",
                "opencode_session.schema_session_adapter",
            }
        )
        with temporarily_unimported(
            "opencode_session.api_client",
            "opencode_session.schema_session_adapter",
            "opencode_session.run_record",
            "opencode_session.worker_state",
        ):
            sys.meta_path.insert(0, blocked)
            try:
                run_record = importlib.import_module("opencode_session.run_record")
            finally:
                sys.meta_path.remove(blocked)

        self.assertTrue(hasattr(run_record, "normalize_run"))

    def test_worker_state_is_canonical_worker_state_surface(self):
        with temporarily_unimported(
            "opencode_session.worker_lifecycle_reducer",
            "opencode_session.worker_state",
        ):
            import opencode_session.worker_lifecycle_reducer as worker_lifecycle_reducer
            import opencode_session.worker_state as worker_state

            self.assertIs(worker_lifecycle_reducer.WorkerTransition, worker_state.WorkerTransition)

    def test_worker_state_does_not_own_cli_exit_policy(self):
        blocked = BlockedModuleFinder({"opencode_session.cli_policy"})
        with temporarily_unimported(
            "opencode_session.cli_policy",
            "opencode_session.worker_state",
        ):
            sys.meta_path.insert(0, blocked)
            try:
                worker_state = importlib.import_module("opencode_session.worker_state")
            finally:
                sys.meta_path.remove(blocked)

        for name in (
            "EX_ABORTED",
            "EX_BLOCKED",
            "EX_PARTIAL",
            "EX_TIMEOUT",
            "EX_UNAVAILABLE",
            "EX_UNSUPPORTED",
            "WORKER_EXIT_CODE_BY_STATUS",
            "exit_code_for_run",
            "exit_code_for_status",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(worker_state, name))

        self.assertFalse(
            any(hasattr(metadata, "exit_code") for metadata in worker_state.WORKER_LIFECYCLE_METADATA.values())
        )

    def test_removed_worker_state_compatibility_modules_are_not_importable(self):
        with temporarily_unimported(
            "opencode_session.status_policy",
            "opencode_session.worker_lifecycle",
            "opencode_session.worker_snapshot_codec",
        ):
            for module_name in (
                "opencode_session.status_policy",
                "opencode_session.worker_lifecycle",
                "opencode_session.worker_snapshot_codec",
            ):
                with self.subTest(module=module_name):
                    with self.assertRaises(ModuleNotFoundError):
                        importlib.import_module(module_name)

    def test_worker_state_imports_use_canonical_surface(self):
        package_dir = Path(__file__).resolve().parents[1] / "opencode_session"
        forbidden_imports = (
            "from opencode_session.worker_lifecycle import",
            "from opencode_session.worker_snapshot_codec import",
            "from opencode_session.status_policy import",
            "from opencode_session.worker_lifecycle_reducer import WorkerTransition",
        )
        offenders = []
        for path in sorted(package_dir.glob("*.py")):
            source = path.read_text()
            for forbidden_import in forbidden_imports:
                if forbidden_import in source:
                    offenders.append(f"{path.name}: {forbidden_import}")

        reducer_source = (package_dir / "worker_lifecycle_reducer.py").read_text()

        self.assertEqual([], offenders)
        self.assertFalse((package_dir / "status_policy.py").exists())
        self.assertFalse((package_dir / "worker_lifecycle.py").exists())
        self.assertFalse((package_dir / "worker_snapshot_codec.py").exists())
        self.assertIn("from opencode_session.worker_state import", reducer_source)

    def test_python_39_claim_avoids_pep604_type_union_annotations(self):
        project_root = Path(__file__).resolve().parents[1]
        package_dir = project_root / "opencode_session"
        pyproject_source = (project_root / "pyproject.toml").read_text()
        if 'requires-python = ">=3.9"' not in pyproject_source:
            self.skipTest("Python 3.9 support is not claimed")

        offenders = []
        for path in sorted(package_dir.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                annotations = []
                if isinstance(node, ast.AnnAssign):
                    annotations.append(node.annotation)
                elif isinstance(node, ast.arg) and node.annotation is not None:
                    annotations.append(node.annotation)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
                    annotations.append(node.returns)

                for annotation in annotations:
                    for child in ast.walk(annotation):
                        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr):
                            relative_path = path.relative_to(project_root)
                            offenders.append(f"{relative_path}:{child.lineno}")

        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
