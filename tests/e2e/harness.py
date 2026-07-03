import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "bin" / "ocs"
SERVER_ENV = "OCS_E2E_SERVER_URL"
TIMEOUT_ENV = "OCS_E2E_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 20.0


def require_server_url(testcase):
    server_url = os.environ.get(SERVER_ENV)
    if not server_url:
        testcase.skipTest(f"set {SERVER_ENV} to run OpenCode E2E tests")
    return server_url


def run_ocs(*args):
    command = [sys.executable, str(CLI), *args]
    try:
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_timeout_seconds(),
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(_format_timeout(error, command)) from error


def assert_success(testcase, result):
    testcase.assertEqual(result.returncode, 0, format_completed_process(result))


def load_json(testcase, result, description="CLI"):
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        testcase.fail(f"{description} did not emit valid JSON: {error}\n{format_completed_process(result)}")


def load_json_lines(testcase, result, description="CLI"):
    records = []
    for line_number, line in enumerate(result.stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            testcase.fail(
                f"{description} line {line_number} did not emit valid JSON: {error}\n"
                f"{format_completed_process(result)}"
            )
    return records


def format_completed_process(result):
    return "\n".join(
        [
            f"command: {_format_command(result.args)}",
            f"exit code: {result.returncode}",
            "stdout:",
            _format_stream(result.stdout),
            "stderr:",
            _format_stream(result.stderr),
        ]
    )


def _timeout_seconds():
    raw_timeout = os.environ.get(TIMEOUT_ENV)
    if raw_timeout is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(raw_timeout)
    except ValueError as error:
        raise AssertionError(f"{TIMEOUT_ENV} must be a positive number; got {raw_timeout!r}") from error
    if timeout <= 0:
        raise AssertionError(f"{TIMEOUT_ENV} must be a positive number; got {raw_timeout!r}")
    return timeout


def _format_timeout(error, command):
    return "\n".join(
        [
            f"command: {_format_command(command)}",
            f"exit code: timeout after {error.timeout:g}s",
            "stdout:",
            _format_stream(error.stdout),
            "stderr:",
            _format_stream(error.stderr),
        ]
    )


def _format_command(command):
    return shlex.join(str(part) for part in command)


def _format_stream(value):
    if value is None or value == "":
        return "(empty)"
    return str(value).rstrip("\n")
