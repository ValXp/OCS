import unittest

from opencode_session.api_domain import OpenCodeDomainClient
from opencode_session.api_routes import OpenCodeRoutePlanner
from opencode_session.api_transport import OpenCodeApiResponse
from opencode_session.session_services import SessionCommandService


class RecordingAuxiliaryTransport:
    def __init__(self):
        self.calls = []

    def post_response(self, path, payload, *, timeout=None, deadline=None):
        self.calls.append(("POST", path, payload))
        if path.endswith("/abort"):
            return OpenCodeApiResponse({"sessionID": "ses_1", "accepted": True, "status": "aborted"}, "{}")
        return OpenCodeApiResponse({"sessionID": "ses_child", "messageID": payload.get("messageID")}, "{}")

    def get_response(self, path, *, timeout=None, deadline=None):
        self.calls.append(("GET", path, None))
        return OpenCodeApiResponse({"children": [{"sessionID": "ses_child", "name": "Child"}]}, "{}")


class SessionCommandServiceRouteTest(unittest.TestCase):
    def test_auxiliary_session_operations_honor_custom_route_configuration(self):
        transport = RecordingAuxiliaryTransport()
        client = OpenCodeDomainClient(transport, OpenCodeRoutePlanner())
        service = SessionCommandService(
            client,
            route_capabilities={
                "route_plan": {
                    "session_abort": "/custom/{sessionID}/abort",
                    "session_fork": "/custom/{sessionID}/fork",
                    "session_children": "/custom/{sessionID}/children",
                }
            },
        )

        abort = service.abort("ses_1")
        fork = service.fork("ses_1", message_id="msg_1")
        children = service.children("ses_1")

        self.assertEqual(abort.abort["status"], "aborted")
        self.assertEqual(fork.fork["session_id"], "ses_child")
        self.assertEqual(children.children[0]["id"], "ses_child")
        self.assertEqual(
            transport.calls,
            [
                ("POST", "custom/ses_1/abort", {}),
                ("POST", "custom/ses_1/fork", {"messageID": "msg_1"}),
                ("GET", "custom/ses_1/children", None),
            ],
        )


if __name__ == "__main__":
    unittest.main()
