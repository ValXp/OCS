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
    "attempts",
)
REMOVABLE_WORKER_TRANSITION_FIELDS = ("error", "failure_retryable", "manual_retry_required")
UNSET_TRANSITION_FIELD = object()

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
    "retry_count",
    "timeout_started_at",
    "timed_out_at",
    "failure_category",
    "failure_reason",
    "last_failure_category",
    "last_failure_reason",
    "blockers",
    "output_refs",
    "error",
    "failure_retryable",
    "manual_retry_required",
    "result",
    "attempts",
    "cleanup",
    "abort",
)


@dataclass(frozen=True)
class WorkerTransitionSpec:
    set_fields: dict = field(default_factory=dict)
    delete_fields: tuple = ()
    set_if_missing_fields: dict = field(default_factory=dict)
    merge_unique_fields: dict = field(default_factory=dict)
    append_attempt: dict = field(default_factory=dict)
    finalize_attempt: dict = field(default_factory=dict)
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

    def with_finalized_attempt(self, attempt_id, fields):
        return self._from_spec(
            self.worker_id,
            self.name,
            _transition_spec(
                set_fields=self.spec.set_fields,
                delete_fields=self.spec.delete_fields,
                set_if_missing_fields=self.spec.set_if_missing_fields,
                merge_unique_fields=self.spec.merge_unique_fields,
                append_attempt=self.spec.append_attempt,
                finalize_attempt={"id": attempt_id, "fields": fields},
                replace_worker=self.spec.replace_worker,
                preserve_accepted_abort=self.spec.preserve_accepted_abort,
            ),
        )

    @classmethod
    def provisioned(cls, worker):
        worker_id = worker["id"]
        return cls._from_spec(
            worker_id,
            "provisioned",
            _provisioned_transition_spec(worker_id, worker),
        )

    @classmethod
    def active(cls, worker_id, *, timeout_started_at=UNSET_TRANSITION_FIELD, clear_prompt_ids=False):
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
    def attempt_started(cls, worker_id, attempt):
        return cls._from_spec(
            worker_id,
            "attempt_started",
            _transition_spec(append_attempt=attempt),
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
        timeout_started_at=UNSET_TRANSITION_FIELD,
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
        timeout_started_at=UNSET_TRANSITION_FIELD,
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
        timeout_started_at=UNSET_TRANSITION_FIELD,
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
    def result_applied(cls, worker_id, result, *, prompt_ids=(), timeout_started_at=UNSET_TRANSITION_FIELD):
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
    return {"id": worker_id, "lifecycle_state": lifecycle_state}


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


@dataclass(frozen=True)
class WorkerSchedulingState:
    lifecycle_state: str | None
    status: str | None
    next_eligible_action: str
    has_prompt: bool

    @classmethod
    def from_worker(cls, worker):
        if not isinstance(worker, dict):
            return cls(None, None, WORKER_ACTION_NONE, False)
        return WorkerRecord.from_worker(worker).scheduling_state()

    def can_execute(self):
        return self.has_prompt and self.next_eligible_action in EXECUTABLE_WORKER_ACTIONS

    def can_block_for_dependency(self):
        return self.has_prompt and is_dependency_blockable_status(self.status)


@dataclass(frozen=True)
class WorkerRecord:
    worker_id: str
    fields: dict
    has_explicit_lifecycle: bool = False

    @classmethod
    def from_worker(cls, worker, worker_id=None):
        fields = dict(worker) if isinstance(worker, dict) else {}
        resolved_worker_id = fields.get("id") or worker_id
        has_explicit_lifecycle = isinstance(worker, dict) and bool(worker.get("lifecycle_state"))
        return cls(resolved_worker_id, fields, has_explicit_lifecycle)

    @classmethod
    def default_snapshot_fields(cls, worker_id):
        return {
            "id": worker_id,
            "role": None,
            "session_id": None,
            "agent": None,
            "model": None,
            "dependencies": [],
            "prompt_ids": [],
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
            "blockers": [],
            "output_refs": [],
            "attempts": [],
        }

    @classmethod
    def default_fields(cls, worker_id):
        fields = cls.default_snapshot_fields(worker_id)
        fields.update(cls.public_state_fields(fields["lifecycle_state"]))
        return fields

    @classmethod
    def public_state(cls, lifecycle_state):
        return public_worker_state(lifecycle_state)

    @classmethod
    def public_state_fields(cls, lifecycle_state):
        return public_worker_state_fields(lifecycle_state)

    @classmethod
    def lifecycle_set_fields(cls, worker_id, lifecycle_state):
        return worker_lifecycle_set_fields(worker_id, lifecycle_state)

    @property
    def lifecycle_state(self):
        lifecycle_state = self.fields.get("lifecycle_state")
        if lifecycle_state in WORKER_LIFECYCLE_STATES:
            return lifecycle_state
        return self._infer_lifecycle_state(self.fields)

    @property
    def status(self):
        return self.public_state(self.lifecycle_state)[0]

    @property
    def next_eligible_action(self):
        return self.public_state(self.lifecycle_state)[1]

    @property
    def has_prompt(self):
        prompt = self.fields.get("prompt")
        return prompt is not None and bool(str(prompt))

    def scheduling_state(self):
        return WorkerSchedulingState(
            self.lifecycle_state,
            self.status,
            self.next_eligible_action,
            self.has_prompt,
        )

    def to_snapshot(self):
        normalized = self.default_snapshot_fields(self.worker_id)
        fields = dict(self.fields)
        legacy_state_source = dict(fields)
        fields.pop("status", None)
        fields.pop("next_eligible_action", None)
        normalized.update(fields)
        normalized["id"] = normalized.get("id") or self.worker_id
        for key in WORKER_LIST_FIELDS:
            value = normalized.get(key)
            normalized[key] = value if isinstance(value, list) else []
        if normalized.get("retry_count") is None:
            normalized["retry_count"] = 0
        if normalized.get("retry_limit") is None:
            normalized["retry_limit"] = 0
        if not normalized.get("timeout_policy"):
            normalized["timeout_policy"] = WORKER_STATUS_TIMEOUT
        if not self.has_explicit_lifecycle or normalized.get("lifecycle_state") not in WORKER_LIFECYCLE_STATES:
            normalized["lifecycle_state"] = self._infer_lifecycle_state(legacy_state_source)
        return normalized

    def to_worker(self):
        normalized = self.to_snapshot()
        normalized.update(self.public_state_fields(normalized["lifecycle_state"]))
        return normalized

    def serialized_public_state(self):
        return self.public_state_fields(self.lifecycle_state)

    def apply_transition_spec(self, spec):
        latest_worker = self.to_snapshot()
        set_fields = deepcopy(spec.set_fields or {})
        set_if_missing_fields = deepcopy(spec.set_if_missing_fields or {})
        merge_unique_fields = deepcopy(spec.merge_unique_fields or {})
        if spec.preserve_accepted_abort and _accepted_abort(latest_worker) and not _accepted_abort(set_fields):
            merged = self._transition_for_aborted_worker(latest_worker, set_fields, merge_unique_fields)
        else:
            merged = {} if spec.replace_worker else deepcopy(latest_worker)
            merged.update(set_fields)
            for field_name in spec.delete_fields:
                merged.pop(field_name, None)
            _merge_unique_fields(merged, latest_worker, merge_unique_fields)
            for field_name, value in set_if_missing_fields.items():
                if not merged.get(field_name):
                    merged[field_name] = deepcopy(value)
            if "abort" not in set_fields and "abort" in latest_worker:
                merged["abort"] = deepcopy(latest_worker["abort"])
        _append_attempt(merged, spec.append_attempt)
        _finalize_attempt(merged, spec.finalize_attempt)
        return WorkerRecord.from_worker(merged, self.worker_id).to_worker()

    @staticmethod
    def _transition_for_aborted_worker(latest_worker, set_fields, merge_unique_fields):
        merged = deepcopy(latest_worker)
        _merge_unique_fields(merged, latest_worker, merge_unique_fields)
        if "cleanup" in set_fields:
            merged["cleanup"] = deepcopy(set_fields["cleanup"])
        return merged

    @staticmethod
    def _infer_lifecycle_state(worker):
        status = short_status(worker.get("status") if isinstance(worker, dict) else None)
        if status == WORKER_STATUS_QUEUED:
            return WORKER_LIFECYCLE_QUEUED
        if status == WORKER_STATUS_ACTIVE:
            if worker.get("next_eligible_action") == WORKER_ACTION_RETRY:
                return WORKER_LIFECYCLE_ACTIVE_RETRY
            return WORKER_LIFECYCLE_ACTIVE_WAIT
        if is_blocked_status(status):
            if worker.get("failure_category") == WORKER_STATUS_TIMEOUT or WORKER_STATUS_TIMEOUT in set(
                worker.get("blockers") or []
            ):
                return WORKER_LIFECYCLE_BLOCKED_TIMEOUT
            return WORKER_LIFECYCLE_BLOCKED_DEPENDENCY
        if status == WORKER_STATUS_DONE:
            return WORKER_LIFECYCLE_DONE_COLLECT
        if status == WORKER_STATUS_FAILED:
            if worker.get("failure_category") == WORKER_STATUS_TIMEOUT:
                if worker_retry_available(worker, WORKER_STATUS_TIMEOUT):
                    return WORKER_LIFECYCLE_TIMEOUT_FAILED_RETRY
                return WORKER_LIFECYCLE_TIMEOUT_FAILED_TERMINAL
            if worker_retry_available(worker):
                return WORKER_LIFECYCLE_FAILED_RETRY
            return WORKER_LIFECYCLE_FAILED_TERMINAL
        if status == WORKER_STATUS_TIMEOUT:
            if worker_retry_available(worker, WORKER_STATUS_TIMEOUT):
                return WORKER_LIFECYCLE_TIMEOUT_RETRY
            return WORKER_LIFECYCLE_TIMEOUT_TERMINAL
        if status == WORKER_STATUS_ABORTED:
            if worker.get("failure_category") == WORKER_STATUS_TIMEOUT:
                return WORKER_LIFECYCLE_TIMEOUT_ABORTED
            return WORKER_LIFECYCLE_ABORTED
        return WORKER_LIFECYCLE_QUEUED


def default_worker_record(worker_id):
    return WorkerRecord.default_fields(worker_id)


def deserialize_worker_record(worker, worker_id):
    return WorkerRecord.from_worker(worker, worker_id).to_worker()


def serialize_worker_snapshot(worker, worker_id):
    return WorkerRecord.from_worker(worker, worker_id).to_snapshot()


def new_worker_attempt_record(worker, *, started_at, created_session_ids=()):
    attempts = worker.get("attempts") if isinstance(worker, dict) else None
    attempt_count = len(attempts) if isinstance(attempts, list) else 0
    return {
        "id": f"attempt-{attempt_count + 1}",
        "session_id": worker.get("session_id") if isinstance(worker, dict) else None,
        "created_session_ids": list(created_session_ids),
        "status": "active",
        "started_at": started_at,
        "finished_at": None,
    }


def worker_lifecycle_state(worker):
    if not isinstance(worker, dict):
        return None
    return WorkerRecord.from_worker(worker).lifecycle_state


def infer_worker_lifecycle_state(worker):
    return WorkerRecord._infer_lifecycle_state(worker)


def latest_prompt_ids_are_retry_marker(latest_worker):
    return (
        isinstance(latest_worker, dict)
        and WorkerRecord.from_worker(latest_worker).lifecycle_state == WORKER_LIFECYCLE_ACTIVE_RETRY
        and latest_worker.get("last_failure_category") is not None
    )


def snapshot_state_source(worker):
    source = deepcopy(worker)
    if "status" in source or "next_eligible_action" in source:
        source.pop("lifecycle_state", None)
    return source


def next_eligible_worker_action(worker):
    if not isinstance(worker, dict):
        return WORKER_ACTION_NONE
    return WorkerRecord.from_worker(worker).next_eligible_action


def worker_has_prompt(worker):
    if not isinstance(worker, dict):
        return False
    return WorkerRecord.from_worker(worker).has_prompt


def is_executable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_execute()


def is_dependency_blockable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_block_for_dependency()


def _accepted_abort(worker):
    abort = worker.get("abort") if isinstance(worker, dict) else None
    return isinstance(abort, dict) and abort.get("accepted") and WorkerRecord.from_worker(worker).status == WORKER_STATUS_ABORTED


def _merge_unique_fields(target, latest_worker, merge_unique_fields):
    for field_name, values in merge_unique_fields.items():
        source_worker = latest_worker
        if field_name == "prompt_ids" and latest_prompt_ids_are_retry_marker(latest_worker):
            source_worker = {}
        _merge_unique_list_field(target, source_worker, {field_name: list(values)}, field_name)


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


def _append_attempt(worker, attempt):
    if not attempt:
        return
    attempts = worker.get("attempts") if isinstance(worker.get("attempts"), list) else []
    attempt = deepcopy(attempt)
    if any(isinstance(existing, dict) and existing.get("id") == attempt.get("id") for existing in attempts):
        return
    worker["attempts"] = [*deepcopy(attempts), attempt]


def _finalize_attempt(worker, finalization):
    if not finalization:
        return
    attempt_id = finalization.get("id")
    fields = finalization.get("fields") if isinstance(finalization.get("fields"), dict) else {}
    attempts = worker.get("attempts") if isinstance(worker.get("attempts"), list) else []
    finalized = []
    found = False
    for attempt in attempts:
        if isinstance(attempt, dict) and attempt.get("id") == attempt_id:
            updated = deepcopy(attempt)
            updated.update(deepcopy(fields))
            finalized.append(updated)
            found = True
        else:
            finalized.append(deepcopy(attempt))
    if found:
        worker["attempts"] = finalized


def _transition_spec(
    *,
    set_fields=None,
    delete_fields=(),
    set_if_missing_fields=None,
    merge_unique_fields=None,
    append_attempt=None,
    finalize_attempt=None,
    replace_worker=False,
    preserve_accepted_abort=True,
):
    return WorkerTransitionSpec(
        set_fields=deepcopy(set_fields or {}),
        delete_fields=tuple(delete_fields),
        set_if_missing_fields=deepcopy(set_if_missing_fields or {}),
        merge_unique_fields=deepcopy(merge_unique_fields or {}),
        append_attempt=deepcopy(append_attempt or {}),
        finalize_attempt=deepcopy(finalize_attempt or {}),
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


def _active_transition_spec(worker_id, *, timeout_started_at=UNSET_TRANSITION_FIELD, clear_prompt_ids=False):
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
    timeout_started_at=UNSET_TRANSITION_FIELD,
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
        set_fields.update(worker_lifecycle_set_fields(worker_id, WORKER_LIFECYCLE_ABORTED))
    return _transition_spec(set_fields=set_fields)


def _retry_scheduled_transition_spec(
    worker_id,
    category,
    reason,
    *,
    retry_count,
    timeout_started_at=UNSET_TRANSITION_FIELD,
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
    timeout_started_at=UNSET_TRANSITION_FIELD,
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
    timeout_started_at=UNSET_TRANSITION_FIELD,
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
    set_fields = {"id": worker_id}
    selected_state_fields = state_fields or WORKER_SNAPSHOT_STATE_FIELDS
    for field_name in selected_state_fields:
        if field_name in worker:
            set_fields[field_name] = deepcopy(worker[field_name])
    prompt_ids = worker.get("prompt_ids")
    return _transition_spec(
        set_fields=set_fields,
        delete_fields=tuple(field_name for field_name in removable_fields if field_name not in worker),
        set_if_missing_fields={
            field_name: deepcopy(worker[field_name])
            for field_name in set_if_missing_fields
            if worker.get(field_name)
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
    if value is not UNSET_TRANSITION_FIELD:
        fields[name] = value
