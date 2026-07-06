# Sessions

Session commands operate directly on OpenCode server sessions. They are useful for manual control, scripting, and preparing sessions for local `run` workers.

## Create

```bash
bin/ocs create /path/to/project --agent build --model openai/gpt-5.5
```

`create` posts the target directory as `location.directory`. `--agent` and `--model` are passed through when supplied. Compact output prints normalized session fields; `--json` prints normalized JSON; `--raw` prints the exact response body.

## List

```bash
bin/ocs list
bin/ocs list --directory /path/to/project --agent build --model openai/gpt-5.5
bin/ocs list --blockers
```

`list` filters client-side by normalized directory, agent, and model. `--blockers` loads permission and question inventory and appends counts per session.

## Inspect And Get

```bash
bin/ocs inspect ses_1 --blockers
bin/ocs get ses_1 --json
bin/ocs inspect ses_1 --raw
```

`get` is an alias for `inspect`. Compact output shows one status line with ID, title, directory, agent, model, cost, token total, creation time, and update time.

## Delete

```bash
bin/ocs delete ses_1
```

`delete` calls the session delete route, then verifies the session is no longer readable. Successful compact output reports `verified=unreadable`.

## Abort

```bash
bin/ocs abort ses_1
```

`abort` posts to the session abort route and normalizes the response into `accepted`, `status`, and `raw_status`. A missing session is reported as `session not found`.

## Fork And Children

```bash
bin/ocs fork ses_parent --message-id msg_branch
bin/ocs children ses_parent
bin/ocs children ses_parent --directory /path/to/project --json
```

`fork` creates a child session, optionally from a specific message. `children` lists child sessions and can filter by directory.

## Raw And JSON Output

Use `--json` when automation needs stable data. Use `--raw` when debugging server response shape or route behavior.
