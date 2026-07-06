from dataclasses import dataclass

from opencode_session.worker_domain import (
    WorkerSchedulingState,
    is_dependency_blockable_worker,
    is_executable_worker,
    is_failed_dependency_status,
    is_runnable_status,
    worker_has_prompt,
)


@dataclass(frozen=True)
class WorkerDependencyAnalysis:
    worker_ids_in_dependency_order: tuple
    ready_worker_ids: tuple
    blockers_by_worker_id: dict
    invalid_graph_blockers_by_worker_id: dict
    dependency_blockers_by_worker_id: dict


def analyze_worker_dependencies(workers):
    workers = workers if isinstance(workers, dict) else {}
    dependency_graph = _worker_dependency_graph(workers)
    worker_ids_in_dependency_order, cycles = _dependency_order_and_cycles(workers, dependency_graph)
    seeded_blockers = {
        "invalid": _invalid_graph_blocker_seeds(workers, dependency_graph, cycles),
        "dependency": _dependency_blocker_seeds(workers, dependency_graph),
    }
    propagated_blockers = _propagate_blocker_maps(workers, dependency_graph, seeded_blockers)
    invalid_graph_blockers = propagated_blockers["invalid"]
    dependency_blockers = propagated_blockers["dependency"]
    blockers = _merge_blocker_maps(invalid_graph_blockers, dependency_blockers)
    return WorkerDependencyAnalysis(
        worker_ids_in_dependency_order=tuple(worker_ids_in_dependency_order),
        ready_worker_ids=tuple(_ready_worker_ids(workers, dependency_graph, blockers)),
        blockers_by_worker_id=blockers,
        invalid_graph_blockers_by_worker_id=invalid_graph_blockers,
        dependency_blockers_by_worker_id=dependency_blockers,
    )


def _worker_dependency_graph(workers):
    return {
        worker_id: tuple(_worker_dependencies(worker))
        for worker_id, worker in workers.items()
        if isinstance(worker, dict)
    }


def _dependency_order_and_cycles(workers, dependency_graph):
    ordered = []
    cycles = []
    visited = set()
    visiting = []

    def visit(worker_id):
        if worker_id in visited:
            return
        if worker_id in visiting:
            cycles.append(visiting[visiting.index(worker_id) :] + [worker_id])
            return

        if worker_id not in dependency_graph:
            return

        visiting.append(worker_id)
        for dependency in dependency_graph.get(worker_id, ()):
            visit(dependency)
        visiting.pop()
        visited.add(worker_id)
        ordered.append(worker_id)

    for worker_id in sorted(workers):
        visit(worker_id)
    return ordered, cycles


def _ready_worker_ids(workers, dependency_graph, blockers_by_worker_id):
    ready = []
    for worker_id in sorted(workers):
        if worker_id in blockers_by_worker_id:
            continue
        worker = workers[worker_id]
        if not _runnable_prompted_worker(worker):
            continue
        if _dependencies_done(worker_id, workers, dependency_graph):
            ready.append(worker_id)
    return ready


def _invalid_graph_blocker_seeds(workers, dependency_graph, cycles):
    blockers_by_worker_id = {}

    for cycle in cycles:
        blocker = f"dependency-cycle:{'->'.join(cycle)}"
        for worker_id in set(cycle[:-1]):
            worker = workers.get(worker_id)
            if _dependency_blockable_prompted_worker(worker):
                _add_blocker(blockers_by_worker_id, worker_id, blocker)

    for worker_id in sorted(workers):
        if worker_id in blockers_by_worker_id:
            continue
        worker = workers.get(worker_id)
        if not _dependency_blockable_prompted_worker(worker):
            continue
        blockers = [
            f"dependency-not-runnable:{dependency}"
            for dependency in dependency_graph.get(worker_id, ())
            if _non_runnable_dependency(workers.get(dependency))
        ]
        if blockers:
            blockers_by_worker_id[worker_id] = tuple(blockers)

    return blockers_by_worker_id


def _dependency_blocker_seeds(workers, dependency_graph):
    blockers_by_worker_id = {}

    for worker_id in sorted(workers):
        worker = workers.get(worker_id)
        if not _dependency_blockable_prompted_worker(worker):
            continue
        blockers = []
        for dependency in dependency_graph.get(worker_id, ()):
            dependency_worker = workers.get(dependency)
            if not isinstance(dependency_worker, dict) or is_failed_dependency_status(_worker_status(dependency_worker)):
                blockers.append(f"dependency:{dependency}")
        if blockers:
            blockers_by_worker_id[worker_id] = tuple(blockers)

    return blockers_by_worker_id


def _propagate_blocker_maps(workers, dependency_graph, seeded_blocker_maps):
    blockers_by_kind = {kind: dict(blockers) for kind, blockers in seeded_blocker_maps.items()}
    blocked_ids_by_kind = {kind: set(blockers) for kind, blockers in blockers_by_kind.items()}
    while True:
        newly_blocked_by_kind = {kind: set() for kind in blockers_by_kind}
        for kind, blockers_by_worker_id in blockers_by_kind.items():
            blocked_worker_ids = blocked_ids_by_kind[kind]
            for worker_id in sorted(workers):
                if worker_id in blocked_worker_ids:
                    continue
                worker = workers.get(worker_id)
                if not _dependency_blockable_prompted_worker(worker):
                    continue
                blockers = [
                    f"dependency:{dependency}"
                    for dependency in dependency_graph.get(worker_id, ())
                    if dependency in blocked_worker_ids
                ]
                if blockers:
                    blockers_by_worker_id[worker_id] = tuple(blockers)
                    newly_blocked_by_kind[kind].add(worker_id)
        if not any(newly_blocked_by_kind.values()):
            break
        for kind, newly_blocked in newly_blocked_by_kind.items():
            blocked_ids_by_kind[kind].update(newly_blocked)

    return blockers_by_kind


def _merge_blocker_maps(*blocker_maps):
    merged = {}
    for blocker_map in blocker_maps:
        for worker_id, blockers in blocker_map.items():
            worker_blockers = list(merged.get(worker_id, ()))
            for blocker in blockers:
                if blocker not in worker_blockers:
                    worker_blockers.append(blocker)
            merged[worker_id] = tuple(worker_blockers)
    return merged


def _dependencies_done(worker_id, workers, dependency_graph):
    for dependency in dependency_graph.get(worker_id, ()):
        dependency_worker = workers.get(dependency)
        if not isinstance(dependency_worker, dict) or _worker_status(dependency_worker) != "done":
            return False
    return True


def _non_runnable_dependency(worker):
    if not isinstance(worker, dict):
        return False
    if not is_runnable_status(_worker_status(worker)):
        return False
    return not _worker_has_prompt(worker)


def _runnable_prompted_worker(worker):
    return is_executable_worker(worker)


def _dependency_blockable_prompted_worker(worker):
    return is_dependency_blockable_worker(worker)


def _worker_has_prompt(worker):
    return worker_has_prompt(worker)


def _worker_dependencies(worker):
    dependencies = worker.get("dependencies", [])
    return dependencies if isinstance(dependencies, list) else []


def _worker_status(worker):
    return WorkerSchedulingState.from_worker(worker).status


def _add_blocker(blockers_by_worker_id, worker_id, blocker):
    blockers = list(blockers_by_worker_id.get(worker_id, ()))
    blockers.append(blocker)
    blockers_by_worker_id[worker_id] = tuple(blockers)
