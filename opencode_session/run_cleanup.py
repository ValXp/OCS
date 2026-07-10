from copy import deepcopy
from dataclasses import dataclass
from typing import Optional

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.capabilities import configure_client_route_plan, detect_capabilities
from opencode_session.disposable_session_lifecycle import cleanup_disposable_sessions
from opencode_session.run_cleanup_audit import begin_cleanup_audit, delete_audited_run, finish_cleanup_audit
from opencode_session.run_cleanup_local import (
    remove_branches,
    remove_logs,
    remove_worktrees,
)
from opencode_session.run_cleanup_preflight import (
    active_cleanup_is_blocked,
    preflight_cleanup,
    public_preflight,
)
from opencode_session.run_cleanup_projects import freeze_project_refresh_scope, project_plan_error
from opencode_session.run_resource_schema import normalize_run_resources
from opencode_session.run_resources import run_owned_session_ids
from opencode_session.run_store import RunStoreError


@dataclass(frozen=True)
class RunCleanupRequest:
    name: str
    sessions: bool = False
    worktrees: bool = False
    branches: bool = False
    project_metadata: bool = False
    logs: bool = False
    run_store: bool = False
    apply: bool = False
    force: bool = False
    server_url: Optional[str] = None


@dataclass(frozen=True)
class RunCleanupResult:
    record: dict
    exit_code: int = 0


class RunCleanupService:
    def __init__(
        self,
        store,
        *,
        client_factory,
        capability_detector=detect_capabilities,
        project_cleanup=None,
    ):
        self.store = store
        self.client_factory = client_factory
        self.capability_detector = capability_detector
        self.project_cleanup = project_cleanup

    def cleanup(self, request):
        if request.apply:
            with self.store.cleanup_lease(request.name):
                return self._cleanup(request)
        return self._cleanup(request)

    def _cleanup(self, request):
        run = self.store.load_run(request.name)
        cleanup = run.get("resource_cleanup")
        if (
            request.apply
            and isinstance(cleanup, dict)
            and cleanup.get("status") == "in_progress"
            and not request.force
        ):
            raise RunStoreError(
                f"run '{request.name}' has an interrupted cleanup; inspect it and pass --force to resume",
                kind="conflict",
            )
        plan = RunCleanupPlanner().plan(run, request)
        preflight = preflight_cleanup(run, plan, force=request.force)
        preview_record = _dry_run_record(plan, preflight)
        assumed_missing_paths = [
            record["path"]
            for record in plan["worktrees"]
            if record["path"] not in preflight["_blocked_worktree_set"]
        ]
        project_plans, project_preflight_failed = self._preview_project_metadata(
            plan,
            preview_record,
            assumed_missing_paths=assumed_missing_paths,
            blocked_projects=preflight["_blocked_project_set"],
        )
        if not request.apply:
            return RunCleanupResult(preview_record, 0 if not preview_record["errors"] else 1)
        if active_cleanup_is_blocked(preflight, force=request.force) or project_preflight_failed:
            preview_record["mode"] = "apply"
            return RunCleanupResult(preview_record, 1)
        return RunCleanupExecutor(
            self.store,
            client_factory=self.client_factory,
            capability_detector=self.capability_detector,
            project_cleanup=self.project_cleanup,
        ).execute(run, plan, preflight, project_plans, request)

    def _preview_project_metadata(self, plan, record, *, assumed_missing_paths, blocked_projects):
        project_plans = []
        if not plan["project_metadata"]:
            return project_plans, False
        if self.project_cleanup is None:
            record["errors"].append(
                {"category": "project_metadata", "error": "project metadata cleanup is unsupported"}
            )
            record["status"] = "blocked"
            return project_plans, True
        failed = bool(blocked_projects)
        for entry in plan["project_metadata"]:
            if (entry["project_id"], entry["directory_prefix"]) in blocked_projects:
                continue
            try:
                result = self.project_cleanup(
                    entry,
                    server_url=plan["server_url"],
                    apply=False,
                    assumed_missing_paths=assumed_missing_paths,
                )
            except Exception as error:
                failed = True
                record["errors"].append(
                    {"category": "project_metadata", "resource": entry, "error": str(error)}
                )
                continue
            outcome = result.get("outcome") or result
            record["project_metadata_preview"].append(outcome)
            project_plans.append({"entry": deepcopy(entry), "outcome": deepcopy(outcome)})
        freeze_project_refresh_scope(project_plans)
        record["project_metadata_preview"] = [deepcopy(item["outcome"]) for item in project_plans]
        for project_plan in project_plans:
            outcome = project_plan["outcome"]
            if outcome.get("status") not in {"planned", "done"}:
                failed = True
                record["errors"].append(
                    {
                        "category": "project_metadata",
                        "resource": project_plan["entry"],
                        "error": project_plan_error(outcome),
                    }
                )
        if record["errors"]:
            record["status"] = "blocked"
        return project_plans, failed


class RunCleanupPlanner:
    def plan(self, run, request):
        resources = normalize_run_resources(run.get("resources"))
        worktrees = deepcopy(resources["worktrees"]) if request.worktrees else []
        return {
            "run": request.name,
            "server_url": request.server_url or run.get("server_url"),
            "sessions": run_owned_session_ids(run) if request.sessions else [],
            "worktrees": worktrees,
            "branches": [deepcopy(record) for record in resources["worktrees"] if record.get("branch")]
            if request.branches
            else [],
            "project_metadata": deepcopy(resources["project_copies"]) if request.project_metadata else [],
            "logs": deepcopy(resources["logs"]) if request.logs else [],
            "run_store": bool(request.run_store),
        }


class RunCleanupExecutor:
    def __init__(self, store, *, client_factory, capability_detector, project_cleanup=None):
        self.store = store
        self.client_factory = client_factory
        self.capability_detector = capability_detector
        self.project_cleanup = project_cleanup

    def execute(self, run, plan, preflight, project_plans, request):
        record = {
            "mode": "apply",
            "run": request.name,
            "status": "done",
            "planned": _public_plan(plan),
            "completed": {name: [] for name in ("sessions", "worktrees", "branches", "project_metadata", "logs")},
            "run_store_deleted": False,
            "preflight": public_preflight(preflight),
            "project_metadata_preview": [deepcopy(item["outcome"]) for item in project_plans],
            "project_metadata_results": [],
            "errors": deepcopy(preflight["errors"]),
        }
        audit_baseline = begin_cleanup_audit(self.store, request.name, run, record)

        self._cleanup_sessions(plan, record)
        remove_worktrees(
            plan["worktrees"],
            preflight["_blocked_worktree_set"],
            request.force,
            record,
            branch_records=plan["branches"],
            blocked_branches=preflight["_blocked_branch_set"],
            expected_branch_tips=preflight["_branch_tip_map"],
        )
        self._cleanup_project_metadata(plan, project_plans, record)
        remove_branches(
            plan["branches"],
            preflight["_blocked_branch_set"],
            preflight["_branch_tip_map"],
            request.force,
            record,
        )
        remove_logs(plan["logs"], preflight["_blocked_log_set"], record)

        if record["errors"]:
            record["status"] = "partial" if any(record["completed"].values()) else "failed"
        audited_run = finish_cleanup_audit(self.store, request.name, audit_baseline, record)
        if plan["run_store"] and not record["errors"]:
            delete_audited_run(self.store, request.name, audited_run, record)
        return RunCleanupResult(record, 0 if not record["errors"] else 1)

    def _cleanup_sessions(self, plan, record):
        if not plan["sessions"]:
            return
        try:
            client = self.client_factory(plan["server_url"])
            capabilities = self.capability_detector(client)
            configure_client_route_plan(client, capabilities)
            outcome = cleanup_disposable_sessions(client, plan["sessions"])
        except OpenCodeApiError as error:
            record["errors"].append({"category": "sessions", "error": str(error)})
            return
        record["completed"]["sessions"].extend(outcome.record["verified"])
        record["errors"].extend(
            {"category": "sessions", **error}
            for error in outcome.record["errors"]
        )

    def _cleanup_project_metadata(self, plan, project_plans, record):
        if not plan["project_metadata"]:
            return
        if self.project_cleanup is None:
            record["errors"].append(
                {"category": "project_metadata", "error": "project metadata cleanup is unsupported"}
            )
            return
        for project_plan in project_plans:
            entry = project_plan["entry"]
            try:
                result = self.project_cleanup(
                    entry,
                    server_url=plan["server_url"],
                    apply=True,
                    planned_outcome=project_plan["outcome"],
                )
            except Exception as error:
                record["errors"].append({"category": "project_metadata", "resource": entry, "error": str(error)})
                continue
            record["project_metadata_results"].append(result.get("outcome") or result)
            if result.get("verified") is True:
                record["completed"]["project_metadata"].append(entry)
            else:
                record["errors"].append(
                    {"category": "project_metadata", "resource": entry, "error": result.get("error") or "verification failed"}
                )

def format_run_cleanup_compact(record):
    planned = record["planned"]
    return (
        f"run={record['run']} cleanup={record['mode']} status={record['status']} "
        f"sessions={len(planned['sessions'])} worktrees={len(planned['worktrees'])} "
        f"branches={len(planned['branches'])} projects={len(planned['project_metadata'])} "
        f"logs={len(planned['logs'])} run_store={str(planned['run_store']).lower()} errors={len(record['errors'])}"
    )


def _dry_run_record(plan, preflight):
    record = {
        "mode": "dry-run",
        "run": plan["run"],
        "status": "blocked" if preflight["errors"] else "planned",
        "planned": _public_plan(plan),
        "completed": {name: [] for name in ("sessions", "worktrees", "branches", "project_metadata", "logs")},
        "run_store_deleted": False,
        "preflight": public_preflight(preflight),
        "project_metadata_preview": [],
        "project_metadata_results": [],
        "errors": deepcopy(preflight["errors"]),
    }
    return record


def _public_plan(plan):
    return {
        key: deepcopy(plan[key])
        for key in ("server_url", "sessions", "worktrees", "branches", "project_metadata", "logs", "run_store")
    }
