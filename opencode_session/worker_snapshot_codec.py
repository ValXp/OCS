from opencode_session.worker_state import (
    WORKER_LIST_FIELDS,
    WORKER_OPTIONAL_LIST_FIELDS,
    WORKER_SNAPSHOT_STATE_FIELDS,
    WorkerRecord,
    default_worker_record,
    deserialize_worker_record,
    require_internal_worker,
    serialize_worker_snapshot,
    snapshot_state_source,
)
