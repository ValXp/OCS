import unittest
from unittest.mock import patch
from urllib.error import URLError

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired


class ApiClientTransportErrorTest(unittest.TestCase):
    def test_stream_events_maps_deadline_url_timeout_to_timeout_expired(self):
        client = OpenCodeApiClient("http://127.0.0.1:4096")

        with patch("opencode_session.api_client.urlopen", side_effect=URLError(TimeoutError("timed out"))):
            with self.assertRaises(TimeoutExpired):
                list(client.stream_events("/api/event", deadline=TimeoutDeadline(5)))


if __name__ == "__main__":
    unittest.main()
