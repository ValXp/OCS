import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocking_execution import (
    BlockingProviderFailure,
    blocking_execution_strategy,
    execute_blocking_prompt,
    unsupported_blocking_execution_message,
)
from opencode_session.capabilities import detect_capabilities
from opencode_session.run_store import DEFAULT_SERVER_URL, RunStoreError
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired
from opencode_session.worker_state import (
    EX_ABORTED,
    EX_BLOCKED,
    EX_PARTIAL,
    EX_TIMEOUT,
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
    apply_worker_result as _apply_worker_result,
    create_isolated_timeout_retry_session as _create_isolated_timeout_retry_session,
    ensure_worker as _ensure_worker,
    exit_code_for_run as _exit_code_for_run,
    mark_worker_failed as _mark_worker_failed,
    mark_worker_timeout as _mark_worker_timeout,
    refresh_run_summary as _refresh_run_summary,
    schedule_worker_retry as _schedule_worker_retry,
    session_value as _session_value,
    worker_timeout_reason as _worker_timeout_reason,
)


@dataclass
class SingleWorkerRunStartRequest:
    name: str
    worker_id: str
    role: str
    prompt: str
    directory: Optional[str] = None
    server_url: Optional[str] = None
    session_id: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    cleanup: bool = False
    default_server_url: Optional[str] = None


@dataclass
class SingleWorkerRunStartOutcome:
    run: dict
    exit_code: int
    error: Optional[str] = None


class WorkerExecutionTimeout(TimeoutExpired):
    pass


class SingleWorkerRunStateService:
    def __init__(
        self,
        store,
        *,
        client_factory=OpenCodeApiClient,
        capability_detector=detect_capabilities,
        executor=execute_blocking_prompt,
        now=None,
    ):
        self.store = store
        self.client_factory = client_factory
        self.capability_detector = capability_detector
        self.executor = executor
        self.now = now or _utc_now

    def start(self, request):
        run = self._load_or_create_run(request)
        worker = _ensure_worker(run, request.worker_id, role=request.role)
        worker["prompt"] = request.prompt
        run["status"] = "active"
        worker["status"] = "active"
        self._save(run)

        created_session_ids = []
        try:
            client = self.client_factory(run["server_url"])
            capabilities = self.capability_detector(client)
            if blocking_execution_strategy(capabilities) is None:
                message = unsupported_blocking_execution_message()
                _mark_orchestration_failed(worker, message)
                run["status"] = "failed"
                self._save(run)
                return SingleWorkerRunStartOutcome(run, EX_UNSUPPORTED, message)

            session_id = request.session_id or worker.get("session_id")
            if session_id is None:
                create_response = client.create_session_response(run["directory"], agent=request.agent, model=request.model)
                session_id = _session_value(create_response.data, "id", "sessionID", "sessionId")
                if session_id is not None:
                    created_session_ids.append(session_id)
        except OpenCodeApiError as error:
            run["status"] = "failed"
            _mark_orchestration_failed(worker, str(error))
            self._save(run)
            return SingleWorkerRunStartOutcome(run, EX_UNAVAILABLE, f"api failure: {error}")
        worker["session_id"] = session_id
        self._save(run)

        if request.agent is not None:
            worker["agent"] = request.agent
        if request.model is not None:
            worker["model"] = request.model

        while True:
            worker["status"] = "active"
            worker["next_eligible_action"] = "wait"
            worker["timeout_started_at"] = self.now() if worker.get("timeout_seconds") else None
            attempt_session_id = session_id
            try:
                result = _call_worker_with_timeout(
                    worker,
                    lambda attempt_session_id=attempt_session_id: self.executor(
                        client,
                        attempt_session_id,
                        request.prompt,
                        capabilities,
                    ),
                )
            except WorkerExecutionTimeout:
                reason = _worker_timeout_reason(worker)
                if _schedule_worker_retry(worker, "timeout", reason):
                    timed_out_at = self.now()
                    worker["timed_out_at"] = timed_out_at
                    try:
                        session_id = _create_isolated_timeout_retry_session(
                            client,
                            run,
                            worker,
                            reason,
                            timed_out_at,
                            agent=request.agent,
                            model=request.model,
                        )
                    except OpenCodeApiError as error:
                        message = f"timeout retry session creation failed: {error}"
                        _mark_worker_failed(worker, "api", message)
                        _refresh_run_summary(run)
                        self._save(run)
                        return SingleWorkerRunStartOutcome(run, _exit_code_for_run(run), f"api failure: {message}")
                    created_session_ids.append(session_id)
                    self._save(run)
                    continue
                _mark_worker_timeout(worker, reason, self.now)
                _refresh_run_summary(run)
                self._save(run)
                return SingleWorkerRunStartOutcome(run, _exit_code_for_run(run), reason)
            except OpenCodeApiError as error:
                if _schedule_worker_retry(worker, "api", str(error)):
                    self._save(run)
                    continue
                _mark_worker_failed(worker, "api", str(error))
                _refresh_run_summary(run)
                self._save(run)
                return SingleWorkerRunStartOutcome(run, _exit_code_for_run(run), f"api failure: {error}")
            except BlockingProviderFailure as error:
                if error.prompt_id is not None:
                    worker["prompt_ids"] = [error.prompt_id]
                if _schedule_worker_retry(worker, "provider", str(error)):
                    self._save(run)
                    continue
                _mark_worker_failed(worker, "provider", str(error))
                _refresh_run_summary(run)
                self._save(run)
                return SingleWorkerRunStartOutcome(run, _exit_code_for_run(run), f"provider failure: {error}")

            prompt_id = result["message_ids"].get("user")
            if prompt_id is not None:
                worker["prompt_ids"] = [prompt_id]
            break
        _apply_worker_result(worker, result)
        _refresh_run_summary(run)
        if request.cleanup:
            worker["cleanup"] = {"requested": True, "deleted": False}
            deleted_session_ids = []
            for created_session_id in created_session_ids:
                try:
                    client.delete_session(created_session_id)
                except OpenCodeApiError as error:
                    worker["cleanup"]["error"] = str(error)
                    run["status"] = "failed"
                    _mark_orchestration_failed(worker, str(error))
                    self._save(run)
                    return SingleWorkerRunStartOutcome(
                        run,
                        EX_UNAVAILABLE,
                        f"api failure: disposable session cleanup failed: {error}",
                    )
                deleted_session_ids.append(created_session_id)
            if deleted_session_ids:
                worker["cleanup"]["deleted"] = True
                if len(deleted_session_ids) > 1:
                    worker["cleanup"]["sessions"] = deleted_session_ids
        self._save(run)
        return SingleWorkerRunStartOutcome(run, _exit_code_for_run(run))

    def _load_or_create_run(self, request):
        try:
            run = self.store.load_run(request.name)
        except RunStoreError as error:
            if error.kind != "missing":
                raise
            run = self.store.create_run(
                request.name,
                directory=request.directory or ".",
                server_url=request.server_url or request.default_server_url or _server_default(),
            )
        else:
            if request.directory is not None:
                run["directory"] = str(Path(request.directory).resolve())
            if request.server_url is not None:
                run["server_url"] = request.server_url
        return run

    def _save(self, run):
        run["updated_at"] = self.now()
        self.store.save_run(run)


def _mark_orchestration_failed(worker, error):
    worker["status"] = "failed"
    worker["error"] = error
    worker["failure_category"] = "api"
    worker["failure_reason"] = error
    worker["last_failure_category"] = "api"
    worker["last_failure_reason"] = error
    worker["next_eligible_action"] = "none"


def _call_worker_with_timeout(worker, callback):
    timeout = worker.get("timeout_seconds")
    try:
        return TimeoutDeadline(timeout).run(callback)
    except TimeoutExpired as error:
        raise WorkerExecutionTimeout() from error


def _server_default():
    return os.environ.get("OPENCODE_SERVER_URL") or os.environ.get("OPENCODE_SERVER") or DEFAULT_SERVER_URL


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
