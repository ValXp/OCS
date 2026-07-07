from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Optional

from opencode_session.run_store import RunStoreError


_PERSISTENCE_ERRORS = (RunStoreError, OSError)
_RESERVED_RECORD_FIELDS = {"id", "kind"}


@dataclass(frozen=True)
class RemoteJournalRecord:
    id: str
    kind: str
    fields: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        _require_non_empty_string(self.id, "record id")
        _require_non_empty_string(self.kind, "record kind")
        if not isinstance(self.fields, Mapping):
            raise TypeError("remote journal record fields must be a mapping")
        fields = dict(self.fields)
        reserved = _RESERVED_RECORD_FIELDS.intersection(fields)
        if reserved:
            names = ", ".join(sorted(reserved))
            raise ValueError(f"remote journal record fields must not include {names}")
        for field_name in fields:
            if not isinstance(field_name, str):
                raise TypeError("remote journal record field names must be strings")
        object.__setattr__(self, "fields", fields)

    def get(self, field_name, default=None):
        if field_name == "id":
            return self.id
        if field_name == "kind":
            return self.kind
        return self.fields.get(field_name, default)

    def to_journal_entry(self):
        return {"id": self.id, "kind": self.kind, **dict(self.fields)}


@dataclass(frozen=True)
class RemoteMutationOperation:
    kind: str
    discard_cleanup_operation: str
    finalize_cleanup_operation: str
    call_remote_operation: str

    def __post_init__(self):
        _require_non_empty_string(self.kind, "kind")
        _require_non_empty_string(
            self.discard_cleanup_operation,
            "discard_cleanup_operation",
        )
        _require_non_empty_string(
            self.finalize_cleanup_operation,
            "finalize_cleanup_operation",
        )
        _require_non_empty_string(
            self.call_remote_operation,
            "call_remote_operation",
        )


@dataclass(frozen=True)
class RemoteMutationResult:
    journal_record: Optional[RemoteJournalRecord] = None
    mutate_run: Optional[object] = None
    append_if_missing: bool = False
    finalize: bool = True

    def __post_init__(self):
        if self.journal_record is not None and not isinstance(self.journal_record, RemoteJournalRecord):
            raise TypeError("remote mutation journal_record must be a RemoteJournalRecord")
        if self.mutate_run is not None and not callable(self.mutate_run):
            raise TypeError("remote mutation run update must be callable")


@dataclass(frozen=True)
class RemoteMutationExecution:
    run: object
    remote_result: object
    intent: RemoteJournalRecord


@dataclass(frozen=True)
class RecordedIntent:
    run: object
    intent: RemoteJournalRecord


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

    def runner(self):
        return RemoteMutationRunner(self)

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

    def apply_result(self, run, result):
        if not isinstance(result, RemoteMutationResult):
            raise TypeError("remote mutation result must be a RemoteMutationResult")
        if result.journal_record is not None:
            run = self.mark_applied(
                run,
                result.journal_record,
                mutate_run=result.mutate_run,
                append_if_missing=result.append_if_missing,
            )
            if result.finalize:
                return self.finalize(run)
            return run
        if result.mutate_run is not None and not result.finalize:
            raise ValueError("remote mutation run updates require a journal record or finalization")
        if result.finalize:
            return self.finalize(run, mutate_run=result.mutate_run)
        return run

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

    def mark_uncertain_best_effort(self, run, error):
        return self.journal.mark_uncertain_best_effort(
            run,
            self.entry_id,
            error,
            operation=self.operation.call_remote_operation,
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


class RemoteMutationRunner:
    def __init__(self, transaction):
        self.transaction = transaction

    def execute(self, run, *, intent_factory, call_remote, apply_result=None):
        recorded = self.transaction.record_intent_from(run, intent_factory)
        run = recorded.run
        intent = recorded.intent
        try:
            remote_result = call_remote(run, intent)
        except Exception as remote_error:
            self.transaction.mark_uncertain_best_effort(run, remote_error)
            raise

        result = RemoteMutationResult()
        if apply_result is not None:
            result = apply_result(remote_result, intent)
            if result is None:
                result = RemoteMutationResult()
        run = self.transaction.apply_result(run, result)
        return RemoteMutationExecution(run=run, remote_result=remote_result, intent=intent)


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
        entry = _journal_entry(record)
        if entry.get("id") != entry_id:
            raise ValueError(f"remote journal record id must be {entry_id!r}")
        update = _journal_update(entry)
        for journal_entry in journal:
            if isinstance(journal_entry, dict) and journal_entry.get("id") == entry_id:
                journal_entry.update(update)
                return
        if append_if_missing:
            journal.append(entry)

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

    def mark_uncertain(self, run, entry_id, uncertainty):
        journal = run.get(self.field)
        if not isinstance(journal, list):
            return
        for entry in journal:
            if isinstance(entry, dict) and entry.get("id") == entry_id:
                entry["status"] = "uncertain"
                entry["uncertain_failure"] = dict(uncertainty)
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
        intent = _UNSET

        def record(latest_run):
            nonlocal intent
            intent = entry_factory(latest_run)
            self.journal.record_intent(latest_run, intent)

        persisted_run = self.persist_run_mutation(run, record)
        if intent is _UNSET:
            raise RuntimeError("remote mutation persistence did not record an intent")
        return RecordedIntent(run=persisted_run, intent=intent)

    def mark_applied(self, run, entry_id, record, *, mutate_run=None, append_if_missing=False):
        def persisted_mutation(latest_run):
            if mutate_run is not None:
                _apply_run_update(mutate_run, latest_run)
            self.journal.mark_applied(latest_run, entry_id, record, append_if_missing=append_if_missing)

        return self.persist_run_mutation(run, persisted_mutation)

    def finalize(self, run, entry_id, *, mutate_run=None):
        def persisted_mutation(latest_run):
            if mutate_run is not None:
                _apply_run_update(mutate_run, latest_run)
            self.journal.finalize(latest_run, entry_id)

        return self.persist_run_mutation(run, persisted_mutation)

    def discard_intent_best_effort(self, run, entry_id, *, operation):
        return self._finalize_best_effort(run, entry_id, operation=operation)

    def finalize_best_effort(self, run, entry_id, *, operation):
        return self._finalize_best_effort(run, entry_id, operation=operation)

    def pending_entries(self, run, *, kind=None):
        return self.journal.pending_entries(run, kind=kind)

    def mark_uncertain_best_effort(self, run, entry_id, remote_error, *, operation):
        uncertainty = {
            "operation": _remote_mutation_operation_name(operation),
            "error_type": type(remote_error).__name__,
            "message": str(remote_error),
            "recorded_at": self.now(),
        }
        try:
            return self.persist_run_mutation(
                run,
                lambda latest_run: self.journal.mark_uncertain(latest_run, entry_id, uncertainty),
            )
        except _PERSISTENCE_ERRORS:
            return run

    def _finalize_best_effort(self, run, entry_id, *, operation):
        try:
            return self.finalize(run, entry_id)
        except _PERSISTENCE_ERRORS as cleanup_error:
            return self.record_cleanup_failure_best_effort(
                run,
                entry_id,
                cleanup_error,
                operation=operation,
            )

    def record_cleanup_failure_best_effort(self, run, entry_id, cleanup_error, *, operation):
        cleanup_failure = {
            "operation": _remote_mutation_operation_name(operation),
            "error_type": type(cleanup_error).__name__,
            "message": str(cleanup_error),
            "recorded_at": self.now(),
        }
        try:
            return self.persist_run_mutation(
                run,
                lambda latest_run: self.journal.mark_cleanup_failure(latest_run, entry_id, cleanup_failure),
            )
        except _PERSISTENCE_ERRORS:
            return run


_UNSET = object()


def _string_list(value):
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _append_unique(values, value):
    if isinstance(value, str) and value and value not in values:
        values.append(value)


def _apply_run_update(run_update, run):
    if not callable(run_update):
        raise TypeError("remote mutation run updates must be callable")
    run_update(run)


def _journal_entry(record):
    if not isinstance(record, RemoteJournalRecord):
        raise TypeError("remote journal records must be RemoteJournalRecord instances")
    entry = record.to_journal_entry()
    if not isinstance(entry, dict):
        raise TypeError("remote journal record serialization must be a dict")
    return dict(entry)


def _journal_update(entry):
    return {key: value for key, value in entry.items() if key not in _RESERVED_RECORD_FIELDS}


def _require_non_empty_string(value, field_name):
    if not isinstance(value, str) or not value:
        raise TypeError(f"remote mutation {field_name} must be a non-empty string")


def _remote_mutation_operation_name(operation):
    _require_non_empty_string(operation, "operation")
    return operation
