import shutil
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.capabilities import configure_client_route_plan, detect_capabilities
from opencode_session.disposable_session_lifecycle import cleanup_disposable_sessions
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
        run = self.store.load_run(request.name)
        plan = RunCleanupPlanner().plan(run, request)
        if not request.apply:
            return RunCleanupResult(_dry_run_record(plan))
        return RunCleanupExecutor(
            self.store,
            client_factory=self.client_factory,
            capability_detector=self.capability_detector,
            project_cleanup=self.project_cleanup,
        ).execute(run, plan, request)


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

    def execute(self, run, plan, request):
        record = {
            "mode": "apply",
            "run": request.name,
            "status": "done",
            "planned": _public_plan(plan),
            "completed": {name: [] for name in ("sessions", "worktrees", "branches", "project_metadata", "logs")},
            "run_store_deleted": False,
            "errors": [],
        }
        blocked_worktrees = _preflight_worktrees(plan["worktrees"], force=request.force, errors=record["errors"])
        blocked_branches = _preflight_branches(plan["branches"], force=request.force, errors=record["errors"])

        self._cleanup_sessions(plan, record)
        self._cleanup_project_metadata(plan, record)
        _remove_worktrees(plan["worktrees"], blocked_worktrees, request.force, record)
        _remove_branches(plan["branches"], blocked_branches, request.force, record)
        _remove_logs(plan["logs"], record)

        if record["errors"]:
            record["status"] = "partial" if any(record["completed"].values()) else "failed"
        self._persist_audit(request.name, record)
        if plan["run_store"] and not record["errors"]:
            self.store.delete_run(request.name)
            record["run_store_deleted"] = True
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

    def _cleanup_project_metadata(self, plan, record):
        if not plan["project_metadata"]:
            return
        if self.project_cleanup is None:
            record["errors"].append(
                {"category": "project_metadata", "error": "project metadata cleanup is unsupported"}
            )
            return
        for entry in plan["project_metadata"]:
            try:
                result = self.project_cleanup(entry, server_url=plan["server_url"])
            except Exception as error:
                record["errors"].append({"category": "project_metadata", "resource": entry, "error": str(error)})
                continue
            if result.get("verified") is True:
                record["completed"]["project_metadata"].append(entry)
            else:
                record["errors"].append(
                    {"category": "project_metadata", "resource": entry, "error": result.get("error") or "verification failed"}
                )

    def _persist_audit(self, name, record):
        self.store.update_run(name, lambda latest_run: latest_run.__setitem__("resource_cleanup", deepcopy(record)))


def format_run_cleanup_compact(record):
    planned = record["planned"]
    return (
        f"run={record['run']} cleanup={record['mode']} status={record['status']} "
        f"sessions={len(planned['sessions'])} worktrees={len(planned['worktrees'])} "
        f"branches={len(planned['branches'])} projects={len(planned['project_metadata'])} "
        f"logs={len(planned['logs'])} run_store={str(planned['run_store']).lower()} errors={len(record['errors'])}"
    )


def _dry_run_record(plan):
    return {
        "mode": "dry-run",
        "run": plan["run"],
        "status": "planned",
        "planned": _public_plan(plan),
        "completed": {name: [] for name in ("sessions", "worktrees", "branches", "project_metadata", "logs")},
        "run_store_deleted": False,
        "errors": [],
    }


def _public_plan(plan):
    return {key: deepcopy(plan[key]) for key in ("sessions", "worktrees", "branches", "project_metadata", "logs", "run_store")}


def _preflight_worktrees(records, *, force, errors):
    blocked = set()
    for record in records:
        path = Path(record["path"])
        if not path.exists():
            continue
        error = _verify_registered_worktree(record)
        if error is None and not force and _git(record, "-C", str(path), "status", "--porcelain"):
            error = "owned worktree is dirty; pass --force to remove it"
        if error is not None:
            blocked.add(record["path"])
            errors.append({"category": "worktrees", "resource": record, "error": error})
    return blocked


def _preflight_branches(records, *, force, errors):
    blocked = set()
    for record in records:
        branch = record.get("branch")
        if not branch or not _branch_exists(record):
            continue
        if not force and branch not in _merged_branches(record):
            blocked.add((record["git_dir"], branch))
            errors.append({"category": "branches", "resource": record, "error": "owned branch is not merged; pass --force to remove it"})
    return blocked


def _remove_worktrees(records, blocked, force, result):
    for record in records:
        path = Path(record["path"])
        if record["path"] in blocked:
            continue
        if path.exists():
            args = ["--git-dir", record["git_dir"], "worktree", "remove"]
            if force:
                args.append("--force")
            args.append(record["path"])
            error = _run_git(record, *args)
            if error is not None:
                result["errors"].append({"category": "worktrees", "resource": record, "error": error})
                continue
        result["completed"]["worktrees"].append(record)


def _remove_branches(records, blocked, force, result):
    for record in records:
        branch = record.get("branch")
        identity = (record["git_dir"], branch)
        if identity in blocked:
            continue
        if branch and _branch_exists(record):
            flag = "-D" if force else "-d"
            error = _run_git(record, "--git-dir", record["git_dir"], "branch", flag, "--", branch)
            if error is not None:
                result["errors"].append({"category": "branches", "resource": record, "error": error})
                continue
        result["completed"]["branches"].append(record)


def _remove_logs(records, result):
    for record in records:
        path = Path(record["path"])
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
        except OSError as error:
            result["errors"].append({"category": "logs", "resource": record, "error": str(error)})
            continue
        result["completed"]["logs"].append(record)


def _verify_registered_worktree(record):
    path = Path(record["path"])
    current = _git(record, "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir", allow_failure=True)
    if not current:
        return "owned worktree is no longer a Git worktree"
    if Path(current).resolve() != Path(record["git_dir"]).resolve():
        return "owned worktree no longer belongs to the recorded repository"
    return None


def _branch_exists(record):
    return _git(record, "--git-dir", record["git_dir"], "show-ref", "--verify", f"refs/heads/{record['branch']}", allow_failure=True) != ""


def _merged_branches(record):
    output = _git(record, "--git-dir", record["git_dir"], "branch", "--merged", "HEAD", "--format=%(refname:short)", allow_failure=True)
    return set(output.splitlines())


def _run_git(record, *args):
    completed = _git_completed(*args)
    if completed.returncode == 0:
        return None
    return completed.stderr.strip() or completed.stdout.strip() or f"git exited {completed.returncode}"


def _git(record, *args, allow_failure=False):
    completed = _git_completed(*args)
    if completed.returncode == 0:
        return completed.stdout.strip()
    if allow_failure:
        return ""
    raise RunStoreError(completed.stderr.strip() or completed.stdout.strip() or f"git exited {completed.returncode}")


def _git_completed(*args):
    return subprocess.run(["git", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
