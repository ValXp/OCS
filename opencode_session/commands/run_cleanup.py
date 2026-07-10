from opencode_session.commands.rendering import CommandResult, render_command_result
from opencode_session.project_metadata import ProjectMetadataService
from opencode_session.project_metadata_cleanup import ProjectCopyCleanupService
from opencode_session.run_cleanup import RunCleanupRequest, RunCleanupService, format_run_cleanup_compact
from opencode_session.run_resources import RunResourceError, register_run_resources
from opencode_session.run_store import RunStoreError


def add_run_cleanup_parser(run_subparsers):
    parser = run_subparsers.add_parser("cleanup", help="plan or remove resources explicitly owned by a run")
    parser.add_argument("name", help="local run name")
    categories = parser.add_argument_group("resource categories")
    categories.add_argument("--sessions", action="store_true", help="delete recorded worker sessions")
    categories.add_argument("--worktrees", action="store_true", help="remove explicitly registered worktrees")
    categories.add_argument("--branches", action="store_true", help="remove branches recorded with owned worktrees")
    categories.add_argument("--project-metadata", action="store_true", help="remove registered project/workspace metadata")
    categories.add_argument("--logs", action="store_true", help="remove explicitly registered log paths")
    categories.add_argument("--run-store", action="store_true", help="remove the run record after successful cleanup")
    categories.add_argument("--all", action="store_true", help="select every resource category")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the plan without changing anything (default)")
    mode.add_argument("--apply", action="store_true", help="execute the cleanup plan")
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow active-worker, dirty-worktree, and unmerged recorded branch cleanup",
    )
    parser.add_argument("--server", help="override the run's OpenCode server URL")
    parser.add_argument("--json", action="store_true", help="print cleanup plan/result JSON")


def add_run_resource_registration_arguments(parser):
    parser.add_argument("--owned-worktree", action="append", default=[], help="register an exact worktree path owned by this worker")
    parser.add_argument("--owned-log", action="append", default=[], help="register an exact existing log file or symlink owned by this worker")
    parser.add_argument(
        "--owned-project-copy",
        action="append",
        nargs=2,
        metavar=("PROJECT_ID", "DIRECTORY_PREFIX"),
        default=[],
        help="register project metadata ownership for an exact directory prefix",
    )


def register_resources_from_args(args, service, run):
    if not (args.owned_worktree or args.owned_log or args.owned_project_copy):
        return run
    try:
        return register_run_resources(
            service.store,
            args.name,
            args.worker_id,
            worktree_paths=args.owned_worktree,
            log_paths=args.owned_log,
            project_copies=args.owned_project_copy,
        )
    except RunResourceError as error:
        raise RunStoreError(str(error)) from error


def cleanup_run_command(args, service, *, print_error, **_context):
    selected = any((args.sessions, args.worktrees, args.branches, args.project_metadata, args.logs, args.run_store))
    if not selected and not args.all:
        return render_command_result(
            args,
            CommandResult(error="run cleanup requires a resource category or --all", exit_code=64),
            print_error=print_error,
        )
    request = RunCleanupRequest(
        args.name,
        sessions=args.sessions or args.all,
        worktrees=args.worktrees or args.all,
        branches=args.branches or args.all,
        project_metadata=args.project_metadata or args.all,
        logs=args.logs or args.all,
        run_store=args.run_store or args.all,
        apply=args.apply,
        force=args.force,
        server_url=args.server,
    )
    result = RunCleanupService(
        service.store,
        client_factory=service.client_factory,
        capability_detector=service.capability_detector,
        project_cleanup=_project_cleanup(service.client_factory),
    ).cleanup(request)
    warnings = tuple(error["error"] for error in result.record["errors"])
    return render_command_result(
        args,
        CommandResult(
            result.record,
            compact=format_run_cleanup_compact(result.record),
            exit_code=result.exit_code,
            warnings=warnings,
        ),
        print_error=print_error,
    )


def _project_cleanup(client_factory):
    def cleanup(
        entry,
        *,
        server_url,
        apply,
        assumed_missing_paths=(),
        planned_outcome=None,
    ):
        metadata = ProjectMetadataService(client_factory(server_url))
        cleanup_service = ProjectCopyCleanupService(metadata)
        if apply and planned_outcome is not None:
            outcome = cleanup_service.apply_plan(planned_outcome)
        else:
            outcome = cleanup_service.cleanup(
                entry["project_id"],
                entry["resolved_directory_prefix"],
                apply=apply,
                assumed_missing_paths=assumed_missing_paths,
            )
        return {
            "verified": outcome.get("status") == "done",
            "safe": outcome.get("status") in {"planned", "done"},
            "error": _project_cleanup_error(outcome),
            "outcome": outcome,
        }

    return cleanup


def _project_cleanup_error(outcome):
    if outcome.get("status") == "done":
        return None
    details = [f"project-copy cleanup status={outcome.get('status') or 'unknown'}"]
    if outcome.get("unsupported"):
        details.append(f"unsupported={','.join(outcome['unsupported'])}")
    remaining = [name for name, values in (outcome.get("remaining") or {}).items() if values]
    if remaining:
        details.append(f"remaining={','.join(remaining)}")
    if outcome.get("errors"):
        details.append(f"errors={len(outcome['errors'])}")
    return "; ".join(details)
