#!/usr/bin/env bash
# Installs `claude-codex-fusion` (full-screen app) and the `claude codex fusion` shortcut.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/fusion.py"
BIN="${HOME}/.local/bin"
VENV="${HOME}/.local/share/claude-codex-fusion/venv"
mkdir -p "$BIN"
chmod +x "$SRC"

# The full-screen app needs one package (textual). It lives in its own venv so
# nothing touches your system Python. If this fails, the classic shell still works.
PY=python3
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" && echo "created $VENV"
fi
if "$VENV/bin/python" -m pip install -q --upgrade textual; then
  PY="$VENV/bin/python"
  echo "textual ready ($("$PY" -c 'import textual; print(textual.__version__)'))"
else
  echo "could not install textual; claude codex fusion will use the classic shell"
fi

rm -f "$BIN/claude-codex-fusion"
cat > "$BIN/claude-codex-fusion" <<LAUNCH
#!/usr/bin/env bash
exec "$PY" "$SRC" "\$@"
LAUNCH
chmod +x "$BIN/claude-codex-fusion"
echo "installed $BIN/claude-codex-fusion"

# The shortcut wraps whatever `claude` already is (a dotfiles function, an
# alias, or the plain CLI), so existing launchers keep working. Only the exact
# words `claude codex fusion ...` are intercepted.
read -r -d '' SNIPPET <<'SH' || true
# >>> claude-codex-fusion >>>
export PATH="$HOME/.local/bin:$PATH"
if [ -n "${ZSH_VERSION:-}" ]; then
  if (( ${+aliases[claude]} )); then _fusion_claude_alias="${aliases[claude]}"; unalias claude; fi
  if (( ${+functions[claude]} )) && ! (( ${+functions[_fusion_orig_claude]} )); then functions -c claude _fusion_orig_claude; fi
elif [ -n "${BASH_VERSION:-}" ]; then
  if alias claude >/dev/null 2>&1; then _fusion_claude_alias="$(alias claude | sed "s/^alias claude='//; s/'\$//")"; unalias claude; fi
  if declare -f claude >/dev/null && ! declare -f _fusion_orig_claude >/dev/null; then eval "_fusion_orig_$(declare -f claude)"; fi
fi
claude() {
  if [ "${1:-}" = "codex" ] && [ "${2:-}" = "fusion" ]; then shift 2; claude-codex-fusion "$@"; return; fi
  if typeset -f _fusion_orig_claude >/dev/null 2>&1; then _fusion_orig_claude "$@"
  elif [ -n "${_fusion_claude_alias:-}" ]; then eval "$_fusion_claude_alias \"\$@\""
  else command claude "$@"; fi
}
alias fusion-cc='claude-codex-fusion'
# <<< claude-codex-fusion <<<
SH

install_rc() {
  local rc="$1"
  touch "$rc"
  # Replace an older block so re-running upgrades it. Write through the file
  # (not sed -i) so a dotfiles symlink stays a symlink.
  if grep -q ">>> claude-codex-fusion >>>" "$rc"; then
    local tmp
    tmp="$(mktemp)"
    sed '/# >>> claude-codex-fusion >>>/,/# <<< claude-codex-fusion <<</d' "$rc" > "$tmp"
    cp "$rc" "$rc.fusion-backup"
    cat "$tmp" > "$rc"
    rm -f "$tmp"
  fi
  printf '\n%s\n' "$SNIPPET" >> "$rc"
  echo "installed shortcut in $rc (last block, so it wraps your existing claude launcher)"
}

install_rc "${ZDOTDIR:-$HOME}/.zshrc"
[ -f "$HOME/.bashrc" ] && install_rc "$HOME/.bashrc"
echo
echo "now run:  source ${ZDOTDIR:-$HOME}/.zshrc   (or open a new tab)"
echo "then:     claude codex fusion      (fallback that always works: claude-codex-fusion)"
