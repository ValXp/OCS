# Command Reference

This reference describes the CLI surface implemented in `opencode_session/commands`. Commands use compact output by default. Commands that expose structured data generally support `--json`; session and blocker inventory commands also support `--raw` for the exact API response body.

## Global Server Selection

Most server-backed commands accept `--server URL`. The default is selected in this order:

- `--server URL`
- `OPENCODE_SERVER_URL`
- `OPENCODE_SERVER`
- `http://127.0.0.1:4096`

## Capability Probe

`capabilities` probes server health and route support.

```bash
bin/ocs capabilities --server http://127.0.0.1:4096
bin/ocs capabilities --json
```

The compact output reports health, version, session route, v2 prompt route, v2 wait route, event route, blocking execution route, and legacy run/reply support.

## Read-Only Diagnostics

`diagnostics` inspects routes outside OCS's first-class command surface without exposing a generic mutation tool.

```bash
bin/ocs diagnostics routes --filter workspace
bin/ocs diagnostics routes --json
bin/ocs diagnostics get /project --json
bin/ocs diagnostics get /project --raw
```

`diagnostics routes` prints stable, sorted OpenAPI paths and methods. `diagnostics get` accepts only same-server absolute paths advertised as GET-capable by the server's OpenAPI document. It rejects URLs, fragments, unadvertised paths, and mutating methods.
Diagnostics route discovery and GET requests do not follow HTTP redirects, so an advertised same-server read cannot be redirected to another origin.

## Session Commands

- `create DIRECTORY [--agent NAME] [--model NAME]`: create a session for a target directory.
- `list [--directory PATH] [--agent NAME] [--model NAME] [--blockers]`: list sessions with optional filters.
- `inspect SESSION_ID [--blockers]`: inspect one session.
- `get SESSION_ID [--blockers]`: alias for `inspect`.
- `delete SESSION_ID`: delete a session and verify it is no longer readable.
- `abort SESSION_ID`: request session abort.
- `fork SESSION_ID [--message-id ID]`: fork a session, optionally from a message.
- `children SESSION_ID [--directory PATH]`: list child sessions.

Session compact output includes normalized ID, title, directory, agent, model, cost, token total, and timestamps. `--blockers` adds permission/question counts.

## Prompt Commands

- `steer SESSION_ID TEXT [--delivery steer|queue] [--message-id ID]`: admit durable input and report admission state.
- `run_blocking [--session SESSION_ID] [--directory PATH] [--agent NAME] [--model NAME] PROMPT...`: execute a task and wait for a terminal result.
- `watch SESSION_ID [--json] [--timeout SECONDS]`: stream normalized session events.

If `run_blocking` has no prompt words, it reads prompt text from stdin. If `--session` is omitted, it creates a disposable session in `--directory` or `.` and deletes it before exit.

## Local Run Commands

All run commands accept `run --store PATH ...`. If omitted, the store is `OCS_RUN_STORE` or `.ocs/runs`.

- `run init NAME [--directory PATH] [--server URL]`: create a local run record.
- `run worker NAME WORKER_ID --role ROLE [options]`: add or update worker metadata.
- `run start NAME [--prompt TEXT] [--worker ID] [--role ROLE] [--directory PATH] [--server URL] [--session ID] [--agent NAME] [--model NAME] [--execution-policy fail-fast|continue] [--cleanup]`: start one prompt or stored worker prompts with dependency-ordered serial execution.
- `run status NAME [--json]`: show persisted run state.
- `run collect NAME [--worker ID] [--json]`: print collected worker results.
- `run steer NAME WORKER_ID TEXT [--delivery steer|queue] [--message-id ID] [--json]`: admit input to a worker session and record the prompt ID.
- `run abort NAME WORKER_ID [--json]`: abort a worker session and mark the worker aborted when accepted.
- `run cleanup NAME [--sessions] [--worktrees] [--branches] [--project-metadata] [--logs] [--run-store] [--all] [--dry-run|--apply] [--force] [--server URL] [--json]`: inspect or remove resources explicitly registered to the run.

Worker metadata options include `--session`, `--agent`, `--model`, `--prompt`, `--depends-on`, `--prompt-id`, `--status active|blocked`, `--retry-count`, `--retry-limit`, `--retryable`, `--timeout-seconds`, `--timeout-policy`, `--blocker`, `--output-ref`, `--owned-worktree PATH`, `--owned-log PATH`, and `--owned-project-copy PROJECT_ID DIRECTORY_PREFIX`. Ownership paths must be absolute. Worktrees must be attached to a branch, and logs must already exist as a regular file or symlink so OCS can record their filesystem identity. `--status blocked` requires at least one `--blocker`; terminal states are owned by `run start`, `run abort`, and result/failure/timeout reducers.

Stored prompted workers are intentionally serial: `run start` plans one ready worker, executes it, persists state, and then plans the next step. `--execution-policy continue` changes failure handling only; it does not run independent workers in parallel.

`run cleanup` is a dry run unless `--apply` is present, and it requires at least one category or `--all`. Dry-run and apply share the same safety preflight: active workers, dirty worktrees, unmerged branches, changed resource identities, unsupported project cleanup, and the exact target server are reported in JSON. Active workers block all apply side effects unless `--force` is explicit. `--force` permits active-worker, dirty-worktree, and unmerged-branch cleanup only; it never bypasses recorded ownership identity or run-store concurrency checks.

Apply processes sessions, worktrees, project/workspace metadata, branches, and logs, then deletes the run record last when requested and every selected operation verified. The run record carries an in-progress/final cleanup audit and is retained on partial failure or if it changes concurrently. OpenCode versions without an API for legacy `project.sandboxes` return a partial result rather than editing the database.

Project/workspace cleanup is remotely inventoried before apply freezes the exact paths and workspace IDs. An unsupported or partial project preview blocks apply before any selected local resource is changed. An interrupted apply leaves an `in_progress` audit that fences worker mutations; inspect it and use `--force` only to deliberately resume that cleanup.

Branch cleanup must be selected together with its still-present owned worktree in the same invocation. OCS freezes the branch tip during preflight, removes the verified worktree, and deletes the ref only if that exact tip is still current. It refuses branch-only cleanup after a worktree has already disappeared.

## Blocker Commands

- `permission list [--session SESSION_ID]`: list pending permission requests.
- `permission reply REQUEST_ID once|always|reject [--message TEXT]`: resolve a permission request.
- `question list [--session SESSION_ID]`: list pending question requests.
- `question answer REQUEST_ID ANSWER...`: answer with one or more labels/text values.
- `question answer REQUEST_ID --answers-json JSON`: submit nested answer arrays for multi-select questions.
- `question reject REQUEST_ID`: reject a question request.

## Project And Workspace Metadata

- `project list [--directory PATH] [--json|--raw]`: list projects, optionally filtering by a worktree or sandbox directory.
- `project inspect PROJECT_ID [--json|--raw]`: inspect one project from the project inventory.
- `project directories PROJECT_ID [--directory PATH] [--json|--raw]`: list known root and project-copy directories.
- `workspace list [--project-id ID] [--directory PATH] [--json|--raw]`: list experimental workspaces.

These commands discover their routes through `/doc`. If the server does not expose a requested project or
experimental workspace route, OCS exits with `70` and names the unsupported method and path.

`project-copy cleanup PROJECT_ID --directory-prefix PATH [--apply] [--json]` inventories missing directories
and workspaces owned by one project. It is a dry run unless `--apply` is present. Apply mode refreshes project-copy
metadata, removes only matching workspace IDs, and then verifies the project, project-directory, and workspace
inventories. OCS never edits the OpenCode database. Some server versions expose no supported operation for stale
legacy `project.sandboxes`; OCS reports those residual paths as a partial cleanup and exits with `70`. Because the
server refresh route is project-wide, OCS also refuses to invoke it when unrelated stale project directories would
be affected by a prefix-scoped cleanup.

## Validation Commands

- `smoke [--directory PATH] [--prefix PREFIX] [--event-timeout SECONDS] [--event-limit N] [--json]`: run deterministic no-live-model validation.
- `live_validate [--directory PATH] [--prefix PREFIX] [--agent NAME] [--model NAME] [--json]`: run opt-in live-provider validation when `OCS_LIVE_VALIDATE=1`.
- `cleanup [--directory PATH] [--prefix PREFIX] [--json]`: delete stale disposable sessions matching a prefix and directory.
