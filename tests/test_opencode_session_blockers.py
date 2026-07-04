import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "ocs"


class BlockerOpenCodeServer:
    def __init__(self, *, sessions=None, permissions=None, questions=None, missing_paths=None):
        self.sessions = sessions or []
        self.permissions = permissions or []
        self.questions = questions or []
        self.missing_paths = set(missing_paths or [])
        self.requests = []
        self.server = None
        self.thread = None

    def __enter__(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                parent.requests.append(("GET", self.path, None))
                if self.path == "/api/session":
                    self._write_json({"sessions": parent.sessions})
                    return
                if self.path.startswith("/api/session/"):
                    session_id = self.path.rsplit("/", 1)[-1]
                    for session in parent.sessions:
                        if session.get("id") == session_id:
                            self._write_json(session)
                            return
                    self.send_error(404)
                    return
                if self.path == "/permission":
                    self._write_json(parent.permissions)
                    return
                if self.path == "/question":
                    self._write_json(parent.questions)
                    return
                self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                payload = json.loads(body or "{}")
                parent.requests.append(("POST", self.path, payload))
                if self.path in parent.missing_paths:
                    self._write_json({"message": "request not found"}, status=404)
                    return
                if self.path.startswith("/permission/") and self.path.endswith("/reply"):
                    self._write_json(True)
                    return
                if self.path.startswith("/question/") and self.path.endswith("/reply"):
                    self._write_json(True)
                    return
                if self.path.startswith("/question/") and self.path.endswith("/reject"):
                    self._write_json(True)
                    return
                self.send_error(404)

            def _write_json(self, payload, *, status=200):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"


class BlockerCliTest(unittest.TestCase):
    def run_cli(self, *args, env=None):
        command_env = os.environ.copy()
        if env:
            command_env.update(env)
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=REPO_ROOT,
            env=command_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_permission_list_prints_pending_requests_compact(self):
        permissions = [
            {
                "id": "per_bash_1",
                "sessionID": "ses_build",
                "permission": "bash",
                "patterns": ["git status --short"],
                "always": ["git status *"],
                "metadata": {"command": "git status --short"},
                "tool": {"messageID": "msg_1", "callID": "tool_1"},
            }
        ]

        with BlockerOpenCodeServer(permissions=permissions) as server:
            result = self.run_cli("permission", "list", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            'id=per_bash_1 session=ses_build permission=bash patterns="git status --short" '
            'always="git status *" tool=msg_1/tool_1\n',
        )
        self.assertEqual(server.requests, [("GET", "/permission", None)])

    def test_permission_list_filters_pending_requests_by_session(self):
        permissions = [
            {
                "id": "per_build",
                "sessionID": "ses_build",
                "permission": "bash",
                "patterns": ["pytest tests/test_auth.py"],
                "always": ["pytest *"],
                "metadata": {},
            },
            {
                "id": "per_plan",
                "sessionID": "ses_plan",
                "permission": "edit",
                "patterns": ["docs/plan.md"],
                "always": [],
                "metadata": {},
            },
        ]

        with BlockerOpenCodeServer(permissions=permissions) as server:
            result = self.run_cli("permission", "list", "--session", "ses_build", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            'id=per_build session=ses_build permission=bash patterns="pytest tests/test_auth.py" '
            'always="pytest *" tool=-\n',
        )
        self.assertEqual(server.requests, [("GET", "/permission", None)])

    def test_permission_list_multiple_requests_prints_compact_table(self):
        permissions = [
            {
                "id": "per_build",
                "sessionID": "ses_build",
                "permission": "bash",
                "patterns": ["pytest tests/test_auth.py"],
                "always": ["pytest *"],
                "metadata": {},
            },
            {
                "id": "per_plan",
                "sessionID": "ses_plan",
                "permission": "edit",
                "patterns": ["docs/plan.md"],
                "always": [],
                "metadata": {},
            },
        ]

        with BlockerOpenCodeServer(permissions=permissions) as server:
            result = self.run_cli("permission", "list", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "id\tsession\tpermission\tpatterns\talways\ttool\n"
            "per_build\tses_build\tbash\t\"pytest tests/test_auth.py\"\t\"pytest *\"\t-\n"
            "per_plan\tses_plan\tedit\tdocs/plan.md\t-\t-\n",
        )
        self.assertEqual(server.requests, [("GET", "/permission", None)])

    def test_permission_reply_posts_selected_response(self):
        with BlockerOpenCodeServer() as server:
            result = self.run_cli("permission", "reply", "per_bash_1", "always", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "id=per_bash_1 reply=always ok=true\n")
        self.assertEqual(
            server.requests,
            [("POST", "/permission/per_bash_1/reply", {"reply": "always"})],
        )

    def test_missing_permission_reply_reports_request_not_found(self):
        with BlockerOpenCodeServer(missing_paths={"/permission/per_missing/reply"}) as server:
            result = self.run_cli("permission", "reply", "per_missing", "once", "--server", server.url)

        self.assertEqual(result.returncode, 66)
        self.assertEqual(result.stdout, "")
        self.assertIn("permission request not found: per_missing", result.stderr)
        self.assertEqual(
            server.requests,
            [("POST", "/permission/per_missing/reply", {"reply": "once"})],
        )

    def test_missing_blocker_resolution_with_escaped_id_reports_request_not_found(self):
        cases = [
            (
                ("permission", "reply", "per/missing", "once"),
                "/permission/per%2Fmissing/reply",
                {"reply": "once"},
                "permission request not found: per/missing",
            ),
            (
                ("question", "answer", "que/missing", "Ship"),
                "/question/que%2Fmissing/reply",
                {"answers": [["Ship"]]},
                "question request not found: que/missing",
            ),
            (
                ("question", "reject", "que/missing"),
                "/question/que%2Fmissing/reject",
                {},
                "question request not found: que/missing",
            ),
        ]

        for args, missing_path, payload, message in cases:
            with self.subTest(args=args):
                with BlockerOpenCodeServer(missing_paths={missing_path}) as server:
                    result = self.run_cli(*args, "--server", server.url)

                self.assertEqual(result.returncode, 66)
                self.assertEqual(result.stdout, "")
                self.assertIn(message, result.stderr)
                self.assertEqual(server.requests, [("POST", missing_path, payload)])

    def test_question_list_prints_pending_questions_compact(self):
        questions = [
            {
                "id": "que_release_1",
                "sessionID": "ses_build",
                "questions": [
                    {
                        "question": "Ship the release now?",
                        "header": "Release",
                        "options": [
                            {"label": "Ship", "description": "Deploy immediately"},
                            {"label": "Hold", "description": "Wait for review"},
                        ],
                    }
                ],
                "tool": {"messageID": "msg_2", "callID": "tool_2"},
            }
        ]

        with BlockerOpenCodeServer(questions=questions) as server:
            result = self.run_cli("question", "list", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            'id=que_release_1 session=ses_build questions=1 headers=Release '
            'question="Ship the release now?" tool=msg_2/tool_2\n',
        )
        self.assertEqual(server.requests, [("GET", "/question", None)])

    def test_question_answer_posts_nested_answers(self):
        with BlockerOpenCodeServer() as server:
            result = self.run_cli("question", "answer", "que_release_1", "Ship", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "id=que_release_1 action=answer ok=true\n")
        self.assertEqual(
            server.requests,
            [("POST", "/question/que_release_1/reply", {"answers": [["Ship"]]})],
        )

    def test_question_answer_json_reports_normalized_result(self):
        with BlockerOpenCodeServer() as server:
            result = self.run_cli("question", "answer", "que_release_1", "Ship", "--json", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {"id": "que_release_1", "action": "answer", "ok": True, "response": True, "answers": [["Ship"]]},
        )
        self.assertEqual(
            server.requests,
            [("POST", "/question/que_release_1/reply", {"answers": [["Ship"]]})],
        )

    def test_question_answer_accepts_full_nested_answers_json(self):
        with BlockerOpenCodeServer() as server:
            result = self.run_cli(
                "question",
                "answer",
                "que_tests_1",
                "--answers-json",
                '[["Unit", "Integration"]]',
                "--server",
                server.url,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "id=que_tests_1 action=answer ok=true\n")
        self.assertEqual(
            server.requests,
            [("POST", "/question/que_tests_1/reply", {"answers": [["Unit", "Integration"]]})],
        )

    def test_question_reject_posts_reject_endpoint(self):
        with BlockerOpenCodeServer() as server:
            result = self.run_cli("question", "reject", "que_release_1", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "id=que_release_1 action=reject ok=true\n")
        self.assertEqual(server.requests, [("POST", "/question/que_release_1/reject", {})])

    def test_missing_question_reject_reports_request_not_found(self):
        with BlockerOpenCodeServer(missing_paths={"/question/que_missing/reject"}) as server:
            result = self.run_cli("question", "reject", "que_missing", "--server", server.url)

        self.assertEqual(result.returncode, 66)
        self.assertEqual(result.stdout, "")
        self.assertIn("question request not found: que_missing", result.stderr)
        self.assertEqual(server.requests, [("POST", "/question/que_missing/reject", {})])

    def test_inspect_can_include_blocker_counts(self):
        sessions = [{"id": "ses_build", "directory": "/tmp/project"}]
        permissions = [
            {"id": "per_build", "sessionID": "ses_build", "permission": "bash", "patterns": [], "always": [], "metadata": {}},
            {"id": "per_other", "sessionID": "ses_other", "permission": "edit", "patterns": [], "always": [], "metadata": {}},
        ]
        questions = [{"id": "que_build", "sessionID": "ses_build", "questions": []}]

        with BlockerOpenCodeServer(sessions=sessions, permissions=permissions, questions=questions) as server:
            result = self.run_cli("inspect", "ses_build", "--blockers", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "id=ses_build title=- dir=/tmp/project agent=- model=- cost=- tokens=- created=- updated=- "
            "permissions=1 questions=1 blockers=2\n",
        )
        self.assertEqual(
            server.requests,
            [("GET", "/api/session/ses_build", None), ("GET", "/permission", None), ("GET", "/question", None)],
        )

    def test_question_list_filters_pending_questions_by_session(self):
        questions = [
            {
                "id": "que_build",
                "sessionID": "ses_build",
                "questions": [{"question": "Run the slower integration tests?", "header": "Tests", "options": []}],
            },
            {
                "id": "que_plan",
                "sessionID": "ses_plan",
                "questions": [{"question": "Which design should we pick?", "header": "Design", "options": []}],
            },
        ]

        with BlockerOpenCodeServer(questions=questions) as server:
            result = self.run_cli("question", "list", "--session", "ses_build", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            'id=que_build session=ses_build questions=1 headers=Tests '
            'question="Run the slower integration tests?" tool=-\n',
        )
        self.assertEqual(server.requests, [("GET", "/question", None)])


if __name__ == "__main__":
    unittest.main()
