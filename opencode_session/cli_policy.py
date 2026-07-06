import os

from opencode_session.run_record import DEFAULT_SERVER_URL
from opencode_session.status import short_status
from opencode_session.worker_state import (
    WORKER_STATUS_ABORTED,
    WORKER_STATUS_BLOCKED,
    WORKER_STATUS_DONE,
    WORKER_STATUS_FAILED,
    WORKER_STATUS_TIMEOUT,
    has_partial_worker_success,
)


__all__ = [
    "CLI_NAME",
    "DEFAULT_SERVER_URL",
    "EX_ABORTED",
    "EX_BLOCKED",
    "EX_DATAERR",
    "EX_NOINPUT",
    "EX_PARTIAL",
    "EX_TIMEOUT",
    "EX_UNAVAILABLE",
    "EX_UNSUPPORTED",
    "EX_USAGE",
    "WORKER_EXIT_CODE_BY_STATUS",
    "exit_code_for_run",
    "exit_code_for_status",
    "server_default",
]


CLI_NAME = "ocs"
EX_USAGE = 64
EX_DATAERR = 65
EX_NOINPUT = 66
EX_UNAVAILABLE = 69
EX_UNSUPPORTED = 70
EX_TIMEOUT = 124
EX_PARTIAL = 1
EX_BLOCKED = 75
EX_ABORTED = 130

WORKER_EXIT_CODE_BY_STATUS = {
    WORKER_STATUS_BLOCKED: EX_BLOCKED,
    WORKER_STATUS_DONE: 0,
    WORKER_STATUS_FAILED: EX_UNAVAILABLE,
    WORKER_STATUS_TIMEOUT: EX_TIMEOUT,
    WORKER_STATUS_ABORTED: EX_ABORTED,
}


def exit_code_for_status(status, *, partial_success=False):
    status = short_status(status)
    if status == WORKER_STATUS_FAILED and partial_success:
        return EX_PARTIAL
    return WORKER_EXIT_CODE_BY_STATUS.get(status, EX_UNAVAILABLE)


def exit_code_for_run(run):
    return exit_code_for_status(run.get("status"), partial_success=has_partial_worker_success(run))


def server_default(env=None):
    if env is None:
        env = os.environ
    return env.get("OPENCODE_SERVER_URL") or env.get("OPENCODE_SERVER") or DEFAULT_SERVER_URL
