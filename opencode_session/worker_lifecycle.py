from copy import deepcopy
from dataclasses import dataclass, field

from opencode_session.status import short_status


WORKER_STATUS_QUEUED = "queued"
WORKER_STATUS_ACTIVE = "active"
WORKER_STATUS_BLOCKED = "blocked"
WORKER_STATUS_DONE = "done"
WORKER_STATUS_FAILED = "failed"
WORKER_STATUS_ABORTED = "aborted"
WORKER_STATUS_TIMEOUT = "timeout"

WORKER_ACTION_START = "start"
WORKER_ACTION_WAIT = "wait"
WORKER_ACTION_RETRY = "retry"
WORKER_ACTION_RESOLVE_BLOCKER = "resolve_blocker"
WORKER_ACTION_COLLECT = "collect"
WORKER_ACTION_NONE = "none"

WORKER_LIFECYCLE_QUEUED = "queued"
WORKER_LIFECYCLE_ACTIVE_WAIT = "active_wait"
WORKER_LIFECYCLE_ACTIVE_RETRY = "active_retry"
WORKER_LIFECYCLE_BLOCKED_DEPENDENCY = "blocked_dependency"
WORKER_LIFECYCLE_BLOCKED_TIMEOUT = "blocked_timeout"
WORKER_LIFECYCLE_DONE_COLLECT = "done_collect"
WORKER_LIFECYCLE_FAILED_RETRY = "failed_retry"
WORKER_LIFECYCLE_FAILED_TERMINAL = "failed_terminal"
WORKER_LIFECYCLE_TIMEOUT_RETRY = "timeout_retry"
WORKER_LIFECYCLE_TIMEOUT_TERMINAL = "timeout_terminal"
WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY = "timeout_failed_retry"
WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL = "timeout_failed_terminal"
WORKER_LIFECYCLE_TIMEOUT_ABORTED = "timeout_aborted"
WORKER_LIFECYCLE_ABORTED = "aborted"

BLOCKED_WORKER_STATUS = WORKER_STATUS_BLOCKED
TERMINAL_WORKER_STATUSES = frozenset(
    {WORKER_STATUS_DONE, WORKER_STATUS_FAILED, WORKER_STATUS_ABORTED, WORKER_STATUS_TIMEOUT}
)
FAILED_DEPENDENCY_STATUSES = frozenset(
    {WORKER_STATUS_FAILED, WORKER_STATUS_ABORTED, WORKER_STATUS_TIMEOUT, WORKER_STATUS_BLOCKED}
)
EXECUTABLE_WORKER_ACTIONS = frozenset({WORKER_ACTION_START, WORKER_ACTION_RETRY})
WORKER_LIST_FIELDS = (
    "dependencies",
    "prompt_ids",
    "retryable_failures",
    "blockers",
    "output_refs",
)
REMOVABLE_WORKER_TRANSITION_FIELDS = ("error", "failure_retryable", "manual_retry_required")
UNSET_TRANSITION_FIELD = object()
_UNSET = UNSET_TRANSITION_FIELD

PUBLIC_WORKER_STATE_BY_LIFECYCLE = {
    WORKER_LIFECYCLE_QUEUED: (WORKER_STATUS_QUEUED, WORKER_ACTION_START),
    WORKER_LIFECYCLE_ACTIVE_WAIT: (WORKER_STATUS_ACTIVE, WORKER_ACTION_WAIT),
    WORKER_LIFECYCLE_ACTIVE_RETRY: (WORKER_STATUS_ACTIVE, WORKER_ACTION_RETRY),
    WORKER_LIFECYCLE_BLOCKED_DEPENDENCY: (WORKER_STATUS_BLOCKED, WORKER_ACTION_RESOLVE_BLOCKER),
    WORKER_LIFECYCLE_BLOCKED_TIMEOUT: (WORKER_STATUS_BLOCKED, WORKER_ACTION_RESOLVE_BLOCKER),
    WORKER_LIFECYCLE_DONE_COLLECT: (WORKER_STATUS_DONE, WORKER_ACTION_COLLECT),
    WORKER_LIFECYCLE_FAILED_RETRY: (WORKER_STATUS_FAILED, WORKER_ACTION_RETRY),
    WORKER_LIFECYCLE_FAILED_TERMINAL: (WORKER_STATUS_FAILED, WORKER_ACTION_NONE),
    WORKER_LIFECYCLE_TIMEOUT_RETRY: (WORKER_STATUS_TIMEOUT, WORKER_ACTION_RETRY),
    WORKER_LIFECYCLE_TIMEOUT_TERMINAL: (WORKER_STATUS_TIMEOUT, WORKER_ACTION_NONE),
    WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY: (WORKER_STATUS_FAILED, WORKER_ACTION_RETRY),
    WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL: (WORKER_STATUS_FAILED, WORKER_ACTION_NONE),
    WORKER_LIFECYCLE_TIMEOUT_ABORTED: (WORKER_STATUS_ABORTED, WORKER_ACTION_NONE),
    WORKER_LIFECYCLE_ABORTED: (WORKER_STATUS_ABORTED, WORKER_ACTION_NONE),
}
WORKER_LIFECYCLE_STATES = frozenset(PUBLIC_WORKER_STATE_BY_LIFECYCLE)

WORKER_SNAPSHOT_STATE_FIELDS = (
    "lifecycle_state",
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


@dataclass(frozen=True)
class WorkerTransitionSpec:
    set_fields: dict = field(default_factory=dict)
    delete_fields: tuple = ()
    set_if_missing_fields: dict = field(default_factory=dict)
    merge_unique_fields: dict = field(default_factory=dict)
    replace_worker: bool = False
    preserve_accepted_abort: bool = True


@dataclass(frozen=True)
class WorkerTransition:
    """Lifecycle command applied to the latest worker snapshot."""

    worker_id: str
    name: str
    spec: WorkerTransitionSpec

    @property
    def set_fields(self):
        return self.spec.set_fields

    @classmethod
    def _from_spec(cls, worker_id, name, spec):
        return cls(worker_id, name, spec)

    @classmethod
    def provisioned(cls, worker):
        worker_id = worker["id"]
        return cls._from_spec(
            worker_id,
            "provisioned",
            _provisioned_transition_spec(worker_id, worker),
        )

    @classmethod
    def active(cls, worker_id, *, timeout_started_at=_UNSET, clear_prompt_ids=False):
        return cls._from_spec(
            worker_id,
            "active",
            _active_transition_spec(
                worker_id,
                timeout_started_at=timeout_started_at,
                clear_prompt_ids=clear_prompt_ids,
            ),
        )

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
        return cls._from_spec(
            worker_id,
            "failed",
            _failed_transition_spec(
                worker_id,
                category,
                reason,
                retryable=retryable,
                retry_available=retry_available,
                timeout_started_at=timeout_started_at,
                prompt_ids=prompt_ids,
            ),
        )

    @classmethod
    def dependency_blocked(cls, worker_id, blockers):
        return cls._from_spec(
            worker_id,
            "dependency_blocked",
            _dependency_blocked_transition_spec(worker_id, blockers),
        )

    @classmethod
    def aborted(cls, worker_id, abort):
        return cls._from_spec(
            worker_id,
            "aborted",
            _aborted_transition_spec(worker_id, abort),
        )

    @classmethod
    def retry_scheduled(
        cls,
        worker_id,
        category,
        reason,
        *,
        retry_count,
        timeout_started_at=_UNSET,
        prompt_ids=(),
    ):
        return cls._from_spec(
            worker_id,
            "retry_scheduled",
            _retry_scheduled_transition_spec(
                worker_id,
                category,
                reason,
                retry_count=retry_count,
                timeout_started_at=timeout_started_at,
                prompt_ids=prompt_ids,
            ),
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
        return cls._from_spec(
            worker_id,
            "timed_out",
            _timed_out_transition_spec(
                worker_id,
                reason,
                status=status,
                timed_out_at=timed_out_at,
                retry_available=retry_available,
                manual_retry_required=manual_retry_required,
                timeout_started_at=timeout_started_at,
            ),
        )

    @classmethod
    def result_applied(cls, worker_id, result, *, prompt_ids=(), timeout_started_at=_UNSET):
        return cls._from_spec(
            worker_id,
            "result_applied",
            _result_applied_transition_spec(
                worker_id,
                result,
                prompt_ids=prompt_ids,
                timeout_started_at=timeout_started_at,
            ),
        )

    @classmethod
    def cleanup_updated(cls, worker):
        worker_id = worker["id"]
        return cls._from_spec(
            worker_id,
            "cleanup_updated",
            _cleanup_updated_transition_spec(worker_id, worker),
        )

    @classmethod
    def snapshot_applied(cls, worker):
        worker_id = worker["id"]
        return cls._from_spec(
            worker_id,
            "snapshot_applied",
            _snapshot_applied_transition_spec(worker_id, worker),
        )

    def apply_to(self, latest_workers):
        latest_worker = latest_workers.get(self.worker_id)
        merged = self.apply_to_snapshot(latest_worker)
        latest_workers[self.worker_id] = merged
        return merged

    def apply_to_worker(self, worker):
        merged = self.apply_to_snapshot(worker)
        worker.clear()
        worker.update(merged)
        return worker

    def apply_to_snapshot(self, worker):
        from opencode_session.worker_normalization import WorkerRecord

        return WorkerRecord.from_worker(worker, self.worker_id).apply_transition_spec(self.spec)


def public_worker_state(lifecycle_state):
    return PUBLIC_WORKER_STATE_BY_LIFECYCLE.get(lifecycle_state, (None, WORKER_ACTION_NONE))


def public_worker_state_fields(lifecycle_state):
    status, action = public_worker_state(lifecycle_state)
    return {
        "lifecycle_state": lifecycle_state,
        "status": status,
        "next_eligible_action": action,
    }


def worker_lifecycle_set_fields(worker_id, lifecycle_state):
    fields = {"id": worker_id}
    fields.update(public_worker_state_fields(lifecycle_state))
    return fields


def worker_retry_available(worker, category=None):
    if not isinstance(worker, dict):
        return False
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


def is_blocked_status(status):
    return short_status(status) == BLOCKED_WORKER_STATUS


def is_terminal_status(status):
    return short_status(status) in TERMINAL_WORKER_STATUSES


def is_runnable_status(status):
    return not is_terminal_status(status) and not is_blocked_status(status)


def is_dependency_blockable_status(status):
    return is_runnable_status(status)


def is_failed_dependency_status(status):
    return short_status(status) in FAILED_DEPENDENCY_STATUSES


def _transition_spec(
    *,
    set_fields=None,
    delete_fields=(),
    set_if_missing_fields=None,
    merge_unique_fields=None,
    replace_worker=False,
    preserve_accepted_abort=True,
):
    return WorkerTransitionSpec(
        set_fields=deepcopy(set_fields or {}),
        delete_fields=tuple(delete_fields),
        set_if_missing_fields=deepcopy(set_if_missing_fields or {}),
        merge_unique_fields=deepcopy(merge_unique_fields or {}),
        replace_worker=replace_worker,
        preserve_accepted_abort=preserve_accepted_abort,
    )


def _provisioned_transition_spec(worker_id, worker):
    set_fields = {"id": worker_id}
    for field_name in ("agent", "model"):
        if worker.get(field_name) is not None:
            set_fields[field_name] = deepcopy(worker[field_name])
    set_if_missing_fields = {}
    if worker.get("session_id"):
        set_if_missing_fields["session_id"] = deepcopy(worker["session_id"])
    return _transition_spec(set_fields=set_fields, set_if_missing_fields=set_if_missing_fields)


def _active_transition_spec(worker_id, *, timeout_started_at=_UNSET, clear_prompt_ids=False):
    set_fields, delete_fields = _cleared_current_status_fields()
    set_fields.update(worker_lifecycle_set_fields(worker_id, WORKER_LIFECYCLE_ACTIVE_WAIT))
    _set_if_not_unset(set_fields, "timeout_started_at", timeout_started_at)
    if clear_prompt_ids:
        set_fields["prompt_ids"] = []
    return _transition_spec(set_fields=set_fields, delete_fields=delete_fields)


def _failed_transition_spec(
    worker_id,
    category,
    reason,
    *,
    retryable=True,
    retry_available=False,
    timeout_started_at=_UNSET,
    prompt_ids=(),
):
    lifecycle_state = (
        WORKER_LIFECYCLE_FAILED_RETRY
        if retryable and retry_available
        else WORKER_LIFECYCLE_FAILED_TERMINAL
    )
    set_fields = worker_lifecycle_set_fields(worker_id, lifecycle_state)
    set_fields.update(
        {
            "error": reason,
            "failure_category": category,
            "failure_reason": reason,
            "last_failure_category": category,
            "last_failure_reason": reason,
        }
    )
    _set_if_not_unset(set_fields, "timeout_started_at", timeout_started_at)
    delete_fields = ["manual_retry_required"]
    if retryable:
        delete_fields.append("failure_retryable")
    else:
        set_fields["failure_retryable"] = False
    return _transition_spec(
        set_fields=set_fields,
        delete_fields=tuple(delete_fields),
        merge_unique_fields=_prompt_ids_merge(prompt_ids),
    )


def _dependency_blocked_transition_spec(worker_id, blockers):
    set_fields = worker_lifecycle_set_fields(worker_id, WORKER_LIFECYCLE_BLOCKED_DEPENDENCY)
    set_fields["blockers"] = list(blockers)
    return _transition_spec(set_fields=set_fields)


def _aborted_transition_spec(worker_id, abort):
    set_fields = {"id": worker_id, "abort": deepcopy(abort)}
    if isinstance(abort, dict) and abort.get("accepted"):
        set_fields.update(public_worker_state_fields(WORKER_LIFECYCLE_ABORTED))
    return _transition_spec(set_fields=set_fields)


def _retry_scheduled_transition_spec(
    worker_id,
    category,
    reason,
    *,
    retry_count,
    timeout_started_at=_UNSET,
    prompt_ids=(),
):
    set_fields, delete_fields = _cleared_current_status_fields()
    set_fields.update(worker_lifecycle_set_fields(worker_id, WORKER_LIFECYCLE_ACTIVE_RETRY))
    set_fields.update(
        {
            "retry_count": retry_count,
            "last_failure_category": category,
            "last_failure_reason": reason,
        }
    )
    _set_if_not_unset(set_fields, "timeout_started_at", timeout_started_at)
    return _transition_spec(
        set_fields=set_fields,
        delete_fields=delete_fields,
        merge_unique_fields=_prompt_ids_merge(prompt_ids),
    )


def _timed_out_transition_spec(
    worker_id,
    reason,
    *,
    status,
    timed_out_at,
    retry_available=False,
    manual_retry_required=False,
    timeout_started_at=_UNSET,
):
    lifecycle_state = _timeout_lifecycle_state(status, retry_available)
    set_fields = worker_lifecycle_set_fields(worker_id, lifecycle_state)
    set_fields.update(
        {
            "error": reason,
            "failure_category": WORKER_STATUS_TIMEOUT,
            "failure_reason": reason,
            "last_failure_category": WORKER_STATUS_TIMEOUT,
            "last_failure_reason": reason,
            "timed_out_at": timed_out_at,
            "output_refs": [],
        }
    )
    if status == WORKER_STATUS_BLOCKED:
        set_fields["blockers"] = [WORKER_STATUS_TIMEOUT]
    _set_if_not_unset(set_fields, "timeout_started_at", timeout_started_at)
    delete_fields = []
    if manual_retry_required:
        set_fields["manual_retry_required"] = True
    else:
        delete_fields.append("manual_retry_required")
    return _transition_spec(set_fields=set_fields, delete_fields=tuple(delete_fields))


def _result_applied_transition_spec(
    worker_id,
    result,
    *,
    prompt_ids=(),
    timeout_started_at=_UNSET,
):
    status = short_status(result["status"])
    set_fields = worker_lifecycle_set_fields(worker_id, _result_lifecycle_state(status))
    set_fields["result"] = deepcopy(result)
    _set_if_not_unset(set_fields, "timeout_started_at", timeout_started_at)
    delete_fields = ()
    if status == WORKER_STATUS_DONE:
        clear_fields, delete_fields = _cleared_current_status_fields()
        set_fields.update(clear_fields)
        assistant_message_id = result["message_ids"].get("assistant")
        set_fields["output_refs"] = [f"assistant:{assistant_message_id}"] if assistant_message_id else []
    else:
        set_fields["failure_category"] = None
        set_fields["failure_reason"] = None
    return _transition_spec(
        set_fields=set_fields,
        delete_fields=delete_fields,
        merge_unique_fields=_prompt_ids_merge(prompt_ids),
    )


def _cleanup_updated_transition_spec(worker_id, worker):
    return _transition_spec(set_fields={"id": worker_id, "cleanup": deepcopy(worker.get("cleanup"))})


def _snapshot_applied_transition_spec(
    worker_id,
    worker,
    *,
    state_fields=None,
    set_if_missing_fields=("session_id",),
    removable_fields=REMOVABLE_WORKER_TRANSITION_FIELDS,
):
    from opencode_session.worker_normalization import WorkerRecord, snapshot_state_source

    normalized = WorkerRecord.from_worker(snapshot_state_source(worker), worker_id).to_worker()
    set_fields = {"id": worker_id}
    selected_state_fields = state_fields or WORKER_SNAPSHOT_STATE_FIELDS
    for field_name in selected_state_fields:
        if field_name in normalized:
            set_fields[field_name] = deepcopy(normalized[field_name])
    prompt_ids = normalized.get("prompt_ids")
    return _transition_spec(
        set_fields=set_fields,
        delete_fields=tuple(field_name for field_name in removable_fields if field_name not in normalized),
        set_if_missing_fields={
            field_name: deepcopy(normalized[field_name])
            for field_name in set_if_missing_fields
            if normalized.get(field_name)
        },
        merge_unique_fields={"prompt_ids": tuple(prompt_ids)} if isinstance(prompt_ids, list) else {},
    )


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


def _cleared_current_status_fields():
    return {
        "blockers": [],
        "failure_category": None,
        "failure_reason": None,
    }, REMOVABLE_WORKER_TRANSITION_FIELDS


def _prompt_ids_merge(prompt_ids):
    prompt_ids = tuple(prompt_id for prompt_id in prompt_ids if prompt_id is not None)
    return {"prompt_ids": prompt_ids} if prompt_ids else {}


def _set_if_not_unset(fields, name, value):
    if value is not _UNSET:
        fields[name] = value
