from opencode_session.formatting import compact_value, format_table
from opencode_session.project_metadata import workspace_project_id


def format_project_compact(project):
    return " ".join(
        [
            "project",
            f"id={compact_value(project.get('id'))}",
            f"name={compact_value(project.get('name'))}",
            f"worktree={compact_value(project.get('worktree') or project.get('directory'))}",
            f"sandboxes={len(project.get('sandboxes') or [])}",
        ]
    )


def format_project_table(projects):
    return format_table(
        ("ID", "NAME", "WORKTREE", "SANDBOXES"),
        [
            (
                project.get("id"),
                project.get("name"),
                project.get("worktree") or project.get("directory"),
                len(project.get("sandboxes") or []),
            )
            for project in projects
        ],
    )


def format_directory_compact(record):
    return " ".join(
        [
            "project-directory",
            f"directory={compact_value(record.get('directory'))}",
            f"strategy={compact_value(record.get('strategy'))}",
        ]
    )


def format_directory_table(records):
    return format_table(
        ("DIRECTORY", "STRATEGY"),
        [(record.get("directory"), record.get("strategy")) for record in records],
    )


def format_workspace_compact(workspace):
    return " ".join(
        [
            "workspace",
            f"id={compact_value(workspace.get('id'))}",
            f"project={compact_value(workspace_project_id(workspace))}",
            f"directory={compact_value(workspace.get('directory'))}",
            f"name={compact_value(workspace.get('name'))}",
        ]
    )


def format_workspace_table(workspaces):
    return format_table(
        ("ID", "PROJECT", "DIRECTORY", "NAME", "TYPE"),
        [
            (
                workspace.get("id"),
                workspace_project_id(workspace),
                workspace.get("directory"),
                workspace.get("name"),
                workspace.get("type"),
            )
            for workspace in workspaces
        ],
    )


def format_cleanup_compact(result):
    return " ".join(
        [
            "project-copy-cleanup",
            f"status={result['status']}",
            f"mode={result['mode']}",
            f"project={compact_value(result['project_id'])}",
            f"directories={len(result['planned_directories'])}",
            f"workspaces={len(result['planned_workspaces'])}",
            f"removed={len(result['removed_workspaces'])}",
            f"unsupported={compact_value(','.join(result['unsupported']))}",
        ]
    )
