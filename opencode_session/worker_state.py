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
from opencode_session.worker_model import (
    WORKER_ACTION_COLLECT,
    WORKER_ACTION_NONE,
    WORKER_ACTION_RESOLVE_BLOCKER,
    WORKER_ACTION_RETRY,
    WORKER_ACTION_START,
    WORKER_ACTION_WAIT,
    WORKER_LIFECYCLE_ABORTED,
    WORKER_LIFECYCLE_ACTIVE_RETRY,
    WORKER_LIFECYCLE_ACTIVE_WAIT,
    WORKER_LIFECYCLE_BLOCKED_DEPENDENCY,
    WORKER_LIFECYCLE_BLOCKED_TIMEOUT,
    WORKER_LIFECYCLE_DONE_COLLECT,
    WORKER_LIFECYCLE_FAILED_RETRY,
    WORKER_LIFECYCLE_FAILED_TERMINAL,
    WORKER_LIFECYCLE_QUEUED,
    WORKER_LIFECYCLE_TIMEOUT_ABORTED,
    WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY,
    WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL,
    WORKER_LIFECYCLE_TIMEOUT_RETRY,
    WORKER_LIFECYCLE_TIMEOUT_TERMINAL,
    WORKER_STATUS_ABORTED,
    WORKER_STATUS_ACTIVE,
    WORKER_STATUS_BLOCKED,
    WORKER_STATUS_DONE,
    WORKER_STATUS_FAILED,
    WORKER_STATUS_QUEUED,
    WORKER_STATUS_TIMEOUT,
    next_eligible_worker_action,
    public_worker_state_fields,
    worker_lifecycle_state,
    worker_retry_available,
)


WORKER_LIST_FIELDS = (
    "dependencies",
    "prompt_ids",
    "retryable_failures",
    "blockers",
    "output_refs",
)

REMOVABLE_WORKER_TRANSITION_FIELDS = ("error", "failure_retryable", "manual_retry_required")
_UNSET = object()


@dataclass(frozen=True)
class WorkerTransition:
    """Explicit patch produced by a worker lifecycle reducer."""

    worker_id: str
    set_fields: dict = field(default_factory=dict)
    delete_fields: tuple = ()
    set_if_missing_fields: dict = field(default_factory=dict)
    merge_unique_fields: dict = field(default_factory=dict)
    preserve_accepted_abort: bool = True
    replace_worker: bool = False

    @classmethod
    def provisioned(cls, worker):
        worker_id = worker["id"]
        set_fields = {"id": worker_id}
        for field_name in ("agent", "model"):
            if worker.get(field_name) is not None:
                set_fields[field_name] = deepcopy(worker[field_name])
        set_if_missing_fields = {}
        if worker.get("session_id"):
            set_if_missing_fields["session_id"] = deepcopy(worker["session_id"])
        return cls(worker_id, set_fields=set_fields, set_if_missing_fields=set_if_missing_fields)

    @classmethod
    def active(cls, worker_id, *, timeout_started_at=_UNSET):
        return reduce_worker_lifecycle(worker_id, "active", timeout_started_at=timeout_started_at)

    @classmethod
    def failed(
        cls,
        worker_id,
        category,
        reason,
        *,
        retryable=True,
        retry_available=False,
        timeout_started_at=_UNSET,
        prompt_ids=(),
    ):
        return reduce_worker_lifecycle(
            worker_id,
            "failed",
            category=category,
            reason=reason,
            retryable=retryable,
            retry_available=retry_available,
            timeout_started_at=timeout_started_at,
            prompt_ids=prompt_ids,
        )

    @classmethod
    def dependency_blocked(cls, worker_id, blockers):
        return reduce_worker_lifecycle(worker_id, "dependency_blocked", blockers=blockers)

    @classmethod
    def aborted(cls, worker_id, abort):
        return reduce_worker_lifecycle(worker_id, "aborted", abort=abort)

    @classmethod
    def retry_scheduled(cls, worker_id, category, reason, *, retry_count, timeout_started_at=_UNSET, prompt_ids=()):
        return reduce_worker_lifecycle(
            worker_id,
            "retry_scheduled",
            category=category,
            reason=reason,
            retry_count=retry_count,
            timeout_started_at=timeout_started_at,
            prompt_ids=prompt_ids,
        )

    @classmethod
    def timed_out(
        cls,
        worker_id,
        reason,
        *,
        status,
        timed_out_at,
        retry_available=False,
        manual_retry_required=False,
        timeout_started_at=_UNSET,
    ):
        return reduce_worker_lifecycle(
            worker_id,
            "timed_out",
            reason=reason,
            status=status,
            timed_out_at=timed_out_at,
            retry_available=retry_available,
            manual_retry_required=manual_retry_required,
            timeout_started_at=timeout_started_at,
        )

    @classmethod
    def result_applied(cls, worker_id, result, *, prompt_ids=(), timeout_started_at=_UNSET):
        return reduce_worker_lifecycle(
            worker_id,
            "result_applied",
            result=result,
            prompt_ids=prompt_ids,
            timeout_started_at=timeout_started_at,
        )

    @classmethod
    def cleanup_updated(cls, worker):
        return cls(worker["id"], set_fields={"id": worker["id"], "cleanup": deepcopy(worker.get("cleanup"))})

    def apply_to(self, latest_workers):
        latest_worker = latest_workers.get(self.worker_id)
        if self.preserve_accepted_abort and _accepted_abort(latest_worker) and not _accepted_abort(self.set_fields):
            merged = self._apply_to_aborted_worker(latest_worker)
        else:
            merged = self._apply_to_worker(latest_worker)
        latest_workers[self.worker_id] = merged
        return merged

    def apply_to_worker(self, worker):
        if self.preserve_accepted_abort and _accepted_abort(worker) and not _accepted_abort(self.set_fields):
            merged = self._apply_to_aborted_worker(worker)
        else:
            merged = self._apply_to_worker(worker)
        worker.clear()
        worker.update(merged)
        return worker

    def _apply_to_worker(self, latest_worker):
        merged = {} if self.replace_worker else deepcopy(latest_worker) if isinstance(latest_worker, dict) else {}
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


def reduce_worker_lifecycle(worker_id, event, **data):
    """Create the only lifecycle patch shape that may set public status/action."""
    if event == "active":
        set_fields, delete_fields = _cleared_current_status_patch()
        set_fields.update(_lifecycle_set_fields(worker_id, WORKER_LIFECYCLE_ACTIVE_WAIT))
        _set_if_not_unset(set_fields, "timeout_started_at", data.get("timeout_started_at", _UNSET))
        return WorkerTransition(worker_id, set_fields=set_fields, delete_fields=delete_fields)

    if event == "failed":
        retryable = data.get("retryable", True)
        retry_available = data.get("retry_available", False)
        category = data["category"]
        reason = data["reason"]
        lifecycle_state = WORKER_LIFECYCLE_FAILED_RETRY if retryable and retry_available else WORKER_LIFECYCLE_FAILED_TERMINAL
        set_fields = _lifecycle_set_fields(worker_id, lifecycle_state)
        set_fields.update(
            {
                "error": reason,
                "failure_category": category,
                "failure_reason": reason,
                "last_failure_category": category,
                "last_failure_reason": reason,
            }
        )
        _set_if_not_unset(set_fields, "timeout_started_at", data.get("timeout_started_at", _UNSET))
        delete_fields = ["manual_retry_required"]
        if retryable:
            delete_fields.append("failure_retryable")
        else:
            set_fields["failure_retryable"] = False
        return WorkerTransition(
            worker_id,
            set_fields=set_fields,
            delete_fields=tuple(delete_fields),
            merge_unique_fields=_prompt_ids_merge(data.get("prompt_ids", ())),
        )

    if event == "dependency_blocked":
        set_fields = _lifecycle_set_fields(worker_id, WORKER_LIFECYCLE_BLOCKED_DEPENDENCY)
        set_fields["blockers"] = list(data["blockers"])
        return WorkerTransition(worker_id, set_fields=set_fields)

    if event == "aborted":
        abort = data.get("abort")
        set_fields = {"id": worker_id, "abort": deepcopy(abort)}
        if isinstance(abort, dict) and abort.get("accepted"):
            set_fields.update(public_worker_state_fields(WORKER_LIFECYCLE_ABORTED))
        return WorkerTransition(worker_id, set_fields=set_fields)

    if event == "retry_scheduled":
        set_fields, delete_fields = _cleared_current_status_patch()
        set_fields.update(_lifecycle_set_fields(worker_id, WORKER_LIFECYCLE_ACTIVE_RETRY))
        set_fields.update(
            {
                "retry_count": data["retry_count"],
                "last_failure_category": data["category"],
                "last_failure_reason": data["reason"],
            }
        )
        _set_if_not_unset(set_fields, "timeout_started_at", data.get("timeout_started_at", _UNSET))
        return WorkerTransition(
            worker_id,
            set_fields=set_fields,
            delete_fields=delete_fields,
            merge_unique_fields=_prompt_ids_merge(data.get("prompt_ids", ())),
        )

    if event == "timed_out":
        status = data["status"]
        retry_available = data.get("retry_available", False)
        lifecycle_state = _timeout_lifecycle_state(status, retry_available)
        reason = data["reason"]
        set_fields = _lifecycle_set_fields(worker_id, lifecycle_state)
        set_fields.update(
            {
                "error": reason,
                "failure_category": WORKER_STATUS_TIMEOUT,
                "failure_reason": reason,
                "last_failure_category": WORKER_STATUS_TIMEOUT,
                "last_failure_reason": reason,
                "timed_out_at": data["timed_out_at"],
                "output_refs": [],
            }
        )
        if status == WORKER_STATUS_BLOCKED:
            set_fields["blockers"] = [WORKER_STATUS_TIMEOUT]
        _set_if_not_unset(set_fields, "timeout_started_at", data.get("timeout_started_at", _UNSET))
        delete_fields = []
        if data.get("manual_retry_required", False):
            set_fields["manual_retry_required"] = True
        else:
            delete_fields.append("manual_retry_required")
        return WorkerTransition(worker_id, set_fields=set_fields, delete_fields=tuple(delete_fields))

    if event == "result_applied":
        result = data["result"]
        status = short_status(result["status"])
        lifecycle_state = _result_lifecycle_state(status)
        set_fields = _lifecycle_set_fields(worker_id, lifecycle_state)
        set_fields["result"] = deepcopy(result)
        _set_if_not_unset(set_fields, "timeout_started_at", data.get("timeout_started_at", _UNSET))
        delete_fields = ()
        if status == WORKER_STATUS_DONE:
            clear_fields, delete_fields = _cleared_current_status_patch()
            set_fields.update(clear_fields)
            assistant_message_id = result["message_ids"].get("assistant")
            set_fields["output_refs"] = [f"assistant:{assistant_message_id}"] if assistant_message_id else []
        else:
            set_fields["failure_category"] = None
            set_fields["failure_reason"] = None
        return WorkerTransition(
            worker_id,
            set_fields=set_fields,
            delete_fields=delete_fields,
            merge_unique_fields=_prompt_ids_merge(data.get("prompt_ids", ())),
        )

    raise ValueError(f"unsupported worker lifecycle event: {event}")


def _lifecycle_set_fields(worker_id, lifecycle_state):
    fields = {"id": worker_id}
    fields.update(public_worker_state_fields(lifecycle_state))
    return fields


def _set_if_not_unset(fields, name, value):
    if value is not _UNSET:
        fields[name] = value


def _timeout_lifecycle_state(status, retry_available):
    if status == WORKER_STATUS_BLOCKED:
        return WORKER_LIFECYCLE_BLOCKED_TIMEOUT
    if status == WORKER_STATUS_FAILED:
        return WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY if retry_available else WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL
    if status == WORKER_STATUS_ABORTED:
        return WORKER_LIFECYCLE_TIMEOUT_ABORTED
    return WORKER_LIFECYCLE_TIMEOUT_RETRY if retry_available else WORKER_LIFECYCLE_TIMEOUT_TERMINAL


def _result_lifecycle_state(status):
    if status == WORKER_STATUS_DONE:
        return WORKER_LIFECYCLE_DONE_COLLECT
    if status == WORKER_STATUS_ABORTED:
        return WORKER_LIFECYCLE_ABORTED
    if status == WORKER_STATUS_TIMEOUT:
        return WORKER_LIFECYCLE_TIMEOUT_TERMINAL
    if status == WORKER_STATUS_BLOCKED:
        return WORKER_LIFECYCLE_BLOCKED_DEPENDENCY
    return WORKER_LIFECYCLE_FAILED_TERMINAL


def _latest_prompt_ids_are_retry_marker(latest_worker):
    return (
        isinstance(latest_worker, dict)
        and worker_lifecycle_state(latest_worker) == WORKER_LIFECYCLE_ACTIVE_RETRY
        and latest_worker.get("last_failure_category") is not None
    )


def _accepted_abort(worker):
    abort = worker.get("abort") if isinstance(worker, dict) else None
    return isinstance(abort, dict) and abort.get("accepted") and worker.get("status") == WORKER_STATUS_ABORTED


def _cleared_current_status_patch():
    return {
        "blockers": [],
        "failure_category": None,
        "failure_reason": None,
    }, REMOVABLE_WORKER_TRANSITION_FIELDS


def _prompt_ids_merge(prompt_ids):
    prompt_ids = tuple(prompt_id for prompt_id in prompt_ids if prompt_id is not None)
    return {"prompt_ids": prompt_ids} if prompt_ids else {}


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
        "status": WORKER_STATUS_QUEUED,
        "retry_count": 0,
        "retry_limit": 0,
        "retryable_failures": [],
        "timeout_seconds": None,
        "timeout_policy": WORKER_STATUS_TIMEOUT,
        "timeout_started_at": None,
        "timed_out_at": None,
        "lifecycle_state": WORKER_LIFECYCLE_QUEUED,
        "failure_category": None,
        "failure_reason": None,
        "last_failure_category": None,
        "last_failure_reason": None,
        "next_eligible_action": WORKER_ACTION_START,
        "blockers": [],
        "output_refs": [],
    }


def normalize_worker(worker, worker_id):
    normalized = default_worker(worker_id)
    has_lifecycle_state = isinstance(worker, dict) and worker.get("lifecycle_state")
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
        normalized["timeout_policy"] = WORKER_STATUS_TIMEOUT
    if not normalized.get("status"):
        normalized["status"] = WORKER_STATUS_QUEUED
    else:
        normalized["status"] = short_status(normalized["status"])
    lifecycle_source = dict(normalized)
    if not has_lifecycle_state:
        lifecycle_source.pop("lifecycle_state", None)
    normalized.update(public_worker_state_fields(worker_lifecycle_state(lifecycle_source)))
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
    timeout_started_at = _UNSET
    if now is not None:
        timeout_started_at = now() if worker.get("timeout_seconds") else None
    transition = WorkerTransition.active(_worker_id(worker), timeout_started_at=timeout_started_at)
    transition.apply_to_worker(worker)
    return transition


def _clear_current_status_metadata(worker):
    set_fields, delete_fields = _cleared_current_status_patch()
    worker.update(set_fields)
    for field_name in delete_fields:
        worker.pop(field_name, None)


def mark_worker_failed(worker, category, reason, *, retryable=True, prompt_ids=()):
    transition = WorkerTransition.failed(
        _worker_id(worker),
        category,
        reason,
        retryable=retryable,
        retry_available=worker_retry_available(worker, category),
        timeout_started_at=_existing_or_unset(worker, "timeout_started_at"),
        prompt_ids=prompt_ids,
    )
    transition.apply_to_worker(worker)
    return transition


def mark_dependency_blocked(worker, blockers):
    transition = WorkerTransition.dependency_blocked(_worker_id(worker), blockers)
    transition.apply_to_worker(worker)
    return transition


def mark_worker_aborted(worker, abort):
    transition = WorkerTransition.aborted(_worker_id(worker), abort)
    transition.apply_to_worker(worker)
    return transition


def schedule_worker_retry(worker, category, reason, *, prompt_ids=()):
    if not worker_retry_available(worker, category):
        return False
    transition = WorkerTransition.retry_scheduled(
        _worker_id(worker),
        category,
        reason,
        retry_count=int(worker.get("retry_count") or 0) + 1,
        timeout_started_at=_existing_or_unset(worker, "timeout_started_at"),
        prompt_ids=prompt_ids,
    )
    transition.apply_to_worker(worker)
    return transition


def worker_timeout_reason(worker):
    return f"worker timed out after {format_timeout(worker.get('timeout_seconds'))}s"


def mark_worker_timeout(worker, reason, now, *, manual_retry_required=False):
    status = worker.get("timeout_policy") or WORKER_STATUS_TIMEOUT
    transition = WorkerTransition.timed_out(
        _worker_id(worker),
        reason,
        status=status,
        timed_out_at=now(),
        retry_available=worker_retry_available(worker, WORKER_STATUS_TIMEOUT),
        manual_retry_required=manual_retry_required,
        timeout_started_at=_existing_or_unset(worker, "timeout_started_at"),
    )
    transition.apply_to_worker(worker)
    return transition


def format_timeout(timeout):
    return str(timeout)


def apply_worker_result(worker, result, *, prompt_ids=()):
    transition = WorkerTransition.result_applied(
        _worker_id(worker),
        result,
        prompt_ids=prompt_ids,
        timeout_started_at=_existing_or_unset(worker, "timeout_started_at"),
    )
    transition.apply_to_worker(worker)
    return transition


def _worker_id(worker):
    return worker["id"]


def _existing_or_unset(worker, field_name):
    return worker[field_name] if field_name in worker else _UNSET


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
        if worker.get("status") != WORKER_STATUS_DONE:
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
    return WORKER_STATUS_DONE in statuses and any(
        status in {WORKER_STATUS_FAILED, WORKER_STATUS_BLOCKED, WORKER_STATUS_ABORTED, WORKER_STATUS_TIMEOUT}
        for status in statuses
    )


def worker_prompt(worker):
    prompt = worker.get("prompt")
    if prompt is None:
        return None
    return str(prompt)
