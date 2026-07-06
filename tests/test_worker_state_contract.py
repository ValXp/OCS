import ast
import inspect
from collections.abc import Mapping, MutableMapping
from dataclasses import fields, is_dataclass
import unittest

from opencode_session.worker_storage_adapter import (
    canonicalize_legacy_worker_record,
    hydrate_worker_record,
    normalize_worker_snapshot_for_storage,
    worker_snapshot_transition,
    worker_snapshot_transition_patch,
)
from opencode_session.worker_state import (
    _WORKER_LIFECYCLE_TABLE,
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
    WORKER_TRANSITION_DEFINITIONS,
    WORKER_TRANSITION_METADATA,
    WorkerLifecycleAction,
    WorkerLifecycleDimensions,
    WorkerLifecycleStatus,
    WorkerRecord,
    WorkerSnapshotTransitionPatch,
    WorkerTransition,
    WorkerTransitionError,
    WorkerTransitionName,
    WorkerTransitionSpec,
    apply_worker_transition,
    apply_worker_transition_to_worker,
    deserialize_worker_record,
    is_executable_worker,
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
    worker_has_prompt,
    worker_lifecycle_source_states,
    worker_lifecycle_state,
    worker_lifecycle_state_for_dimensions,
    worker_lifecycle_state_for_status_alias,
    worker_lifecycle_target_states,
    worker_field,
    worker_output_field,
    worker_retry_available,
    worker_timeout_reason,
    worker_timeout_lifecycle_state,
    worker_record_for_mutation,
)

try:
    from tests.worker_state_scenarios import assert_worker_outcome
except ModuleNotFoundError:
    from worker_state_scenarios import assert_worker_outcome


class WorkerStateContractTest(unittest.TestCase):
    def test_lifecycle_metadata_derives_public_status_policy_and_flags(self):
        rows_by_state = {row.state: row for row in _WORKER_LIFECYCLE_TABLE}

        self.assertEqual(WORKER_LIFECYCLE_STATES, frozenset(rows_by_state))
        self.assertEqual(
            {status.value for status in WorkerLifecycleStatus},
            {row.status for row in _WORKER_LIFECYCLE_TABLE},
        )
        self.assertEqual(
            {action.value for action in WorkerLifecycleAction},
            {row.action for row in _WORKER_LIFECYCLE_TABLE},
        )
        self.assertEqual(WORKER_LIFECYCLE_STATES, frozenset(WORKER_LIFECYCLE_METADATA))
        for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items():
            with self.subTest(lifecycle_state=lifecycle_state):
                row = rows_by_state[lifecycle_state]
                self.assertEqual(metadata.status, row.status)
                self.assertEqual(metadata.retryable, row.retryable)
                self.assertEqual(metadata.timeout_origin, row.timeout_origin)
                self.assertEqual(metadata.source_transitions, row.source_transitions)
                self.assertEqual(metadata.target_transitions, row.target_transitions)
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
                self.assertEqual(
                    worker_lifecycle_state(normalize_worker({"lifecycle_state": lifecycle_state}, "review")),
                    lifecycle_state,
                )
                self.assertEqual(status_priority(status), WORKER_STATUS_PRIORITY_BY_STATUS[status])

        for transition_name, metadata in WORKER_TRANSITION_METADATA.items():
            with self.subTest(transition=transition_name):
                self.assertEqual(worker_lifecycle_source_states(transition_name), metadata.source_states)
                self.assertEqual(worker_lifecycle_target_states(transition_name), metadata.target_states)

    def test_transition_legality_is_derived_from_lifecycle_model(self):
        for transition_name, metadata in WORKER_TRANSITION_METADATA.items():
            if not metadata.public_lifecycle_transition:
                continue

            expected_source_states = frozenset(
                lifecycle_state
                for lifecycle_state, lifecycle_metadata in WORKER_LIFECYCLE_METADATA.items()
                if transition_name in lifecycle_metadata.source_transitions
            )
            expected_target_states = frozenset(
                lifecycle_state
                for lifecycle_state, lifecycle_metadata in WORKER_LIFECYCLE_METADATA.items()
                if transition_name in lifecycle_metadata.target_transitions
            )

            with self.subTest(transition=transition_name):
                self.assertEqual(
                    WORKER_TRANSITION_DEFINITIONS[transition_name].source_states,
                    expected_source_states,
                )
                self.assertEqual(
                    WORKER_TRANSITION_DEFINITIONS[transition_name].target_states,
                    expected_target_states,
                )
                self.assertEqual(metadata.source_states, expected_source_states)
                self.assertEqual(metadata.target_states, expected_target_states)

    def test_transition_specs_are_table_driven_reducer_rows(self):
        annotations = WorkerTransitionSpec.__annotations__
        for field_name in ("name", "source_states", "target_states", "target_state", "payload_type", "applier"):
            with self.subTest(field_name=field_name):
                self.assertNotEqual(annotations[field_name], object)
        for removed_hook in ("payload_factory", "target_resolver", "legality_checker"):
            with self.subTest(removed_hook=removed_hook):
                self.assertNotIn(removed_hook, annotations)

        for spec in WORKER_TRANSITION_DEFINITIONS.values():
            with self.subTest(transition=spec.name):
                self.assertIsInstance(spec.payload_type, type)
                self.assertTrue(callable(spec.applier))
                self.assertIs(spec.metadata, spec)
                self.assertIs(WORKER_TRANSITION_METADATA[spec.name], spec)
                if spec.target_state is not None:
                    self.assertIn(spec.target_state, spec.target_states)

        with self.assertRaisesRegex(ValueError, "missing payload type"):
            WorkerTransitionSpec(
                WorkerTransitionName.ACTIVE,
                source_states=frozenset(),
                target_states=frozenset(),
                payload_type=object(),
                applier=lambda reducer, transition, payload, target_state: {},
            )
        with self.assertRaisesRegex(ValueError, "missing applier"):
            WorkerTransitionSpec(
                WorkerTransitionName.ACTIVE,
                source_states=frozenset(),
                target_states=frozenset(),
                payload_type=object,
                applier=object(),
            )
        with self.assertRaisesRegex(ValueError, "configured unknown target lifecycle state"):
            WorkerTransitionSpec(
                WorkerTransitionName.ACTIVE,
                source_states=frozenset(),
                target_states=frozenset({"active_wait"}),
                payload_type=object,
                applier=lambda reducer, transition, payload, target_state: {},
                target_state="missing",
            )

    def test_core_worker_state_invariants_use_worker_record_accessors(self):
        import opencode_session.worker_state as worker_state_module

        tree = ast.parse(inspect.getsource(worker_state_module))
        core_functions = {
            "_cleanup_updated_transition_payload",
            "_provisioned_transition_payload",
            "_snapshot_worker_id",
            "_timeout_started_at_or_unset",
            "_worker_id",
            "apply_worker_result",
            "ensure_worker",
            "latest_prompt_ids_are_retry_marker",
            "mark_dependency_blocked",
            "mark_worker_aborted",
            "mark_worker_active",
            "mark_worker_failed",
            "mark_worker_timeout",
            "retry_available",
            "schedule_worker_retry",
            "worker_has_prompt",
            "worker_output_refs_in_dependency_order",
            "worker_prompt",
            "worker_retry_available",
            "worker_timeout_reason",
        }
        offenders = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in core_functions:
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                if isinstance(child.func, ast.Name) and child.func.id == "worker_field":
                    offenders.append(f"{node.name}:{child.lineno}: worker_field")
                if isinstance(child.func, ast.Attribute) and child.func.attr == "field":
                    offenders.append(f"{node.name}:{child.lineno}: field")

        self.assertEqual([], offenders)

    def test_worker_record_accessors_back_core_retry_timeout_and_prompt_fields(self):
        worker = normalize_worker(
            {
                "id": "review",
                "agent": "plan",
                "model": "openai/gpt-5.5",
                "session_id": "ses_review",
                "prompt": "Review",
                "lifecycle_state": "failed_retry",
                "retry_count": "1",
                "retry_limit": "2",
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

    def test_normalize_worker_applies_defaults_and_derives_next_action(self):
        worker = normalize_worker(
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
        self.assertEqual(worker.field("id"), "review")
        self.assertEqual(worker.field("dependencies"), [])
        self.assertEqual(worker.field("prompt_ids"), [])
        self.assertEqual(worker.field("timeout_policy"), "timeout")
        self.assertEqual(worker.lifecycle_state, "failed_retry")
        self.assertIsNone(worker.field("status"))
        self.assertIsNone(worker.field("next_eligible_action"))
        self.assertEqual(worker_output_field(worker, "status"), "failed")
        self.assertEqual(worker_output_field(worker, "next_eligible_action"), "retry")

    def test_worker_execution_eligibility_derives_canonical_action(self):
        queued = normalize_worker({"id": "build", "prompt": "Build", "lifecycle_state": "queued"}, "build")
        waiting = normalize_worker({"id": "review", "prompt": "Review", "lifecycle_state": "active_wait"}, "review")
        retrying = normalize_worker({"id": "test", "prompt": "Test", "lifecycle_state": "active_retry"}, "test")
        stale_action = normalize_worker(
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

        record = worker_record_for_mutation(normalize_worker(snapshot, "review"), "review")
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
        self.assertIsNone(worker_field(record, "status"))
        self.assertEqual(worker_output_field(record, "status"), "queued")
        self.assertNotIn("status", snapshot)
        self.assertEqual(snapshot["prompt_ids"], ["msg_review"])

    def test_worker_record_canonical_fields_are_explicit_dataclass_fields(self):
        record = normalize_worker(
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

        dataclass_field_names = {field.name for field in fields(WorkerRecord)}

        self.assertTrue(is_dataclass(record))
        self.assertNotIn("_fields", record.__dict__)
        for field_name in (
            "id",
            "role",
            "session_id",
            "agent",
            "model",
            "prompt",
            "lifecycle_state",
            "dependencies",
            "prompt_ids",
            "retry_count",
            "retry_limit",
            "retryable_failures",
            "timeout_seconds",
            "timeout_policy",
            "timeout_started_at",
            "timed_out_at",
            "failure_category",
            "failure_reason",
            "last_failure_category",
            "last_failure_reason",
            "blockers",
            "output_refs",
            "result",
            "cleanup",
            "abort",
            "extras",
        ):
            with self.subTest(field_name=field_name):
                self.assertIn(field_name, dataclass_field_names)

        self.assertEqual(record.worker_id, "review")
        self.assertEqual(record.role, "reviewer")
        self.assertEqual(record.prompt, "Review the change")
        self.assertEqual(record.lifecycle_state, "active_wait")
        self.assertEqual(record.extras, {"custom_persisted": {"kept": True}})
        self.assertIsNone(record.field("status"))
        self.assertIsNone(record.field("next_eligible_action"))

    def test_worker_record_unknown_persisted_fields_round_trip_through_extras(self):
        record = normalize_worker(
            {
                "id": "review",
                "role": "reviewer",
                "unknown_plugin_state": {"attempt": 2},
            },
            "review",
        )

        snapshot = record.to_snapshot()
        round_tripped = deserialize_worker_record(snapshot, "review")

        self.assertEqual(record.extras, {"unknown_plugin_state": {"attempt": 2}})
        self.assertEqual(snapshot["unknown_plugin_state"], {"attempt": 2})
        self.assertEqual(round_tripped.extras, {"unknown_plugin_state": {"attempt": 2}})
        self.assertEqual(round_tripped.field("unknown_plugin_state"), {"attempt": 2})
        self.assertNotIn("unknown_plugin_state", WorkerRecord.default_snapshot_fields("review"))

    def test_worker_record_mutation_updates_hydrated_object_without_sync(self):
        worker = WorkerRecord.default_fields("review")
        worker.set_field("prompt", "Review the change")
        workers = {"review": worker}

        record = apply_worker_transition(workers, mark_worker_active(worker))
        record.remember_prompt_id("msg_review")
        snapshot = serialize_worker_snapshot(record, "review")

        self.assertIs(record, worker)
        self.assertIs(workers["review"], worker)
        self.assertEqual(worker.lifecycle_state, "active_wait")
        self.assertEqual(worker_output_field(worker, "status"), "active")
        self.assertEqual(worker.field("prompt_ids"), ["msg_review"])
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

        self.assertEqual(worker_lifecycle_state(normalize_worker(worker, "review")), "active_wait")
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

    def test_snapshot_transition_requires_storage_boundary_patch(self):
        with self.assertRaisesRegex(TypeError, "storage boundary"):
            WorkerTransition.snapshot_applied({"id": "review", "lifecycle_state": "done_collect"})

    def test_stale_snapshot_recovery_requires_adapter_declared_patch(self):
        worker = normalize_worker({"id": "review", "lifecycle_state": "done_collect"}, "review")
        transition = WorkerTransition.snapshot_applied(
            WorkerSnapshotTransitionPatch("review", {"id": "review", "lifecycle_state": "active_wait"})
        )

        with self.assertRaisesRegex(WorkerTransitionError, "illegal worker transition"):
            apply_worker_transition_to_worker(worker, transition)

        self.assertEqual(worker_lifecycle_state(worker), "done_collect")

    def test_storage_snapshot_patch_owns_legacy_and_persistence_compatibility(self):
        patch = worker_snapshot_transition_patch(
            {
                "id": "review",
                "status": "active",
                "next_eligible_action": "retry",
                "prompt_ids": ["msg_retry"],
                "cleanup": {"requested": True, "deleted": False},
            }
        )

        self.assertEqual(patch.target_lifecycle_state, "active_retry")
        self.assertEqual(patch.fields["lifecycle_state"], "active_retry")
        self.assertNotIn("status", patch.fields)
        self.assertNotIn("next_eligible_action", patch.fields)
        self.assertEqual(patch.prompt_ids, ("msg_retry",))
        self.assertIn("error", patch.remove_fields)
        self.assertTrue(patch.stale_recovery_allowed)
        self.assertEqual(patch.accepted_abort_fields, {"cleanup": {"requested": True, "deleted": False}})
        self.assertEqual(patch.accepted_abort_prompt_ids, ("msg_retry",))

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
