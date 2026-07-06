---
name: ocs-repository-orchestration
description: Orchestrate work in this repository through the OCS CLI with visible OpenCode sessions, local run records, git worktrees, cleanup, and issue reporting. Use when a user asks for implementation-loop style delegation through OCS, visible OpenCode worker sessions, OCS-only orchestration, or cleanup of OCS-created sessions/workspaces.
---

# OCS Repository Orchestration

Use this skill to run repository work through `bin/ocs` instead of native subagents. The goal is a visible, auditable OpenCode workflow: each worker has a server session, a git worktree, and a local OCS run record.

## Quick Start

```bash
bin/ocs capabilities --server http://127.0.0.1:80 --json
bin/ocs run --store /tmp/opencode/RUN_STORE init RUN_NAME --directory "$PWD" --server http://127.0.0.1:80
bin/ocs create /tmp/opencode/WORKTREE --server http://127.0.0.1:80 --json
bin/ocs run --store /tmp/opencode/RUN_STORE worker RUN_NAME worker-id --role build --session SESSION_ID --prompt "Do the scoped task"
bin/ocs run --store /tmp/opencode/RUN_STORE start RUN_NAME --worker worker-id --session SESSION_ID --prompt "Do the scoped task"
```

Always set the server explicitly. The tested local server was `http://127.0.0.1:80`; do not assume that is correct for other environments.

## Workflow

1. Inspect repo state first with `git status --short` and the current branch.
2. Probe OCS server capabilities with `bin/ocs capabilities --server URL --json`.
3. Create a local run store under `/tmp/opencode` unless the user asks for a repo-local store.
4. Build a dependency tree in the run record with `run init` and `run worker`.
5. Create one git worktree per worker using non-conflicting branch names.
6. Create one OpenCode session per worker with `bin/ocs create WORKTREE --server URL --json`.
7. Record each session in the run with `run worker --session SESSION_ID`.
8. Start workers through `bin/ocs run ... start`, not native subagents, when the user asks for OCS-only delegation.
9. Inspect worker worktrees after each run; OCS may time out even when the server later edits files.
10. Integrate accepted worker diffs into the main worktree manually or through a dedicated OCS validation worker.
11. Run focused verification, then broader tests when feasible.
12. Clean up every session, worktree, branch, run store, temp log, and OpenCode project/workspace record created for the run.

## Dependency Records

Use OCS run metadata as the source of truth for the orchestration graph:

```bash
bin/ocs run --store "$STORE" worker "$RUN" planner --role plan --session "$PLANNER_SESSION" --prompt "Plan the change"
bin/ocs run --store "$STORE" worker "$RUN" builder --role build --session "$BUILDER_SESSION" --depends-on planner --prompt "Implement the plan"
bin/ocs run --store "$STORE" worker "$RUN" validator --role validate --session "$VALIDATOR_SESSION" --depends-on builder --prompt "Validate the change"
bin/ocs run --store "$STORE" status "$RUN" --json
```

Mark blockers honestly. If OCS itself blocks the workflow, record a worker blocker such as `ocs-blocking-execution-timeout` and report it.

## Known OCS Pitfalls

- `create --json` may return `{"data": {...}}` while `list --json` returns top-level session objects. Extract IDs defensively until the JSON contract is fixed.
- `run start` can time out client-side while the server-side OpenCode turn keeps running. Inspect the worker worktree and abort orphan sessions if needed.
- Timed-out workers may have empty `prompt_ids`, making server-side work hard to correlate with the run record.
- `run --cleanup` only covers sessions created by that start. It does not clean pre-created sessions, git worktrees, branches, run stores, logs, or OpenCode project metadata.
- Deleted worktrees can remain visible in the OpenCode UI through project `sandboxes` or project-copy metadata after sessions are gone.

Track these as repository issues when they affect a run. Current issue examples: #41 for `create --json` shape, #42 for orphaned timeout execution, #43-#46 for project/workspace and cleanup feature gaps.

## Cleanup Checklist

For every OCS-created worker, clean up in this order:

1. Abort active or suspicious sessions: `bin/ocs abort SESSION_ID --server URL`.
2. Delete sessions and verify unreadable: `bin/ocs delete SESSION_ID --server URL --json`.
3. Remove disposable git worktrees: `git worktree remove --force PATH`.
4. Delete disposable branches: `git branch -D BRANCH`.
5. Remove run stores and temp logs created under `/tmp/opencode`.
6. Refresh OpenCode project-copy metadata if the server exposes it: `POST /experimental/project/{projectID}/copy/refresh`.
7. Verify `bin/ocs list --directory PATH --server URL --json` returns `[]` for each worker directory.
8. Verify `git worktree list --porcelain` no longer contains the worker paths.
9. Verify OpenCode project/workspace APIs no longer contain stale worker paths when those APIs are available.

Do not delete unrelated sessions, worktrees, branches, or project metadata. Match on the exact run-specific prefix or recorded session IDs.

## Reporting Bugs And Feature Gaps

When the OCS workflow forces ad-hoc Python, direct API calls, direct database edits, or manual cleanup that OCS should own, file a GitHub issue with:

- The exact command that failed or was missing.
- The simplest repro steps.
- Observed and expected behavior.
- Session IDs, run store paths, and worktree paths when useful and non-sensitive.
- Any cleanup performed afterward.

If the user asks for verification before filing, use a separate agent to reproduce or inspect the bug before creating the issue.
