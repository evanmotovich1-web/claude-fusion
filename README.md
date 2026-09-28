# Claude × Codex Fusion

The Fusion Pi harness (disler/fusion-harness, the Pi extension behind `fusion` on the Mac), rebuilt with Claude Code and Codex as its two slots. The commands, the prompt contracts, the single-writer rule and the communication system prompt are copied from upstream commit `01a3482` into `prompts/` (MIT, `prompts/LICENSE-fusion-harness`).

## Install (Mac)

```bash
bash tools/claude_codex_fusion/install.sh   # then open a new terminal
claude codex fusion
```

The terminal switches to a deep-purple background (`FUSION_BG`, or turn it off with `FUSION_NO_BG=1`) and switches back when you exit. Every other `claude …` command still goes to the normal CLI.

## Slots and stacks

A stack has 2–5 slots. Each slot is `cli: claude`, `cli: codex`, or `cli: pi` (a Pi-routed model such as `xai/grok-4.7`). There is exactly one architect and one Main builder.

| Preset | Agents |
|---|---|
| `duo` (default) | claude ◆ architect · codex ● main |
| `trio` | opus ◆ · sol ● · grok ○ |
| `quint` | opus ◆ · sol ● · astra ○ · grok ○ · sonnet ○ |

Switch presets with `/fh-stack quint` or `claude codex fusion --fh-config quint`, or pass your own JSON file. The model ids in `stacks/*.json` are guesses from the vault, so edit them to match what your CLIs accept. Raw chat goes to the Main builder only. Claude and Codex slots keep their sessions between turns. Pi slots start a fresh session every turn.

## Live view

While a command runs, the terminal switches to a live dashboard. When the command finishes, the full answers are printed in the normal scrollback.

- **2–3 agents: columns.** Each agent streams its tool calls (`▸ Read …`, `▸ $ rg …`) and its text in its own column.
- **4–5 agents: tabs and a board.** One large pane shows the selected agent. Below it, a TASKS row shows each `/fh-collaborate` task with its owner, whether it reads or writes, and its state, and a SLOTS row shows each agent's time and cost.
- **Header:** command, number of agents, elapsed time, total cost, and `✎ writer:` (who currently holds the single writer lock).
- **Keys:** `1-5` picks a pane, `Tab` goes to the next one, `c` or `t` forces columns or tabs, and `Ctrl-C` interrupts the run.

**`--panes` (tmux):** `claude codex fusion --panes --fh-config trio` opens tmux with one pane per agent streaming live, plus a full-width control pane at the bottom where you type. Requires tmux (`brew install tmux`). The panes are fixed at launch, so `/fh-stack` inside that session does not add or remove panes.

## Commands

| Command | What it does |
|---|---|
| `/fh [on\|off]` | Shows the command index. `on`/`off` toggles the model bar |
| `/fh-opinion <prompt>` | Every slot answers independently with read-only tools. The answers show side by side |
| `/fh-fusion "<prompt>" "<instruction>"` | Workers research read-only, then a fresh FUSION agent (the architect CLI) is the only writer. Every slot then replies `ACK FUSION <run>` |
| `/fh-debate [--rounds N] <prompt>` | Opening, rebuttal and closing rounds. Each slot sees the others' labeled opinions. There is no judge |
| `/fh-collaborate <prompt>` | Each slot proposes a plan, the architect turns them into a task list with dependencies (DAG), read tasks run in parallel, write tasks run one at a time, and the architect does the final integration |
| `/fh-stack [name]` | Lists the stack presets, or switches to one |
| `/fh-only [slot] [prompt]` | Talk to one slot. Without a prompt it arms the next input for that slot |
| `/fh-model` | Picker for slot → model → thinking. Lasts for the session only |
| `/fh-system-prompt` | Shows each slot's effective appended prompt |
| `/fh-reset` | Starts fresh sessions for every slot |

Aliases from the communication prompt (`scr`, `eli`, `foc`, `ref`) work as plain input. The models expand them.

## Differences from Pi fusion

- The communication prompt goes to Claude through `--append-system-prompt`. Codex has no equivalent flag, so the text is sent as the first turn of each Codex session.
- Read-only means Claude's `--allowedTools Read Grep Glob LS` and Codex's `--sandbox read-only`. Writers get `acceptEdits` plus Bash on Claude and `workspace-write` on Codex. `--read-only` turns off all writes.
- Stacks are JSON, not YAML, so the tool needs no dependencies. The fields match upstream, plus `cli`: `name`, `cli`, `model`, `thinking`, `architect`, `primary`, `color`, `append_system_prompt`.
- Run artifacts go to `~/.cache/claude-codex-fusion/runs/<command>-<ts>/` (sources, manifest, fused.md, context-sync.json, debate rounds, plan and reports).

## Status

Every command, both live layouts, and `--panes` were tested end to end against stub `claude`, `codex` and `pi` scripts that stream output in a container without the real CLIs. It has not yet run against live Claude Code or Codex. The Codex flags are `exec --json --output-last-message --sandbox`, `exec resume <id>` and `-c model_reasoning_effort`. The Pi slot uses `pi -p --model --thinking --append-system-prompt --tools`. If your Codex or Pi version rejects one of these flags, the failure shows in the slot's panel.

## Known risk

Codex OAuth shares a refresh token with the Hermes seats (see [[wiki/connectors-and-capabilities]]). Running `codex login` again on another machine can sign them out.

---
Governed by [[AGENTS]] — see AGENTS.md for the rules this file operates under.
