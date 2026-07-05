import unittest

from opencode_session.cli_policy import DEFAULT_SERVER_URL, server_default


class CliPolicyTest(unittest.TestCase):
    def test_server_default_prefers_new_env_name_then_legacy_then_constant(self):
        self.assertEqual(server_default({}), DEFAULT_SERVER_URL)
        self.assertEqual(server_default({"OPENCODE_SERVER": "http://legacy.example"}), "http://legacy.example")
        self.assertEqual(
            server_default(
                {
                    "OPENCODE_SERVER_URL": "http://current.example",
                    "OPENCODE_SERVER": "http://legacy.example",
                }
            ),
            "http://current.example",
        )


if __name__ == "__main__":
    unittest.main()
