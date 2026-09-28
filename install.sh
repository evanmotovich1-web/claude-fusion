#!/usr/bin/env bash
# Installs `claude-codex-fusion` (and `claude codex fusion` via a shell function).
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)/fusion.py"
BIN="${HOME}/.local/bin"
mkdir -p "$BIN"
chmod +x "$SRC"
ln -sf "$SRC" "$BIN/claude-codex-fusion"
echo "linked $BIN/claude-codex-fusion -> $SRC"

# `claude codex fusion` (three words) routes to the fusion shell; every other
# `claude ...` call passes straight through to the real Claude Code CLI.
SNIPPET='
# >>> claude-codex-fusion >>>
claude() { if [ "${1:-}" = "codex" ] && [ "${2:-}" = "fusion" ]; then shift 2; claude-codex-fusion "$@"; else command claude "$@"; fi; }
# <<< claude-codex-fusion <<<'
for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
  [ -f "$rc" ] || continue
  grep -q ">>> claude-codex-fusion >>>" "$rc" || { printf '%s\n' "$SNIPPET" >> "$rc"; echo "added shell function to $rc"; }
done
case ":$PATH:" in *":$BIN:"*) ;; *) echo "add $BIN to PATH";; esac
echo "open a new terminal, then run: claude codex fusion"
