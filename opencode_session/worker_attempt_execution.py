from dataclasses import dataclass
from typing import Optional, Protocol

from opencode_session.blocking_execution import (
    blocking_execution_strategy,
    execute_blocking_prompt,
    new_session_message_id,
)
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
    prompt_id: Optional[str] = None


class WorkerPromptExecutor(Protocol):
    def execute_prompt(self, execution): ...


class CallableWorkerPromptExecutor:
    def __init__(self, executor=execute_blocking_prompt):
        self.executor = executor

    def prepare_prompt_id(self, capabilities):
        if self.executor is not execute_blocking_prompt:
            return None
        if blocking_execution_strategy(capabilities) != "session_message":
            return None
        return new_session_message_id()

    def execute_prompt(self, execution):
        kwargs = {}
        if execution.deadline is not None:
            kwargs["deadline"] = execution.deadline
        if execution.prompt_id is not None:
            kwargs["message_id"] = execution.prompt_id
        return self.executor(
            execution.client,
            execution.session_id,
            execution.prompt,
            execution.capabilities,
            **kwargs,
        )


def coerce_worker_prompt_executor(executor=None):
    if executor is None:
        return CallableWorkerPromptExecutor()
    if hasattr(executor, "execute_prompt"):
        return executor
    return CallableWorkerPromptExecutor(executor)


def prepare_worker_prompt_id(executor, capabilities):
    prepare_prompt_id = getattr(executor, "prepare_prompt_id", None)
    if not callable(prepare_prompt_id):
        return None
    return prepare_prompt_id(capabilities)


def execute_single_worker_attempt(client, worker, prompt, capabilities, *, executor, prompt_id=None):
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
                    prompt_id=prompt_id,
                )
            ),
        )
    except Exception as error:
        attempt = classify_worker_attempt_exception(worker, error)
        if attempt is None:
            raise
        if attempt.prompt_id is None:
            attempt.prompt_id = prompt_id
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
