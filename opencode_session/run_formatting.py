from opencode_session.formatting import (
    compact_list as _compact_list,
    compact_value as _compact_value,
    format_table as _format_table,
)
from opencode_session.schema_common import tokens_total as _tokens_total
from opencode_session.worker_state import normalize_worker, worker_field


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
    result = worker_field(worker, "result")
    fields = [
        ("worker", worker_field(worker, "id")),
        ("role", worker_field(worker, "role")),
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
        ("worker", worker_field(worker, "id")),
        ("role", worker_field(worker, "role")),
        ("status", worker_field(worker, "status")),
        ("session", worker_field(worker, "session_id")),
        ("agent", worker_field(worker, "agent")),
        ("model", worker_field(worker, "model")),
        ("deps", _compact_list(worker_field(worker, "dependencies"))),
        ("prompts", _compact_list(worker_field(worker, "prompt_ids"))),
        ("retries", worker_field(worker, "retry_count")),
        ("timeout", worker_field(worker, "timeout_seconds")),
        ("blockers", _compact_list(worker_field(worker, "blockers"))),
        ("outputs", _compact_list(worker_field(worker, "output_refs"))),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_worker_table(workers):
    rows = []
    for worker in workers:
        rows.append(
            [
                worker_field(worker, "id"),
                worker_field(worker, "role"),
                worker_field(worker, "status"),
                worker_field(worker, "session_id"),
                worker_field(worker, "agent"),
                worker_field(worker, "model"),
                _compact_list(worker_field(worker, "dependencies")),
                _compact_list(worker_field(worker, "prompt_ids")),
                worker_field(worker, "retry_count"),
                worker_field(worker, "timeout_seconds"),
                _compact_list(worker_field(worker, "blockers")),
                _compact_list(worker_field(worker, "output_refs")),
            ]
        )
    return _format_table(
        ["worker", "role", "status", "session", "agent", "model", "deps", "prompts", "retries", "timeout", "blockers", "outputs"],
        rows,
    )


def _worker_status_counts(workers):
    counts = {"queued": 0, "active": 0, "done": 0, "blocked": 0, "failed": 0, "aborted": 0, "timeout": 0}
    for worker_id, worker in workers.items():
        status = worker_field(normalize_worker(worker, worker_id), "status")
        if status in counts:
            counts[status] += 1
    return counts
