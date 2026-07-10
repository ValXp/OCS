import subprocess
from pathlib import Path

from opencode_session.run_branch_transaction import BranchDeleteTransaction
from opencode_session.run_resources import verify_owned_branch, verify_owned_log, verify_owned_worktree
from opencode_session.run_store import RunStoreError


def preflight_worktrees(records, *, force, errors):
    blocked = set()
    for record in records:
        path = Path(record["path"])
        if not path.exists():
            try:
                registered = _worktree_is_registered(record)
            except RunStoreError as error:
                registered = True
                _block(errors, "worktrees", record, str(error))
            if registered:
                blocked.add(record["path"])
                if not any(error.get("resource") == record for error in errors):
                    _block(errors, "worktrees", record, "owned worktree path is missing but its Git registration remains")
            continue
        error = verify_owned_worktree(record)
        if error is None and not force:
            try:
                if _git("-C", str(path), "status", "--porcelain"):
                    error = "owned worktree is dirty; pass --force to remove it"
            except RunStoreError as git_error:
                error = str(git_error)
        if error is not None:
            blocked.add(record["path"])
            _block(errors, "worktrees", record, error)
    return blocked


def preflight_branches(records, *, force, errors, blocked_worktrees=(), selected_worktrees=()):
    blocked = set()
    verified_tips = {}
    selected_worktrees = set(selected_worktrees)
    for record in records:
        branch = record.get("branch")
        identity = (record["git_dir"], branch)
        try:
            tip = _branch_tip(record) if branch else None
        except RunStoreError as error:
            blocked.add(identity)
            _block(errors, "branches", record, str(error))
            continue
        if tip is None:
            continue
        if record["path"] not in selected_worktrees:
            blocked.add(identity)
            _block(errors, "branches", record, "branch cleanup requires the same invocation to select its owned worktree")
            continue
        if record["path"] in blocked_worktrees:
            blocked.add(identity)
            continue
        path = Path(record["path"])
        if not path.exists():
            blocked.add(identity)
            _block(errors, "branches", record, "owned worktree must still exist when branch cleanup is planned")
            continue
        branch_error = verify_owned_branch(record)
        if branch_error is not None:
            blocked.add(identity)
            _block(errors, "branches", record, branch_error)
            continue
        if path.exists():
            ownership_error = verify_owned_worktree(record)
            if ownership_error is not None:
                blocked.add(identity)
                _block(errors, "branches", record, ownership_error)
                continue
        verified_tips[identity] = tip
        if not force:
            try:
                merged = branch in _merged_branches(record)
            except RunStoreError as error:
                blocked.add(identity)
                _block(errors, "branches", record, str(error))
                continue
            if not merged:
                blocked.add(identity)
                _block(errors, "branches", record, "owned branch is not merged; pass --force to remove it")
    return blocked, verified_tips


def preflight_logs(records, *, errors):
    blocked = set()
    for record in records:
        path = Path(record["path"])
        if not path.exists() and not path.is_symlink():
            continue
        error = verify_owned_log(record)
        if error is not None:
            blocked.add(record["path"])
            _block(errors, "logs", record, error)
    return blocked


def remove_worktrees(
    records,
    blocked,
    force,
    result,
    *,
    branch_records=(),
    blocked_branches=(),
    expected_branch_tips=None,
):
    expected_branch_tips = expected_branch_tips or {}
    branches_by_path = {
        record["path"]: record
        for record in branch_records
        if (record["git_dir"], record.get("branch")) not in blocked_branches
        and (record["git_dir"], record.get("branch")) in expected_branch_tips
    }
    for record in records:
        path = Path(record["path"])
        branch_record = branches_by_path.get(record["path"])
        if record["path"] in blocked:
            continue
        if path.exists():
            identity_error = verify_owned_worktree(record)
            if identity_error is not None:
                _block(result["errors"], "worktrees", record, identity_error)
                continue
            branch_transaction = None
            if branch_record is not None:
                branch_identity = (branch_record["git_dir"], branch_record["branch"])
                branch_transaction, branch_error = BranchDeleteTransaction.prepare(
                    branch_record["git_dir"],
                    branch_record["branch"],
                    expected_branch_tips[branch_identity],
                )
                if branch_error is not None:
                    _block(result["errors"], "branches", branch_record, branch_error)
                    continue
                identity_error = verify_owned_worktree(record)
                if identity_error is not None:
                    branch_transaction.abort()
                    _block(result["errors"], "worktrees", record, identity_error)
                    continue
            args = ["--git-dir", record["git_dir"], "worktree", "remove"]
            if force:
                args.append("--force")
            args.append(record["path"])
            error = _run_git(*args)
            if error is not None:
                if branch_transaction is not None:
                    branch_transaction.abort()
                result["errors"].append({"category": "worktrees", "resource": record, "error": error})
                continue
            if branch_transaction is not None:
                branch_error = branch_transaction.commit()
                if branch_error is not None:
                    _block(result["errors"], "branches", branch_record, branch_error)
                else:
                    result["completed"]["branches"].append(branch_record)
        else:
            if branch_record is not None:
                _block(result["errors"], "worktrees", record, "owned worktree disappeared after branch preflight")
                _block(result["errors"], "branches", branch_record, "atomic branch deletion was not acquired")
                continue
            try:
                registered = _worktree_is_registered(record)
            except RunStoreError as error:
                _block(result["errors"], "worktrees", record, str(error))
                continue
            if registered:
                _block(result["errors"], "worktrees", record, "owned worktree path is missing but its Git registration remains")
                continue
        result["completed"]["worktrees"].append(record)


def remove_branches(records, blocked, expected_tips, force, result):
    for record in records:
        branch = record.get("branch")
        identity = (record["git_dir"], branch)
        if any(
            completed.get("git_dir") == record["git_dir"] and completed.get("branch") == branch
            for completed in result["completed"]["branches"]
        ):
            continue
        if any(error.get("category") == "branches" and error.get("resource") == record for error in result["errors"]):
            continue
        if identity in blocked:
            continue
        try:
            tip = _branch_tip(record) if branch else None
        except RunStoreError as error:
            _block(result["errors"], "branches", record, str(error))
            continue
        if tip is not None:
            ownership_error = verify_owned_branch(record)
            if ownership_error is not None:
                _block(result["errors"], "branches", record, ownership_error)
                continue
            if tip != expected_tips.get(identity):
                _block(result["errors"], "branches", record, "owned branch tip changed after preflight")
                continue
            error = _run_git(
                "--git-dir",
                record["git_dir"],
                "update-ref",
                "-d",
                f"refs/heads/{branch}",
                expected_tips[identity],
            )
            if error is not None:
                result["errors"].append({"category": "branches", "resource": record, "error": error})
                continue
        result["completed"]["branches"].append(record)


def remove_logs(records, blocked, result):
    for record in records:
        path = Path(record["path"])
        if record["path"] in blocked:
            continue
        if not path.exists() and not path.is_symlink():
            result["completed"]["logs"].append(record)
            continue
        identity_error = verify_owned_log(record)
        if identity_error is not None:
            _block(result["errors"], "logs", record, identity_error)
            continue
        try:
            path.unlink()
        except OSError as error:
            result["errors"].append({"category": "logs", "resource": record, "error": str(error)})
            continue
        result["completed"]["logs"].append(record)


def _worktree_is_registered(record):
    output = _git("--git-dir", record["git_dir"], "worktree", "list", "--porcelain")
    return f"worktree {record['path']}" in output.splitlines()


def _branch_tip(record):
    completed = _git_completed(
        "--git-dir",
        record["git_dir"],
        "rev-parse",
        "--verify",
        f"refs/heads/{record['branch']}",
    )
    if completed.returncode == 0:
        return completed.stdout.strip()
    if completed.returncode == 1 and not completed.stderr.strip():
        return None
    if completed.returncode == 128 and "Needed a single revision" in completed.stderr:
        return None
    raise RunStoreError(_git_error(completed))


def _merged_branches(record):
    output = _git(
        "--git-dir",
        record["git_dir"],
        "branch",
        "--merged",
        "HEAD",
        "--format=%(refname:short)",
    )
    return set(output.splitlines())


def _run_git(*args):
    completed = _git_completed(*args)
    return None if completed.returncode == 0 else _git_error(completed)


def _git(*args, allow_failure=False):
    completed = _git_completed(*args)
    if completed.returncode == 0:
        return completed.stdout.strip()
    if allow_failure:
        return ""
    raise RunStoreError(_git_error(completed))


def _git_error(completed):
    return completed.stderr.strip() or completed.stdout.strip() or f"git exited {completed.returncode}"


def _git_completed(*args):
    try:
        return subprocess.run(["git", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as error:
        raise RunStoreError(f"cannot execute git: {error}") from error


def _block(errors, category, record, error):
    errors.append({"category": category, "resource": record, "error": error})
