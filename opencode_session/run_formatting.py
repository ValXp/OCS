from opencode_session.formatting import (
    compact_list as _compact_list,
    compact_value as _compact_value,
    format_table as _format_table,
)
from opencode_session.records import tokens_total as _tokens_total
from opencode_session.status import short_status
from opencode_session.worker_state import normalize_worker


def format_run_compact(run):
    workers = run.get("workers") or {}
    counts = _worker_status_counts(workers)
    fields = [
        ("run", run.get("name")),
        ("status", run.get("status")),
        ("dir", run.get("directory")),
        ("server", run.get("server_url")),
        ("workers", len(workers)),
        ("queued", counts["queued"]),
        ("active", counts["active"]),
        ("done", counts["done"]),
        ("blocked", counts["blocked"]),
        ("failed", counts["failed"]),
        ("aborted", counts["aborted"]),
        ("timeout", counts["timeout"]),
        ("retries", run.get("retry_count")),
        ("timeout_s", run.get("timeout_seconds")),
        ("blockers", _compact_list(run.get("blockers"))),
        ("outputs", _compact_list(run.get("output_refs"))),
    ]
    lines = [" ".join(f"{key}={_compact_value(value)}" for key, value in fields)]
    worker_records = [normalize_worker(workers[worker_id], worker_id) for worker_id in sorted(workers)]
    if len(worker_records) > 1:
        lines.append(_format_worker_table(worker_records))
    elif worker_records:
        lines.append(_format_worker_compact(worker_records[0]))
    return "\n".join(lines)


def format_worker_result_compact(worker):
    result = worker["result"]
    fields = [
        ("worker", worker.get("id")),
        ("role", worker.get("role")),
        ("session", result["session_id"]),
        ("status", result["status"]),
        ("user", result["message_ids"]["user"]),
        ("assistant", result["message_ids"]["assistant"]),
        ("cost", result["cost"]),
        ("tokens", _tokens_total(result["tokens"])),
        ("text", result["text"]),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_worker_compact(worker):
    fields = [
        ("worker", worker.get("id")),
        ("role", worker.get("role")),
        ("status", worker.get("status")),
        ("session", worker.get("session_id")),
        ("agent", worker.get("agent")),
        ("model", worker.get("model")),
        ("deps", _compact_list(worker.get("dependencies"))),
        ("prompts", _compact_list(worker.get("prompt_ids"))),
        ("retries", worker.get("retry_count")),
        ("timeout", worker.get("timeout_seconds")),
        ("blockers", _compact_list(worker.get("blockers"))),
        ("outputs", _compact_list(worker.get("output_refs"))),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_worker_table(workers):
    rows = []
    for worker in workers:
        rows.append(
            [
                worker.get("id"),
                worker.get("role"),
                worker.get("status"),
                worker.get("session_id"),
                worker.get("agent"),
                worker.get("model"),
                _compact_list(worker.get("dependencies")),
                _compact_list(worker.get("prompt_ids")),
                worker.get("retry_count"),
                worker.get("timeout_seconds"),
                _compact_list(worker.get("blockers")),
                _compact_list(worker.get("output_refs")),
            ]
        )
    return _format_table(
        ["worker", "role", "status", "session", "agent", "model", "deps", "prompts", "retries", "timeout", "blockers", "outputs"],
        rows,
    )


def _worker_status_counts(workers):
    counts = {"queued": 0, "active": 0, "done": 0, "blocked": 0, "failed": 0, "aborted": 0, "timeout": 0}
    for worker in workers.values():
        status = short_status(worker.get("status")) if isinstance(worker, dict) else None
        if status in counts:
            counts[status] += 1
    return counts
