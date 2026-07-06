import ast
import importlib
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path


_MISSING = object()


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
    saved_package_attrs = {}
    for module_name in module_names:
        if module_name in sys.modules:
            saved[module_name] = sys.modules.pop(module_name)
        if "." in module_name:
            parent_name, attr_name = module_name.rsplit(".", 1)
            parent = sys.modules.get(parent_name)
            if parent is not None:
                saved_package_attrs[module_name] = getattr(parent, attr_name, _MISSING)
                if hasattr(parent, attr_name):
                    delattr(parent, attr_name)
    try:
        yield
    finally:
        for module_name in module_names:
            sys.modules.pop(module_name, None)
        sys.modules.update(saved)
        for module_name, attr_value in saved_package_attrs.items():
            parent_name, attr_name = module_name.rsplit(".", 1)
            parent = sys.modules.get(parent_name)
            if parent is None:
                continue
            if attr_value is _MISSING:
                if hasattr(parent, attr_name):
                    delattr(parent, attr_name)
            else:
                setattr(parent, attr_name, attr_value)


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

    def test_api_client_does_not_redeclare_domain_transport_forwarders(self):
        from opencode_session.api_client import OpenCodeApiClient

        redundant_forwarders = (
            "get_json",
            "get_response",
            "post_json",
            "post_response",
            "delete_json",
            "delete_response",
        )

        declared = [name for name in redundant_forwarders if name in OpenCodeApiClient.__dict__]

        self.assertEqual([], declared)

    def test_api_error_imports_use_transport_owner(self):
        project_root = Path(__file__).resolve().parents[1]
        offenders = []
        for root in (project_root / "opencode_session", project_root / "tests"):
            for path in sorted(root.rglob("*.py")):
                tree = ast.parse(path.read_text(), filename=str(path))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ImportFrom) or node.module != "opencode_session.api_client":
                        continue
                    if any(alias.name == "OpenCodeApiError" for alias in node.names):
                        offenders.append(f"{path.relative_to(project_root)}:{node.lineno}")

        self.assertEqual([], offenders)

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

    def test_schema_common_hydrated_types_do_not_import_worker_state(self):
        blocked = BlockedModuleFinder({"opencode_session.worker_state"})
        with temporarily_unimported(
            "opencode_session.schema_common",
            "opencode_session.worker_state",
        ):
            sys.meta_path.insert(0, blocked)
            try:
                schema_common = importlib.import_module("opencode_session.schema_common")
            finally:
                sys.meta_path.remove(blocked)

        self.assertTrue(hasattr(schema_common, "HydratedRunRecord"))
        self.assertTrue(hasattr(schema_common, "HydratedWorker"))

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

    def test_worker_state_public_types_are_not_imported_from_reducer(self):
        project_root = Path(__file__).resolve().parents[1]
        package_dir = project_root / "opencode_session"
        public_worker_state_names = {
            "WorkerRecord",
            "WorkerTransition",
            "WorkerTransitionName",
            "WorkerTransitionResult",
        }
        offenders = []
        for path in sorted(package_dir.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.module != "opencode_session.worker_lifecycle_reducer":
                    continue
                leaked_names = sorted(alias.name for alias in node.names if alias.name in public_worker_state_names)
                if leaked_names:
                    offenders.append(f"{path.relative_to(project_root)}:{node.lineno}: {', '.join(leaked_names)}")

        self.assertEqual([], offenders)

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
