from copy import deepcopy

from opencode_session.run_cleanup_local import preflight_branches, preflight_logs, preflight_worktrees
from opencode_session.run_record import run_record_for_output
from opencode_session.run_project_ownership import verify_owned_project_copy


def preflight_cleanup(run, plan, *, force):
    errors = []
    blocked_worktrees = preflight_worktrees(plan["worktrees"], force=force, errors=errors)
    blocked_branches, branch_tips = preflight_branches(
        plan["branches"],
        force=force,
        errors=errors,
        blocked_worktrees=blocked_worktrees,
        selected_worktrees=(record["path"] for record in plan["worktrees"]),
    )
    blocked_branch_paths = {
        record["path"]
        for record in plan["branches"]
        if (record["git_dir"], record.get("branch")) in blocked_branches
    }
    blocked_worktrees.update(blocked_branch_paths)
    blocked_logs = preflight_logs(plan["logs"], errors=errors)
    blocked_projects = set()
    for record in plan["project_metadata"]:
        error = verify_owned_project_copy(record)
        if error is not None:
            identity = (record["project_id"], record["directory_prefix"])
            blocked_projects.add(identity)
            errors.append({"category": "project_metadata", "resource": record, "error": error})
    active_workers = _active_worker_ids(run)
    if active_workers and not force:
        errors.append(
            {
                "category": "run",
                "error": "active workers require --force before cleanup can apply",
                "workers": active_workers,
            }
        )
    return {
        "active_workers": active_workers,
        "blocked_worktrees": sorted(blocked_worktrees),
        "blocked_branches": [list(identity) for identity in sorted(blocked_branches)],
        "branch_tips": [
            {"git_dir": git_dir, "branch": branch, "tip": tip}
            for (git_dir, branch), tip in sorted(branch_tips.items())
        ],
        "blocked_logs": sorted(blocked_logs),
        "blocked_project_metadata": [list(identity) for identity in sorted(blocked_projects)],
        "errors": errors,
        "_blocked_worktree_set": blocked_worktrees,
        "_blocked_branch_set": blocked_branches,
        "_branch_tip_map": branch_tips,
        "_blocked_log_set": blocked_logs,
        "_blocked_project_set": blocked_projects,
    }


def public_preflight(preflight):
    return {
        key: deepcopy(preflight[key])
        for key in (
            "active_workers",
            "blocked_worktrees",
            "blocked_branches",
            "blocked_logs",
            "blocked_project_metadata",
            "branch_tips",
        )
    }


def active_cleanup_is_blocked(preflight, *, force):
    return bool(preflight["active_workers"]) and not force


def _active_worker_ids(run):
    workers = run_record_for_output(run).get("workers") or {}
    return sorted(
        worker_id
        for worker_id, worker in workers.items()
        if isinstance(worker, dict) and worker.get("status") == "active"
    )
