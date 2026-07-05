from opencode_session.status import short_status
from opencode_session.status_policy import (
    EX_ABORTED,
    EX_BLOCKED,
    EX_PARTIAL,
    EX_TIMEOUT,
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
    aggregate_run_status,
    exit_code_for_status,
)
from opencode_session.worker_dependencies import analyze_worker_dependencies
from opencode_session.worker_status import is_blocked_status


WORKER_LIST_FIELDS = (
    "dependencies",
    "prompt_ids",
    "retryable_failures",
    "blockers",
    "output_refs",
)


def default_worker(worker_id):
    return {
        "id": worker_id,
        "role": None,
        "session_id": None,
        "agent": None,
        "model": None,
        "dependencies": [],
        "prompt_ids": [],
        "status": "queued",
        "retry_count": 0,
        "retry_limit": 0,
        "retryable_failures": [],
        "timeout_seconds": None,
        "timeout_policy": "timeout",
        "timeout_started_at": None,
        "timed_out_at": None,
        "failure_category": None,
        "failure_reason": None,
        "last_failure_category": None,
        "last_failure_reason": None,
        "next_eligible_action": "start",
        "blockers": [],
        "output_refs": [],
    }


def normalize_worker(worker, worker_id):
    normalized = default_worker(worker_id)
    if isinstance(worker, dict):
        normalized.update(worker)
    normalized["id"] = normalized.get("id") or worker_id
    for key in WORKER_LIST_FIELDS:
        value = normalized.get(key)
        normalized[key] = value if isinstance(value, list) else []
    if normalized.get("retry_count") is None:
        normalized["retry_count"] = 0
    if normalized.get("retry_limit") is None:
        normalized["retry_limit"] = 0
    if not normalized.get("timeout_policy"):
        normalized["timeout_policy"] = "timeout"
    if not normalized.get("status"):
        normalized["status"] = "queued"
    else:
        normalized["status"] = short_status(normalized["status"])
    normalized["next_eligible_action"] = next_eligible_action(normalized)
    return normalized


def next_eligible_action(worker):
    status = worker.get("status")
    if status == "queued":
        return "start"
    if status == "active":
        return "retry" if worker.get("next_eligible_action") == "retry" else "wait"
    if is_blocked_status(status):
        return "resolve_blocker"
    if status == "done":
        return "collect"
    if status == "timeout" and worker_retry_available(worker, "timeout"):
        return "retry"
    if status == "failed" and worker_retry_available(worker):
        return "retry"
    return "none"


def ensure_worker(run, worker_id, *, role):
    workers = run.setdefault("workers", {})
    worker = normalize_worker(workers.get(worker_id), worker_id)
    if not worker.get("role"):
        worker["role"] = role
    worker["id"] = worker_id
    workers[worker_id] = worker
    return worker


def mark_worker_active(worker, *, now=None):
    worker["status"] = "active"
    _clear_current_status_metadata(worker)
    worker["next_eligible_action"] = "wait"
    if now is not None:
        worker["timeout_started_at"] = now() if worker.get("timeout_seconds") else None


def _clear_current_status_metadata(worker):
    worker["blockers"] = []
    worker.pop("error", None)
    worker["failure_category"] = None
    worker["failure_reason"] = None
    worker.pop("failure_retryable", None)
    worker.pop("manual_retry_required", None)


def mark_worker_failed(worker, category, reason, *, retryable=True):
    worker["status"] = "failed"
    worker["error"] = reason
    worker["failure_category"] = category
    worker["failure_reason"] = reason
    worker["last_failure_category"] = category
    worker["last_failure_reason"] = reason
    if retryable:
        worker.pop("failure_retryable", None)
    else:
        worker["failure_retryable"] = False
    worker["next_eligible_action"] = "retry" if retryable and worker_retry_available(worker, category) else "none"


def mark_dependency_blocked(worker, blockers):
    worker["status"] = "blocked"
    worker["blockers"] = list(blockers)
    worker["next_eligible_action"] = "resolve_blocker"


def mark_worker_aborted(worker, abort):
    worker["abort"] = abort
    if isinstance(abort, dict) and abort.get("accepted"):
        worker["status"] = "aborted"
        worker["next_eligible_action"] = "none"


def schedule_worker_retry(worker, category, reason):
    if not worker_retry_available(worker, category):
        return False
    worker["retry_count"] = int(worker.get("retry_count") or 0) + 1
    worker["status"] = "active"
    _clear_current_status_metadata(worker)
    worker["last_failure_category"] = category
    worker["last_failure_reason"] = reason
    worker["next_eligible_action"] = "retry"
    return True


def worker_retry_available(worker, category=None):
    if worker.get("failure_retryable") is False:
        return False
    retryable = set(worker.get("retryable_failures") or [])
    if not retryable:
        return False
    if category is None:
        category = worker.get("failure_category") or worker.get("last_failure_category")
    if category and category not in retryable and "all" not in retryable:
        return False
    try:
        retry_count = int(worker.get("retry_count") or 0)
        retry_limit = int(worker.get("retry_limit") or 0)
    except (TypeError, ValueError):
        return False
    return retry_count < retry_limit


def worker_timeout_reason(worker):
    return f"worker timed out after {format_timeout(worker.get('timeout_seconds'))}s"


def mark_worker_timeout(worker, reason, now):
    status = worker.get("timeout_policy") or "timeout"
    worker["status"] = status
    worker["error"] = reason
    worker["failure_category"] = "timeout"
    worker["failure_reason"] = reason
    worker["last_failure_category"] = "timeout"
    worker["last_failure_reason"] = reason
    worker["timed_out_at"] = now()
    worker["output_refs"] = []
    if status == "blocked":
        worker["blockers"] = ["timeout"]
        worker["next_eligible_action"] = "resolve_blocker"
    elif status in {"failed", "timeout"} and worker_retry_available(worker, "timeout"):
        worker["next_eligible_action"] = "retry"
    else:
        worker["next_eligible_action"] = "none"


def format_timeout(timeout):
    return str(timeout)


def apply_worker_result(worker, result):
    worker["result"] = result
    worker["status"] = result["status"]
    if result["status"] == "done":
        _clear_current_status_metadata(worker)
    else:
        worker["failure_category"] = None
        worker["failure_reason"] = None
    worker["next_eligible_action"] = "collect" if result["status"] == "done" else "none"
    assistant_message_id = result["message_ids"].get("assistant")
    worker["output_refs"] = [f"assistant:{assistant_message_id}"] if result["status"] == "done" and assistant_message_id else []


def refresh_run_summary(run, *, include_unprompted_when_no_prompts=False):
    workers = run.get("workers", {})
    run["output_refs"] = worker_output_refs_in_dependency_order(workers)
    status = run_status_from_workers(
        workers,
        include_unprompted_when_no_prompts=include_unprompted_when_no_prompts,
    )
    if status is not None:
        run["status"] = status


def run_status_from_workers(workers, *, include_unprompted_when_no_prompts=False):
    prompted_workers = [worker for worker in workers.values() if isinstance(worker, dict) and worker_prompt(worker)]
    status_workers = prompted_workers
    if include_unprompted_when_no_prompts:
        status_workers = prompted_workers or [worker for worker in workers.values() if isinstance(worker, dict)]
    return aggregate_run_status(worker.get("status") for worker in status_workers)


def worker_output_refs_in_dependency_order(workers):
    ordered = []
    for worker in workers_in_dependency_order(workers):
        worker_id = worker.get("id")
        if worker.get("status") != "done":
            continue
        for output_ref in worker.get("output_refs", []):
            if isinstance(output_ref, str) and output_ref.startswith("assistant:"):
                ordered.append(f"{worker_id}:{output_ref.split(':', 1)[1]}")
            else:
                ordered.append(f"{worker_id}:{output_ref}")
    return ordered


def workers_in_dependency_order(workers):
    analysis = analyze_worker_dependencies(workers)
    return [workers[worker_id] for worker_id in analysis.worker_ids_in_dependency_order]


def exit_code_for_run(run):
    return exit_code_for_status(run.get("status"), partial_success=has_partial_worker_success(run))


def has_partial_worker_success(run):
    workers = [worker for worker in (run.get("workers") or {}).values() if isinstance(worker, dict) and worker_prompt(worker)]
    if not workers:
        return False
    statuses = {worker.get("status") for worker in workers}
    return "done" in statuses and any(status in {"failed", "blocked", "aborted", "timeout"} for status in statuses)


def worker_prompt(worker):
    prompt = worker.get("prompt")
    if prompt is None:
        return None
    return str(prompt)
