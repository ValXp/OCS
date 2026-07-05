from opencode_session.worker_model import (
    BLOCKED_WORKER_STATUS,
    FAILED_DEPENDENCY_STATUSES,
    TERMINAL_WORKER_STATUSES,
    is_blocked_status as _is_blocked_status,
    is_dependency_blockable_status as _is_dependency_blockable_status,
    is_failed_dependency_status as _is_failed_dependency_status,
    is_runnable_status as _is_runnable_status,
    is_terminal_status as _is_terminal_status,
)


def is_blocked_status(status):
    return _is_blocked_status(status)


def is_terminal_status(status):
    return _is_terminal_status(status)


def is_runnable_status(status):
    return _is_runnable_status(status)


def is_dependency_blockable_status(status):
    return _is_dependency_blockable_status(status)


def is_failed_dependency_status(status):
    return _is_failed_dependency_status(status)
