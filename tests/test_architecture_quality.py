import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "opencode_session"
PACKAGE_ROOT = PROJECT_ROOT / PACKAGE_NAME
MAX_SOURCE_LINES = 300
LONG_SOURCE_FILE_DECOMPOSITION_PLAN = PROJECT_ROOT / "docs" / "ocs" / "architecture-quality.md"

# Existing long source files are grandfathered at their current size so this
# gate prevents further growth, and fails when a file shrinks without lowering
# its ceiling, without forcing skipped decomposition work.
GRANDFATHERED_LONG_SOURCE_FILES = {
    "opencode_session/api_profile.py": 337,
    "opencode_session/remote_journal.py": 732,
    "opencode_session/run_services.py": 418,
    "opencode_session/schema_event_adapter.py": 378,
    "opencode_session/schema_message_adapter.py": 326,
    "opencode_session/validation_live.py": 303,
    "opencode_session/worker_field_spec.py": 386,
    "opencode_session/worker_session_provisioning.py": 438,
    "opencode_session/worker_state.py": 2395,
}

# Long-file exceptions are temporary. Each one must declare the intended
# reduction target here and appear in the decomposition plan, but debt above the
# target is not failed while it stays at the recorded current ceiling so skipped
# worker_state decomposition stays non-blocking.
LONG_SOURCE_FILE_RATCHET_TARGETS = {
    "opencode_session/api_profile.py": MAX_SOURCE_LINES,
    "opencode_session/remote_journal.py": MAX_SOURCE_LINES,
    "opencode_session/run_services.py": MAX_SOURCE_LINES,
    "opencode_session/schema_event_adapter.py": MAX_SOURCE_LINES,
    "opencode_session/schema_message_adapter.py": MAX_SOURCE_LINES,
    "opencode_session/validation_live.py": MAX_SOURCE_LINES,
    "opencode_session/worker_field_spec.py": MAX_SOURCE_LINES,
    "opencode_session/worker_session_provisioning.py": MAX_SOURCE_LINES,
    "opencode_session/worker_state.py": MAX_SOURCE_LINES,
}

# The direct worker_state cycle is an explicitly skipped review finding. Keep
# this exception exact so any new cycle, or growth of this cycle, fails CI.
GRANDFATHERED_IMPORT_CYCLES = {
    (
        "opencode_session.worker_dependencies",
        "opencode_session.worker_field_spec",
        "opencode_session.worker_state",
    ),
}

def _python_source_paths():
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _module_name(path):
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = (PACKAGE_NAME,) + relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_modules():
    return {_module_name(path): path for path in _python_source_paths()}


def _resolve_local_module(candidate, module_names):
    if candidate != PACKAGE_NAME and not candidate.startswith(PACKAGE_NAME + "."):
        return None
    parts = candidate.split(".")
    while parts:
        module_name = ".".join(parts)
        if module_name in module_names:
            return module_name
        parts.pop()
    return None


def _is_package_module(module_name, module_names):
    return any(name.startswith(module_name + ".") for name in module_names)


def _import_from_base(importer, node, module_names):
    if node.level == 0:
        return node.module

    if _is_package_module(importer, module_names):
        package_parts = importer.split(".")
    else:
        package_parts = importer.split(".")[:-1]
    if node.level > 1:
        package_parts = package_parts[: -(node.level - 1)]
    if node.module:
        package_parts = package_parts + node.module.split(".")
    return ".".join(package_parts)


def _local_import_targets(importer, node, module_names):
    candidates = []
    if isinstance(node, ast.Import):
        candidates = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        base = _import_from_base(importer, node, module_names)
        if base is None:
            return set()
        candidates.append(base)
        candidates.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")

    targets = set()
    for candidate in candidates:
        target = _resolve_local_module(candidate, module_names)
        if target is not None and target != importer:
            targets.add(target)
    return targets


def _package_import_edges():
    modules = _package_modules()
    module_names = set(modules)
    edges = {module_name: set() for module_name in module_names}

    for module_name, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                edges[module_name].update(_local_import_targets(module_name, node, module_names))
    return edges


def _strongly_connected_components(edges):
    index_by_module = {}
    lowlink_by_module = {}
    stack = []
    stacked = set()
    components = []

    def visit(module_name):
        index_by_module[module_name] = len(index_by_module)
        lowlink_by_module[module_name] = index_by_module[module_name]
        stack.append(module_name)
        stacked.add(module_name)

        for target in sorted(edges[module_name]):
            if target not in index_by_module:
                visit(target)
                lowlink_by_module[module_name] = min(
                    lowlink_by_module[module_name],
                    lowlink_by_module[target],
                )
            elif target in stacked:
                lowlink_by_module[module_name] = min(
                    lowlink_by_module[module_name],
                    index_by_module[target],
                )

        if lowlink_by_module[module_name] != index_by_module[module_name]:
            return

        component = []
        while True:
            target = stack.pop()
            stacked.remove(target)
            component.append(target)
            if target == module_name:
                break
        if len(component) > 1 or module_name in edges[module_name]:
            components.append(tuple(sorted(component)))

    for module_name in sorted(edges):
        if module_name not in index_by_module:
            visit(module_name)
    return sorted(components)


class ArchitectureQualityGateTest(unittest.TestCase):
    def test_source_files_do_not_exceed_max_length_without_grandfathering(self):
        offenders = []
        for path in _python_source_paths():
            relative_path = path.relative_to(PROJECT_ROOT).as_posix()
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            grandfathered_limit = GRANDFATHERED_LONG_SOURCE_FILES.get(relative_path)
            if grandfathered_limit is not None:
                if line_count > grandfathered_limit:
                    target = LONG_SOURCE_FILE_RATCHET_TARGETS.get(relative_path, MAX_SOURCE_LINES)
                    offenders.append(
                        f"{relative_path} grew from {grandfathered_limit} to {line_count} lines; "
                        f"ratchet target is {target}"
                    )
                elif line_count <= MAX_SOURCE_LINES:
                    offenders.append(
                        f"{relative_path} is now {line_count} lines; remove its grandfathered exception"
                    )
                elif line_count < grandfathered_limit:
                    offenders.append(
                        f"{relative_path} shrank from {grandfathered_limit} to {line_count} lines; "
                        "lower its grandfathered ceiling to keep the ratchet current"
                    )
            elif line_count > MAX_SOURCE_LINES:
                offenders.append(f"{relative_path} has {line_count} lines; max is {MAX_SOURCE_LINES}")

        self.assertEqual([], offenders)

    def test_grandfathered_long_source_files_have_explicit_ratchet_targets(self):
        plan_text = LONG_SOURCE_FILE_DECOMPOSITION_PLAN.read_text(encoding="utf-8")
        offenders = []

        grandfathered_paths = set(GRANDFATHERED_LONG_SOURCE_FILES)
        ratchet_paths = set(LONG_SOURCE_FILE_RATCHET_TARGETS)
        for relative_path in sorted(grandfathered_paths - ratchet_paths):
            offenders.append(f"{relative_path} is grandfathered without a ratchet target")
        for relative_path in sorted(ratchet_paths - grandfathered_paths):
            offenders.append(f"{relative_path} has a ratchet target but is not grandfathered")

        for relative_path, grandfathered_limit in sorted(GRANDFATHERED_LONG_SOURCE_FILES.items()):
            target = LONG_SOURCE_FILE_RATCHET_TARGETS.get(relative_path)
            if target is None:
                continue
            if target >= grandfathered_limit:
                offenders.append(
                    f"{relative_path} ratchet target {target} must be below grandfathered ceiling "
                    f"{grandfathered_limit}"
                )
            if target > MAX_SOURCE_LINES:
                offenders.append(
                    f"{relative_path} ratchet target {target} exceeds standard max {MAX_SOURCE_LINES}"
                )
            if relative_path not in plan_text:
                plan_path = LONG_SOURCE_FILE_DECOMPOSITION_PLAN.relative_to(PROJECT_ROOT).as_posix()
                offenders.append(f"{relative_path} is missing from {plan_path}")

        self.assertEqual([], offenders)

    def test_import_graph_has_no_new_cycles_beyond_grandfathered_worker_state_cycle(self):
        cycles = _strongly_connected_components(_package_import_edges())
        offenders = [cycle for cycle in cycles if cycle not in GRANDFATHERED_IMPORT_CYCLES]

        self.assertEqual([], [" -> ".join(cycle) for cycle in offenders])

    def test_core_modules_do_not_depend_on_cli_command_handlers(self):
        offenders = []
        for importer, targets in sorted(_package_import_edges().items()):
            for target in sorted(targets):
                if not (
                    target == "opencode_session.commands" or target.startswith("opencode_session.commands.")
                ):
                    continue
                if importer == "opencode_session.cli" or importer.startswith("opencode_session.commands."):
                    continue
                offenders.append(f"{importer} imports {target}")

        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
