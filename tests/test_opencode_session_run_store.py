import tempfile
import threading
import unittest

from opencode_session.run_store import RunStore


class RunStoreConcurrencyTest(unittest.TestCase):
    def test_save_run_preserves_concurrent_worker_update_after_stale_load(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            first = RunStore(store)
            first.create_run("demo", directory=directory, server_url="http://opencode.example")
            stale_run = first.load_run("demo")

            second = RunStore(store)
            second.upsert_worker("demo", "planner", role="plan", status="active")

            stale_run["status"] = "active"
            first.save_run(stale_run)

            run = RunStore(store).load_run("demo")

        self.assertEqual(run["status"], "active")
        self.assertIn("planner", run["workers"])
        self.assertEqual(run["workers"]["planner"]["role"], "plan")

    def test_concurrent_worker_upserts_preserve_both_workers(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = _InterleavedRunStore(store)
            run_store.create_run("demo", directory=directory, server_url="http://opencode.example")

            errors = []

            def upsert(worker_id, role):
                try:
                    run_store.upsert_worker("demo", worker_id, role=role, status="active")
                except Exception as error:  # pragma: no cover - assertion reports the unexpected exception
                    errors.append(error)

            run_store.interleave_loads = True
            threads = [
                threading.Thread(target=upsert, args=("planner", "plan")),
                threading.Thread(target=upsert, args=("builder", "build")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            run_store.interleave_loads = False

            run = RunStore(store).load_run("demo")

        self.assertEqual(errors, [])
        self.assertEqual(set(run["workers"]), {"planner", "builder"})


class _InterleavedRunStore(RunStore):
    def __init__(self, root):
        super().__init__(root)
        self.interleave_loads = False
        self._load_barrier = threading.Barrier(2)
        self._save_lock = threading.Lock()

    def load_run(self, name):
        run = super().load_run(name)
        if self.interleave_loads and threading.current_thread() is not threading.main_thread():
            self._load_barrier.wait(timeout=5)
        return run

    def save_run(self, run):
        with self._save_lock:
            super().save_run(run)


if __name__ == "__main__":
    unittest.main()
