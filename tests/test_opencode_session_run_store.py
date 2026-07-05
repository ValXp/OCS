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

    def test_conflicting_same_worker_saves_do_not_merge_fields_into_invalid_state(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            first = RunStore(store)
            first.create_run("demo", directory=directory, server_url="http://opencode.example")
            first.upsert_worker("demo", "builder", role="build", status="active")
            stale_run = first.load_run("demo")

            second = RunStore(store)
            current_run = second.load_run("demo")
            current_worker = current_run["workers"]["builder"]
            current_worker["status"] = "done"
            current_worker["prompt_ids"] = ["prompt-done"]
            current_worker["result"] = {
                "session_id": "ses_builder",
                "status": "done",
                "message_ids": {"user": "prompt-done", "assistant": "msg_done"},
            }
            second.save_run(current_run)

            stale_run["workers"]["builder"]["prompt_ids"] = ["prompt-stale"]
            first.save_run(stale_run)

            run = RunStore(store).load_run("demo")

        self.assertEqual(run["workers"]["builder"]["status"], "done")
        self.assertEqual(run["workers"]["builder"]["prompt_ids"], ["prompt-done"])
        self.assertEqual(run["workers"]["builder"]["result"]["message_ids"]["user"], "prompt-done")

    def test_stale_worker_done_save_preserves_concurrent_steer_prompt_id(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            first = RunStore(store)
            first.create_run("demo", directory=directory, server_url="http://opencode.example")
            first.upsert_worker("demo", "builder", role="build", status="active", prompt_ids=["prompt-start"])
            stale_run = first.load_run("demo")

            second = RunStore(store)
            steered_run = second.load_run("demo")
            steered_run["workers"]["builder"]["prompt_ids"].append("prompt-steer")
            second.save_run(steered_run)

            stale_worker = stale_run["workers"]["builder"]
            stale_worker["status"] = "done"
            stale_worker["result"] = {
                "session_id": "ses_builder",
                "status": "done",
                "message_ids": {"user": "prompt-start", "assistant": "msg_done"},
            }
            stale_worker["output_refs"] = ["assistant:msg_done"]
            first.save_run(stale_run)

            run = RunStore(store).load_run("demo")

        worker = run["workers"]["builder"]
        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["prompt_ids"], ["prompt-start", "prompt-steer"])
        self.assertEqual(worker["result"]["message_ids"]["assistant"], "msg_done")
        self.assertEqual(worker["output_refs"], ["assistant:msg_done"])


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
