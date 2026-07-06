from dataclasses import dataclass
from typing import Optional, Protocol

from opencode_session.blocking_execution import execute_blocking_prompt
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired
from opencode_session.worker_attempt_policy import (
    WorkerExecutionTimeout,
    classify_worker_attempt_exception,
    classify_worker_attempt_result,
)


@dataclass(frozen=True)
class WorkerPromptExecution:
    client: object
    session_id: str
    prompt: str
    capabilities: dict
    deadline: Optional[TimeoutDeadline] = None


class WorkerPromptExecutor(Protocol):
    def execute_prompt(self, execution): ...


class CallableWorkerPromptExecutor:
    def __init__(self, executor=execute_blocking_prompt):
        self.executor = executor

    def execute_prompt(self, execution):
        if execution.deadline is None:
            return self.executor(execution.client, execution.session_id, execution.prompt, execution.capabilities)
        return self.executor(
            execution.client,
            execution.session_id,
            execution.prompt,
            execution.capabilities,
            deadline=execution.deadline,
        )


def coerce_worker_prompt_executor(executor=None):
    if executor is None:
        return CallableWorkerPromptExecutor()
    if hasattr(executor, "execute_prompt"):
        return executor
    return CallableWorkerPromptExecutor(executor)


def execute_single_worker_attempt(client, worker, prompt, capabilities, *, executor):
    attempt_executor = coerce_worker_prompt_executor(executor)
    attempt_session_id = worker.session_id
    try:
        result = _call_worker_with_deadline(
            worker,
            lambda deadline, attempt_session_id=attempt_session_id: attempt_executor.execute_prompt(
                WorkerPromptExecution(
                    client,
                    attempt_session_id,
                    prompt,
                    capabilities,
                    deadline=deadline,
                )
            ),
        )
    except Exception as error:
        attempt = classify_worker_attempt_exception(worker, error)
        if attempt is None:
            raise
        return attempt
    return classify_worker_attempt_result(result)


def _call_worker_with_deadline(worker, callback):
    timeout = worker.timeout_seconds
    deadline = TimeoutDeadline(timeout) if timeout is not None else None
    try:
        if deadline is not None:
            deadline.require_time()
        return callback(deadline)
    except TimeoutExpired as error:
        raise WorkerExecutionTimeout() from error
    except TimeoutError as error:
        raise WorkerExecutionTimeout() from error
