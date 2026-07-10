def freeze_project_refresh_scope(project_plans):
    allowed_by_project = {}
    for project_plan in project_plans:
        outcome = project_plan["outcome"]
        allowed_by_project.setdefault(outcome["project_id"], set()).update(
            outcome.get("planned_project_directories") or ()
        )
    for project_plan in project_plans:
        outcome = project_plan["outcome"]
        allowed = allowed_by_project[outcome["project_id"]]
        outcome["allowed_refresh_directories"] = sorted(allowed)
        outcome["unrelated_stale_directories"] = [
            path for path in outcome.get("unrelated_stale_directories") or () if path not in allowed
        ]
        if not outcome["unrelated_stale_directories"]:
            outcome["unsupported"] = [
                reason
                for reason in outcome.get("unsupported") or ()
                if reason != "project_copy_refresh_unscoped"
            ]
        if outcome.get("mode") == "dry-run":
            outcome["status"] = "partial" if outcome.get("unsupported") else "planned"


def project_plan_error(outcome):
    detail = f"project-copy cleanup status={outcome.get('status') or 'unknown'}"
    if outcome.get("unsupported"):
        detail += f"; unsupported={','.join(outcome['unsupported'])}"
    return detail
