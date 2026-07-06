from dataclasses import dataclass
from typing import Optional

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.schema_common import ExecutionResultRecord
from opencode_session.timeout_boundary import TimeoutExpired
from opencode_session.worker_state import (
    WorkerTransition,
    apply_worker_result,
    mark_worker_failed,
    mark_worker_timeout,
    schedule_worker_retry,
    worker_retry_available,
    worker_timeout_reason,
)


COMPLETED = "completed"
RETRY_SCHEDULED = "retry_scheduled"
TERMINAL_FAILURE = "terminal_failure"

ATTEMPT_COMPLETED = "completed"
ATTEMPT_FAILED = "failed"


class WorkerExecutionTimeout(TimeoutExpired):
    pass


@dataclass
class WorkerAttemptOutcome:
    kind: str
    result: Optional[ExecutionResultRecord] = None
    failure_category: Optional[str] = None
    reason: Optional[str] = None
    prompt_id: Optional[str] = None


@dataclass
class WorkerAttemptTransition:
    kind: str
    created_session_id: Optional[str] = None
    error: Optional[str] = None
    failure_category: Optional[str] = None
    worker_transition: Optional[WorkerTransition] = None


def classify_worker_attempt_result(result):
    return WorkerAttemptOutcome(ATTEMPT_COMPLETED, result=result)


def classify_worker_attempt_exception(worker, error):
    if isinstance(error, WorkerExecutionTimeout):
        return WorkerAttemptOutcome(ATTEMPT_FAILED, failure_category="timeout", reason=worker_timeout_reason(worker))
    if isinstance(error, OpenCodeApiError):
        return WorkerAttemptOutcome(ATTEMPT_FAILED, failure_category="api", reason=str(error))
    if isinstance(error, BlockingProviderFailure):
        return WorkerAttemptOutcome(
            ATTEMPT_FAILED,
            failure_category="provider",
            reason=str(error),
            prompt_id=error.prompt_id,
        )
    return None


def apply_worker_attempt_transition(worker, attempt, *, now):
    if attempt.kind == ATTEMPT_COMPLETED:
        return _apply_completed_attempt(worker, attempt.result)
    if attempt.failure_category == "timeout":
        return _apply_timeout_attempt_failure(worker, attempt.reason, now)
    if attempt.failure_category == "api":
        return _apply_retryable_attempt_failure(worker, "api", attempt.reason, error_prefix="api failure")
    if attempt.failure_category == "provider":
        return _apply_retryable_attempt_failure(
            worker,
            "provider",
            attempt.reason,
            error_prefix="provider failure",
            prompt_ids=(attempt.prompt_id,),
        )
    failure_transition = mark_worker_failed(worker, "unknown", attempt.reason or "worker attempt failed")
    return WorkerAttemptTransition(
        TERMINAL_FAILURE,
        error=attempt.reason or "worker attempt failed",
        failure_category="unknown",
        worker_transition=failure_transition,
    )


def _apply_completed_attempt(worker, result):
    prompt_id = result["message_ids"].get("user")
    transition = apply_worker_result(worker, result, prompt_ids=(prompt_id,))
    return WorkerAttemptTransition(COMPLETED, worker_transition=transition)


def _apply_timeout_attempt_failure(worker, reason, now):
    manual_retry_available = worker_retry_available(worker, "timeout")
    transition = mark_worker_timeout(worker, reason, now, manual_retry_required=manual_retry_available)
    if manual_retry_available:
        return WorkerAttemptTransition(
            TERMINAL_FAILURE,
            error=f"{reason}; automatic timeout retry skipped because the timed-out request may still be running",
            failure_category="timeout",
            worker_transition=transition,
        )
    return WorkerAttemptTransition(TERMINAL_FAILURE, error=reason, failure_category="timeout", worker_transition=transition)


def _apply_retryable_attempt_failure(worker, category, reason, *, error_prefix, prompt_ids=()):
    retry_transition = schedule_worker_retry(worker, category, reason, prompt_ids=prompt_ids)
    if retry_transition:
        return WorkerAttemptTransition(RETRY_SCHEDULED, failure_category=category, worker_transition=retry_transition)
    failure_transition = mark_worker_failed(worker, category, reason, prompt_ids=prompt_ids)
    return WorkerAttemptTransition(
        TERMINAL_FAILURE,
        error=f"{error_prefix}: {reason}",
        failure_category=category,
        worker_transition=failure_transition,
    )
