# Claude × Codex Fusion

A terminal shell that runs Claude Code and OpenAI Codex together and merges what they say.

## Install (Mac)

```bash
bash tools/claude_codex_fusion/install.sh
# open a new terminal
claude codex fusion
```

The installer symlinks `~/.local/bin/claude-codex-fusion` and adds a small `claude()` shell function. `claude codex fusion` opens the fusion shell, and every other `claude …` command still goes to the real CLI. You need both `claude` and `codex` logged in. If one is missing, its panel shows `[... CLI not found]` and the shell keeps working.

## Modes

| Command | What happens |
|---|---|
| `/fuse` (default) | Claude and Codex answer at the same time, then the fuser merges the two into one answer and adds "Fusion notes" on where they agreed and disagreed |
| `/duel` | Claude drafts, Codex reviews the draft, then Claude revises it |
| `/claude`, `/codex` | Only one agent answers |
| `/fuser codex` | Codex does the merging instead of Claude |
| `/status` `/history` `/clear` `/quit` | Shell controls |

One-shot: `claude codex fusion "why is this test flaky?"`. Flags: `--mode duel`, `--write` (lets both agents edit files; the default is read-only), `--claude-model`, `--codex-model`, `--cwd`.

## How the two agents work together

- They are **independent drafters**: each one answers without seeing the other's answer, so the two drafts are real second opinions and not echoes.
- They are also **reviewers of each other**: in `/duel`, Codex's review goes back to Claude, and Claude has to accept or reject each point.
- The **record**: every turn (both drafts plus the final answer) is written to `~/.cache/claude-codex-fusion/*.jsonl`. File anything durable from it into the vault with `semantic_note` (AGENTS.md "write as much as you read").
- **Safety**: by default Codex runs with `--sandbox read-only` and Claude runs in print mode without edit permission. `--write` has to be passed explicitly.

## Known risk

Codex OAuth shares a refresh token with the Hermes seats (see [[wiki/connectors-and-capabilities]]). Running `codex login` again on another machine can sign those seats out.

---
Governed by [[AGENTS]] — see AGENTS.md for the rules this file operates under.
