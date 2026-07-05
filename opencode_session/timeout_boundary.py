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
        self.require_time()
        result = callback(self)
        self.require_time()
        return result
