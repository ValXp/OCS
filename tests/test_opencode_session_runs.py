import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "ocs"


class RunStoreCliTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=REPO_ROOT,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_init_persists_named_run_and_compact_status_survives_restart(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            init = self.run_cli(
                "run",
                "--store",
                store,
                "init",
                "demo",
                "--directory",
                directory,
                "--server",
                "http://opencode.example",
            )
            status = self.run_cli("run", "--store", store, "status", "demo")

        expected = (
            f"run=demo status=queued dir={directory} server=http://opencode.example "
            "workers=0 queued=0 active=0 done=0 blocked=0 failed=0 aborted=0 timeout=0 "
            "retries=0 timeout_s=- blockers=- outputs=-\n"
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(init.stderr, "")
        self.assertEqual(init.stdout, expected)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stderr, "")
        self.assertEqual(status.stdout, expected)

    def test_worker_command_adds_and_updates_compact_worker_records(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            init = self.run_cli(
                "run",
                "--store",
                store,
                "init",
                "demo",
                "--directory",
                directory,
                "--server",
                "http://opencode.example",
            )
            add_worker = self.run_cli(
                "run",
                "--store",
                store,
                "worker",
                "demo",
                "builder",
                "--role",
                "build",
                "--session",
                "ses_builder",
                "--agent",
                "build",
                "--model",
                "openai/gpt-5.5",
                "--depends-on",
                "planner",
                "--depends-on",
                "qa",
                "--prompt-id",
                "prompt-123",
                "--status",
                "active",
                "--retry-count",
                "2",
                "--timeout-seconds",
                "600",
            )
            update_worker = self.run_cli(
                "run",
                "--store",
                store,
                "worker",
                "demo",
                "builder",
                "--output-ref",
                "file:summary.md",
            )
            status = self.run_cli("run", "--store", store, "status", "demo")

        expected = (
            f"run=demo status=queued dir={directory} server=http://opencode.example "
            "workers=1 queued=0 active=1 done=0 blocked=0 failed=0 aborted=0 timeout=0 "
            "retries=0 timeout_s=- blockers=- outputs=-\n"
            "worker=builder role=build status=active session=ses_builder agent=build "
            "model=openai/gpt-5.5 deps=planner,qa prompts=prompt-123 "
            "retries=2 timeout=600 blockers=- outputs=file:summary.md\n"
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(add_worker.returncode, 0, add_worker.stderr)
        self.assertEqual(update_worker.returncode, 0, update_worker.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stderr, "")
        self.assertEqual(status.stdout, expected)

    def test_worker_command_rejects_raw_lifecycle_state_flag(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            init = self.run_cli(
                "run",
                "--store",
                store,
                "init",
                "demo",
                "--directory",
                directory,
                "--server",
                "http://opencode.example",
            )
            worker = self.run_cli(
                "run",
                "--store",
                store,
                "worker",
                "demo",
                "builder",
                "--role",
                "build",
                "--lifecycle-state",
                "done_collect",
            )
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 2)
        self.assertEqual(worker.stdout, "")
        self.assertIn("unrecognized arguments: --lifecycle-state done_collect", worker.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["workers"], {})

    def test_worker_command_rejects_terminal_status_without_reducer_payload(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            init = self.run_cli(
                "run",
                "--store",
                store,
                "init",
                "demo",
                "--directory",
                directory,
                "--server",
                "http://opencode.example",
            )
            worker = self.run_cli(
                "run",
                "--store",
                store,
                "worker",
                "demo",
                "builder",
                "--role",
                "build",
                "--status",
                "done",
                "--output-ref",
                "file:summary.md",
            )
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 65)
        self.assertEqual(worker.stdout, "")
        self.assertIn("worker status 'done' cannot be set manually", worker.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["workers"], {})

    def test_worker_command_rejects_blocked_status_without_blockers(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            init = self.run_cli(
                "run",
                "--store",
                store,
                "init",
                "demo",
                "--directory",
                directory,
                "--server",
                "http://opencode.example",
            )
            worker = self.run_cli(
                "run",
                "--store",
                store,
                "worker",
                "demo",
                "builder",
                "--role",
                "build",
                "--status",
                "blocked",
            )
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 65)
        self.assertEqual(worker.stdout, "")
        self.assertIn("--status blocked requires at least one --blocker", worker.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["workers"], {})

    def test_worker_command_active_status_clears_stale_blockers(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            init = self.run_cli(
                "run",
                "--store",
                store,
                "init",
                "demo",
                "--directory",
                directory,
                "--server",
                "http://opencode.example",
            )
            blocked = self.run_cli(
                "run",
                "--store",
                store,
                "worker",
                "demo",
                "builder",
                "--role",
                "build",
                "--status",
                "blocked",
                "--blocker",
                "dependency:planner",
            )
            active = self.run_cli(
                "run",
                "--store",
                store,
                "worker",
                "demo",
                "builder",
                "--status",
                "active",
            )
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(active.returncode, 0, active.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        worker = json.loads(status.stdout)["workers"]["builder"]
        self.assertEqual(worker["status"], "active")
        self.assertEqual(worker["lifecycle_state"], "active_wait")
        self.assertEqual(worker["blockers"], [])

    def test_init_existing_run_fails_without_destroying_workers(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            init = self.run_cli(
                "run",
                "--store",
                store,
                "init",
                "demo",
                "--directory",
                directory,
                "--server",
                "http://opencode.example",
            )
            worker = self.run_cli(
                "run",
                "--store",
                store,
                "worker",
                "demo",
                "build",
                "--role",
                "build",
                "--prompt",
                "Build",
            )
            duplicate = self.run_cli(
                "run",
                "--store",
                store,
                "init",
                "demo",
                "--directory",
                "/tmp/other",
                "--server",
                "http://other.example",
            )
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(duplicate.returncode, 65)
        self.assertEqual(duplicate.stdout, "")
        self.assertIn("run 'demo' already exists", duplicate.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["directory"], directory)
        self.assertEqual(payload["server_url"], "http://opencode.example")
        self.assertEqual(set(payload["workers"]), {"build"})

    def test_status_with_multiple_workers_prints_compact_worker_table(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            init = self.run_cli(
                "run",
                "--store",
                store,
                "init",
                "demo",
                "--directory",
                directory,
                "--server",
                "http://opencode.example",
            )
            builder = self.run_cli(
                "run",
                "--store",
                store,
                "worker",
                "demo",
                "builder",
                "--role",
                "build",
                "--session",
                "ses_builder",
                "--status",
                "active",
            )
            reviewer = self.run_cli(
                "run",
                "--store",
                store,
                "worker",
                "demo",
                "reviewer",
                "--role",
                "qa",
                "--status",
                "blocked",
                "--blocker",
                "#8",
            )
            status = self.run_cli("run", "--store", store, "status", "demo")

        expected = (
            f"run=demo status=queued dir={directory} server=http://opencode.example "
            "workers=2 queued=0 active=1 done=0 blocked=1 failed=0 aborted=0 timeout=0 "
            "retries=0 timeout_s=- blockers=- outputs=-\n"
            "worker\trole\tstatus\tsession\tagent\tmodel\tdeps\tprompts\tretries\ttimeout\tblockers\toutputs\n"
            "builder\tbuild\tactive\tses_builder\t-\t-\t-\t-\t0\t-\t-\t-\n"
            "reviewer\tqa\tblocked\t-\t-\t-\t-\t-\t0\t-\t#8\t-\n"
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(builder.returncode, 0, builder.stderr)
        self.assertEqual(reviewer.returncode, 0, reviewer.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stderr, "")
        self.assertEqual(status.stdout, expected)

    def test_status_json_outputs_run_and_worker_metadata(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            init = self.run_cli(
                "run",
                "--store",
                store,
                "init",
                "demo",
                "--directory",
                directory,
                "--server",
                "http://opencode.example",
            )
            worker = self.run_cli(
                "run",
                "--store",
                store,
                "worker",
                "demo",
                "builder",
                "--role",
                "build",
                "--session",
                "ses_builder",
                "--agent",
                "build",
                "--model",
                "openai/gpt-5.5",
                "--depends-on",
                "planner",
                "--prompt-id",
                "prompt-123",
                "--status",
                "blocked",
                "--retry-count",
                "1",
                "--timeout-seconds",
                "300",
                "--blocker",
                "#8",
                "--output-ref",
                "file:worker-summary.md",
            )
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stderr, "")
        payload = json.loads(status.stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["name"], "demo")
        self.assertEqual(payload["run_id"], "demo")
        self.assertEqual(payload["directory"], directory)
        self.assertEqual(payload["server_url"], "http://opencode.example")
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["retry_count"], 0)
        self.assertIsNone(payload["timeout_seconds"])
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["output_refs"], [])
        self.assertIn("created_at", payload)
        self.assertIn("updated_at", payload)
        self.assertEqual(
            payload["workers"],
            {
                "builder": {
                    "id": "builder",
                    "role": "build",
                    "session_id": "ses_builder",
                    "agent": "build",
                    "model": "openai/gpt-5.5",
                    "dependencies": ["planner"],
                    "prompt_ids": ["prompt-123"],
                    "status": "blocked",
                    "retry_count": 1,
                    "retry_limit": 0,
                    "retryable_failures": [],
                    "timeout_seconds": 300,
                    "timeout_policy": "timeout",
                    "timeout_started_at": None,
                    "timed_out_at": None,
                    "lifecycle_state": "blocked_dependency",
                    "failure_category": None,
                    "failure_reason": None,
                    "last_failure_category": None,
                    "last_failure_reason": None,
                    "next_eligible_action": "resolve_blocker",
                    "blockers": ["#8"],
                    "output_refs": ["file:worker-summary.md"],
                }
            },
        )
        self.assertNotIn("transcript", payload)

    def test_worker_timeout_seconds_accepts_positive_fractional_seconds(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            init = self.run_cli(
                "run",
                "--store",
                store,
                "init",
                "demo",
                "--directory",
                directory,
                "--server",
                "http://opencode.example",
            )
            worker = self.run_cli(
                "run",
                "--store",
                store,
                "worker",
                "demo",
                "builder",
                "--role",
                "build",
                "--timeout-seconds",
                "0.05",
            )
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["workers"]["builder"]["timeout_seconds"], 0.05)

    def test_worker_timeout_seconds_rejects_non_positive_values(self):
        for timeout in ("0", "-1"):
            with self.subTest(timeout=timeout):
                with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
                    init = self.run_cli(
                        "run",
                        "--store",
                        store,
                        "init",
                        "demo",
                        "--directory",
                        directory,
                        "--server",
                        "http://opencode.example",
                    )
                    worker = self.run_cli(
                        "run",
                        "--store",
                        store,
                        "worker",
                        "demo",
                        "builder",
                        "--role",
                        "build",
                        "--timeout-seconds",
                        timeout,
                    )
                    status = self.run_cli("run", "--store", store, "status", "demo", "--json")

                self.assertEqual(init.returncode, 0, init.stderr)
                self.assertEqual(worker.returncode, 2)
                self.assertEqual(worker.stdout, "")
                self.assertIn("argument --timeout-seconds: must be greater than zero", worker.stderr)
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertEqual(json.loads(status.stdout)["workers"], {})

    def test_status_json_defaults_legacy_run_records(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            store_path = Path(store)
            (store_path / "legacy.json").write_text(
                json.dumps({"directory": directory, "workers": {"builder": {"role": "build"}}}),
                encoding="utf-8",
            )
            status = self.run_cli("run", "--store", store, "status", "legacy", "--json")

        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stderr, "")
        payload = json.loads(status.stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["name"], "legacy")
        self.assertEqual(payload["run_id"], "legacy")
        self.assertEqual(payload["directory"], directory)
        self.assertEqual(payload["server_url"], "http://127.0.0.1:4096")
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["retry_count"], 0)
        self.assertIsNone(payload["timeout_seconds"])
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["output_refs"], [])
        self.assertEqual(
            payload["workers"],
            {
                "builder": {
                    "id": "builder",
                    "role": "build",
                    "session_id": None,
                    "agent": None,
                    "model": None,
                    "dependencies": [],
                    "prompt_ids": [],
                    "status": "queued",
                    "retry_count": 0,
                    "retry_limit": 0,
                    "retryable_failures": [],
                    "timeout_seconds": None,
                    "timeout_policy": "timeout",
                    "timeout_started_at": None,
                    "timed_out_at": None,
                    "lifecycle_state": "queued",
                    "failure_category": None,
                    "failure_reason": None,
                    "last_failure_category": None,
                    "last_failure_reason": None,
                    "next_eligible_action": "start",
                    "blockers": [],
                    "output_refs": [],
                }
            },
        )

    def test_status_reports_missing_and_corrupted_run_records_clearly(self):
        with tempfile.TemporaryDirectory() as store:
            missing = self.run_cli("run", "--store", store, "status", "missing")
            Path(store, "broken.json").write_text(
                json.dumps({"name": "broken", "workers": ["not", "an", "object"]}),
                encoding="utf-8",
            )
            corrupted = self.run_cli("run", "--store", store, "status", "broken")

        self.assertEqual(missing.returncode, 66)
        self.assertEqual(missing.stdout, "")
        self.assertIn("ocs: run 'missing' not found", missing.stderr)
        self.assertIn(store, missing.stderr)
        self.assertEqual(corrupted.returncode, 65)
        self.assertEqual(corrupted.stdout, "")
        self.assertIn("ocs: run record for 'broken' is corrupted", corrupted.stderr)
        self.assertIn("workers must be an object", corrupted.stderr)


if __name__ == "__main__":
    unittest.main()
