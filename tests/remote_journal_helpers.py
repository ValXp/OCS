from opencode_session.remote_journal import RemoteJournalRecord, RemoteMutationOperation


PROMPT_OPERATION = RemoteMutationOperation(
    kind="prompt",
    discard_cleanup_operation="discard_prompt",
    finalize_cleanup_operation="finalize_prompt",
    call_remote_operation="call_prompt",
)


def intent_record(record_id="mutation-1", kind="prompt", session_id="ses_1", *, message_id=None):
    fields = {"session_id": session_id}
    if message_id is not None:
        fields["message_id"] = message_id
    return RemoteJournalRecord(record_id, kind, fields)


def applied_record(record_id="mutation-1", kind="prompt", status="applied", *, message_id=None):
    fields = {"status": status}
    if message_id is not None:
        fields["message_id"] = message_id
    return RemoteJournalRecord(record_id, kind, fields)


def remember_message(message_id):
    def mutate(run):
        run["applied_message_id"] = message_id

    return mutate
