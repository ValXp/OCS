class RemoteMutationJournal:
    def __init__(self, field):
        self.field = field

    def record_intent(self, run, entry):
        journal = run.get(self.field)
        if not isinstance(journal, list):
            journal = []
        journal.append(dict(entry))
        run[self.field] = journal

    def mark_applied(self, run, entry_id, fields, *, missing_entry=None):
        journal = run.get(self.field)
        if not isinstance(journal, list):
            journal = []
            run[self.field] = journal
        for entry in journal:
            if isinstance(entry, dict) and entry.get("id") == entry_id:
                entry.update(dict(fields))
                return
        if missing_entry is not None:
            journal.append(dict(missing_entry))

    def finalize(self, run, entry_id):
        journal = run.get(self.field)
        if not isinstance(journal, list):
            run.pop(self.field, None)
            return
        remaining = [entry for entry in journal if not isinstance(entry, dict) or entry.get("id") != entry_id]
        if remaining:
            run[self.field] = remaining
        else:
            run.pop(self.field, None)

    def mark_cleanup_failure(self, run, entry_id, cleanup_failure):
        journal = run.get(self.field)
        if not isinstance(journal, list):
            return
        for entry in journal:
            if isinstance(entry, dict) and entry.get("id") == entry_id:
                entry["cleanup_failure"] = dict(cleanup_failure)
                return

    def pending_entries(self, run, *, kind=None):
        journal = run.get(self.field) if isinstance(run, dict) else None
        if not isinstance(journal, list):
            return ()
        return tuple(
            entry
            for entry in journal
            if isinstance(entry, dict) and (kind is None or entry.get("kind") == kind)
        )


class PersistedRemoteMutationJournal:
    def __init__(self, field, persist_run_mutation, *, now):
        self.journal = RemoteMutationJournal(field)
        self.persist_run_mutation = persist_run_mutation
        self.now = now

    def record_intent(self, run, entry):
        return self.persist_run_mutation(
            run,
            lambda latest_run: self.journal.record_intent(latest_run, entry),
        )

    def record_intent_from(self, run, entry_factory):
        def record(latest_run):
            self.journal.record_intent(latest_run, entry_factory(latest_run))

        return self.persist_run_mutation(run, record)

    def mark_applied(self, run, entry_id, fields, *, before_mark=None, missing_entry=None):
        def record(latest_run):
            if before_mark is not None:
                before_mark(latest_run)
            self.journal.mark_applied(latest_run, entry_id, fields, missing_entry=missing_entry)

        return self.persist_run_mutation(run, record)

    def finalize(self, run, entry_id, *, before_finalize=None):
        def record(latest_run):
            if before_finalize is not None:
                before_finalize(latest_run)
            self.journal.finalize(latest_run, entry_id)

        return self.persist_run_mutation(run, record)

    def discard_intent_best_effort(self, run, entry_id, *, operation):
        return self._finalize_best_effort(run, entry_id, operation=operation)

    def finalize_best_effort(self, run, entry_id, *, operation):
        return self._finalize_best_effort(run, entry_id, operation=operation)

    def pending_entries(self, run, *, kind=None):
        return self.journal.pending_entries(run, kind=kind)

    def _finalize_best_effort(self, run, entry_id, *, operation):
        try:
            return self.finalize(run, entry_id)
        except Exception as cleanup_error:
            return self.record_cleanup_failure_best_effort(
                run,
                entry_id,
                cleanup_error,
                operation=operation,
            )

    def record_cleanup_failure_best_effort(self, run, entry_id, cleanup_error, *, operation):
        cleanup_failure = {
            "operation": operation,
            "error_type": type(cleanup_error).__name__,
            "message": str(cleanup_error),
            "recorded_at": self.now(),
        }
        try:
            return self.persist_run_mutation(
                run,
                lambda latest_run: self.journal.mark_cleanup_failure(latest_run, entry_id, cleanup_failure),
            )
        except Exception:
            return run
