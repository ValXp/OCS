import os

from opencode_session.run_record import DEFAULT_SERVER_URL
from opencode_session.status_policy import (
    EX_ABORTED,
    EX_BLOCKED,
    EX_PARTIAL,
    EX_TIMEOUT,
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
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
    "server_default",
]


CLI_NAME = "ocs"
EX_USAGE = 64
EX_DATAERR = 65
EX_NOINPUT = 66


def server_default(env=None):
    if env is None:
        env = os.environ
    return env.get("OPENCODE_SERVER_URL") or env.get("OPENCODE_SERVER") or DEFAULT_SERVER_URL
