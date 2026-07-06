import unittest

from opencode_session.event_watcher import BackgroundSessionEventWatcher, SessionEventWatcher


class FakeEventClient:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def stream_events(self, path, *, on_open=None, deadline=None, stop_event=None):
        self.calls.append((path, on_open is not None, deadline))
        if on_open is not None:
            on_open()
        yield from self.events


class TerminalThenOpenEventClient:
    def __init__(self):
        self.calls = []

    def stream_events(self, path, *, on_open=None, deadline=None, stop_event=None):
        self.calls.append((path, on_open is not None, deadline, stop_event is not None))
        if on_open is not None:
            on_open()
        yield {"type": "session.status", "properties": {"sessionID": "ses_target", "status": "completed"}}
        while stop_event is not None and not stop_event.wait(0.01):
            pass


class StoppableEventClient:
    def stream_events(self, path, *, on_open=None, deadline=None, stop_event=None):
        if on_open is not None:
            on_open()
        while stop_event is not None and not stop_event.wait(0.01):
            pass
        if False:
            yield {}


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

    def test_background_watcher_closes_after_terminal_event(self):
        client = TerminalThenOpenEventClient()
        watcher = BackgroundSessionEventWatcher(client, "/api/event", "ses_target")

        watcher.start()
        watcher.wait_open(1.0)
        events = watcher.collect(timeout=1.0, limit=10)

        self.assertEqual([event["kind"] for event in events], ["status"])
        self.assertFalse(watcher.thread.is_alive())
        self.assertEqual(client.calls, [("/api/event", True, None, True)])

    def test_background_watcher_close_stops_stream_thread(self):
        watcher = BackgroundSessionEventWatcher(StoppableEventClient(), "/api/event", "ses_target")

        watcher.start()
        watcher.wait_open(1.0)
        closed = watcher.close(timeout=1.0)

        self.assertTrue(closed)
        self.assertFalse(watcher.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
