import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "bin" / "ocs"
SERVER_ENV = "OCS_E2E_SERVER_URL"
LIVE_VALIDATE_ENV = "OCS_LIVE_VALIDATE"
AGENT_ENV = "OCS_E2E_AGENT"
MODEL_ENV = "OCS_E2E_MODEL"
TIMEOUT_ENV = "OCS_E2E_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_SERVER_URL = "http://127.0.0.1"


def require_server_url(testcase):
    return os.environ.get(SERVER_ENV) or DEFAULT_SERVER_URL


def require_live_server_url(testcase):
    return require_server_url(testcase)


def live_validate_selection_args():
    args = []
    agent = os.environ.get(AGENT_ENV)
    if agent:
        args.extend(["--agent", agent])
    model = os.environ.get(MODEL_ENV)
    if model:
        args.extend(["--model", model])
    return args


def run_ocs(*args, timeout_seconds=None):
    command = [sys.executable, str(CLI), *args]
    timeout = _timeout_seconds(timeout_seconds)
    env = os.environ.copy()
    env.setdefault(LIVE_VALIDATE_ENV, "1")
    try:
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
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


def create_session_fixture(testcase, server_url, directory, *, title=None, metadata=None):
    payload = {"location": {"directory": str(Path(directory).resolve())}}
    session = http_json(testcase, "POST", server_url, "api/session", payload)
    if title is not None or metadata is not None:
        session_id = _session_id(session)
        patch = {}
        if title is not None:
            patch["title"] = title
        if metadata is not None:
            patch["metadata"] = metadata
        return http_json(testcase, "PATCH", server_url, f"session/{quote(session_id, safe='')}", patch)
    return session


def delete_session_fixture(testcase, server_url, session_id, *, ignore_not_found=False):
    ignored_statuses = {404} if ignore_not_found else set()
    return http_json(
        testcase,
        "DELETE",
        server_url,
        f"session/{quote(session_id, safe='')}",
        ignored_statuses=ignored_statuses,
    )


def add_session_cleanup(testcase, server_url, session_id, *, label="session"):
    testcase.addCleanup(cleanup_session_fixture, testcase, server_url, session_id, label=label)


def cleanup_session_fixture(testcase, server_url, session_id, *, label="session"):
    try:
        delete_session_fixture(testcase, server_url, session_id, ignore_not_found=True)
    except Exception as error:
        testcase.fail(f"cleanup failed for {label} {session_id!r} at {server_url}: {error}")


def http_json(testcase, method, server_url, path, payload=None, *, ignored_statuses=None):
    ignored_statuses = ignored_statuses or set()
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(_server_url(server_url, path), data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=_timeout_seconds()) as response:
            response_body = response.read().decode("utf-8")
    except HTTPError as error:
        response_body = error.read().decode("utf-8")
        if error.code in ignored_statuses:
            return None
        testcase.fail(
            f"{method} /{path.lstrip('/')} failed: HTTP {error.code}\n"
            f"response:\n{_format_stream(response_body)}"
        )
    except URLError as error:
        testcase.fail(f"{method} /{path.lstrip('/')} could not reach {server_url}: {error.reason}")
    except TimeoutError:
        testcase.fail(f"{method} /{path.lstrip('/')} timed out after {_timeout_seconds():g}s")
    try:
        return json.loads(response_body or "{}")
    except json.JSONDecodeError as error:
        testcase.fail(
            f"{method} /{path.lstrip('/')} did not return valid JSON: {error}\n"
            f"response:\n{_format_stream(response_body)}"
        )


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


def _server_url(server_url, path):
    return urljoin(server_url.rstrip("/") + "/", path.lstrip("/"))


def _session_id(session):
    if isinstance(session, dict) and isinstance(session.get("data"), dict):
        session = session["data"]
    if isinstance(session, dict):
        for name in ("id", "sessionID", "sessionId"):
            if session.get(name):
                return session[name]
    raise AssertionError(f"session fixture response did not include a session id: {session!r}")


def _timeout_seconds(value=None):
    raw_timeout = value if value is not None else os.environ.get(TIMEOUT_ENV)
    if raw_timeout is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(raw_timeout)
    except ValueError as error:
        raise AssertionError(f"{_timeout_label(value)} must be a positive number; got {raw_timeout!r}") from error
    if timeout <= 0:
        raise AssertionError(f"{_timeout_label(value)} must be a positive number; got {raw_timeout!r}")
    return timeout


def _timeout_label(value):
    return "timeout_seconds" if value is not None else TIMEOUT_ENV


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
