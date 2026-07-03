import json
import os
import tempfile
import unittest
import uuid

from harness import (
    assert_success,
    create_session_fixture,
    delete_session_fixture,
    format_completed_process,
    load_json,
    require_server_url,
    run_ocs,
)


SERVER_ENV = "OCS_E2E_SERVER_URL"
SMOKE_STATUS_TERMS = {"queued", "active", "blocked", "done", "failed", "aborted", "timeout"}


@unittest.skipUnless(os.environ.get(SERVER_ENV), f"set {SERVER_ENV} to run OpenCode E2E tests")
class NoLiveSmokeCleanupE2ETest(unittest.TestCase):
    def test_smoke_json_exercises_real_server_without_live_model_execution(self):
        server_url = require_server_url(self)
        marker = f"ocs-e2e-smoke-{uuid.uuid4().hex[:12]}"
        prefix = f"{marker}-"

        with tempfile.TemporaryDirectory(prefix=f"{marker}-target-") as directory:
            result = run_ocs(
                "smoke",
                "--json",
                "--directory",
                directory,
                "--prefix",
                prefix,
                "--event-timeout",
                "2.0",
                "--event-limit",
                "5",
                "--server",
                server_url,
            )

        assert_success(self, result)
        payload = load_json(self, result, "smoke --json")
        context = self._context(payload)
        self.assertTrue(payload.get("ok"), context)
        self.assertEqual(payload.get("status"), "done", context)
        self.assertEqual(payload.get("mode"), "no-live-model", context)
        self.assertTrue(payload.get("no_live_model"), context)
        self.assertEqual(payload.get("directory"), directory, context)
        self.assertEqual(payload.get("prefix"), prefix, context)
        self.assertIsInstance(payload.get("session_id"), str, context)
        self.assertNotEqual(payload.get("session_id"), "", context)

        self._assert_smoke_capabilities(payload.get("capabilities"), context)
        self._assert_smoke_steer(payload, context)
        self._assert_smoke_events(payload, context)
        self._assert_smoke_blockers(payload, context)
        self._assert_smoke_skipped_live_model_execution(payload, context)
        self._assert_smoke_cleanup(payload, context)

        inspect_result = run_ocs("inspect", payload["session_id"], "--json", "--server", server_url)
        self.assertNotEqual(inspect_result.returncode, 0, format_completed_process(inspect_result))

    def test_cleanup_json_deletes_only_prefixed_sessions_in_target_directory(self):
        server_url = require_server_url(self)
        marker = f"ocs-e2e-cleanup-{uuid.uuid4().hex[:12]}"
        prefix = f"{marker}-"

        with tempfile.TemporaryDirectory(prefix=f"{marker}-target-") as directory, tempfile.TemporaryDirectory(
            prefix=f"{marker}-other-"
        ) as other_directory:
            stale_id = self._create_session_fixture(
                server_url,
                directory,
                title=f"{prefix}stale",
                metadata={"disposable": True, "prefix": prefix, "smoke_id": f"{prefix}stale"},
            )
            keep_same_directory_id = self._create_session_fixture(
                server_url,
                directory,
                title=f"regular-{marker}",
                metadata={"disposable": True, "prefix": f"not-{prefix}"},
            )
            keep_other_directory_id = self._create_session_fixture(
                server_url,
                other_directory,
                title=f"{prefix}other-directory",
                metadata={"disposable": True, "prefix": prefix, "smoke_id": f"{prefix}other-directory"},
            )

            result = run_ocs(
                "cleanup",
                "--json",
                "--directory",
                directory,
                "--prefix",
                prefix,
                "--server",
                server_url,
            )

            assert_success(self, result)
            payload = load_json(self, result, "cleanup --json")
            context = self._context(payload)
            self.assertEqual(payload.get("status"), "done", context)
            self.assertEqual(payload.get("prefix"), prefix, context)
            self.assertEqual(payload.get("directory"), directory, context)
            self.assertEqual(payload.get("stale"), 1, context)
            self.assertEqual(payload.get("sessions"), [stale_id], context)
            self.assertEqual(payload.get("deleted"), [stale_id], context)
            self.assertEqual(payload.get("verified"), [stale_id], context)
            self.assertEqual(payload.get("errors"), [], context)
            for kept_id in (keep_same_directory_id, keep_other_directory_id):
                self.assertNotIn(kept_id, payload.get("sessions", []), context)
                self.assertNotIn(kept_id, payload.get("deleted", []), context)
                self.assertNotIn(kept_id, payload.get("verified", []), context)

            self._assert_session_deleted(server_url, stale_id)
            self._assert_session_readable(server_url, keep_same_directory_id, directory)
            self._assert_session_readable(server_url, keep_other_directory_id, other_directory)

    def _assert_smoke_capabilities(self, capabilities, context):
        self.assertIsInstance(capabilities, dict, context)
        for field in ("health", "version"):
            self.assertIsInstance(capabilities.get(field), str, context)
            self.assertNotEqual(capabilities.get(field), "", context)
        for field in ("v2_prompt_support", "event_support", "legacy_fallback_available"):
            self.assertIs(capabilities.get(field), True, context)
        routes = capabilities.get("route_availability")
        self.assertIsInstance(routes, dict, context)
        for name in ("session", "v2_prompt", "events", "legacy_run", "legacy_reply"):
            route = routes.get(name)
            self.assertIsInstance(route, dict, context)
            self.assertTrue(route.get("available"), context)
            self.assertIsInstance(route.get("path"), str, context)
            self.assertNotEqual(route.get("path"), "", context)

    def _assert_smoke_steer(self, payload, context):
        steer = payload.get("checks", {}).get("steer")
        self.assertIsInstance(steer, dict, context)
        self.assertEqual(steer.get("session_id"), payload.get("session_id"), context)
        self.assertEqual(steer.get("delivery"), "steer", context)
        self.assertIn(steer.get("status"), SMOKE_STATUS_TERMS, context)
        self.assertIsNone(steer.get("terminal_state"), context)
        self.assertFalse((steer.get("fallback") or {}).get("used"), context)
        self.assertEqual(
            steer.get("api_path"),
            payload["capabilities"]["route_availability"]["v2_prompt"]["path"],
            context,
        )
        for execution_field in ("executed", "message_ids", "assistant_message_id", "text", "tokens", "cost"):
            self.assertNotIn(execution_field, steer, context)

    def _assert_smoke_events(self, payload, context):
        events = payload.get("checks", {}).get("events")
        self.assertIsInstance(events, dict, context)
        self.assertEqual(events.get("status"), "done", context)
        self.assertIsInstance(events.get("types"), list, context)
        self.assertGreater(len(events.get("types")), 0, context)
        self.assertEqual(payload.get("event_types"), events.get("types"), context)

    def _assert_smoke_blockers(self, payload, context):
        blockers = payload.get("checks", {}).get("blockers")
        self.assertIsInstance(blockers, dict, context)
        self.assertIn(blockers.get("status"), {"done", "skipped"}, context)
        for field in ("permissions", "questions", "total"):
            self.assertIn(field, blockers, context)
        if blockers.get("status") == "done":
            for field in ("permissions", "questions", "total"):
                self.assertIsInstance(blockers.get(field), int, context)
            self.assertEqual(blockers["total"], blockers["permissions"] + blockers["questions"], context)

    def _assert_smoke_skipped_live_model_execution(self, payload, context):
        run_blocking = payload.get("checks", {}).get("run_blocking")
        self.assertIsInstance(run_blocking, dict, context)
        self.assertEqual(run_blocking.get("status"), "skipped", context)
        self.assertEqual(run_blocking.get("reason"), "no-live-model", context)
        self.assertEqual(run_blocking.get("terminal_state"), "skipped", context)
        self.assertTrue((run_blocking.get("fallback") or {}).get("available"), context)
        self.assertFalse((run_blocking.get("fallback") or {}).get("used"), context)
        self.assertEqual(
            run_blocking.get("api_path"),
            {
                "run": payload["capabilities"]["route_availability"]["legacy_run"]["path"],
                "reply": payload["capabilities"]["route_availability"]["legacy_reply"]["path"],
            },
            context,
        )
        for execution_field in ("message_ids", "assistant_message_id", "text", "tokens", "cost"):
            self.assertNotIn(execution_field, run_blocking, context)

    def _assert_smoke_cleanup(self, payload, context):
        cleanup = payload.get("cleanup")
        self.assertIsInstance(cleanup, dict, context)
        self.assertEqual(cleanup.get("status"), "done", context)
        self.assertEqual(cleanup.get("deleted"), [payload.get("session_id")], context)
        self.assertEqual(cleanup.get("verified"), [payload.get("session_id")], context)
        self.assertEqual(payload.get("checks", {}).get("cleanup"), cleanup, context)

    def _create_session_fixture(self, server_url, directory, *, title, metadata):
        session = create_session_fixture(self, server_url, directory, title=title, metadata=metadata)
        session_id = self._session_id(session, "fixture create payload")
        self.addCleanup(delete_session_fixture, self, server_url, session_id, ignore_not_found=True)
        return session_id

    def _assert_session_deleted(self, server_url, session_id):
        result = run_ocs("inspect", session_id, "--json", "--server", server_url)
        self.assertNotEqual(result.returncode, 0, format_completed_process(result))

    def _assert_session_readable(self, server_url, session_id, directory):
        result = run_ocs("inspect", session_id, "--json", "--server", server_url)
        assert_success(self, result)
        session = load_json(self, result, "inspect --json")
        context = self._context(session)
        self.assertEqual(self._session_id(session, "inspect payload"), session_id, context)
        self.assertEqual(session.get("directory") or session.get("cwd"), directory, context)

    def _session_id(self, session, label):
        if not isinstance(session, dict):
            self.fail(f"{label} was not a JSON object:\n{self._context(session)}")
        for name in ("id", "sessionID", "sessionId"):
            value = session.get(name)
            if value:
                return value
        self.fail(f"{label} did not include a session id:\n{self._context(session)}")

    def _context(self, payload):
        return json.dumps(payload, indent=2, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
