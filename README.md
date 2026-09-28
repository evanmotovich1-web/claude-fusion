# Claude × Codex Fusion

The Fusion Pi harness (disler/fusion-harness, the Pi extension behind `fusion` on the Mac), rebuilt with Claude Code and Codex as its two slots. The commands, the prompt contracts, the single-writer rule and the communication system prompt are copied from upstream commit `01a3482` into `prompts/` (MIT, `prompts/LICENSE-fusion-harness`).

## Install (Mac)

```bash
bash tools/claude_codex_fusion/install.sh   # then open a new terminal
claude codex fusion
```

The terminal switches to a deep-purple background (`FUSION_BG`, or turn it off with `FUSION_NO_BG=1`) and switches back when you exit. Every other `claude …` command still goes to the normal CLI.

## Slots

| Slot | Role | CLI |
|---|---|---|
| `claude` | ARCHITECT: merges /fh-fusion results, plans /fh-collaborate | `claude -p` |
| `codex` | BUILDER (Main): receives raw chat | `codex exec` |

Raw chat goes to the Main builder only, the same as Pi. The other slot joins only through a `/fh-*` command. Each slot keeps its own resumed session: Claude uses `--session-id` and `--resume`, and Codex uses `exec resume <thread>`.

## Commands

| Command | What it does |
|---|---|
| `/fh [on\|off]` | Shows the command index. `on`/`off` toggles the model bar |
| `/fh-opinion <prompt>` | Every slot answers independently with read-only tools. The answers show side by side |
| `/fh-fusion "<prompt>" "<instruction>"` | Workers research read-only, then a fresh FUSION agent (the architect CLI) is the only writer. Every slot then replies `ACK FUSION <run>` |
| `/fh-debate [--rounds N] <prompt>` | Opening, rebuttal and closing rounds. Each slot sees the others' labeled opinions. There is no judge |
| `/fh-collaborate <prompt>` | Each slot proposes a plan, the architect turns them into a task list with dependencies (DAG), read tasks run in parallel, write tasks run one at a time, and the architect does the final integration |
| `/fh-only [slot] [prompt]` | Talk to one slot. Without a prompt it arms the next input for that slot |
| `/fh-model` | Picker for slot → model → thinking. Lasts for the session only |
| `/fh-system-prompt` | Shows each slot's effective appended prompt |
| `/fh-reset` | Starts fresh sessions for every slot |

Aliases from the communication prompt (`scr`, `eli`, `foc`, `ref`) work as plain input. The models expand them.

## Differences from Pi fusion

- The communication prompt goes to Claude through `--append-system-prompt`. Codex has no equivalent flag, so the text is sent as the first turn of each Codex session.
- Read-only means Claude's `--allowedTools Read Grep Glob LS` and Codex's `--sandbox read-only`. Writers get `acceptEdits` plus Bash on Claude and `workspace-write` on Codex. `--read-only` turns off all writes.
- `--fh-config stack.json` takes 2–5 slots as JSON, not YAML, so the tool needs no dependencies. The fields match upstream (`name`, `cli`, `model`, `thinking`, `architect`, `primary`, `color`, `append_system_prompt`).
- Run artifacts go to `~/.cache/claude-codex-fusion/runs/<command>-<ts>/` (sources, manifest, fused.md, context-sync.json, debate rounds, plan and reports).

## Status

Every command was tested end to end against stub `claude` and `codex` scripts in a container without the real CLIs. It has not yet run against live Claude Code or Codex. The Codex flags are `exec --json --output-last-message --sandbox`, `exec resume <id>` and `-c model_reasoning_effort`. If your Codex version rejects one of them, the failure shows in the slot's panel.

## Known risk

Codex OAuth shares a refresh token with the Hermes seats (see [[wiki/connectors-and-capabilities]]). Running `codex login` again on another machine can sign them out.

---
Governed by [[AGENTS]] — see AGENTS.md for the rules this file operates under.
