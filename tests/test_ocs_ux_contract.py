import os
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "ocs"


class OcsUxContractTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=REPO_ROOT,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_top_level_help_uses_finalized_command_names(self):
        result = self.run_cli("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("usage: ocs", result.stdout)
        self.assertIn("run_blocking", result.stdout)
        self.assertIn("steer", result.stdout)
        self.assertNotIn("opencode-session", result.stdout)
        self.assertNotIn("queue", result.stdout)

    def test_steer_help_describes_admission_not_execution(self):
        result = self.run_cli("steer", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("admit", result.stdout.lower())
        self.assertIn("does not wait for an assistant reply", result.stdout)
        self.assertNotIn("execute", result.stdout.lower())
        self.assertNotIn("complete", result.stdout.lower())

    def test_live_validate_help_explains_gate_tokens_and_cleanup(self):
        result = self.run_cli("live_validate", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("OCS_LIVE_VALIDATE=1", result.stdout)
        self.assertIn("PONG", result.stdout)
        self.assertIn("token", result.stdout.lower())
        self.assertIn("disposable", result.stdout.lower())
        self.assertIn("deleted before the command exits", result.stdout)

    def test_readme_examples_use_finalized_ocs_vocabulary(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("bin/ocs", readme)
        self.assertIn("run_blocking", readme)
        self.assertIn("steer", readme)
        self.assertIn("Live-provider validation is separate and opt-in", readme)
        self.assertIn("bin/ocs live_validate", readme)
        self.assertIn("OCS_LIVE_VALIDATE=1", readme)
        self.assertIn("Reply exactly PONG", readme)
        self.assertIn("token", readme.lower())
        self.assertIn("deleted before the command exits", readme)
        self.assertNotIn("bin/opencode-session", readme)
        self.assertNotIn("top-level `queue`", readme)


if __name__ == "__main__":
    unittest.main()
