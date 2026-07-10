from opencode_session.api_client import OpenCodeApiClient
from opencode_session.api_transport import OpenCodeApiError
from opencode_session.commands.rendering import CommandResult, render_command_result
from opencode_session.project_metadata import (
    ProjectMetadataNotFound,
    ProjectMetadataService,
    ProjectMetadataUnsupported,
)
from opencode_session.project_metadata_cleanup import ProjectCopyCleanupError, ProjectCopyCleanupService
from opencode_session.project_metadata_formatting import (
    format_cleanup_compact,
    format_directory_compact,
    format_directory_table,
    format_project_compact,
    format_project_table,
    format_workspace_compact,
    format_workspace_table,
)


def add_project_parsers(subparsers, *, add_server_argument, add_output_arguments, handler):
    project_parser = subparsers.add_parser("project", help="inspect OpenCode project metadata")
    project_subparsers = project_parser.add_subparsers(dest="project_command")
    project_subparsers.required = True

    project_list = project_subparsers.add_parser("list", help="list projects")
    project_list.add_argument("--directory", help="only show projects containing this directory")
    _finish_parser(project_list, add_server_argument, add_output_arguments, handler)

    project_inspect = project_subparsers.add_parser("inspect", help="inspect one project")
    project_inspect.add_argument("project_id", help="project ID")
    _finish_parser(project_inspect, add_server_argument, add_output_arguments, handler)

    project_directories = project_subparsers.add_parser("directories", help="list project directories")
    project_directories.add_argument("project_id", help="project ID")
    project_directories.add_argument("--directory", help="only show this directory")
    _finish_parser(project_directories, add_server_argument, add_output_arguments, handler)

    workspace_parser = subparsers.add_parser("workspace", help="inspect OpenCode workspace metadata")
    workspace_subparsers = workspace_parser.add_subparsers(dest="workspace_command")
    workspace_subparsers.required = True
    workspace_list = workspace_subparsers.add_parser("list", help="list workspaces")
    workspace_list.add_argument("--project-id", help="only show workspaces for this project")
    workspace_list.add_argument("--directory", help="only show workspaces for this directory")
    _finish_parser(workspace_list, add_server_argument, add_output_arguments, handler)

    copy_parser = subparsers.add_parser("project-copy", help="manage OpenCode project-copy metadata")
    copy_subparsers = copy_parser.add_subparsers(dest="project_copy_command")
    copy_subparsers.required = True
    cleanup = copy_subparsers.add_parser("cleanup", help="clean metadata for deleted project-copy directories")
    cleanup.add_argument("project_id", help="project ID")
    cleanup.add_argument("--directory-prefix", required=True, help="path-component prefix owned by this cleanup")
    cleanup.add_argument("--apply", action="store_true", help="apply the cleanup; default is a dry run")
    add_server_argument(cleanup)
    cleanup.add_argument("--json", action="store_true", help="print cleanup result JSON")
    cleanup.set_defaults(command_handler=handler)


def handle_project_metadata_command(
    args,
    *,
    print_error,
    unavailable_exit,
    unsupported_exit,
    noinput_exit,
    dataerr_exit,
    client_factory=OpenCodeApiClient,
):
    try:
        metadata = ProjectMetadataService(client_factory(args.server))
        if args.command == "project":
            return _handle_project(args, metadata)
        if args.command == "workspace":
            return _handle_workspace(args, metadata)
        if args.command == "project-copy":
            return _handle_project_copy(
                args,
                metadata,
                print_error=print_error,
                unavailable_exit=unavailable_exit,
                unsupported_exit=unsupported_exit,
            )
    except ProjectMetadataUnsupported as error:
        print_error(str(error))
        return unsupported_exit
    except ProjectMetadataNotFound as error:
        print_error(str(error))
        return noinput_exit
    except ProjectCopyCleanupError as error:
        print_error(str(error))
        return dataerr_exit
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit
    return 64


def _handle_project(args, metadata):
    if args.project_command == "list":
        result = metadata.list_projects(directory=args.directory)
        return _render_records(args, result, format_project_compact, format_project_table)
    if args.project_command == "inspect":
        result = metadata.inspect_project(args.project_id)
        return _render_records(args, result, format_project_compact, format_project_table, singular=True)
    if args.project_command == "directories":
        result = metadata.list_project_directories(args.project_id, directory=args.directory)
        return _render_records(args, result, format_directory_compact, format_directory_table)
    return 64


def _handle_workspace(args, metadata):
    if args.workspace_command != "list":
        return 64
    result = metadata.list_workspaces(project_id=args.project_id, directory=args.directory)
    return _render_records(args, result, format_workspace_compact, format_workspace_table)


def _handle_project_copy(args, metadata, *, print_error, unavailable_exit, unsupported_exit):
    if args.project_copy_command != "cleanup":
        return 64
    result = ProjectCopyCleanupService(metadata).cleanup(
        args.project_id,
        args.directory_prefix,
        apply=args.apply,
    )
    exit_code = 0
    warnings = ()
    if result["status"] == "partial":
        exit_code = unsupported_exit
        warnings = ("project-copy cleanup is partial; unsupported metadata remains",)
    elif result["status"] == "failed":
        exit_code = unavailable_exit
        warnings = ("project-copy cleanup failed; inspect errors and remaining metadata",)
    return render_command_result(
        args,
        CommandResult(
            result,
            compact=format_cleanup_compact(result),
            exit_code=exit_code,
            warnings=warnings,
        ),
        print_error=print_error,
    )


def _render_records(args, result, compact_formatter, table_formatter, *, singular=False):
    records = result.records
    data = records[0] if singular else records
    compact = None
    if records:
        compact = compact_formatter(records[0]) if singular or len(records) == 1 else table_formatter(records)
    return render_command_result(args, data, raw_body=result.raw_body, compact=compact)


def _finish_parser(parser, add_server_argument, add_output_arguments, handler):
    add_server_argument(parser)
    add_output_arguments(parser)
    parser.set_defaults(command_handler=handler)
