import tempfile
import threading
import unittest

from opencode_session.run_persistence import persist_worker_update
from opencode_session.run_store import RunStore
from opencode_session.worker_state import refresh_run_summary


class RunStoreConcurrencyTest(unittest.TestCase):
    def test_loaded_run_mutation_is_not_persisted_without_update_run(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            first = RunStore(store)
            first.create_run("demo", directory=directory, server_url="http://opencode.example")
            stale_run = first.load_run("demo")

            RunStore(store).upsert_worker("demo", "planner", role="plan", status="active")
            stale_run["status"] = "active"
            run = RunStore(store).load_run("demo")

        self.assertEqual(run["status"], "queued")
        self.assertIn("planner", run["workers"])

    def test_update_run_preserves_concurrent_worker_update_after_stale_load(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            first = RunStore(store)
            first.create_run("demo", directory=directory, server_url="http://opencode.example")
            first.load_run("demo")

            RunStore(store).upsert_worker("demo", "planner", role="plan", status="active")

            def activate(latest_run):
                latest_run["status"] = "active"

            first.update_run("demo", activate)
            run = RunStore(store).load_run("demo")

        self.assertEqual(run["status"], "active")
        self.assertIn("planner", run["workers"])
        self.assertEqual(run["workers"]["planner"]["role"], "plan")

    def test_concurrent_worker_upserts_preserve_both_workers(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(store)
            run_store.create_run("demo", directory=directory, server_url="http://opencode.example")

            errors = []

            def upsert(worker_id, role):
                try:
                    run_store.upsert_worker("demo", worker_id, role=role, status="active")
                except Exception as error:  # pragma: no cover - assertion reports the unexpected exception
                    errors.append(error)

            threads = [
                threading.Thread(target=upsert, args=("planner", "plan")),
                threading.Thread(target=upsert, args=("builder", "build")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            run = RunStore(store).load_run("demo")

        self.assertEqual(errors, [])
        self.assertEqual(set(run["workers"]), {"planner", "builder"})

    def test_worker_patch_preserves_concurrent_prompt_id_on_other_worker(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(store)
            run = run_store.create_run("demo", directory=directory, server_url="http://opencode.example")
            run_store.upsert_worker("demo", "docs", role="write", prompt="Draft docs", status="active")
            run_store.upsert_worker("demo", "build", role="build", prompt="Build", status="active")
            run = run_store.load_run("demo")

            run_store.update_run(
                "demo",
                lambda latest_run: latest_run["workers"]["docs"]["prompt_ids"].append("prompt-docs"),
            )
            build_worker = run["workers"]["build"]
            build_worker["status"] = "done"
            build_worker["prompt_ids"] = ["prompt-build"]
            build_worker["result"] = {
                "session_id": "ses_build",
                "status": "done",
                "message_ids": {"user": "prompt-build", "assistant": "msg_build"},
            }
            build_worker["output_refs"] = ["assistant:msg_build"]
            persist_worker_update(
                run_store,
                run,
                build_worker,
                refresh_run_summary=refresh_run_summary,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            persisted = RunStore(store).load_run("demo")

        self.assertEqual(persisted["workers"]["docs"]["prompt_ids"], ["prompt-docs"])
        self.assertEqual(persisted["workers"]["build"]["status"], "done")
        self.assertEqual(persisted["output_refs"], ["build:msg_build"])

if __name__ == "__main__":
    unittest.main()
