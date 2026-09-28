# Claude × Codex Fusion

The Fusion Pi harness (disler/fusion-harness, the Pi extension behind `fusion` on the Mac), rebuilt with Claude Code and Codex as its two slots. The commands, the prompt contracts, the single-writer rule and the communication system prompt are copied from upstream commit `01a3482` into `prompts/` (MIT, `prompts/LICENSE-fusion-harness`).

## Install (Mac)

```bash
bash tools/claude_codex_fusion/install.sh   # then open a new terminal
claude codex fusion
```

The terminal switches to a deep-purple background (`FUSION_BG`, or turn it off with `FUSION_NO_BG=1`) and switches back when you exit. Every other `claude …` command still goes to the normal CLI.

## The app

`claude codex fusion` takes over the whole terminal (Textual, installed into its own venv by `install.sh`):

- A splash shows the `FUSION` wordmark with a moving color sweep for about a second. Any key skips it.
- A top bar shows `✻ FUSION`, the folder, the number of agents, the writer lock, vault hits, cost, and the run timer and phase.
- The conversation log is on the left. One live pane per agent is on the right, bordered in its color, with a stats line and its tool and thinking stream.
- The input box is pinned to the bottom. Typing `/` opens the full command menu: `↑`/`↓` scroll, `Enter` or `Tab` picks, `Esc` closes. `↑`/`↓` otherwise recall history. `ctrl+s` sends the next message to the next agent, `ctrl+l` clears the log, and `ctrl+c` twice quits.
- `--classic` gives the older line-based shell. If textual is missing, the classic shell starts automatically.

## Folders agents can edit

Write-enabled agents can edit the folder fusion runs in. To work on another repo, start fusion there, or:

- `/fh-cwd ~/code/agentic-os` moves every agent to that folder, with fresh sessions.
- `/fh-add-dir ~/code/agentic-os-current-board` (or `--add-dir PATH` at launch, repeatable) lets writers also edit that folder. This is needed when a task creates a git worktree beside the repo.

Codex gets this as `-c sandbox_mode=…` and `-c sandbox_workspace_write.writable_roots=[…]`, which also applies to resumed sessions. Claude gets `--add-dir`.

## Vault (read and write, automatic)

- **Read:** before every turn, the harness runs `vault-semantic search "<your prompt>" -k 5` and passes the hits to the agents as `<vault_context>`, marked as untrusted evidence.
- **Write:** each slot's system prompt includes `prompts/VAULT.md`, which asks the agents to end with a `## Vault note` when a turn produced a durable fact. The harness files that note with `vault-semantic note ... --agent claude-codex-fusion/<slot>`, which writes a new file under `wiki/inbox/`, never an edit to an existing page. This covers fused results, the final `/fh-collaborate` integration, and chat replies. Opinions and debate rounds are not filed.
- The header shows `vault N hits`. `--no-vault` or `FUSION_VAULT=off` turns both directions off. If `vault-semantic` can't be found, both are skipped and the model bar says so.

## Slots and stacks

A stack has 2–5 slots. Each slot is `cli: claude`, `cli: codex`, or `cli: pi` (a Pi-routed model such as `xai/grok-4.7`). There is exactly one architect and one Main builder.

| Preset | Agents |
|---|---|
| `duo` (default) | claude ◆ architect · codex ● main |
| `trio` | opus ◆ · sol ● · grok ○ |
| `quint` | opus ◆ · sol ● · astra ○ · grok ○ · sonnet ○ |

Switch presets with `/fh-stack quint` or `claude codex fusion --fh-config quint`, or pass your own JSON file. The model ids in `stacks/*.json` are guesses from the vault, so edit them to match what your CLIs accept. Raw chat goes to the Main builder only. Claude and Codex slots keep their sessions between turns. Pi slots start a fresh session every turn.

## Live view

While a command runs, each agent gets its own pane, stacked like Fusion Pi:

```
◆ ARCHITECT | opus | claude-opus-5-5
◐ working 52s · in 531.1k out 3.1k · 67 tps · 5 tools · $0.4889
  ▸ Read src/api.ts
  ▹ thinking…
● BUILDER (Main) | sol | gpt-6-sol
✓ done 34.8s · in 593.8k out 6.4k · 183 tps · 8 tools
```

- `s`, `c` and `t` switch between stack (the default), columns and tabs. `1-5` or `Tab` picks the pane that gets the most room. `Ctrl-C` interrupts.
- `FUSION_LAYOUT=columns` or `FUSION_LAYOUT=tabs` changes the default.
- The header shows the command, elapsed time, total cost, who holds the writer lock (`✎ writer:`) and the number of vault hits.
- A board at the bottom lists the tasks and slots.
- Final answers print in the same stacked format.

## Logo

Put your logo in `~/.config/claude-codex-fusion/logo.txt` (plain text or ANSI color, up to 12 lines). It appears on the left of the start screen. `FUSION_LOGO=/path/to/file` points somewhere else. Without a file, the built-in mark is shown.

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
