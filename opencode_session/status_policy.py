from opencode_session.status import short_status


EX_UNAVAILABLE = 69
EX_UNSUPPORTED = 70
EX_TIMEOUT = 124
EX_PARTIAL = 1
EX_BLOCKED = 75
EX_ABORTED = 130

_STATUS_PRIORITY = {
    "queued": 0,
    "active": 1,
    "blocked": 2,
    "done": 3,
    "timeout": 4,
    "aborted": 5,
    "failed": 6,
}
_EXIT_CODE_BY_STATUS = {
    "done": 0,
    "timeout": EX_TIMEOUT,
    "blocked": EX_BLOCKED,
    "aborted": EX_ABORTED,
    "failed": EX_UNAVAILABLE,
}


def status_priority(status):
    return _STATUS_PRIORITY.get(short_status(status), _STATUS_PRIORITY["queued"])


def merge_status(incoming, current):
    if not isinstance(incoming, str) or not isinstance(current, str):
        return incoming
    return current if status_priority(current) > status_priority(incoming) else incoming


def status_owner(incoming_status, current_status):
    if not isinstance(incoming_status, str) or not isinstance(current_status, str):
        return "incoming"
    return "current" if status_priority(current_status) > status_priority(incoming_status) else "incoming"


def aggregate_run_status(statuses):
    statuses = [short_status(status) for status in statuses]
    if not statuses:
        return None
    if statuses == ["done"] or all(status == "done" for status in statuses):
        return "done"
    candidates = [status for status in statuses if status != "done"]
    status = max(candidates, key=status_priority)
    if status not in _STATUS_PRIORITY:
        return "queued"
    return status


def exit_code_for_status(status, *, partial_success=False):
    status = short_status(status)
    if status == "failed" and partial_success:
        return EX_PARTIAL
    return _EXIT_CODE_BY_STATUS.get(status, EX_UNAVAILABLE)
