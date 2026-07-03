STATUS_TERMS = ("queued", "active", "blocked", "done", "failed", "aborted", "timeout")


def short_status(status):
    if status is None:
        return None
    value = str(status).strip().lower().replace("-", "_")
    if value in {"queued", "pending", "initialized", "submitted", "admitted"}:
        return "queued"
    if value in {"active", "running", "started", "promoted", "processing", "in_progress", "working", "aborting"}:
        return "active"
    if value in {"blocked", "waiting", "needs_input"}:
        return "blocked"
    if value in {"done", "complete", "completed", "success", "succeeded", "idle"}:
        return "done"
    if value in {"failed", "failure", "error", "errored"}:
        return "failed"
    if value in {"aborted", "abort", "cancelled", "canceled"}:
        return "aborted"
    if value in {"timeout", "timed_out", "timedout"}:
        return "timeout"
    return value
