import json
import tempfile
import threading
import unittest
from pathlib import Path

from opencode_session.run_persistence import persist_worker_snapshot_update
from opencode_session.run_store import RunStore, RunStoreError
from opencode_session.worker_state import apply_worker_transition_to_worker, mark_worker_aborted, refresh_run_summary

try:
    from tests.worker_state_scenarios import assert_worker_outcome
except ModuleNotFoundError:
    from worker_state_scenarios import assert_worker_outcome


class RunStoreConcurrencyTest(unittest.TestCase):
    def test_create_run_fails_when_run_already_exists(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(store)
            run_store.create_run("demo", directory=directory, server_url="http://opencode.example")
            run_store.upsert_worker("demo", "build", role="build", prompt="Build")

            with self.assertRaisesRegex(RunStoreError, "run 'demo' already exists"):
                run_store.create_run("demo", directory="/tmp/other", server_url="http://other.example")

            persisted = RunStore(store).load_run("demo")

        self.assertEqual(persisted["directory"], directory)
        self.assertEqual(persisted["server_url"], "http://opencode.example")
        self.assertEqual(set(persisted["workers"]), {"build"})

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

    def test_store_persists_canonical_worker_lifecycle_without_public_state(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(store)
            run_store.create_run("demo", directory=directory, server_url="http://opencode.example")
            run_store.upsert_worker("demo", "planner", role="plan", status="active")

            loaded = run_store.load_run("demo")
            stored = json.loads((Path(store) / "demo.json").read_text(encoding="utf-8"))

        assert_worker_outcome(
            self,
            loaded["workers"]["planner"],
            status="active",
            action="wait",
            lifecycle="active_wait",
        )
        self.assertEqual(stored["workers"]["planner"]["lifecycle_state"], "active_wait")
        self.assertNotIn("status", stored["workers"]["planner"])
        self.assertNotIn("next_eligible_action", stored["workers"]["planner"])

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

    def test_worker_snapshot_persistence_returns_new_run_without_mutating_loaded_run(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(store)
            run_store.create_run("demo", directory=directory, server_url="http://opencode.example")
            run_store.upsert_worker("demo", "build", role="build", prompt="Build", status="active")
            run = run_store.load_run("demo")
            build_worker = dict(run["workers"]["build"])
            build_worker["status"] = "done"
            build_worker["result"] = {
                "session_id": "ses_build",
                "status": "done",
                "message_ids": {"user": "prompt-build", "assistant": "msg_build"},
            }
            build_worker["output_refs"] = ["assistant:msg_build"]

            persisted = persist_worker_snapshot_update(
                run_store,
                run,
                build_worker,
                refresh_run_summary=refresh_run_summary,
                now=lambda: "2026-07-03T00:00:00Z",
            ).run

        self.assertEqual(run["workers"]["build"]["status"], "active")
        self.assertNotIn("result", run["workers"]["build"])
        self.assertEqual(persisted["workers"]["build"]["status"], "done")
        self.assertEqual(persisted["output_refs"], ["build:msg_build"])

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
            persist_worker_snapshot_update(
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

    def test_worker_patch_preserves_concurrent_prompt_id_on_same_worker(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(store)
            run_store.create_run("demo", directory=directory, server_url="http://opencode.example")
            run_store.upsert_worker("demo", "build", role="build", prompt="Build", status="active")
            run = run_store.load_run("demo")

            run_store.update_run(
                "demo",
                lambda latest_run: latest_run["workers"]["build"]["prompt_ids"].append("prompt-steer"),
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
            persist_worker_snapshot_update(
                run_store,
                run,
                build_worker,
                refresh_run_summary=refresh_run_summary,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            persisted = RunStore(store).load_run("demo")

        self.assertEqual(persisted["workers"]["build"]["prompt_ids"], ["prompt-steer", "prompt-build"])
        self.assertEqual(persisted["workers"]["build"]["status"], "done")

    def test_worker_patch_preserves_concurrent_worker_configuration_edits_on_same_worker(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(store)
            run_store.create_run("demo", directory=directory, server_url="http://opencode.example")
            run_store.upsert_worker("demo", "build", role="build", prompt="Build", status="active")
            run = run_store.load_run("demo")

            run_store.upsert_worker(
                "demo",
                "build",
                role="build",
                prompt="Build with new instructions",
                dependencies=["docs"],
                retry_limit=3,
                retryable_failures=["api"],
                timeout_seconds=45,
                timeout_policy="blocked",
                session_id="ses_user",
                agent="plan",
                model="openai/gpt-5.5",
            )
            build_worker = run["workers"]["build"]
            build_worker["status"] = "done"
            build_worker["session_id"] = "ses_created_from_stale_snapshot"
            build_worker["prompt_ids"] = ["prompt-build"]
            build_worker["result"] = {
                "session_id": "ses_created_from_stale_snapshot",
                "status": "done",
                "message_ids": {"user": "prompt-build", "assistant": "msg_build"},
            }
            build_worker["output_refs"] = ["assistant:msg_build"]
            persist_worker_snapshot_update(
                run_store,
                run,
                build_worker,
                refresh_run_summary=refresh_run_summary,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            persisted = RunStore(store).load_run("demo")

        build = persisted["workers"]["build"]
        self.assertEqual(build["status"], "done")
        self.assertEqual(build["prompt"], "Build with new instructions")
        self.assertEqual(build["dependencies"], ["docs"])
        self.assertEqual(build["retry_limit"], 3)
        self.assertEqual(build["retryable_failures"], ["api"])
        self.assertEqual(build["timeout_seconds"], 45)
        self.assertEqual(build["timeout_policy"], "blocked")
        self.assertEqual(build["session_id"], "ses_user")
        self.assertEqual(build["agent"], "plan")
        self.assertEqual(build["model"], "openai/gpt-5.5")
        self.assertEqual(build["prompt_ids"], ["prompt-build"])
        self.assertEqual(build["output_refs"], ["assistant:msg_build"])
        self.assertEqual(persisted["output_refs"], ["build:msg_build"])

    def test_worker_patch_does_not_overwrite_concurrent_abort(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(store)
            run_store.create_run("demo", directory=directory, server_url="http://opencode.example")
            run_store.upsert_worker("demo", "build", role="build", prompt="Build", status="active", session_id="ses_build")
            run = run_store.load_run("demo")

            def abort_build(latest_run):
                latest_worker = latest_run["workers"]["build"]
                apply_worker_transition_to_worker(
                    latest_worker,
                    mark_worker_aborted(
                        latest_worker,
                        {"session_id": "ses_build", "accepted": True, "raw": {"ok": True}},
                    ),
                )

            run_store.update_run("demo", abort_build)
            build_worker = run["workers"]["build"]
            build_worker["status"] = "done"
            build_worker["result"] = {
                "session_id": "ses_build",
                "status": "done",
                "message_ids": {"user": "prompt-build", "assistant": "msg_build"},
            }
            build_worker["output_refs"] = ["assistant:msg_build"]
            persist_worker_snapshot_update(
                run_store,
                run,
                build_worker,
                refresh_run_summary=refresh_run_summary,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            persisted = RunStore(store).load_run("demo")

        build = persisted["workers"]["build"]
        self.assertEqual(build["status"], "aborted")
        self.assertEqual(build["abort"], {"session_id": "ses_build", "accepted": True, "raw": {"ok": True}})
        self.assertNotIn("result", build)
        self.assertEqual(persisted["status"], "aborted")
        self.assertEqual(persisted["output_refs"], [])

if __name__ == "__main__":
    unittest.main()
