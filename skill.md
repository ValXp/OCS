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
touch /tmp/opencode/WORKER.log
bin/ocs run --store /tmp/opencode/RUN_STORE worker RUN_NAME worker-id --role build --session SESSION_ID --prompt "Do the scoped task" --owned-worktree /tmp/opencode/WORKTREE --owned-log /tmp/opencode/WORKER.log
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
12. Dry-run `run cleanup --all`, inspect its exact resource and server plan, then apply it and verify any reported residuals.

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

- A legacy `/run` request can time out before the server returns its message ID. OCS attempts to abort the session, but that worker's `prompt_ids` may remain empty.
- `run start --cleanup` only covers sessions created by that start. Use the separate `run cleanup` command for registered run-owned resources.
- `project-copy cleanup` is dry-run by default. Always review its exact project-scoped plan before adding `--apply`.
- Some OpenCode versions cannot remove residual legacy project `sandboxes` through a supported API. Treat OCS's partial/unsupported result as real; never edit the OpenCode database directly.

Track unresolved gaps as repository issues when they affect a run.

## Cleanup Checklist

Register every owned worktree, existing log path, and project-copy prefix on the worker record before execution. Then:

1. Run `bin/ocs run --store "$STORE" cleanup "$RUN" --all --server "$SERVER" --dry-run --json`.
2. Review the exact sessions, identities, project metadata, logs, run-store flag, server URL, and preflight blockers.
3. Resolve active workers, dirty worktrees, or unmerged branches; use `--force` only when their removal is intentional.
4. Apply the reviewed plan by replacing `--dry-run` with `--apply`.
5. Stop on a partial result. Never bypass an identity mismatch or edit the OpenCode database directly.
6. Verify `bin/ocs list --directory PATH --server "$SERVER" --json` returns `[]` for every worker directory.
7. Verify `git worktree list --porcelain` and the project/directory/workspace inventory no longer contain worker paths.

Do not delete unrelated sessions, worktrees, branches, or project metadata. Match on the exact run-specific prefix or recorded session IDs.

## Reporting Bugs And Feature Gaps

When the OCS workflow forces ad-hoc Python, direct API calls, direct database edits, or manual cleanup that OCS should own, file a GitHub issue with:

- The exact command that failed or was missing.
- The simplest repro steps.
- Observed and expected behavior.
- Session IDs, run store paths, and worktree paths when useful and non-sensitive.
- Any cleanup performed afterward.

If the user asks for verification before filing, use a separate agent to reproduce or inspect the bug before creating the issue.
