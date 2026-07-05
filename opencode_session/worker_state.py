from copy import deepcopy
from dataclasses import dataclass, field

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
from opencode_session.worker_model import next_eligible_worker_action, worker_retry_available


WORKER_LIST_FIELDS = (
    "dependencies",
    "prompt_ids",
    "retryable_failures",
    "blockers",
    "output_refs",
)

WORKER_STATE_TRANSITION_FIELDS = (
    "status",
    "retry_count",
    "timeout_started_at",
    "timed_out_at",
    "failure_category",
    "failure_reason",
    "last_failure_category",
    "last_failure_reason",
    "next_eligible_action",
    "blockers",
    "output_refs",
    "error",
    "failure_retryable",
    "manual_retry_required",
    "result",
    "cleanup",
    "abort",
)
WORKER_SET_IF_MISSING_TRANSITION_FIELDS = ("session_id",)
REMOVABLE_WORKER_TRANSITION_FIELDS = ("error", "failure_retryable", "manual_retry_required")


@dataclass(frozen=True)
class WorkerTransition:
    worker_id: str
    set_fields: dict = field(default_factory=dict)
    delete_fields: tuple = ()
    set_if_missing_fields: dict = field(default_factory=dict)
    merge_unique_fields: dict = field(default_factory=dict)
    preserve_accepted_abort: bool = True

    @classmethod
    def from_worker_state_update(cls, worker):
        worker_id = worker["id"]
        set_fields = {"id": worker_id}
        for field_name in WORKER_STATE_TRANSITION_FIELDS:
            if field_name in worker:
                set_fields[field_name] = deepcopy(worker[field_name])
        merge_unique_fields = {}
        prompt_ids = worker.get("prompt_ids")
        if isinstance(prompt_ids, list):
            merge_unique_fields["prompt_ids"] = tuple(prompt_ids)
        set_if_missing_fields = {
            field_name: deepcopy(worker[field_name])
            for field_name in WORKER_SET_IF_MISSING_TRANSITION_FIELDS
            if worker.get(field_name)
        }
        return cls(
            worker_id,
            set_fields=set_fields,
            delete_fields=tuple(
                field_name for field_name in REMOVABLE_WORKER_TRANSITION_FIELDS if field_name not in worker
            ),
            set_if_missing_fields=set_if_missing_fields,
            merge_unique_fields=merge_unique_fields,
        )

    @classmethod
    def dependency_blocked(cls, worker_id, blockers):
        return cls(
            worker_id,
            set_fields={
                "id": worker_id,
                "status": "blocked",
                "blockers": list(blockers),
                "next_eligible_action": "resolve_blocker",
            },
        )

    def apply_to(self, latest_workers):
        latest_worker = latest_workers.get(self.worker_id)
        if self.preserve_accepted_abort and _accepted_abort(latest_worker) and not _accepted_abort(self.set_fields):
            merged = self._apply_to_aborted_worker(latest_worker)
        else:
            merged = self._apply_to_worker(latest_worker)
        latest_workers[self.worker_id] = merged
        return merged

    def _apply_to_worker(self, latest_worker):
        merged = deepcopy(latest_worker) if isinstance(latest_worker, dict) else {}
        merged.update(deepcopy(self.set_fields))
        for field_name in self.delete_fields:
            merged.pop(field_name, None)
        self._merge_unique_fields(merged, latest_worker)
        self._set_missing_fields(merged)
        if "abort" not in self.set_fields and isinstance(latest_worker, dict) and "abort" in latest_worker:
            merged["abort"] = deepcopy(latest_worker["abort"])
        return merged

    def _apply_to_aborted_worker(self, latest_worker):
        merged = deepcopy(latest_worker)
        self._merge_unique_fields(merged, latest_worker)
        if "cleanup" in self.set_fields:
            merged["cleanup"] = deepcopy(self.set_fields["cleanup"])
        return merged

    def _set_missing_fields(self, target):
        for field_name, value in self.set_if_missing_fields.items():
            if not target.get(field_name):
                target[field_name] = deepcopy(value)

    def _merge_unique_fields(self, target, latest_worker):
        for field_name, values in self.merge_unique_fields.items():
            source_worker = latest_worker
            if field_name == "prompt_ids" and _latest_prompt_ids_are_retry_marker(latest_worker):
                source_worker = {}
            _merge_unique_list_field(target, source_worker, {field_name: list(values)}, field_name)


def _latest_prompt_ids_are_retry_marker(latest_worker):
    return (
        isinstance(latest_worker, dict)
        and latest_worker.get("next_eligible_action") == "retry"
        and latest_worker.get("last_failure_category") is not None
    )


def _accepted_abort(worker):
    abort = worker.get("abort") if isinstance(worker, dict) else None
    return isinstance(abort, dict) and abort.get("accepted") and worker.get("status") == "aborted"


def _merge_unique_list_field(target, latest_worker, worker_record, field_name):
    merged_values = []
    for source in (latest_worker, worker_record):
        values = source.get(field_name) if isinstance(source, dict) else None
        if not isinstance(values, list):
            continue
        for value in values:
            if value not in merged_values:
                merged_values.append(deepcopy(value))
    target[field_name] = merged_values


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
    return next_eligible_worker_action(worker)


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
