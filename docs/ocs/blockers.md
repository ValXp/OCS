# Blockers

OpenCode can expose pending permission and question requests. OCS calls these blockers because they require external input before a session can continue.

## Permission Requests

```bash
bin/ocs permission list
bin/ocs permission list --session ses_1
bin/ocs permission reply per_1 once
bin/ocs permission reply per_1 always
bin/ocs permission reply per_1 reject --message "Use a read-only command instead."
```

`permission list` prints request ID, session ID, permission name, patterns, always-allowed patterns, and tool reference when present. Multiple requests print as a compact table.

Replies are `once`, `always`, or `reject`. A missing request exits as missing input rather than a generic API failure.

## Question Requests

```bash
bin/ocs question list
bin/ocs question list --session ses_1
bin/ocs question answer que_1 Ship
bin/ocs question answer que_1 --answers-json '[["Unit", "Integration"]]'
bin/ocs question reject que_1
```

`question list` prints request ID, session ID, number of question items, headers, first question text, and tool reference when present.

Positional answers are converted to nested one-item arrays. Use `--answers-json` when the server expects multi-select answers or multiple answer groups. The JSON must be an array of string arrays.

## Session Blocker Counts

```bash
bin/ocs list --blockers
bin/ocs inspect ses_1 --blockers
```

The session commands can load both blocker inventories and append permission count, question count, and total blocker count per session.

## Output Modes

Use compact output for humans, `--json` for automation, and `--raw` to debug the exact permission/question API response body.
