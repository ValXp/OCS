from dataclasses import dataclass
from typing import Optional

from opencode_session.worker_lifecycle import (
    EXECUTABLE_WORKER_ACTIONS,
    WORKER_ACTION_NONE,
    is_blocked_status,
    is_dependency_blockable_status,
    is_failed_dependency_status,
    is_runnable_status,
    is_terminal_status,
)
from opencode_session.worker_normalization import WorkerRecord


@dataclass(frozen=True)
class WorkerSchedulingState:
    lifecycle_state: Optional[str]
    status: Optional[str]
    next_eligible_action: str
    has_prompt: bool

    @classmethod
    def from_worker(cls, worker):
        if not isinstance(worker, dict):
            return cls(None, None, "none", False)
        return WorkerRecord.from_worker(worker).scheduling_state()

    def can_execute(self):
        return self.has_prompt and self.next_eligible_action in EXECUTABLE_WORKER_ACTIONS

    def can_block_for_dependency(self):
        return self.has_prompt and is_dependency_blockable_status(self.status)


def next_eligible_worker_action(worker):
    if not isinstance(worker, dict):
        return WORKER_ACTION_NONE
    return WorkerRecord.from_worker(worker).next_eligible_action


def worker_has_prompt(worker):
    if not isinstance(worker, dict):
        return False
    prompt = worker.get("prompt")
    return prompt is not None and bool(str(prompt))


def is_executable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_execute()


def is_dependency_blockable_worker(worker):
    return WorkerSchedulingState.from_worker(worker).can_block_for_dependency()
