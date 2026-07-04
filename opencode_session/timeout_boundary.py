import queue
import threading
import time


class TimeoutExpired(Exception):
    pass


class TimeoutDeadline:
    def __init__(self, timeout):
        self.timeout = timeout
        self.expires_at = None if timeout is None else time.monotonic() + timeout

    def remaining(self):
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - time.monotonic())

    def expired(self):
        remaining = self.remaining()
        return remaining is not None and remaining <= 0

    def require_time(self):
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            raise TimeoutExpired()
        return remaining

    def run(self, callback):
        remaining = self.require_time()
        if remaining is None:
            return callback()

        result = queue.Queue(maxsize=1)
        thread = threading.Thread(target=_run_callback, args=(callback, result), daemon=True)
        thread.start()
        thread.join(timeout=remaining)
        if thread.is_alive():
            raise TimeoutExpired()

        kind, value = result.get_nowait()
        if kind == "error":
            raise value
        return value


def _run_callback(callback, result):
    try:
        result.put(("value", callback()))
    except BaseException as error:
        result.put(("error", error))
