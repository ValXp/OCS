from collections.abc import Mapping, MutableMapping
import unittest

from opencode_session.worker_state import (
    EXECUTABLE_WORKER_ACTIONS,
    FAILED_DEPENDENCY_STATUSES,
    PUBLIC_WORKER_STATE_BY_LIFECYCLE,
    TERMINAL_WORKER_STATUSES,
    WORKER_LIFECYCLE_DIMENSIONS_BY_STATE,
    WORKER_LIFECYCLE_METADATA,
    WORKER_LIFECYCLE_STATE_BY_DIMENSIONS,
    WORKER_LIFECYCLE_STATE_BY_STATUS_ALIAS,
    WORKER_LIFECYCLE_STATES,
    WORKER_STATUS_PRIORITY_BY_STATUS,
    WORKER_TIMEOUT_ORIGIN_LIFECYCLE_STATES,
    WORKER_TRANSITION_METADATA,
    WorkerLifecycleAction,
    WorkerLifecycleDimensions,
    WorkerLifecycleStatus,
    WorkerRecord,
    apply_worker_transition,
    deserialize_worker_record,
    is_executable_worker,
    mark_worker_active,
    next_eligible_worker_action,
    normalize_worker,
    normalize_worker_snapshot,
    refresh_run_summary,
    require_internal_worker,
    serialize_worker_snapshot,
    status_priority,
    worker_lifecycle_source_states,
    worker_lifecycle_state,
    worker_lifecycle_state_for_dimensions,
    worker_lifecycle_state_for_status_alias,
    worker_lifecycle_target_states,
    worker_field,
    worker_timeout_lifecycle_state,
    worker_record_for_mutation,
)

try:
    from tests.worker_state_scenarios import assert_worker_outcome
except ModuleNotFoundError:
    from worker_state_scenarios import assert_worker_outcome


class WorkerStateContractTest(unittest.TestCase):
    def test_lifecycle_metadata_derives_public_status_policy_and_flags(self):
        self.assertEqual(WORKER_LIFECYCLE_STATES, frozenset(WORKER_LIFECYCLE_METADATA))
        self.assertEqual(
            PUBLIC_WORKER_STATE_BY_LIFECYCLE,
            {
                lifecycle_state: (metadata.status, metadata.next_eligible_action)
                for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items()
            },
        )
        self.assertEqual(
            WORKER_LIFECYCLE_DIMENSIONS_BY_STATE,
            {
                lifecycle_state: metadata.dimensions
                for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items()
            },
        )
        self.assertEqual(
            WORKER_LIFECYCLE_STATE_BY_DIMENSIONS,
            {metadata.dimensions: lifecycle_state for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items()},
        )
        self.assertEqual(
            TERMINAL_WORKER_STATUSES,
            frozenset(metadata.status for metadata in WORKER_LIFECYCLE_METADATA.values() if metadata.terminal_status),
        )
        self.assertEqual(
            FAILED_DEPENDENCY_STATUSES,
            frozenset(
                metadata.status for metadata in WORKER_LIFECYCLE_METADATA.values() if metadata.failed_dependency_status
            ),
        )
        self.assertEqual(
            EXECUTABLE_WORKER_ACTIONS,
            frozenset(
                metadata.next_eligible_action for metadata in WORKER_LIFECYCLE_METADATA.values() if metadata.executable
            ),
        )
        self.assertEqual(
            WORKER_LIFECYCLE_STATE_BY_STATUS_ALIAS,
            {
                metadata.status: lifecycle_state
                for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items()
                if metadata.status_alias
            },
        )
        self.assertEqual(WORKER_STATUS_PRIORITY_BY_STATUS, _metadata_by_status("status_priority"))

    def test_timeout_failed_retry_and_terminal_lifecycles_derive_from_dimensions(self):
        retry_dimensions = WorkerLifecycleDimensions(
            WorkerLifecycleStatus.FAILED,
            WorkerLifecycleAction.RETRY,
            retryable=True,
            timeout_origin=True,
        )
        terminal_dimensions = WorkerLifecycleDimensions(
            WorkerLifecycleStatus.FAILED,
            WorkerLifecycleAction.RETRY,
            retryable=False,
            timeout_origin=True,
        )

        self.assertEqual(worker_lifecycle_state_for_dimensions(retry_dimensions), "timeout_failed_retry")
        self.assertEqual(worker_lifecycle_state_for_dimensions(terminal_dimensions), "timeout_failed_terminal")
        self.assertEqual(worker_timeout_lifecycle_state("failed", True), "timeout_failed_retry")
        self.assertEqual(worker_timeout_lifecycle_state("failed", False), "timeout_failed_terminal")

        retry_metadata = WORKER_LIFECYCLE_METADATA["timeout_failed_retry"]
        terminal_metadata = WORKER_LIFECYCLE_METADATA["timeout_failed_terminal"]
        self.assertEqual(retry_metadata.dimensions, retry_dimensions)
        self.assertEqual(terminal_metadata.dimensions, terminal_dimensions)
        self.assertEqual(retry_metadata.next_eligible_action, "retry")
        self.assertEqual(terminal_metadata.next_eligible_action, "none")
        self.assertTrue(retry_metadata.retryable)
        self.assertFalse(terminal_metadata.retryable)
        self.assertIn("timeout_failed_retry", WORKER_TIMEOUT_ORIGIN_LIFECYCLE_STATES)
        self.assertIn("timeout_failed_terminal", WORKER_TIMEOUT_ORIGIN_LIFECYCLE_STATES)

    def test_lifecycle_metadata_feeds_status_helpers_and_reducer_legality(self):
        for status, lifecycle_state in WORKER_LIFECYCLE_STATE_BY_STATUS_ALIAS.items():
            with self.subTest(status=status):
                self.assertEqual(worker_lifecycle_state_for_status_alias(status), lifecycle_state)
                self.assertEqual(worker_lifecycle_state(normalize_worker({"status": status}, "review")), lifecycle_state)
                self.assertEqual(status_priority(status), WORKER_STATUS_PRIORITY_BY_STATUS[status])

        for transition_name, metadata in WORKER_TRANSITION_METADATA.items():
            with self.subTest(transition=transition_name):
                self.assertEqual(worker_lifecycle_source_states(transition_name), metadata.source_states)
                self.assertEqual(worker_lifecycle_target_states(transition_name), metadata.target_states)

    def test_lifecycle_transition_views_are_derived_from_transition_metadata(self):
        source_transitions_by_state = {lifecycle_state: set() for lifecycle_state in WORKER_LIFECYCLE_METADATA}
        target_transitions_by_state = {lifecycle_state: set() for lifecycle_state in WORKER_LIFECYCLE_METADATA}
        for transition_name, metadata in WORKER_TRANSITION_METADATA.items():
            if not metadata.public_lifecycle_transition:
                continue
            for lifecycle_state in metadata.source_states:
                source_transitions_by_state[lifecycle_state].add(transition_name)
            for lifecycle_state in metadata.target_states:
                target_transitions_by_state[lifecycle_state].add(transition_name)

        for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items():
            with self.subTest(lifecycle_state=lifecycle_state):
                self.assertEqual(metadata.source_transitions, frozenset(source_transitions_by_state[lifecycle_state]))
                self.assertEqual(metadata.target_transitions, frozenset(target_transitions_by_state[lifecycle_state]))

    def test_normalize_worker_applies_defaults_and_derives_next_action(self):
        worker = normalize_worker(
            {
                "id": "",
                "status": "failed",
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
        self.assertEqual(worker.field("id"), "review")
        self.assertEqual(worker.status, "failed")
        self.assertEqual(worker.field("dependencies"), [])
        self.assertEqual(worker.field("prompt_ids"), [])
        self.assertEqual(worker.field("timeout_policy"), "timeout")
        self.assertEqual(worker.lifecycle_state, "failed_retry")
        self.assertEqual(worker.next_eligible_action, "retry")

    def test_worker_execution_eligibility_derives_canonical_action(self):
        queued = {"id": "build", "prompt": "Build", "lifecycle_state": "queued"}
        waiting = {"id": "review", "prompt": "Review", "lifecycle_state": "active_wait"}
        retrying = {"id": "test", "prompt": "Test", "lifecycle_state": "active_retry"}
        stale_action = {
            "id": "docs",
            "prompt": "Docs",
            "lifecycle_state": "active_wait",
            "status": "active",
            "next_eligible_action": "retry",
        }

        self.assertTrue(is_executable_worker(queued))
        self.assertFalse(is_executable_worker(waiting))
        self.assertEqual(next_eligible_worker_action(waiting), "wait")
        self.assertTrue(is_executable_worker(retrying))
        self.assertFalse(is_executable_worker(stale_action))
        self.assertEqual(next_eligible_worker_action(stale_action), "wait")

    def test_deserialize_worker_derives_public_state_from_lifecycle(self):
        worker = deserialize_worker_record(
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

        record = worker_record_for_mutation(snapshot, "review")
        record.remember_prompt_id("msg_new")

        self.assertIsInstance(record, WorkerRecord)
        self.assertNotIsInstance(record, dict)
        self.assertEqual(record.field("prompt_ids"), ["msg_previous", "msg_new"])
        self.assertEqual(snapshot["prompt_ids"], ["msg_previous"])

    def test_worker_record_is_not_mapping_hybrid(self):
        record = WorkerRecord.default_fields("review")

        self.assertNotIsInstance(record, Mapping)
        self.assertNotIsInstance(record, MutableMapping)
        with self.assertRaises(TypeError):
            dict(record)
        with self.assertRaises(TypeError):
            record["id"]
        with self.assertRaises(TypeError):
            record["role"] = "review"
        for method_name in ("get", "setdefault", "pop", "update", "clear"):
            self.assertFalse(hasattr(record, method_name), method_name)

    def test_worker_record_explicit_api_replaces_dict_mutation(self):
        record = WorkerRecord.default_fields("review")

        record.set_field("prompt", "Review the change")
        record.remember_prompt_id("msg_review")
        snapshot = record.to_snapshot()

        self.assertEqual(record.field("prompt"), "Review the change")
        self.assertEqual(record.field("prompt_ids"), ["msg_review"])
        self.assertEqual(worker_field(record, "status"), "queued")
        self.assertNotIn("status", snapshot)
        self.assertEqual(snapshot["prompt_ids"], ["msg_review"])

    def test_worker_record_mutation_updates_hydrated_object_without_sync(self):
        worker = WorkerRecord.default_fields("review")
        worker.set_field("prompt", "Review the change")
        workers = {"review": worker}

        record = apply_worker_transition(workers, mark_worker_active(worker))
        record.remember_prompt_id("msg_review")
        snapshot = serialize_worker_snapshot(record, "review")

        self.assertIs(record, worker)
        self.assertIs(workers["review"], worker)
        self.assertEqual(worker.status, "active")
        self.assertEqual(worker.field("prompt_ids"), ["msg_review"])
        self.assertEqual(snapshot["prompt_ids"], ["msg_review"])

    def test_deserialize_worker_snapshot_hydrates_defaults_and_public_state(self):
        worker = deserialize_worker_record(
            {
                "status": "active",
                "next_eligible_action": "retry",
                "dependencies": "build",
            },
            "review",
        )

        self.assertEqual(worker.field("id"), "review")
        self.assertIsNone(worker.field("session_id"))
        self.assertEqual(worker.field("dependencies"), [])
        assert_worker_outcome(self, worker, status="active", action="retry", lifecycle="active_retry")

    def test_serialize_worker_snapshot_keeps_public_state_out_of_persisted_json(self):
        worker = deserialize_worker_record(
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

    def test_serialize_worker_snapshot_trusts_lifecycle_not_public_status(self):
        worker = {
            "id": "review",
            "lifecycle_state": "active_wait",
            "status": "done",
            "next_eligible_action": "collect",
        }

        snapshot = serialize_worker_snapshot(worker, "review")

        self.assertEqual(worker_lifecycle_state(worker), "active_wait")
        self.assertEqual(snapshot["lifecycle_state"], "active_wait")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)

    def test_normalize_worker_snapshot_trusts_lifecycle_over_stale_public_status(self):
        snapshot = normalize_worker_snapshot(
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

    def test_normalize_worker_snapshot_migrates_legacy_public_status_without_lifecycle(self):
        snapshot = normalize_worker_snapshot(
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

    def test_internal_worker_guard_reports_missing_required_fields(self):
        with self.assertRaisesRegex(ValueError, "session_id"):
            require_internal_worker({"id": "review"})

    def test_refresh_run_summary_uses_failed_precedence_for_mixed_terminal_workers(self):
        run = {
            "workers": {
                "build": {"id": "build", "prompt": "Build", "lifecycle_state": "timeout_terminal"},
                "review": {"id": "review", "prompt": "Review", "lifecycle_state": "aborted"},
                "test": {"id": "test", "prompt": "Test", "lifecycle_state": "failed_terminal"},
            }
        }

        refresh_run_summary(run)

        self.assertEqual(run["status"], "failed")


def _metadata_by_status(field_name, *, skip_none=False):
    values = {}
    for metadata in WORKER_LIFECYCLE_METADATA.values():
        value = getattr(metadata, field_name)
        if skip_none and value is None:
            continue
        if metadata.status in values and values[metadata.status] != value:
            raise AssertionError(f"conflicting metadata for {metadata.status}")
        values[metadata.status] = value
    return values


if __name__ == "__main__":
    unittest.main()
