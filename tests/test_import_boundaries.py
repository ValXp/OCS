import importlib
import sys
import unittest
from contextlib import contextmanager


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


if __name__ == "__main__":
    unittest.main()
