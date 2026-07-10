from copy import deepcopy


RESOURCE_LIST_FIELDS = ("worktrees", "logs", "project_copies")


def normalize_run_resources(value):
    source = value if isinstance(value, dict) else {}
    resources = {key: deepcopy(item) for key, item in source.items() if key not in RESOURCE_LIST_FIELDS}
    for field_name in RESOURCE_LIST_FIELDS:
        records = source.get(field_name)
        resources[field_name] = [deepcopy(record) for record in records if isinstance(record, dict)] if isinstance(records, list) else []
    return resources


def ensure_run_resources(run):
    resources = normalize_run_resources(run.get("resources"))
    run["resources"] = resources
    return resources
