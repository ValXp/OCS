from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, MutableMapping, Optional, Tuple, Union

from opencode_session.run_store import RunStoreError
from opencode_session.schema_helpers import JsonObject, JsonValue


_RemoteMutationRun = MutableMapping[str, Any]
_JournalEntry = JsonObject
_RunMutation = Callable[[_RemoteMutationRun], None]
_PersistRunMutation = Callable[[_RemoteMutationRun, _RunMutation], _RemoteMutationRun]
_RunUpdate = Callable[[_RemoteMutationRun], None]
_IntentFactory = Callable[[_RemoteMutationRun], "RemoteJournalRecord"]
_RemoteCall = Callable[[_RemoteMutationRun, "RemoteJournalRecord"], Any]
_ResultHandler = Callable[[Any, "RemoteJournalRecord"], Optional["RemoteMutationResult"]]


_PERSISTENCE_ERRORS = (RunStoreError, OSError)
_RECORD_IDENTITY_FIELDS = {"id", "kind"}
OUTBOX_STATE_INTENT = "intent"
OUTBOX_STATE_REMOTE_SUCCEEDED = "remote_succeeded"
OUTBOX_STATE_APPLIED = "applied"
OUTBOX_STATE_UNCERTAIN = "uncertain"
_PENDING_OUTBOX_STATES: FrozenSet[str] = frozenset(
    {
        OUTBOX_STATE_INTENT,
        OUTBOX_STATE_REMOTE_SUCCEEDED,
        OUTBOX_STATE_APPLIED,
        OUTBOX_STATE_UNCERTAIN,
    }
)
_LEGACY_STATUS_OUTBOX_STATES: Dict[str, str] = {
    "intent": OUTBOX_STATE_INTENT,
    "remote_succeeded": OUTBOX_STATE_REMOTE_SUCCEEDED,
    "created": OUTBOX_STATE_APPLIED,
    "applied": OUTBOX_STATE_APPLIED,
    "uncertain": OUTBOX_STATE_UNCERTAIN,
}
_RESERVED_RECORD_FIELDS: FrozenSet[str] = frozenset((*_RECORD_IDENTITY_FIELDS, "outbox_state"))

MISSING_INTENT_IGNORE = "ignore"
MISSING_INTENT_RECORD_APPLIED = "record_applied"
_MISSING_INTENT_POLICIES: FrozenSet[str] = frozenset(
    {
        MISSING_INTENT_IGNORE,
        MISSING_INTENT_RECORD_APPLIED,
    }
)

REMOTE_MUTATION_RESULT_FINALIZE = "finalize"
REMOTE_MUTATION_RESULT_KEEP_PENDING = "keep_pending"
REMOTE_MUTATION_RESULT_RECORD_APPLIED = "record_applied"
_REMOTE_MUTATION_RESULT_ACTIONS: FrozenSet[str] = frozenset(
    {
        REMOTE_MUTATION_RESULT_FINALIZE,
        REMOTE_MUTATION_RESULT_KEEP_PENDING,
        REMOTE_MUTATION_RESULT_RECORD_APPLIED,
    }
)


@dataclass(frozen=True)
class RemoteJournalRecord:
    id: str
    kind: str
    fields: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty_string(self.id, "record id")
        _require_non_empty_string(self.kind, "record kind")
        if not isinstance(self.fields, Mapping):
            raise TypeError("remote journal record fields must be a mapping")
        fields: _JournalEntry = dict(self.fields)
        reserved = _RESERVED_RECORD_FIELDS.intersection(fields)
        if reserved:
            names = ", ".join(sorted(reserved))
            raise ValueError(f"remote journal record fields must not include {names}")
        for field_name in fields:
            if not isinstance(field_name, str):
                raise TypeError("remote journal record field names must be strings")
        object.__setattr__(self, "fields", fields)

    def get(self, field_name: str, default: JsonValue = None) -> JsonValue:
        _require_non_empty_string(field_name, "record field name")
        if field_name == "id":
            return self.id
        if field_name == "kind":
            return self.kind
        return self.fields.get(field_name, default)

    def to_journal_entry(self) -> _JournalEntry:
        return {"id": self.id, "kind": self.kind, **dict(self.fields)}


@dataclass(frozen=True)
class RemoteMutationOperation:
    kind: str
    discard_cleanup_operation: str
    finalize_cleanup_operation: str
    call_remote_operation: str

    def __post_init__(self) -> None:
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
    outbox_action: str = REMOTE_MUTATION_RESULT_FINALIZE
    applied_record: Optional[RemoteJournalRecord] = None
    run_update: Optional[_RunUpdate] = None
    missing_intent_policy: str = MISSING_INTENT_IGNORE

    @classmethod
    def finalize(cls, *, run_update: Optional[_RunUpdate] = None) -> "RemoteMutationResult":
        return cls(
            outbox_action=REMOTE_MUTATION_RESULT_FINALIZE,
            run_update=run_update,
        )

    @classmethod
    def keep_pending(cls) -> "RemoteMutationResult":
        return cls(outbox_action=REMOTE_MUTATION_RESULT_KEEP_PENDING)

    @classmethod
    def record_applied(
        cls,
        record: RemoteJournalRecord,
        *,
        run_update: Optional[_RunUpdate] = None,
        missing_intent_policy: str = MISSING_INTENT_IGNORE,
    ) -> "RemoteMutationResult":
        return cls(
            outbox_action=REMOTE_MUTATION_RESULT_RECORD_APPLIED,
            applied_record=record,
            run_update=run_update,
            missing_intent_policy=missing_intent_policy,
        )

    def __post_init__(self) -> None:
        _require_known_result_action(self.outbox_action)
        _require_known_missing_intent_policy(self.missing_intent_policy)
        if self.run_update is not None and not callable(self.run_update):
            raise TypeError("remote mutation run update must be callable")
        if self.outbox_action == REMOTE_MUTATION_RESULT_RECORD_APPLIED:
            if not isinstance(self.applied_record, RemoteJournalRecord):
                raise TypeError("remote mutation applied_record must be a RemoteJournalRecord")
            return
        if self.applied_record is not None:
            raise ValueError("remote mutation applied records require record_applied outbox action")
        if self.missing_intent_policy != MISSING_INTENT_IGNORE:
            raise ValueError("remote mutation missing intent policy requires record_applied outbox action")
        if self.outbox_action == REMOTE_MUTATION_RESULT_KEEP_PENDING and self.run_update is not None:
            raise ValueError("remote mutation run updates require finalization or an applied record")


@dataclass(frozen=True)
class RemoteMutationExecution:
    run: _RemoteMutationRun
    remote_result: Any
    intent: RemoteJournalRecord


@dataclass(frozen=True)
class RecordedIntent:
    run: _RemoteMutationRun
    intent: RemoteJournalRecord


class RemoteMutationTransaction:
    def __init__(
        self,
        journal: "PersistedRemoteMutationJournal",
        entry_id: str,
        operation: RemoteMutationOperation,
    ) -> None:
        self.journal = journal
        self.entry_id = entry_id
        self.operation = operation

    def record_intent(self, run: _RemoteMutationRun, record: RemoteJournalRecord) -> _RemoteMutationRun:
        self._validate_record_identity(record)
        return self.journal.record_intent(run, record)

    def record_intent_from(self, run: _RemoteMutationRun, record_factory: _IntentFactory) -> RecordedIntent:
        return self.journal.record_intent_from(
            run,
            lambda latest_run: self._validated_record(record_factory(latest_run)),
        )

    def runner(self) -> "RemoteMutationRunner":
        return RemoteMutationRunner(self)

    def mark_applied(
        self,
        run: _RemoteMutationRun,
        record: RemoteJournalRecord,
        *,
        run_update: Optional[_RunUpdate] = None,
        missing_intent_policy: str = MISSING_INTENT_IGNORE,
    ) -> _RemoteMutationRun:
        self._validate_record_identity(record)
        return self.journal.mark_applied(
            run,
            self.entry_id,
            record,
            run_update=run_update,
            missing_intent_policy=missing_intent_policy,
        )

    def mark_remote_succeeded(
        self,
        run: _RemoteMutationRun,
        record: RemoteJournalRecord,
        *,
        missing_intent_policy: str = MISSING_INTENT_IGNORE,
    ) -> _RemoteMutationRun:
        self._validate_record_identity(record)
        return self.journal.mark_remote_succeeded(
            run,
            self.entry_id,
            record,
            missing_intent_policy=missing_intent_policy,
        )

    def finalize(self, run: _RemoteMutationRun, *, run_update: Optional[_RunUpdate] = None) -> _RemoteMutationRun:
        return self.journal.finalize(run, self.entry_id, run_update=run_update)

    def apply_result(self, run: _RemoteMutationRun, result: RemoteMutationResult) -> _RemoteMutationRun:
        if not isinstance(result, RemoteMutationResult):
            raise TypeError("remote mutation result must be a RemoteMutationResult")
        if result.outbox_action == REMOTE_MUTATION_RESULT_RECORD_APPLIED:
            return self.mark_applied(
                run,
                result.applied_record,
                run_update=result.run_update,
                missing_intent_policy=result.missing_intent_policy,
            )
        if result.outbox_action == REMOTE_MUTATION_RESULT_KEEP_PENDING:
            return run
        return self.finalize(run, run_update=result.run_update)

    def discard_intent_best_effort(self, run: _RemoteMutationRun) -> _RemoteMutationRun:
        return self.journal.discard_intent_best_effort(
            run,
            self.entry_id,
            operation=self.operation.discard_cleanup_operation,
        )

    def finalize_best_effort(self, run: _RemoteMutationRun) -> _RemoteMutationRun:
        return self.journal.finalize_best_effort(
            run,
            self.entry_id,
            operation=self.operation.finalize_cleanup_operation,
        )

    def mark_uncertain_best_effort(self, run: _RemoteMutationRun, error: BaseException) -> _RemoteMutationRun:
        return self.journal.mark_uncertain_best_effort(
            run,
            self.entry_id,
            error,
            operation=self.operation.call_remote_operation,
        )

    def _validated_record(self, record: RemoteJournalRecord) -> RemoteJournalRecord:
        self._validate_record_identity(record)
        return record

    def _validate_record_identity(self, record: RemoteJournalRecord) -> None:
        entry = _journal_entry(record)
        if entry.get("id") != self.entry_id:
            raise ValueError(f"remote journal record id must be {self.entry_id!r}")
        if entry.get("kind") != self.operation.kind:
            raise ValueError(f"remote journal record kind must be {self.operation.kind!r}")


class RemoteMutationRunner:
    def __init__(self, transaction: RemoteMutationTransaction) -> None:
        self.transaction = transaction

    def execute(
        self,
        run: _RemoteMutationRun,
        *,
        intent_factory: _IntentFactory,
        call_remote: _RemoteCall,
        apply_result: Optional[_ResultHandler] = None,
    ) -> RemoteMutationExecution:
        recorded = self.transaction.record_intent_from(run, intent_factory)
        run = recorded.run
        intent = recorded.intent
        try:
            remote_result = call_remote(run, intent)
        except Exception as remote_error:
            self.transaction.mark_uncertain_best_effort(run, remote_error)
            raise

        result = RemoteMutationResult.finalize()
        if apply_result is not None:
            result = apply_result(remote_result, intent)
            if result is None:
                result = RemoteMutationResult.finalize()
        if not isinstance(result, RemoteMutationResult):
            raise TypeError("remote mutation result must be a RemoteMutationResult")
        if result.outbox_action != REMOTE_MUTATION_RESULT_KEEP_PENDING:
            succeeded_record = intent
            missing_intent_policy = MISSING_INTENT_IGNORE
            if result.outbox_action == REMOTE_MUTATION_RESULT_RECORD_APPLIED:
                succeeded_record = result.applied_record
                missing_intent_policy = result.missing_intent_policy
            run = self.transaction.mark_remote_succeeded(
                run,
                succeeded_record,
                missing_intent_policy=missing_intent_policy,
            )
        run = self.transaction.apply_result(run, result)
        return RemoteMutationExecution(run=run, remote_result=remote_result, intent=intent)


class RemoteMutationRecovery:
    def __init__(self, field: str) -> None:
        self.journal = RemoteMutationJournal(field)

    def pending_entries(
        self,
        run: _RemoteMutationRun,
        *,
        kind: Optional[str] = None,
        outbox_states: Optional[Union[str, Iterable[str]]] = None,
    ) -> Tuple[_JournalEntry, ...]:
        return self.journal.pending_entries(run, kind=kind, outbox_states=outbox_states)

    def applied_entries(self, run: _RemoteMutationRun, *, kind: Optional[str] = None) -> Tuple[_JournalEntry, ...]:
        return self.pending_entries(run, kind=kind, outbox_states=(OUTBOX_STATE_APPLIED,))

    def values_by_owner(
        self,
        run: _RemoteMutationRun,
        *,
        kind: str,
        owner_field: str,
        value_fields: Iterable[str] = (),
        list_fields: Iterable[str] = (),
        required_fields: Optional[Mapping[str, JsonValue]] = None,
        outbox_states: Optional[Union[str, Iterable[str]]] = None,
    ) -> Dict[str, List[str]]:
        values_by_owner: Dict[str, List[str]] = {}
        for entry in self.pending_entries(run, kind=kind, outbox_states=outbox_states):
            if not self._matches_required_fields(entry, required_fields):
                continue
            owner = entry.get(owner_field)
            if not isinstance(owner, str) or not owner:
                continue
            values = values_by_owner.setdefault(owner, [])
            for list_field in list_fields:
                for value in _string_list(entry.get(list_field)):
                    _append_unique(values, value)
            for value_field in value_fields:
                _append_unique(values, entry.get(value_field))
        return {owner: values for owner, values in values_by_owner.items() if values}

    def _matches_required_fields(
        self,
        entry: Mapping[str, JsonValue],
        required_fields: Optional[Mapping[str, JsonValue]],
    ) -> bool:
        if not required_fields:
            return True
        for field, expected in required_fields.items():
            if entry.get(field) != expected:
                return False
        return True


class RemoteMutationJournal:
    def __init__(self, field: str) -> None:
        _require_non_empty_string(field, "journal field")
        self.field = field

    def record_intent(self, run: _RemoteMutationRun, entry: RemoteJournalRecord) -> None:
        journal = run.get(self.field)
        if not isinstance(journal, list):
            journal = []
        journal.append(_with_outbox_state(_journal_entry(entry), OUTBOX_STATE_INTENT))
        run[self.field] = journal

    def mark_applied(
        self,
        run: _RemoteMutationRun,
        entry_id: str,
        record: RemoteJournalRecord,
        *,
        missing_intent_policy: str = MISSING_INTENT_IGNORE,
    ) -> None:
        _require_known_missing_intent_policy(missing_intent_policy)
        journal = run.get(self.field)
        if not isinstance(journal, list):
            journal = []
            run[self.field] = journal
        entry = _with_outbox_state(_journal_entry(record), OUTBOX_STATE_APPLIED)
        if entry.get("id") != entry_id:
            raise ValueError(f"remote journal record id must be {entry_id!r}")
        update = _journal_update(entry)
        for journal_entry in journal:
            if isinstance(journal_entry, dict) and journal_entry.get("id") == entry_id:
                journal_entry.update(update)
                return
        if missing_intent_policy == MISSING_INTENT_RECORD_APPLIED:
            journal.append(entry)

    def mark_remote_succeeded(
        self,
        run: _RemoteMutationRun,
        entry_id: str,
        record: RemoteJournalRecord,
        *,
        missing_intent_policy: str = MISSING_INTENT_IGNORE,
    ) -> None:
        _require_known_missing_intent_policy(missing_intent_policy)
        journal = run.get(self.field)
        if not isinstance(journal, list):
            journal = []
            run[self.field] = journal
        entry = _with_outbox_state(_journal_entry(record), OUTBOX_STATE_REMOTE_SUCCEEDED)
        if entry.get("id") != entry_id:
            raise ValueError(f"remote journal record id must be {entry_id!r}")
        update = _journal_update(entry)
        for journal_entry in journal:
            if isinstance(journal_entry, dict) and journal_entry.get("id") == entry_id:
                journal_entry.update(update)
                return
        if missing_intent_policy == MISSING_INTENT_RECORD_APPLIED:
            journal.append(entry)

    def finalize(self, run: _RemoteMutationRun, entry_id: str) -> None:
        journal = run.get(self.field)
        if not isinstance(journal, list):
            run.pop(self.field, None)
            return
        remaining = [entry for entry in journal if not isinstance(entry, dict) or entry.get("id") != entry_id]
        if remaining:
            run[self.field] = remaining
        else:
            run.pop(self.field, None)

    def mark_cleanup_failure(self, run: _RemoteMutationRun, entry_id: str, cleanup_failure: Mapping[str, JsonValue]) -> None:
        journal = run.get(self.field)
        if not isinstance(journal, list):
            return
        for entry in journal:
            if isinstance(entry, dict) and entry.get("id") == entry_id:
                entry["cleanup_failure"] = dict(cleanup_failure)
                return

    def mark_uncertain(self, run: _RemoteMutationRun, entry_id: str, uncertainty: Mapping[str, JsonValue]) -> None:
        journal = run.get(self.field)
        if not isinstance(journal, list):
            return
        for entry in journal:
            if isinstance(entry, dict) and entry.get("id") == entry_id:
                entry["outbox_state"] = OUTBOX_STATE_UNCERTAIN
                entry["status"] = "uncertain"
                entry["uncertain_failure"] = dict(uncertainty)
                return

    def pending_entries(
        self,
        run: _RemoteMutationRun,
        *,
        kind: Optional[str] = None,
        outbox_states: Optional[Union[str, Iterable[str]]] = None,
    ) -> Tuple[_JournalEntry, ...]:
        journal = run.get(self.field) if isinstance(run, dict) else None
        if not isinstance(journal, list):
            return ()
        requested_states = _normalize_outbox_states(outbox_states)
        return tuple(
            entry
            for entry in journal
            if isinstance(entry, dict)
            and (kind is None or entry.get("kind") == kind)
            and _entry_outbox_state(entry) in requested_states
        )


class PersistedRemoteMutationJournal:
    def __init__(self, field: str, persist_run_mutation: _PersistRunMutation, *, now: Callable[[], str]) -> None:
        self.journal = RemoteMutationJournal(field)
        self.persist_run_mutation = persist_run_mutation
        self.now = now

    def record_intent(self, run: _RemoteMutationRun, entry: RemoteJournalRecord) -> _RemoteMutationRun:
        return self.persist_run_mutation(
            run,
            lambda latest_run: self.journal.record_intent(latest_run, entry),
        )

    def transaction(self, entry_id: str, operation: RemoteMutationOperation) -> RemoteMutationTransaction:
        return RemoteMutationTransaction(
            self,
            entry_id,
            operation,
        )

    def record_intent_from(self, run: _RemoteMutationRun, entry_factory: _IntentFactory) -> RecordedIntent:
        intent = _UNSET

        def record(latest_run: _RemoteMutationRun) -> None:
            nonlocal intent
            intent = entry_factory(latest_run)
            self.journal.record_intent(latest_run, intent)

        persisted_run = self.persist_run_mutation(run, record)
        if intent is _UNSET:
            raise RuntimeError("remote mutation persistence did not record an intent")
        return RecordedIntent(run=persisted_run, intent=intent)

    def mark_applied(
        self,
        run: _RemoteMutationRun,
        entry_id: str,
        record: RemoteJournalRecord,
        *,
        run_update: Optional[_RunUpdate] = None,
        missing_intent_policy: str = MISSING_INTENT_IGNORE,
    ) -> _RemoteMutationRun:
        def persisted_mutation(latest_run: _RemoteMutationRun) -> None:
            if run_update is not None:
                _apply_run_update(run_update, latest_run)
            self.journal.mark_applied(latest_run, entry_id, record, missing_intent_policy=missing_intent_policy)

        return self.persist_run_mutation(run, persisted_mutation)

    def mark_remote_succeeded(
        self,
        run: _RemoteMutationRun,
        entry_id: str,
        record: RemoteJournalRecord,
        *,
        missing_intent_policy: str = MISSING_INTENT_IGNORE,
    ) -> _RemoteMutationRun:
        def persisted_mutation(latest_run: _RemoteMutationRun) -> None:
            self.journal.mark_remote_succeeded(
                latest_run,
                entry_id,
                record,
                missing_intent_policy=missing_intent_policy,
            )

        return self.persist_run_mutation(run, persisted_mutation)

    def finalize(self, run: _RemoteMutationRun, entry_id: str, *, run_update: Optional[_RunUpdate] = None) -> _RemoteMutationRun:
        def persisted_mutation(latest_run: _RemoteMutationRun) -> None:
            if run_update is not None:
                _apply_run_update(run_update, latest_run)
            self.journal.finalize(latest_run, entry_id)

        return self.persist_run_mutation(run, persisted_mutation)

    def discard_intent_best_effort(self, run: _RemoteMutationRun, entry_id: str, *, operation: str) -> _RemoteMutationRun:
        return self._finalize_best_effort(run, entry_id, operation=operation)

    def finalize_best_effort(self, run: _RemoteMutationRun, entry_id: str, *, operation: str) -> _RemoteMutationRun:
        return self._finalize_best_effort(run, entry_id, operation=operation)

    def pending_entries(
        self,
        run: _RemoteMutationRun,
        *,
        kind: Optional[str] = None,
        outbox_states: Optional[Union[str, Iterable[str]]] = None,
    ) -> Tuple[_JournalEntry, ...]:
        return self.journal.pending_entries(run, kind=kind, outbox_states=outbox_states)

    def mark_uncertain_best_effort(
        self,
        run: _RemoteMutationRun,
        entry_id: str,
        remote_error: BaseException,
        *,
        operation: str,
    ) -> _RemoteMutationRun:
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

    def _finalize_best_effort(self, run: _RemoteMutationRun, entry_id: str, *, operation: str) -> _RemoteMutationRun:
        try:
            return self.finalize(run, entry_id)
        except _PERSISTENCE_ERRORS as cleanup_error:
            return self.record_cleanup_failure_best_effort(
                run,
                entry_id,
                cleanup_error,
                operation=operation,
            )

    def record_cleanup_failure_best_effort(
        self,
        run: _RemoteMutationRun,
        entry_id: str,
        cleanup_error: BaseException,
        *,
        operation: str,
    ) -> _RemoteMutationRun:
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


def _string_list(value: JsonValue) -> Tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _append_unique(values: List[str], value: JsonValue) -> None:
    if isinstance(value, str) and value and value not in values:
        values.append(value)


def _apply_run_update(run_update: _RunUpdate, run: _RemoteMutationRun) -> None:
    if not callable(run_update):
        raise TypeError("remote mutation run updates must be callable")
    run_update(run)


def _journal_entry(record: RemoteJournalRecord) -> _JournalEntry:
    if not isinstance(record, RemoteJournalRecord):
        raise TypeError("remote journal records must be RemoteJournalRecord instances")
    entry = record.to_journal_entry()
    if not isinstance(entry, dict):
        raise TypeError("remote journal record serialization must be a dict")
    return dict(entry)


def _with_outbox_state(entry: Mapping[str, JsonValue], outbox_state: str) -> _JournalEntry:
    _require_known_outbox_state(outbox_state)
    entry = dict(entry)
    entry["outbox_state"] = outbox_state
    return entry


def _journal_update(entry: Mapping[str, JsonValue]) -> _JournalEntry:
    return {key: value for key, value in entry.items() if key not in _RECORD_IDENTITY_FIELDS}


def _normalize_outbox_states(outbox_states: Optional[Union[str, Iterable[str]]]) -> FrozenSet[str]:
    if outbox_states is None:
        return _PENDING_OUTBOX_STATES
    if isinstance(outbox_states, str):
        outbox_states = (outbox_states,)
    normalized: List[str] = []
    for outbox_state in outbox_states:
        _require_known_outbox_state(outbox_state)
        normalized.append(outbox_state)
    return frozenset(normalized)


def _entry_outbox_state(entry: Mapping[str, JsonValue]) -> Optional[str]:
    outbox_state = entry.get("outbox_state")
    if isinstance(outbox_state, str) and outbox_state in _PENDING_OUTBOX_STATES:
        return outbox_state
    if outbox_state is not None:
        return None
    legacy_status = entry.get("status")
    if isinstance(legacy_status, str):
        return _LEGACY_STATUS_OUTBOX_STATES.get(legacy_status, OUTBOX_STATE_INTENT)
    return OUTBOX_STATE_INTENT


def _require_known_outbox_state(outbox_state: str) -> None:
    if outbox_state not in _PENDING_OUTBOX_STATES:
        names = ", ".join(sorted(_PENDING_OUTBOX_STATES))
        raise ValueError(f"remote mutation outbox state must be one of {names}")


def _require_known_missing_intent_policy(missing_intent_policy: str) -> None:
    if missing_intent_policy not in _MISSING_INTENT_POLICIES:
        names = ", ".join(sorted(_MISSING_INTENT_POLICIES))
        raise ValueError(f"remote mutation missing intent policy must be one of {names}")


def _require_known_result_action(outbox_action: str) -> None:
    if outbox_action not in _REMOTE_MUTATION_RESULT_ACTIONS:
        names = ", ".join(sorted(_REMOTE_MUTATION_RESULT_ACTIONS))
        raise ValueError(f"remote mutation result action must be one of {names}")


def _require_non_empty_string(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise TypeError(f"remote mutation {field_name} must be a non-empty string")


def _remote_mutation_operation_name(operation: str) -> str:
    _require_non_empty_string(operation, "operation")
    return operation
