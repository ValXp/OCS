from opencode_session.schema_common import collection_records, first_present


def load_blocker_counts(client):
    permission_response = client.list_permissions_response()
    question_response = client.list_questions_response()
    counts = {}
    for permission in collection_blockers(permission_response.data, "permissions"):
        _increment_blocker_count(counts, blocker_session_id(permission), "permissions")
    for question in collection_blockers(question_response.data, "questions"):
        _increment_blocker_count(counts, blocker_session_id(question), "questions")
    return counts


def blocker_counts_for_session(counts, session_id):
    session_counts = counts.get(session_id, {})
    permissions = session_counts.get("permissions", 0)
    questions = session_counts.get("questions", 0)
    return {"permissions": permissions, "questions": questions, "total": permissions + questions}


def collection_blockers(collection, plural_name):
    return collection_records(collection, plural_name, "requests", "data")


def blocker_session_id(blocker):
    return first_present(blocker, "sessionID", "sessionId", "session_id")


def _increment_blocker_count(counts, session_id, name):
    if not session_id:
        return
    session_counts = counts.setdefault(session_id, {"permissions": 0, "questions": 0})
    session_counts[name] += 1
