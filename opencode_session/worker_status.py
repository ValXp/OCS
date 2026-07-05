BLOCKED_WORKER_STATUS = "blocked"
TERMINAL_WORKER_STATUSES = frozenset({"done", "failed", "aborted", "timeout"})
FAILED_DEPENDENCY_STATUSES = frozenset({"failed", "aborted", "timeout", BLOCKED_WORKER_STATUS})


def is_blocked_status(status):
    return status == BLOCKED_WORKER_STATUS


def is_terminal_status(status):
    return status in TERMINAL_WORKER_STATUSES


def is_runnable_status(status):
    return not is_terminal_status(status) and not is_blocked_status(status)


def is_dependency_blockable_status(status):
    return is_runnable_status(status)


def is_failed_dependency_status(status):
    return status in FAILED_DEPENDENCY_STATUSES
