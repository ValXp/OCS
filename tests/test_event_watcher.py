import unittest

from opencode_session.event_watcher import BackgroundSessionEventWatcher, SessionEventWatcher


class FakeEventClient:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def stream_events(self, path, *, on_open=None, deadline=None):
        self.calls.append((path, on_open is not None, deadline))
        if on_open is not None:
            on_open()
        yield from self.events


class SessionEventWatcherTest(unittest.TestCase):
    def test_iter_events_filters_and_normalizes_session_events(self):
        client = FakeEventClient(
            [
                {"type": "session.status", "properties": {"sessionID": "ses_other", "status": "completed"}},
                {"type": "session.status", "properties": {"sessionID": "ses_target", "status": "completed"}},
            ]
        )

        events = list(SessionEventWatcher(client, "/api/event", "ses_target").iter_events(deadline="deadline"))

        self.assertEqual(client.calls, [("/api/event", False, "deadline")])
        self.assertEqual(
            events,
            [
                {
                    "kind": "status",
                    "schema_status": "known",
                    "session_id": "ses_target",
                    "type": "session.status",
                    "status": "done",
                    "raw_status": "completed",
                }
            ],
        )

    def test_background_watcher_collects_until_terminal_event(self):
        client = FakeEventClient(
            [
                {"type": "message.part.updated", "properties": {"sessionID": "ses_target", "messageID": "msg_1", "part": {"type": "text", "text": "Hi"}}},
                {"type": "session.status", "properties": {"sessionID": "ses_target", "status": "completed"}},
                {"type": "message.part.updated", "properties": {"sessionID": "ses_target", "messageID": "msg_2", "part": {"type": "text", "text": "ignored"}}},
            ]
        )
        watcher = BackgroundSessionEventWatcher(client, "/api/event", "ses_target")

        watcher.start()
        watcher.wait_open(1.0)
        events = watcher.collect(timeout=1.0, limit=10)

        self.assertEqual([event["kind"] for event in events], ["text", "status"])
        self.assertEqual(client.calls, [("/api/event", True, None)])


if __name__ == "__main__":
    unittest.main()
