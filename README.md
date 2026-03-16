# ai_tools

Small utilities and Codex-facing assets for local automation.

## Repository layout

- `ralph-wiggum/`
  - A simple loop that repeatedly runs Codex against a fixed prompt until a `stop.md` file appears.
- `skills/tmux-codex-orchestrator/`
  - A Codex skill for running worker Codex instances inside tmux panes while keeping a controller pane in the foreground.

## Prerequisites

- `codex` installed and available on `PATH`
- `tmux` installed for the orchestration skill
- A shell environment that can run the included Bash script

## Ralph Wiggum loop

`ralph-wiggum/work_loop.sh` runs Codex in a loop using the contents of `ralph-wiggum/prompt.md`.

Default behavior:

- Reads the prompt from [`ralph-wiggum/prompt.md`](ralph-wiggum/prompt.md)
- Stops when `stop.md` exists in the current working directory
- Sleeps `1` second between runs
- Uses `codex --dangerously-bypass-approvals-and-sandbox exec`

Example:

```bash
cd /path/to/target-repo
/root/ai_tools/ralph-wiggum/work_loop.sh
```

Useful environment variables:

- `STOP_FILE`
  - Override the stop-file path. Default: `"$PWD/stop.md"`
- `CODEX_CMD`
  - Override the Codex binary name. Default: `codex`
- `CODEX_ARGS`
  - Override the Codex argument string. Default: `--dangerously-bypass-approvals-and-sandbox exec`
- `CODEX_PROMPT_FLAG`
  - Used only for non-`exec` invocation styles
- `CODEX_SLEEP_SECONDS`
  - Delay between loop iterations. Default: `1`

Related prompt files:

- [`ralph-wiggum/prompt.md`](ralph-wiggum/prompt.md)
  - Instructions for the work loop agent
- [`ralph-wiggum/task-prompt.md`](ralph-wiggum/task-prompt.md)
  - Instructions for generating a `tasks.md` from an implementation plan

## tmux Codex orchestrator skill

The tmux skill is designed for controller-worker delegation inside tmux.

Core behavior:

- Launch worker Codex instances in tmux panes
- Prefer same-window splits
- Keep the controller pane visible
- Use file-backed payloads plus `tmux wait-for` for worker handoff
- Use unique prompt and payload files under `/tmp`

Key files:

- [`skills/tmux-codex-orchestrator/SKILL.md`](skills/tmux-codex-orchestrator/SKILL.md)
  - Main skill instructions
- [`skills/tmux-codex-orchestrator/references/commands.md`](skills/tmux-codex-orchestrator/references/commands.md)
  - Direct command recipes
- [`skills/tmux-codex-orchestrator/agents/openai.yaml`](skills/tmux-codex-orchestrator/agents/openai.yaml)
  - UI-facing skill metadata

Typical worker handoff:

1. Controller creates a unique prompt file, payload file, and event channel.
2. Controller launches the worker pane with the initial prompt attached on the Codex command line.
3. Worker writes a payload file whose first line is `STATUS=done` or `STATUS=blocked`.
4. Worker signals completion or blocker state with `tmux wait-for -S`.
5. Controller waits on `tmux wait-for`, then reads the payload file.

## Notes

- The repo is intentionally small and mostly contains automation assets rather than a conventional application.
- The tmux skill assumes the controller is responsible for orchestration and the worker is responsible only for delegated execution.
