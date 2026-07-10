import unittest
from unittest.mock import patch
from urllib.error import URLError

from opencode_session.api_domain import OpenCodeDomainClient
from opencode_session.api_routes import OpenCodeRoutePlanner
from opencode_session.api_client import OpenCodeApiClient
from opencode_session.api_transport import OpenCodeApiResponse, OpenCodeApiTimeoutError
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def post_response(self, path, payload, *, timeout=None, deadline=None):
        self.calls.append(("POST", path, payload, timeout, deadline))
        return OpenCodeApiResponse({"id": "ses_1", "directory": "/tmp/project"}, "{}")


class ApiClientTransportErrorTest(unittest.TestCase):
    def test_request_maps_default_socket_timeout_to_api_timeout(self):
        client = OpenCodeApiClient("http://127.0.0.1:4096")

        with patch("opencode_session.api_transport.urlopen", side_effect=URLError(TimeoutError("timed out"))):
            with self.assertRaises(OpenCodeApiTimeoutError):
                client.get_response("/health")

    def test_stream_events_maps_deadline_url_timeout_to_timeout_expired(self):
        client = OpenCodeApiClient("http://127.0.0.1:4096")

        with patch("opencode_session.api_transport.urlopen", side_effect=URLError(TimeoutError("timed out"))):
            with self.assertRaises(TimeoutExpired):
                list(client.stream_events("/api/event", deadline=TimeoutDeadline(5)))


class ApiClientSplitTest(unittest.TestCase):
    def test_domain_client_plans_routes_and_delegates_transport_calls(self):
        transport = RecordingTransport()
        client = OpenCodeDomainClient(transport, OpenCodeRoutePlanner()).configure_route_plan(
            {"session_collection": "/session"}
        )

        response = client.create_session_response("/tmp/project", agent="build")

        self.assertEqual(response.data["id"], "ses_1")
        self.assertEqual(
            transport.calls,
            [
                (
                    "POST",
                    "session",
                    {"location": {"directory": "/tmp/project"}, "agent": "build"},
                    None,
                    None,
                )
            ],
        )

    def test_api_client_domain_methods_still_dispatch_through_public_transport_methods(self):
        calls = []

        class OverridingClient(OpenCodeApiClient):
            def post_response(self, path, payload, *, timeout=None, deadline=None):
                calls.append((path, payload, timeout, deadline))
                return OpenCodeApiResponse({"id": "ses_1", "directory": "/tmp/project"}, "{}")

        client = OverridingClient("http://127.0.0.1:4096").configure_route_plan({"session_collection": "/session"})

        response = client.create_session_response("/tmp/project")

        self.assertEqual(response.data["id"], "ses_1")
        self.assertEqual(calls, [("session", {"location": {"directory": "/tmp/project"}}, None, None)])

    def test_api_client_reexports_transport_response_and_error_types(self):
        from opencode_session import api_client, api_transport

        self.assertIs(api_client.OpenCodeApiError, api_transport.OpenCodeApiError)
        self.assertIs(api_client.OpenCodeApiTimeoutError, api_transport.OpenCodeApiTimeoutError)
        self.assertIs(api_client.OpenCodeApiResponse, api_transport.OpenCodeApiResponse)


if __name__ == "__main__":
    unittest.main()
