import unittest

from opencode_session.worker_storage_adapter import (
    canonicalize_legacy_worker_record,
    hydrate_worker_record,
    normalize_worker_snapshot_for_storage,
    worker_snapshot_transition,
)
from opencode_session.worker_state import (
    aggregate_run_status,
    WorkerRecord,
    WorkerTransition,
    WorkerTransitionError,
    WorkerTransitionName,
    apply_worker_transition,
    apply_worker_transition_to_worker,
    deserialize_worker_record,
    is_failed_dependency_status,
    is_executable_worker,
    is_terminal_status,
    is_worker_record,
    mark_worker_active,
    next_eligible_action,
    next_eligible_worker_action,
    normalize_worker,
    normalize_worker_snapshot,
    refresh_run_summary,
    require_internal_worker,
    schedule_worker_retry,
    serialize_worker_snapshot,
    status_priority,
    worker_has_field,
    worker_has_prompt,
    worker_lifecycle_state,
    worker_failed_lifecycle_state,
    worker_lifecycle_state_for_public_state,
    worker_lifecycle_state_for_status_alias,
    worker_field,
    worker_output_field,
    worker_retry_available,
    worker_timeout_reason,
    worker_timeout_lifecycle_state,
    worker_record_for_mutation,
    worker_transition_is_legal,
)

try:
    from tests.worker_state_scenarios import assert_worker_outcome
except ModuleNotFoundError:
    from worker_state_scenarios import assert_worker_outcome


class WorkerStateContractTest(unittest.TestCase):
    def test_lifecycle_public_state_contracts(self):
        cases = (
            ("queued", "queued", "start", False, True),
            ("active_wait", "active", "wait", False, False),
            ("active_retry", "active", "retry", False, True),
            ("blocked_dependency", "blocked", "resolve_blocker", False, False),
            ("blocked_timeout", "blocked", "resolve_blocker", True, False),
            ("done_collect", "done", "collect", False, False),
            ("failed_retry", "failed", "retry", False, True),
            ("failed_terminal", "failed", "none", False, False),
            ("timeout_retry", "timeout", "retry", True, True),
            ("timeout_terminal", "timeout", "none", True, False),
            ("timeout_failed_retry", "failed", "retry", True, True),
            ("timeout_failed_terminal", "failed", "none", True, False),
            ("timeout_aborted", "aborted", "none", True, False),
            ("aborted", "aborted", "none", False, False),
        )

        for lifecycle_state, status, action, timeout_origin, executable in cases:
            with self.subTest(lifecycle_state=lifecycle_state):
                worker = normalize_worker(
                    {"id": lifecycle_state, "prompt": "Work", "lifecycle_state": lifecycle_state},
                    lifecycle_state,
                )

                assert_worker_outcome(self, worker, status=status, action=action, lifecycle=lifecycle_state)
                self.assertEqual(next_eligible_worker_action(worker), action)
                self.assertEqual(is_executable_worker(worker), executable)
                self.assertEqual(
                    worker_lifecycle_state_for_public_state(status, action, timeout_origin=timeout_origin),
                    lifecycle_state,
                )

    def test_status_helpers_match_observable_worker_outcomes(self):
        alias_cases = (
            ("queued", "queued"),
            ("active", "active_wait"),
            ("blocked", "blocked_dependency"),
            ("done", "done_collect"),
            ("failed", "failed_terminal"),
            ("timeout", "timeout_terminal"),
            ("aborted", "aborted"),
        )

        for status, lifecycle_state in alias_cases:
            with self.subTest(status=status):
                worker = normalize_worker({"lifecycle_state": lifecycle_state}, "review")

                self.assertEqual(worker_lifecycle_state_for_status_alias(status), lifecycle_state)
                self.assertEqual(worker_lifecycle_state(worker), lifecycle_state)
                self.assertEqual(worker_output_field(worker, "status"), status)

        terminal_statuses = {"done", "failed", "timeout", "aborted"}
        failed_dependency_statuses = {"blocked", "failed", "timeout", "aborted"}
        for status in ("queued", "active", "blocked", "done", "failed", "timeout", "aborted"):
            with self.subTest(status=status):
                self.assertEqual(is_terminal_status(status), status in terminal_statuses)
                self.assertEqual(is_failed_dependency_status(status), status in failed_dependency_statuses)

        self.assertLess(status_priority("queued"), status_priority("active"))
        self.assertLess(status_priority("active"), status_priority("blocked"))
        self.assertLess(status_priority("timeout"), status_priority("aborted"))
        self.assertLess(status_priority("aborted"), status_priority("failed"))
        self.assertEqual(aggregate_run_status(["done", "done"]), "done")
        self.assertEqual(aggregate_run_status(["done", "active", "blocked"]), "blocked")
        self.assertEqual(aggregate_run_status(["done", "timeout", "aborted"]), "aborted")
        self.assertEqual(aggregate_run_status(["done", "timeout", "failed"]), "failed")

    def test_timeout_failed_retry_and_terminal_lifecycles_use_public_timeout_state(self):
        cases = (
            (
                "retryable timeout failure",
                {"retryable_failures": ["timeout"], "retry_count": 0, "retry_limit": 1},
                True,
                "timeout_failed_retry",
                "retry",
                True,
            ),
            (
                "terminal timeout failure",
                {},
                False,
                "timeout_failed_terminal",
                "none",
                False,
            ),
        )

        for name, fields, retry_available, lifecycle_state, action, executable in cases:
            with self.subTest(name=name):
                worker = normalize_worker({"prompt": "Review", "lifecycle_state": "active_wait", **fields}, "review")
                transition = WorkerTransition.timed_out(
                    "review",
                    "worker timed out",
                    status="failed",
                    timed_out_at="2026-07-04T00:00:00Z",
                    retry_available=retry_available,
                )

                self.assertEqual(worker_timeout_lifecycle_state("failed", retry_available), lifecycle_state)
                self.assertTrue(worker_transition_is_legal(worker, transition))

                apply_worker_transition_to_worker(worker, transition)

                assert_worker_outcome(self, worker, status="failed", action=action, lifecycle=lifecycle_state)
                self.assertEqual(is_executable_worker(worker), executable)

    def test_worker_transition_boundaries_accept_only_legal_lifecycle_moves(self):
        legal_cases = (
            (
                "queued worker can start",
                {"lifecycle_state": "queued"},
                mark_worker_active,
                {"status": "active", "action": "wait", "lifecycle": "active_wait"},
            ),
            (
                "retryable failure can schedule retry",
                {
                    "lifecycle_state": "failed_retry",
                    "failure_category": "provider",
                    "retryable_failures": ["provider"],
                    "retry_count": 0,
                    "retry_limit": 1,
                },
                lambda worker: schedule_worker_retry(worker, "provider", "try again"),
                {"status": "active", "action": "retry", "lifecycle": "active_retry"},
            ),
            (
                "active worker can apply done result",
                {"lifecycle_state": "active_wait"},
                lambda worker: WorkerTransition.result_applied(
                    worker.worker_id,
                    {"status": "done", "message_ids": {"assistant": "msg_assistant"}},
                ),
                {"status": "done", "action": "collect", "lifecycle": "done_collect"},
            ),
        )

        for name, fields, transition_factory, expected_outcome in legal_cases:
            with self.subTest(name=name):
                worker = normalize_worker({"id": "review", "prompt": "Review", **fields}, "review")
                transition = transition_factory(worker)

                self.assertTrue(worker_transition_is_legal(worker, transition))

                apply_worker_transition_to_worker(worker, transition)

                assert_worker_outcome(self, worker, **expected_outcome)

        illegal_cases = (
            (
                "done worker cannot restart",
                {
                    "lifecycle_state": "done_collect",
                    "result": {"status": "done", "message_ids": {"assistant": "msg_done"}},
                    "output_refs": ["assistant:msg_done"],
                },
                mark_worker_active,
            ),
            (
                "queued worker cannot receive result",
                {"lifecycle_state": "queued"},
                lambda worker: WorkerTransition.result_applied(
                    worker.worker_id,
                    {"status": "done", "message_ids": {"assistant": "msg_assistant"}},
                ),
            ),
        )

        for name, fields, transition_factory in illegal_cases:
            with self.subTest(name=name):
                worker = normalize_worker({"id": "review", "prompt": "Review", **fields}, "review")
                transition = transition_factory(worker)

                self.assertFalse(worker_transition_is_legal(worker, transition))
                with self.assertRaisesRegex(WorkerTransitionError, "illegal worker transition"):
                    apply_worker_transition_to_worker(worker, transition)

    def test_worker_record_accessors_back_core_retry_timeout_and_prompt_fields(self):
        worker = normalize_worker(
            {
                "id": "review",
                "agent": "plan",
                "model": "openai/gpt-5.5",
                "session_id": "ses_review",
                "prompt": "Review",
                "lifecycle_state": "failed_retry",
                "retry_count": 1,
                "retry_limit": 2,
                "retryable_failures": ["provider"],
                "failure_category": "provider",
                "timeout_seconds": 45,
                "timeout_started_at": "2026-07-04T00:00:00Z",
            },
            "review",
        )

        self.assertEqual(worker.worker_id, "review")
        self.assertEqual(worker.session_id, "ses_review")
        self.assertEqual(worker.agent, "plan")
        self.assertEqual(worker.model, "openai/gpt-5.5")
        self.assertTrue(worker.has_prompt)
        self.assertTrue(worker_has_prompt(worker))
        self.assertEqual(worker.retry_count, 1)
        self.assertEqual(worker.retry_limit, 2)
        self.assertTrue(worker.retry_available("provider"))
        self.assertTrue(worker_retry_available(worker, "provider"))
        self.assertEqual(worker_timeout_reason(worker), "worker timed out after 45s")

        transition = schedule_worker_retry(worker, "provider", "provider failed")

        self.assertEqual(transition.worker_id, "review")
        self.assertEqual(transition.payload.retry_count, 2)
        self.assertEqual(transition.payload.timeout_started_at, "2026-07-04T00:00:00Z")

    def test_hydration_boundary_normalizes_malformed_legacy_worker_record(self):
        worker = hydrate_worker_record(
            {
                "id": "",
                "lifecycle_state": "failed_retry",
                "dependencies": "build",
                "retry_count": "1",
                "retry_limit": "2",
                "retryable_failures": ["api"],
                "last_failure_category": "api",
            },
            "review",
        )

        self.assertIsInstance(worker, WorkerRecord)
        self.assertNotIsInstance(worker, dict)
        self.assertEqual(worker.worker_id, "review")
        self.assertEqual(worker.dependencies, [])
        self.assertEqual(worker.prompt_ids, [])
        self.assertEqual(worker.timeout_policy, "timeout")
        self.assertEqual(worker.lifecycle_state, "failed_retry")
        self.assertIsNone(worker_field(worker, "status"))
        self.assertIsNone(worker_field(worker, "next_eligible_action"))
        self.assertEqual(worker_output_field(worker, "status"), "failed")
        self.assertEqual(worker_output_field(worker, "next_eligible_action"), "retry")

    def test_core_worker_record_rejects_invalid_canonical_values(self):
        cases = (
            ({"id": ""}, "worker id"),
            ({"lifecycle_state": "missing"}, "lifecycle_state"),
            ({"retry_count": "1"}, "retry_count"),
            ({"retry_limit": None}, "retry_limit"),
            ({"dependencies": "build"}, "dependencies"),
            ({"attempts": "attempt-1"}, "attempts"),
            ({"timeout_policy": "waiting"}, "timeout_policy"),
            ({"status": "done"}, "output-only"),
            ({"next_eligible_action": "collect"}, "output-only"),
            ({"unknown_plugin_state": {"attempt": 2}}, "unknown worker field"),
        )

        for fields, message in cases:
            with self.subTest(fields=fields):
                with self.assertRaisesRegex((TypeError, ValueError), message):
                    normalize_worker(fields, "review")

    def test_worker_execution_eligibility_derives_canonical_action(self):
        queued = normalize_worker({"id": "build", "prompt": "Build", "lifecycle_state": "queued"}, "build")
        waiting = normalize_worker({"id": "review", "prompt": "Review", "lifecycle_state": "active_wait"}, "review")
        retrying = normalize_worker({"id": "test", "prompt": "Test", "lifecycle_state": "active_retry"}, "test")
        stale_action = hydrate_worker_record(
            {
                "id": "docs",
                "prompt": "Docs",
                "lifecycle_state": "active_wait",
                "status": "active",
                "next_eligible_action": "retry",
            },
            "docs",
        )

        self.assertTrue(is_executable_worker(queued))
        self.assertFalse(is_executable_worker(waiting))
        self.assertEqual(next_eligible_worker_action(waiting), "wait")
        self.assertTrue(is_executable_worker(retrying))
        self.assertFalse(is_executable_worker(stale_action))
        self.assertIsNone(worker_field(stale_action, "next_eligible_action"))
        self.assertEqual(worker_output_field(stale_action, "next_eligible_action"), "wait")
        self.assertEqual(next_eligible_worker_action(stale_action), "wait")

    def test_legacy_worker_mappings_canonicalize_at_hydration_boundary(self):
        cases = (
            (
                "done worker is not executable",
                {"id": "done", "prompt": "Done", "status": "done"},
                "done_collect",
                "done",
                "collect",
                False,
            ),
            (
                "active retry worker is executable",
                {"id": "active_retry", "prompt": "Retry", "status": "active", "next_eligible_action": "retry"},
                "active_retry",
                "active",
                "retry",
                True,
            ),
            (
                "failed retry worker is executable",
                {
                    "id": "failed_retry",
                    "prompt": "Retry",
                    "status": "failed",
                    "failure_category": "provider",
                    "retryable_failures": ["provider"],
                    "retry_count": 0,
                    "retry_limit": 1,
                },
                "failed_retry",
                "failed",
                "retry",
                True,
            ),
            (
                "failed terminal worker is not executable",
                {"id": "failed_terminal", "prompt": "Investigate", "status": "failed"},
                "failed_terminal",
                "failed",
                "none",
                False,
            ),
            (
                "timeout blocker keeps timeout origin",
                {
                    "id": "blocked_timeout",
                    "prompt": "Unblock",
                    "status": "blocked",
                    "failure_category": "timeout",
                },
                "blocked_timeout",
                "blocked",
                "resolve_blocker",
                False,
            ),
        )

        for name, worker, expected_lifecycle, expected_status, expected_action, executable in cases:
            with self.subTest(name=name):
                canonical = canonicalize_legacy_worker_record(worker)
                record = hydrate_worker_record(worker, worker["id"])

                self.assertEqual(canonical["lifecycle_state"], expected_lifecycle)
                self.assertIsInstance(record, WorkerRecord)
                self.assertEqual(worker_lifecycle_state(record), expected_lifecycle)
                self.assertEqual(worker_field(record, "lifecycle_state"), expected_lifecycle)
                self.assertIsNone(worker_field(record, "status"))
                self.assertIsNone(worker_field(record, "next_eligible_action"))
                self.assertEqual(worker_output_field(record, "status"), expected_status)
                self.assertEqual(worker_output_field(record, "next_eligible_action"), expected_action)
                self.assertEqual(next_eligible_worker_action(record), expected_action)
                self.assertEqual(next_eligible_action(record), expected_action)
                self.assertEqual(is_executable_worker(record), executable)

    def test_storage_adapter_projects_legacy_public_statuses_through_canonical_lifecycle_policy(self):
        cases = (
            (
                "active retry action",
                {
                    "id": "active_retry",
                    "prompt": "Retry",
                    "status": "active",
                    "next_eligible_action": "retry",
                },
                worker_lifecycle_state_for_public_state("active", "retry"),
                "active",
                "retry",
            ),
            (
                "failed retry budget",
                {
                    "id": "failed_retry",
                    "prompt": "Retry",
                    "status": "failed",
                    "failure_category": "provider",
                    "retryable_failures": ["provider"],
                    "retry_count": 0,
                    "retry_limit": 1,
                },
                worker_failed_lifecycle_state(retryable=True, retry_available=True),
                "failed",
                "retry",
            ),
            (
                "failed retry disabled by failure_retryable",
                {
                    "id": "failed_terminal",
                    "prompt": "Retry",
                    "status": "failed",
                    "failure_category": "provider",
                    "retryable_failures": ["provider"],
                    "retry_count": 0,
                    "retry_limit": 1,
                    "failure_retryable": False,
                },
                worker_failed_lifecycle_state(retryable=True, retry_available=False),
                "failed",
                "none",
            ),
            (
                "timeout failure retry budget",
                {
                    "id": "timeout_failed_retry",
                    "prompt": "Retry",
                    "status": "failed",
                    "failure_category": "timeout",
                    "retryable_failures": ["timeout"],
                    "retry_count": 0,
                    "retry_limit": 1,
                },
                worker_timeout_lifecycle_state("failed", True),
                "failed",
                "retry",
            ),
            (
                "blocked timeout origin",
                {
                    "id": "blocked_timeout",
                    "prompt": "Unblock",
                    "status": "blocked",
                    "blockers": ["timeout"],
                },
                worker_timeout_lifecycle_state("blocked", False),
                "blocked",
                "resolve_blocker",
            ),
            (
                "malformed retry budget remains terminal",
                {
                    "id": "malformed_retry_budget",
                    "prompt": "Investigate",
                    "status": "failed",
                    "failure_category": "provider",
                    "retryable_failures": ["provider"],
                    "retry_count": "bad",
                    "retry_limit": 1,
                },
                worker_failed_lifecycle_state(retryable=True, retry_available=False),
                "failed",
                "none",
            ),
        )

        for name, worker, expected_lifecycle, expected_status, expected_action in cases:
            with self.subTest(name=name):
                canonical = canonicalize_legacy_worker_record(worker)
                snapshot = normalize_worker_snapshot_for_storage(worker, worker["id"])
                record = hydrate_worker_record(worker, worker["id"])

                self.assertEqual(canonical["lifecycle_state"], expected_lifecycle)
                self.assertEqual(snapshot["lifecycle_state"], expected_lifecycle)
                self.assertNotIn("status", snapshot)
                self.assertNotIn("next_eligible_action", snapshot)
                assert_worker_outcome(
                    self,
                    record,
                    status=expected_status,
                    action=expected_action,
                    lifecycle=expected_lifecycle,
                )

    def test_core_worker_record_helpers_reject_raw_mappings(self):
        worker = {"id": "review", "prompt": "Review", "lifecycle_state": "queued"}

        self.assertFalse(is_worker_record(worker))
        self.assertTrue(is_worker_record(normalize_worker(worker, "review")))
        for helper in (
            lambda: worker_field(worker, "id"),
            lambda: worker_lifecycle_state(worker),
            lambda: next_eligible_worker_action(worker),
            lambda: is_executable_worker(worker),
            lambda: worker_record_for_mutation(worker, "review"),
        ):
            with self.assertRaisesRegex(TypeError, "WorkerRecord"):
                helper()

    def test_hydration_boundary_strips_stale_public_state_from_canonical_lifecycle(self):
        worker = hydrate_worker_record(
            {
                "lifecycle_state": "active_wait",
                "status": "failed",
                "next_eligible_action": "retry",
            },
            "review",
        )

        self.assertIsInstance(worker, WorkerRecord)
        self.assertNotIsInstance(worker, dict)
        assert_worker_outcome(self, worker, status="active", action="wait", lifecycle="active_wait")

    def test_worker_record_mutation_is_object_backed_not_snapshot_backed(self):
        snapshot = {
            "id": "review",
            "prompt_ids": ["msg_previous"],
            "lifecycle_state": "active_wait",
        }

        record = worker_record_for_mutation(normalize_worker(snapshot, "review"), "review")
        record.remember_prompt_id("msg_new")

        self.assertIsInstance(record, WorkerRecord)
        self.assertNotIsInstance(record, dict)
        self.assertEqual(record.prompt_ids, ["msg_previous", "msg_new"])
        self.assertEqual(snapshot["prompt_ids"], ["msg_previous"])

    def test_worker_record_explicit_api_replaces_dict_mutation(self):
        record = WorkerRecord.default_fields("review")

        record.update_canonical_fields(prompt="Review the change")
        record.remember_prompt_id("msg_review")
        snapshot = record.to_snapshot()

        self.assertEqual(record.prompt, "Review the change")
        self.assertEqual(record.prompt_ids, ["msg_review"])
        self.assertIsNone(worker_field(record, "status"))
        self.assertEqual(worker_output_field(record, "status"), "queued")
        self.assertNotIn("status", snapshot)
        self.assertEqual(snapshot["prompt_ids"], ["msg_review"])

    def test_worker_record_hydration_exposes_canonical_fields_and_round_trips_unknown_fields(self):
        record = hydrate_worker_record(
            {
                "id": "review",
                "role": "reviewer",
                "prompt": "Review the change",
                "lifecycle_state": "active_wait",
                "custom_persisted": {"kept": True},
                "status": "done",
                "next_eligible_action": "collect",
            },
            "review",
        )

        self.assertEqual(record.worker_id, "review")
        self.assertEqual(record.role, "reviewer")
        self.assertEqual(record.prompt, "Review the change")
        self.assertEqual(record.lifecycle_state, "active_wait")
        self.assertEqual(record.to_snapshot()["custom_persisted"], {"kept": True})
        self.assertIsNone(worker_field(record, "status"))
        self.assertIsNone(worker_field(record, "next_eligible_action"))

    def test_worker_record_unknown_persisted_fields_round_trip_through_extras(self):
        record = hydrate_worker_record(
            {
                "id": "review",
                "role": "reviewer",
                "unknown_plugin_state": {"attempt": 2},
            },
            "review",
        )

        snapshot = record.to_snapshot()
        round_tripped = hydrate_worker_record(snapshot, "review")

        self.assertEqual(snapshot["unknown_plugin_state"], {"attempt": 2})
        self.assertEqual(round_tripped.to_snapshot()["unknown_plugin_state"], {"attempt": 2})
        self.assertEqual(worker_field(round_tripped, "unknown_plugin_state"), {"attempt": 2})
        self.assertNotIn("unknown_plugin_state", WorkerRecord.default_snapshot_fields("review"))

    def test_worker_record_mutation_updates_hydrated_object_without_sync(self):
        worker = WorkerRecord.default_fields("review")
        worker.update_canonical_fields(prompt="Review the change")
        workers = {"review": worker}

        record = apply_worker_transition(workers, mark_worker_active(worker))
        record.remember_prompt_id("msg_review")
        snapshot = serialize_worker_snapshot(record, "review")

        self.assertIs(record, worker)
        self.assertIs(workers["review"], worker)
        self.assertEqual(worker.lifecycle_state, "active_wait")
        self.assertEqual(worker_output_field(worker, "status"), "active")
        self.assertEqual(worker.prompt_ids, ["msg_review"])
        self.assertEqual(snapshot["prompt_ids"], ["msg_review"])

    def test_hydration_adapter_derives_legacy_public_state(self):
        worker = hydrate_worker_record(
            {
                "status": "active",
                "next_eligible_action": "retry",
                "dependencies": "build",
            },
            "review",
        )

        self.assertEqual(worker.worker_id, "review")
        self.assertIsNone(worker.session_id)
        self.assertEqual(worker.dependencies, [])
        assert_worker_outcome(self, worker, status="active", action="retry", lifecycle="active_retry")

    def test_serialize_worker_snapshot_keeps_public_state_out_of_persisted_json(self):
        worker = hydrate_worker_record(
            {
                "id": "review",
                "session_id": "ses_review",
                "lifecycle_state": "done_collect",
                "status": "active",
                "next_eligible_action": "retry",
            },
            "review",
        )

        snapshot = serialize_worker_snapshot(worker, "review")

        self.assertIs(type(snapshot), dict)
        self.assertEqual(snapshot["lifecycle_state"], "done_collect")
        self.assertEqual(snapshot["session_id"], "ses_review")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)

    def test_storage_snapshot_trusts_lifecycle_not_public_status(self):
        worker = {
            "id": "review",
            "lifecycle_state": "active_wait",
            "status": "done",
            "next_eligible_action": "collect",
        }

        snapshot = normalize_worker_snapshot_for_storage(worker, "review")

        self.assertEqual(snapshot["lifecycle_state"], "active_wait")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)

    def test_core_snapshot_serialization_rejects_public_state_fields(self):
        with self.assertRaisesRegex(ValueError, "output-only"):
            normalize_worker_snapshot(
                {
                    "id": "review",
                    "lifecycle_state": "active_wait",
                    "status": "done",
                    "next_eligible_action": "collect",
                },
                "review",
            )

    def test_storage_worker_snapshot_normalizes_stale_public_status(self):
        snapshot = normalize_worker_snapshot_for_storage(
            {
                "id": "review",
                "lifecycle_state": "active_wait",
                "status": "done",
                "next_eligible_action": "collect",
            },
            "review",
        )

        self.assertEqual(snapshot["lifecycle_state"], "active_wait")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)

    def test_storage_adapter_migrates_legacy_public_status_without_lifecycle(self):
        snapshot = normalize_worker_snapshot_for_storage(
            {
                "id": "review",
                "status": "done",
                "next_eligible_action": "collect",
            },
            "review",
        )

        self.assertEqual(snapshot["lifecycle_state"], "done_collect")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)

    def test_storage_snapshot_transition_normalizes_legacy_public_state_before_apply(self):
        worker = normalize_worker(
            {
                "id": "review",
                "lifecycle_state": "failed_retry",
                "failure_category": "provider",
                "retryable_failures": ["provider"],
                "retry_count": 0,
                "retry_limit": 1,
                "prompt_ids": ["msg_failed"],
            },
            "review",
        )
        transition = worker_snapshot_transition(
            {
                "id": "review",
                "status": "active",
                "next_eligible_action": "retry",
                "prompt_ids": ["msg_retry"],
                "cleanup": {"requested": True, "deleted": False},
            }
        )

        apply_worker_transition_to_worker(worker, transition)

        assert_worker_outcome(self, worker, status="active", action="retry", lifecycle="active_retry")
        self.assertEqual(worker_field(worker, "prompt_ids"), ["msg_failed", "msg_retry"])
        self.assertEqual(worker_field(worker, "cleanup"), {"requested": True, "deleted": False})
        self.assertIsNone(worker_field(worker, "failure_retryable"))

    def test_accepted_abort_snapshot_passthrough_is_adapter_declared(self):
        worker = normalize_worker(
            {
                "id": "review",
                "lifecycle_state": "aborted",
                "abort": {"accepted": True},
                "prompt_ids": ["msg_abort"],
            },
            "review",
        )
        transition = worker_snapshot_transition(
            {
                "id": "review",
                "lifecycle_state": "done_collect",
                "prompt_ids": ["msg_done"],
                "cleanup": {"requested": True, "deleted": False},
                "result": {"status": "done", "message_ids": {"assistant": "msg_done"}},
            }
        )

        apply_worker_transition_to_worker(worker, transition)

        self.assertEqual(worker_output_field(worker, "status"), "aborted")
        self.assertEqual(worker_field(worker, "abort"), {"accepted": True})
        self.assertEqual(worker_field(worker, "cleanup"), {"requested": True, "deleted": False})
        self.assertEqual(worker_field(worker, "prompt_ids"), ["msg_abort", "msg_done"])
        self.assertIsNone(worker_field(worker, "result"))

    def test_internal_worker_guard_reports_missing_required_fields(self):
        with self.assertRaisesRegex(ValueError, "session_id"):
            require_internal_worker({"id": "review"})

    def test_refresh_run_summary_uses_failed_precedence_for_mixed_terminal_workers(self):
        run = {
            "workers": {
                "build": normalize_worker({"id": "build", "prompt": "Build", "lifecycle_state": "timeout_terminal"}, "build"),
                "review": normalize_worker({"id": "review", "prompt": "Review", "lifecycle_state": "aborted"}, "review"),
                "test": normalize_worker({"id": "test", "prompt": "Test", "lifecycle_state": "failed_terminal"}, "test"),
            }
        }

        refresh_run_summary(run)

        self.assertEqual(run["status"], "failed")


if __name__ == "__main__":
    unittest.main()
