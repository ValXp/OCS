from dataclasses import dataclass

from opencode_session.worker_status import (
    is_dependency_blockable_status,
    is_failed_dependency_status,
    is_runnable_status,
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
    worker_ids_in_dependency_order, cycles = _dependency_order_and_cycles(workers)
    invalid_graph_blockers = _invalid_graph_blockers(workers, cycles)
    dependency_blockers = _dependency_blockers(workers)
    blockers = _merge_blocker_maps(invalid_graph_blockers, dependency_blockers)
    return WorkerDependencyAnalysis(
        worker_ids_in_dependency_order=tuple(worker_ids_in_dependency_order),
        ready_worker_ids=tuple(_ready_worker_ids(workers, blockers)),
        blockers_by_worker_id=blockers,
        invalid_graph_blockers_by_worker_id=invalid_graph_blockers,
        dependency_blockers_by_worker_id=dependency_blockers,
    )


def _dependency_order_and_cycles(workers):
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

        worker = workers.get(worker_id)
        if not isinstance(worker, dict):
            return

        visiting.append(worker_id)
        for dependency in _worker_dependencies(worker):
            visit(dependency)
        visiting.pop()
        visited.add(worker_id)
        ordered.append(worker_id)

    for worker_id in sorted(workers):
        visit(worker_id)
    return ordered, cycles


def _ready_worker_ids(workers, blockers_by_worker_id):
    ready = []
    for worker_id in sorted(workers):
        if worker_id in blockers_by_worker_id:
            continue
        worker = workers[worker_id]
        if not _runnable_prompted_worker(worker):
            continue
        if _dependencies_done(worker, workers):
            ready.append(worker_id)
    return ready


def _invalid_graph_blockers(workers, cycles):
    blockers_by_worker_id = {}
    invalid_worker_ids = set()

    for cycle in cycles:
        blocker = f"dependency-cycle:{'->'.join(cycle)}"
        for worker_id in set(cycle[:-1]):
            worker = workers.get(worker_id)
            if _dependency_blockable_prompted_worker(worker):
                _add_blocker(blockers_by_worker_id, worker_id, blocker)
                invalid_worker_ids.add(worker_id)

    for worker_id in sorted(workers):
        if worker_id in invalid_worker_ids:
            continue
        worker = workers.get(worker_id)
        if not _dependency_blockable_prompted_worker(worker):
            continue
        blockers = [
            f"dependency-not-runnable:{dependency}"
            for dependency in _worker_dependencies(worker)
            if _non_runnable_dependency(workers.get(dependency))
        ]
        if blockers:
            blockers_by_worker_id[worker_id] = tuple(blockers)
            invalid_worker_ids.add(worker_id)

    while True:
        newly_blocked = set()
        for worker_id in sorted(workers):
            if worker_id in invalid_worker_ids:
                continue
            worker = workers.get(worker_id)
            if not _dependency_blockable_prompted_worker(worker):
                continue
            blockers = [
                f"dependency:{dependency}"
                for dependency in _worker_dependencies(worker)
                if dependency in invalid_worker_ids
            ]
            if blockers:
                blockers_by_worker_id[worker_id] = tuple(blockers)
                newly_blocked.add(worker_id)
        if not newly_blocked:
            break
        invalid_worker_ids.update(newly_blocked)

    return blockers_by_worker_id


def _dependency_blockers(workers):
    blockers_by_worker_id = {}
    blocked_worker_ids = set()

    for worker_id in sorted(workers):
        worker = workers.get(worker_id)
        if not _dependency_blockable_prompted_worker(worker):
            continue
        blockers = []
        for dependency in _worker_dependencies(worker):
            dependency_worker = workers.get(dependency)
            if not isinstance(dependency_worker, dict) or is_failed_dependency_status(dependency_worker.get("status")):
                blockers.append(f"dependency:{dependency}")
        if blockers:
            blockers_by_worker_id[worker_id] = tuple(blockers)
            blocked_worker_ids.add(worker_id)

    while True:
        newly_blocked = set()
        for worker_id in sorted(workers):
            if worker_id in blocked_worker_ids:
                continue
            worker = workers.get(worker_id)
            if not _dependency_blockable_prompted_worker(worker):
                continue
            blockers = [
                f"dependency:{dependency}"
                for dependency in _worker_dependencies(worker)
                if dependency in blocked_worker_ids
            ]
            if blockers:
                blockers_by_worker_id[worker_id] = tuple(blockers)
                newly_blocked.add(worker_id)
        if not newly_blocked:
            break
        blocked_worker_ids.update(newly_blocked)

    return blockers_by_worker_id


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


def _dependencies_done(worker, workers):
    for dependency in _worker_dependencies(worker):
        dependency_worker = workers.get(dependency)
        if not isinstance(dependency_worker, dict) or dependency_worker.get("status") != "done":
            return False
    return True


def _non_runnable_dependency(worker):
    if not isinstance(worker, dict):
        return False
    if not is_runnable_status(worker.get("status")):
        return False
    return not _worker_has_prompt(worker)


def _runnable_prompted_worker(worker):
    return (
        isinstance(worker, dict)
        and _worker_has_prompt(worker)
        and is_runnable_status(worker.get("status"))
    )


def _dependency_blockable_prompted_worker(worker):
    return (
        isinstance(worker, dict)
        and _worker_has_prompt(worker)
        and is_dependency_blockable_status(worker.get("status"))
    )


def _worker_has_prompt(worker):
    prompt = worker.get("prompt")
    return prompt is not None and bool(str(prompt))


def _worker_dependencies(worker):
    dependencies = worker.get("dependencies", [])
    return dependencies if isinstance(dependencies, list) else []


def _add_blocker(blockers_by_worker_id, worker_id, blocker):
    blockers = list(blockers_by_worker_id.get(worker_id, ()))
    blockers.append(blocker)
    blockers_by_worker_id[worker_id] = tuple(blockers)
