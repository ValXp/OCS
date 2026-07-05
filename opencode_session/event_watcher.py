import queue
import threading
import time

from opencode_session.events import is_terminal_event, normalize_event


class EventWatchOpenTimeout(Exception):
    def __init__(self, timeout):
        super().__init__(f"event stream did not open within {timeout}s")
        self.timeout = timeout


class EventWatchTimeout(Exception):
    def __init__(self, timeout):
        super().__init__(f"event stream timed out after {timeout}s")
        self.timeout = timeout


class EventWatchEmpty(Exception):
    pass


class SessionEventWatcher:
    def __init__(self, client, event_path, session_id):
        self.client = client
        self.event_path = event_path
        self.session_id = session_id

    def iter_events(self, *, deadline=None, on_open=None):
        for raw_event in self.client.stream_events(self.event_path, on_open=on_open, deadline=deadline):
            event = normalize_event(raw_event, self.session_id, route_path=self.event_path)
            if event is not None:
                yield event


class BackgroundSessionEventWatcher:
    def __init__(self, client, event_path, session_id):
        self.watcher = SessionEventWatcher(client, event_path, session_id)
        self.opened = threading.Event()
        self.items = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def wait_open(self, timeout):
        if self.opened.wait(timeout):
            return
        error = self._first_error()
        if error is not None:
            raise error
        raise EventWatchOpenTimeout(timeout)

    def collect(self, *, timeout, limit, stop_on_terminal=True):
        events = []
        deadline = time.monotonic() + timeout
        while len(events) < limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                kind, value = self.items.get(timeout=remaining)
            except queue.Empty:
                break
            if kind == "error":
                raise value
            if kind == "done":
                break
            events.append(value)
            if stop_on_terminal and is_terminal_event(value):
                break
        if events:
            return events
        if self.thread.is_alive():
            raise EventWatchTimeout(timeout)
        raise EventWatchEmpty()

    def _run(self):
        try:
            for event in self.watcher.iter_events(on_open=self.opened.set):
                self.items.put(("event", event))
        except Exception as error:  # pragma: no cover - surfaced through wait/collect callers
            self.items.put(("error", error))
        finally:
            self.items.put(("done", None))

    def _first_error(self):
        pending = []
        error = None
        while True:
            try:
                item = self.items.get_nowait()
            except queue.Empty:
                break
            pending.append(item)
            if item[0] == "error":
                error = item[1]
                break
        for item in pending:
            self.items.put(item)
        return error


def is_invalid_event_stream_error(error):
    return isinstance(getattr(error, "data", None), dict) and error.data.get("kind") == "invalid_event_stream"
