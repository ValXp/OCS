from opencode_session.blocking_execution import (
    blocking_execution_strategy,
    unsupported_blocking_execution_message,
)
from opencode_session.worker_state import mark_worker_failed


def blocking_execution_start_error(capabilities):
    if blocking_execution_strategy(capabilities) is None:
        return unsupported_blocking_execution_message()
    return None


def mark_orchestration_start_failed(run, workers, error):
    run["status"] = "failed"
    transitions = []
    for worker in workers:
        transitions.append(mark_worker_failed(worker, "api", error, retryable=False))
    return transitions
