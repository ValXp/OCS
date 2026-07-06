from dataclasses import dataclass


@dataclass(frozen=True)
class RemoteMutationOperation:
    kind: str
    discard_cleanup_operation: str
    finalize_cleanup_operation: str


class RemoteMutationTransaction:
    def __init__(
        self,
        journal,
        entry_id,
        operation,
    ):
        self.journal = journal
        self.entry_id = entry_id
        self.operation = operation

    def record_intent(self, run, record):
        self._validate_record_identity(record)
        return self.journal.record_intent(run, record)

    def record_intent_from(self, run, record_factory):
        return self.journal.record_intent_from(
            run,
            lambda latest_run: self._validated_record(record_factory(latest_run)),
        )

    def mark_applied(self, run, record, *, mutate_run=None, append_if_missing=False):
        self._validate_record_identity(record)
        return self.journal.mark_applied(
            run,
            self.entry_id,
            record,
            mutate_run=mutate_run,
            append_if_missing=append_if_missing,
        )

    def finalize(self, run, *, mutate_run=None):
        return self.journal.finalize(run, self.entry_id, mutate_run=mutate_run)

    def discard_intent_best_effort(self, run):
        return self.journal.discard_intent_best_effort(
            run,
            self.entry_id,
            operation=self.operation.discard_cleanup_operation,
        )

    def finalize_best_effort(self, run):
        return self.journal.finalize_best_effort(
            run,
            self.entry_id,
            operation=self.operation.finalize_cleanup_operation,
        )

    def _validated_record(self, record):
        self._validate_record_identity(record)
        return record

    def _validate_record_identity(self, record):
        entry = _journal_entry(record)
        if entry.get("id") != self.entry_id:
            raise ValueError(f"remote journal record id must be {self.entry_id!r}")
        if entry.get("kind") != self.operation.kind:
            raise ValueError(f"remote journal record kind must be {self.operation.kind!r}")


class RemoteMutationRecovery:
    def __init__(self, field):
        self.journal = RemoteMutationJournal(field)

    def pending_entries(self, run, *, kind=None):
        return self.journal.pending_entries(run, kind=kind)

    def values_by_owner(
        self,
        run,
        *,
        kind,
        owner_field,
        value_fields=(),
        list_fields=(),
        required_fields=None,
    ):
        values_by_owner = {}
        for entry in self.pending_entries(run, kind=kind):
            if not self._matches_required_fields(entry, required_fields):
                continue
            owner = entry.get(owner_field)
            if not owner:
                continue
            values = values_by_owner.setdefault(owner, [])
            for list_field in list_fields:
                for value in _string_list(entry.get(list_field)):
                    _append_unique(values, value)
            for value_field in value_fields:
                _append_unique(values, entry.get(value_field))
        return {owner: values for owner, values in values_by_owner.items() if values}

    def _matches_required_fields(self, entry, required_fields):
        if not required_fields:
            return True
        for field, expected in required_fields.items():
            if entry.get(field) != expected:
                return False
        return True


class RemoteMutationJournal:
    def __init__(self, field):
        self.field = field

    def record_intent(self, run, entry):
        journal = run.get(self.field)
        if not isinstance(journal, list):
            journal = []
        journal.append(_journal_entry(entry))
        run[self.field] = journal

    def mark_applied(self, run, entry_id, record, *, append_if_missing=False):
        journal = run.get(self.field)
        if not isinstance(journal, list):
            journal = []
            run[self.field] = journal
        update = _journal_update(record)
        for entry in journal:
            if isinstance(entry, dict) and entry.get("id") == entry_id:
                entry.update(update)
                return
        if append_if_missing:
            journal.append(_journal_entry(record))

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

    def transaction(self, entry_id, operation):
        return RemoteMutationTransaction(
            self,
            entry_id,
            operation,
        )

    def record_intent_from(self, run, entry_factory):
        def record(latest_run):
            self.journal.record_intent(latest_run, entry_factory(latest_run))

        return self.persist_run_mutation(run, record)

    def mark_applied(self, run, entry_id, record, *, mutate_run=None, append_if_missing=False):
        def persisted_mutation(latest_run):
            if mutate_run is not None:
                mutate_run(latest_run)
            self.journal.mark_applied(latest_run, entry_id, record, append_if_missing=append_if_missing)

        return self.persist_run_mutation(run, persisted_mutation)

    def finalize(self, run, entry_id, *, mutate_run=None):
        def persisted_mutation(latest_run):
            if mutate_run is not None:
                mutate_run(latest_run)
            self.journal.finalize(latest_run, entry_id)

        return self.persist_run_mutation(run, persisted_mutation)

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


def _string_list(value):
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _append_unique(values, value):
    if isinstance(value, str) and value and value not in values:
        values.append(value)


def _journal_entry(record):
    if hasattr(record, "to_journal_entry"):
        entry = record.to_journal_entry()
    elif isinstance(record, dict):
        entry = record
    else:
        raise TypeError("remote journal records must provide to_journal_entry")
    if not isinstance(entry, dict):
        raise TypeError("remote journal record serialization must be a dict")
    return dict(entry)


def _journal_update(record):
    if hasattr(record, "to_journal_update"):
        update = record.to_journal_update()
        if not isinstance(update, dict):
            raise TypeError("remote journal record update serialization must be a dict")
        return dict(update)
    return _journal_entry(record)
