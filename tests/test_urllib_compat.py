import unittest

from opencode_session.urllib_compat import set_response_socket_timeout


class FakeSocket:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.timeouts = []

    def settimeout(self, timeout):
        if self.fail:
            raise OSError("socket closed")
        self.timeouts.append(timeout)


class FakeRaw:
    def __init__(self, socket):
        self._sock = socket


class FakeFile:
    def __init__(self, raw):
        self.raw = raw


class FakeResponse:
    def __init__(self, socket=None):
        if socket is not None:
            self.fp = FakeFile(FakeRaw(socket))


class UrllibCompatTest(unittest.TestCase):
    def test_sets_nested_urllib_response_socket_timeout(self):
        socket = FakeSocket()

        changed = set_response_socket_timeout(FakeResponse(socket), 1.5)

        self.assertTrue(changed)
        self.assertEqual(socket.timeouts, [1.5])

    def test_missing_socket_is_noop(self):
        self.assertFalse(set_response_socket_timeout(object(), None))

    def test_socket_settimeout_failure_is_noop(self):
        socket = FakeSocket(fail=True)

        self.assertFalse(set_response_socket_timeout(FakeResponse(socket), None))
        self.assertEqual(socket.timeouts, [])


if __name__ == "__main__":
    unittest.main()
