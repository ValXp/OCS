from opencode_session.api_client import OpenCodeApiError


EX_UNAVAILABLE = 69
EX_UNSUPPORTED = 70
EX_TIMEOUT = 124
EX_PARTIAL = 1
EX_BLOCKED = 75
EX_ABORTED = 130


def ensure_worker(run, worker_id, *, role):
    workers = run.setdefault("workers", {})
    worker = workers.get(worker_id)
    if not isinstance(worker, dict):
        worker = {}
    worker.setdefault("id", worker_id)
    worker.setdefault("role", role)
    worker.setdefault("session_id", None)
    worker.setdefault("agent", None)
    worker.setdefault("model", None)
    worker.setdefault("dependencies", [])
    worker.setdefault("prompt_ids", [])
    worker.setdefault("retry_count", 0)
    worker.setdefault("retry_limit", 0)
    worker.setdefault("retryable_failures", [])
    worker.setdefault("timeout_seconds", None)
    worker.setdefault("timeout_policy", "timeout")
    worker.setdefault("timeout_started_at", None)
    worker.setdefault("timed_out_at", None)
    worker.setdefault("failure_category", None)
    worker.setdefault("failure_reason", None)
    worker.setdefault("last_failure_category", None)
    worker.setdefault("last_failure_reason", None)
    worker.setdefault("next_eligible_action", "start")
    worker.setdefault("blockers", [])
    worker.setdefault("output_refs", [])
    if not worker.get("role"):
        worker["role"] = role
    worker["id"] = worker_id
    workers[worker_id] = worker
    return worker


def mark_worker_failed(worker, category, reason):
    worker["status"] = "failed"
    worker["error"] = reason
    worker["failure_category"] = category
    worker["failure_reason"] = reason
    worker["last_failure_category"] = category
    worker["last_failure_reason"] = reason
    worker["next_eligible_action"] = "retry" if worker_retry_available(worker, category) else "none"


def schedule_worker_retry(worker, category, reason):
    if not worker_retry_available(worker, category):
        return False
    worker["retry_count"] = int(worker.get("retry_count") or 0) + 1
    worker["status"] = "active"
    worker["failure_category"] = None
    worker["failure_reason"] = None
    worker["last_failure_category"] = category
    worker["last_failure_reason"] = reason
    worker["next_eligible_action"] = "retry"
    return True


def worker_retry_available(worker, category):
    retryable = set(worker.get("retryable_failures") or [])
    if category not in retryable and "all" not in retryable:
        return False
    try:
        retry_count = int(worker.get("retry_count") or 0)
        retry_limit = int(worker.get("retry_limit") or 0)
    except (TypeError, ValueError):
        return False
    return retry_count < retry_limit


def create_isolated_timeout_retry_session(client, run, worker, reason, created_at, *, agent=None, model=None):
    timed_out_session_id = worker.get("session_id")
    retry_agent = agent if agent is not None else worker.get("agent")
    retry_model = model if model is not None else worker.get("model")
    create_response = client.create_session_response(run["directory"], agent=retry_agent, model=retry_model)
    retry_session_id = session_value(create_response.data, "id", "sessionID", "sessionId")
    if not retry_session_id:
        raise OpenCodeApiError("timeout retry session creation did not return a session id")
    if retry_session_id == timed_out_session_id:
        raise OpenCodeApiError(f"timeout retry session creation returned original in-flight session '{timed_out_session_id}'")
    worker["session_id"] = retry_session_id
    _timeout_retry_sessions(worker).append(
        {
            "timed_out_session_id": timed_out_session_id,
            "retry_session_id": retry_session_id,
            "reason": reason,
            "created_at": created_at,
        }
    )
    return retry_session_id


def _timeout_retry_sessions(worker):
    sessions = worker.get("timeout_retry_sessions")
    if not isinstance(sessions, list):
        sessions = []
        worker["timeout_retry_sessions"] = sessions
    return sessions


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
    else:
        worker["next_eligible_action"] = "none"


def format_timeout(timeout):
    return str(timeout)


def apply_worker_result(worker, result):
    worker["result"] = result
    worker["status"] = result["status"]
    worker["failure_category"] = None
    worker["failure_reason"] = None
    worker["next_eligible_action"] = "collect" if result["status"] == "done" else "none"
    assistant_message_id = result["message_ids"].get("assistant")
    worker["output_refs"] = [f"assistant:{assistant_message_id}"] if result["status"] == "done" and assistant_message_id else []


def refresh_run_summary(run, *, include_unprompted_when_no_prompts=False):
    workers = run.get("workers", {})
    prompted_workers = [worker for worker in workers.values() if isinstance(worker, dict) and worker_prompt(worker)]
    status_workers = prompted_workers
    if include_unprompted_when_no_prompts:
        status_workers = prompted_workers or [worker for worker in workers.values() if isinstance(worker, dict)]
    run["output_refs"] = worker_output_refs_in_dependency_order(workers)
    if not status_workers:
        return
    statuses = {worker.get("status") for worker in status_workers}
    if statuses == {"done"}:
        run["status"] = "done"
    elif any(status == "failed" for status in statuses):
        run["status"] = "failed"
    elif any(status == "aborted" for status in statuses):
        run["status"] = "aborted"
    elif any(status == "timeout" for status in statuses):
        run["status"] = "timeout"
    elif any(status == "blocked" for status in statuses):
        run["status"] = "blocked"
    elif any(status == "active" for status in statuses):
        run["status"] = "active"
    else:
        run["status"] = "queued"


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
    ordered = []
    visited = set()
    visiting = set()

    def visit(worker_id):
        if worker_id in visited or worker_id in visiting:
            return
        visiting.add(worker_id)
        worker = workers.get(worker_id)
        if isinstance(worker, dict):
            for dependency in worker.get("dependencies", []):
                visit(dependency)
            ordered.append(worker)
        visiting.remove(worker_id)
        visited.add(worker_id)

    for worker_id in sorted(workers):
        visit(worker_id)
    return ordered


def exit_code_for_run(run):
    status = run.get("status")
    if status == "done":
        return 0
    if status == "timeout":
        return EX_TIMEOUT
    if status == "blocked":
        return EX_BLOCKED
    if status == "aborted":
        return EX_ABORTED
    if has_partial_worker_success(run):
        return EX_PARTIAL
    return EX_UNAVAILABLE


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


def session_value(session, *names):
    session = session if isinstance(session, dict) else {}
    for name in names:
        value = session.get(name)
        if value is not None:
            return value
    info = session.get("info")
    if isinstance(info, dict):
        for name in names:
            value = info.get(name)
            if value is not None:
                return value
    return None
