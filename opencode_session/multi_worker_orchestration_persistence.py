from opencode_session.run_persistence import (
    persist_run_mutation,
    persist_run_summary,
    persist_worker_transitions,
)
from opencode_session.run_record import run_worker


class DependencyOrderedSerialRunPersistence:
    def __init__(self, store, *, now, refresh_run_summary):
        self.store = store
        self.now = now
        self.refresh_run_summary = refresh_run_summary

    def persist_mutation(self, run, mutator):
        return persist_run_mutation(self.store, run, mutator, now=self.now)

    def persist_worker_transition(self, run, transition):
        return persist_worker_transitions(
            self.store,
            run,
            [transition],
            refresh_run_summary=self.refresh_run_summary,
            now=self.now,
        )

    def persist_worker_execution_transition(self, run, worker, transition):
        result = self.persist_worker_transition(run, transition)
        persisted_worker = result.workers[0] if result.workers else run_worker(result.run, transition.worker_id)
        return result.run, persisted_worker or worker

    def persist_transitions(self, run, transitions):
        result = persist_worker_transitions(
            self.store,
            run,
            transitions,
            refresh_run_summary=self.refresh_run_summary,
            now=self.now,
        )
        return result.run

    def persist_summary(self, run):
        return persist_run_summary(
            self.store,
            run,
            refresh_run_summary=self.refresh_run_summary,
            now=self.now,
        )
