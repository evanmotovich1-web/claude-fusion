"""Live dashboard for claude codex fusion (stdlib only).

Layouts:
  stack    (default) Fusion Pi look: one pane per agent stacked top to bottom,
           each with role header, live stats line, and its tool/thinking stream
  columns  one pane per agent side by side
  tabs     one large pane for the selected agent
Keys while a command runs: 1-5 select a pane, Tab cycles, s/c/t switch
stack/columns/tabs, Ctrl-C interrupts.

--panes (D3) is handled by fusion.py: every slot streams into its own tmux pane
and this module only draws a one-line status bar in the control pane.
"""

from __future__ import annotations

import io
import os
import re
import select
import shutil
import sys
import threading
import time
import unicodedata

ANSI = re.compile(r"\033\[[0-9;]*m")
GLYPH = {"architect": "◆", "main": "●", "builder": "○"}
STATE = {"idle": "·", "queued": "⏳", "run": "▶", "done": "✓", "fail": "✗"}
TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def fg(hex_color: str, text: str, bold: bool = False) -> str:
    if not TTY:
        return text
    r, g, b = rgb(hex_color)
    return f"\033[{'1;' if bold else ''}38;2;{r};{g};{b}m{text}\033[0m"


def dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if TTY else text


def inv(hex_color: str, text: str) -> str:
    if not TTY:
        return f"[{text}]"
    r, g, b = rgb(hex_color)
    return f"\033[1;38;2;16;8;32;48;2;{r};{g};{b}m{text}\033[0m"


def vlen(s: str) -> int:
    s = ANSI.sub("", s)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def fit(s: str, w: int) -> str:
    """Truncate or pad a (possibly colored) string to exactly w cells."""
    if vlen(s) <= w:
        return s + " " * (w - vlen(s))
    plain = ANSI.sub("", s)
    out, n = "", 0
    for ch in plain:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if n + cw > w - 1:
            break
        out, n = out + ch, n + cw
    return out + "…" + " " * (w - n - 1)


def wrap(text: str, w: int) -> list[str]:
    out: list[str] = []
    for line in text.splitlines() or [""]:
        plain = ANSI.sub("", line)
        if len(plain) <= w:
            out.append(line)
            continue
        while plain:
            out.append(plain[:w])
            plain = plain[w:]
    return out


def mmss(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


class Dashboard:
    """Redraws an alternate-screen dashboard while one /fh command runs."""

    def __init__(self, harness, command: str, prompt: str = ""):
        self.h, self.command, self.prompt = harness, command, prompt
        self.selected = 0
        self.force: str | None = None
        self.phase = ""
        self.t0 = time.time()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.compact = bool(os.environ.get("FUSION_PANES"))
        self._term = None
        self.out = sys.stdout
        self._buffer = io.StringIO()

    # context manager ------------------------------------------------------
    def __enter__(self):
        for s in self.h.stack:
            s.reset_live()
        self.h.dash = self
        if getattr(self.h, "app", None):  # the full-screen app draws its own panes
            return self
        # results printed by the command are held until the live view closes
        sys.stdout = self._buffer
        if TTY:
            if not self.compact:
                self.out.write("\033[?1049h\033[?25l")
            self._cbreak(True)
            self._thread.start()
        return self

    def __exit__(self, *_):
        if getattr(self.h, "app", None):
            self.h.dash = None
            return
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        if TTY:
            self._cbreak(False)
            self.out.write("\r\033[K" if self.compact else "\033[?25h\033[?1049l")
        sys.stdout = self.out
        self.h.dash = None
        self.out.write(self._buffer.getvalue())
        self.out.flush()

    def _cbreak(self, on: bool):
        try:
            import termios
            import tty
        except ImportError:
            return
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
        if on:
            self._term = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        elif self._term:
            termios.tcsetattr(fd, termios.TCSADRAIN, self._term)

    def _loop(self):
        while not self._stop.is_set():
            self._keys()
            try:
                self.draw()
            except Exception:  # never let drawing kill a paid run
                pass
            self._stop.wait(0.12)
        self.draw()

    def _keys(self):
        if not os.isatty(sys.stdin.fileno()):
            return
        while select.select([sys.stdin], [], [], 0)[0]:
            ch = sys.stdin.read(1)
            n = len(self.h.stack)
            if ch.isdigit() and 1 <= int(ch) <= n:
                self.selected = int(ch) - 1
            elif ch == "\t":
                self.selected = (self.selected + 1) % n
            elif ch in "sct":
                self.force = {"s": "stack", "c": "columns", "t": "tabs"}[ch]

    # drawing --------------------------------------------------------------
    def layout(self) -> str:
        if self.force:
            return self.force
        return os.environ.get("FUSION_LAYOUT", "stack")

    def status_line(self, w: int) -> str:
        cost = sum(s.cost for s in self.h.stack)
        writer = self.h.writer
        bits = [
            fg("#E9D5FF", "FUSION", bold=True),
            f"/{self.command}",
            f"{len(self.h.stack)} agents",
            mmss(time.time() - self.t0),
            f"${cost:.2f}" if cost else "$—",
            (fg("#FBBF24", f"✎ writer: {writer}") if writer else dim("✎ writer: none")),
        ]
        v = getattr(self.h, "vault", None)
        if v and v.live:
            bits.append(fg("#67E8F9", f"vault {v.hits} hits"))
        if self.phase:
            bits.append(dim(self.phase))
        return fit(" ─ ".join(bits), w)

    def slot_title(self, i: int, s, w: int) -> str:
        t = f"{i + 1} {GLYPH[s.kind]} {s.name} · {s.model_label} {STATE[s.state]}"
        if s.state in ("run", "done", "fail") and s.seconds:
            t += f" {s.seconds:.0f}s"
        return fit(t, w)

    def draw(self):
        cols, rows = shutil.get_terminal_size((120, 40))
        if self.compact:
            chips = " ".join(fg(s.color, f"{GLYPH[s.kind]}{s.name}{STATE[s.state]}") for s in self.h.stack)
            self.out.write("\r\033[K" + fit(self.status_line(cols // 2) + "  " + chips, cols - 1))
            self.out.flush()
            return
        out = ["╭" + self.status_line(cols - 2) + "╮"]
        board = self.board(cols)
        body_h = max(4, rows - len(out) - len(board) - 3)
        layout = self.layout()
        if layout == "columns":
            out += self.columns(cols, body_h)
        elif layout == "tabs":
            out += self.tabs(cols, body_h)
        else:
            out += self.stack(cols, body_h)
        out += board
        out.append(dim(fit(" 1-5 pane · Tab next · s stack · c columns · t tabs · Ctrl-C interrupt", cols)))
        self.out.write("\033[H" + "\n".join(fit(line, cols) for line in out[:rows]) + "\033[J")
        self.out.flush()

    def stack(self, cols: int, h: int) -> list[str]:
        """Fusion Pi layout: every agent is its own pane, stacked."""
        slots = self.h.stack
        n = len(slots)
        # the selected pane gets the spare lines
        each = max(3, h // n)
        spare = h - each * n
        out: list[str] = []
        for i, s in enumerate(slots):
            size = each + (spare if i == self.selected else 0)
            head = f"{GLYPH[s.kind]} {s.role} | {s.name} "
            model = f"| {s.model_label}"
            sel = i == self.selected
            out.append(fg(s.color, head, bold=True) + dim(model) + (fg(s.color, "  ◂") if sel else ""))
            out.append(stats_line(s))
            body = [l for text in s.live for l in wrap(text, cols - 4)]
            body = body[-(size - 2):] if size > 2 else []
            for line in body:
                out.append("  " + line)
            out += [""] * (size - 2 - len(body))
        return out[:h]

    def columns(self, cols: int, h: int) -> list[str]:
        n = len(self.h.stack)
        cw = (cols - (n + 1)) // n
        heads, bodies = [], []
        for i, s in enumerate(self.h.stack):
            title = self.slot_title(i, s, cw)
            heads.append(inv(s.color, title) if i == self.selected else fg(s.color, title, bold=True))
            lines = [l for text in s.live for l in wrap(text, cw)]
            bodies.append(lines[-(h - 1):])
        out = [dim("│") + dim("│").join(heads) + dim("│")]
        for r in range(h - 1):
            row = []
            for i, s in enumerate(self.h.stack):
                row.append(fit(bodies[i][r] if r < len(bodies[i]) else "", cw))
            out.append(dim("│") + dim("│").join(row) + dim("│"))
        return out

    def tabs(self, cols: int, h: int) -> list[str]:
        chips = []
        for i, s in enumerate(self.h.stack):
            label = f" {i + 1} {GLYPH[s.kind]}{s.name} {STATE[s.state]} "
            chips.append(inv(s.color, label) if i == self.selected else fg(s.color, label))
        s = self.h.stack[min(self.selected, len(self.h.stack) - 1)]
        inner = cols - 4
        head = fg(s.color, "┌ " + self.slot_title(self.selected, s, inner) + " ┐", bold=True)
        lines = [l for text in s.live for l in wrap(text, inner)][-(h - 3):]
        out = [" ".join(chips), head]
        for r in range(h - 3):
            out.append(fg(s.color, "│ ") + fit(lines[r] if r < len(lines) else "", inner) + fg(s.color, " │"))
        out.append(fg(s.color, "└" + "─" * (cols - 2) + "┘"))
        return out

    def board(self, cols: int) -> list[str]:
        out = []
        if self.h.tasks:
            chips = []
            for t in self.h.tasks:
                slot = self.h.slot(t["assignee"])
                lock = " ✎" if t.get("state") == "run" and t["mode"] == "write" else ""
                chips.append(fg(slot.color if slot else "#888888",
                                f"{t['id']} {t['assignee']} {t['mode']} {STATE.get(t.get('state', 'queued'), '?')}{lock}"))
            out.append(fit(dim(" TASKS  ") + dim(" │ ").join(chips), cols))
        chips = []
        for s in self.h.stack:
            bit = f"{GLYPH[s.kind]}{s.name} {STATE[s.state]}"
            if s.seconds:
                bit += f" {s.seconds:.0f}s"
            if s.cost:
                bit += f" ${s.cost:.2f}"
            chips.append(fg(s.color, bit))
        out.append(fit(dim(" SLOTS  ") + dim(" │ ").join(chips), cols))
        return out


def human(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def stats_line(s) -> str:
    """`◐ working 52s · in 531.1k out 3.1k · 67 tps · 5 tools · $0.49`"""
    bits = []
    if s.tokens_in or s.tokens_out:
        bits.append(f"in {human(s.tokens_in)} out {human(s.tokens_out)}")
    if s.tokens_out and s.seconds >= 2:
        bits.append(f"{s.tokens_out / s.seconds:.0f} tps")
    if s.tools:
        bits.append(f"{s.tools} tool{'s' if s.tools != 1 else ''}")
    if s.cost:
        bits.append(f"${s.cost:.4f}")
    tail = (" · " + " · ".join(bits)) if bits else ""
    if s.state == "run":
        spin = "◐◓◑◒"[int(time.time() * 4) % 4]
        return fg("#FDE047", f"{spin} working {s.seconds:.0f}s{tail}")
    if s.state == "done":
        return fg(GREEN, f"✓ done {s.seconds:.1f}s{tail}")
    if s.state == "fail":
        return fg("#F87171", f"✗ failed {s.seconds:.1f}s{tail}")
    if s.state == "queued":
        return dim("⏳ queued")
    return dim("· idle")


GREEN = "#4ADE80"


# ── model bar, context bars, model catalog (full-screen app) ────────────────
# Same readout as the Pi fusion-harness model bar (modules/tui.ts cellStr):
#   ◆ ARCHITECT | name | model (hi) | [██--------] 12% | 87 tps | $0.0123

THINKING_SHORT = {"off": "none", "minimal": "min", "low": "low", "medium": "med",
                  "high": "hi", "xhigh": "xhi", "max": "max"}
FALLBACK_WINDOW = 1_000_000  # Pi's fallback when the registry has no window
# Claude models the claude CLI accepts; the CLI reports each one's window after a turn
CLAUDE_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-haiku-4-5-20251001"]
PALETTE = ["#A78BFA", "#F59E0B", "#22D3EE", "#F472B6", "#4ADE80"]


def thinking_tag(level: str) -> str:
    return f" ({THINKING_SHORT.get(level, level)})" if level else ""


def ctx_bar(used: int, window: int) -> str:
    """Pi's bar exactly: `[██--------] 12%`."""
    pct = max(0.0, min(1.0, used / window if window > 0 else 0.0))
    filled = round(pct * 10)
    return f"[{'█' * filled}{'-' * (10 - filled)}] {round(pct * 100)}%"


def _size(text: str) -> int:
    m = re.fullmatch(r"([\d.]+)([KM]?)", text.strip())
    if not m:
        return 0
    return int(float(m.group(1)) * {"K": 1_000, "M": 1_000_000, "": 1}[m.group(2)])


def pi_models(cache_dir, max_age: float = 86400) -> list[dict]:
    """Pi's model registry (`pi --list-models`): provider, id, context window. Cached a day."""
    import subprocess
    cache = cache_dir / "pi-models.txt"
    try:
        fresh = cache.exists() and time.time() - cache.stat().st_mtime < max_age
        if not fresh and shutil.which("pi"):
            out = subprocess.run(["pi", "--list-models"], capture_output=True, text=True, timeout=60).stdout
            if "provider" in out:
                cache.write_text(out)
        raw = cache.read_text() if cache.exists() else ""
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in raw.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 3:
            rows.append({"provider": cols[0], "id": cols[1], "window": _size(cols[2])})
    return rows


PI_STORE = os.path.expanduser("~/.pi/agent/models-store.json")
_PRICES: dict[str, dict] = {}


def pi_prices() -> dict[str, dict]:
    """Per-model $/M-token rates from Pi's own registry, keyed by id and provider/id."""
    if not _PRICES:
        import json
        try:
            with open(PI_STORE) as f:
                store = json.load(f)
        except (OSError, ValueError):
            return _PRICES
        for provider, block in store.items():
            for m in (block or {}).get("models", []) if isinstance(block, dict) else []:
                if m.get("id") and m.get("cost"):
                    _PRICES.setdefault(m["id"], m["cost"])
                    _PRICES[f"{provider}/{m['id']}"] = m["cost"]
    return _PRICES


def token_cost(model: str, fresh_in: int, out: int, cache_read: int = 0, cache_write: int = 0) -> float:
    """Dollar cost of one turn at Pi's rates for the model (0 when Pi has no rate for it)."""
    rate = pi_prices().get(model) or pi_prices().get(model.split("/", 1)[-1])
    if not rate:
        return 0.0
    return (fresh_in * rate.get("input", 0) + out * rate.get("output", 0)
            + cache_read * rate.get("cacheRead", 0) + cache_write * rate.get("cacheWrite", 0)) / 1_000_000


def codex_default_model() -> str:
    try:
        with open(os.path.expanduser("~/.codex/config.toml")) as f:
            m = re.search(r'^model\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


_ROLLOUTS: dict[str, tuple[str, float, int, int]] = {}


def codex_context(thread: str) -> tuple[int, int] | None:
    """(prompt tokens of the latest call, model context window) from the codex rollout file."""
    import glob
    hit = _ROLLOUTS.get(thread)
    path = hit[0] if hit else next(iter(glob.glob(os.path.expanduser(
        f"~/.codex/sessions/*/*/*/rollout-*-{thread}.jsonl"))), "")
    if not path:
        return None
    try:
        mtime = os.stat(path).st_mtime
        if hit and hit[1] == mtime:
            return hit[2], hit[3]
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 262144))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    import json
    for line in reversed(tail):
        if '"token_count"' not in line:
            continue
        try:
            info = json.loads(line)["payload"].get("info") or {}
        except (ValueError, KeyError, AttributeError):
            continue
        last = info.get("last_token_usage") or {}
        if last:
            used, window = int(last.get("input_tokens") or 0), int(info.get("model_context_window") or 0)
            _ROLLOUTS[thread] = (path, mtime, used, window)
            return used, window
    return None


def context_window(s, catalog: list[dict]) -> int:
    """What the CLI reported, else Pi's registry entry for the model, else Pi's 1M fallback."""
    if s.ctx_window:
        return s.ctx_window
    model = s.model or (codex_default_model() if s.cli == "codex" else "")
    ident = model.split("/", 1)[-1]
    for row in catalog:
        if model in (row["id"], f"{row['provider']}/{row['id']}") or ident == row["id"]:
            return row["window"]
    return FALLBACK_WINDOW


def model_choices(catalog: list[dict]) -> list[tuple[str, str, int]]:
    """Every model /fusion can seat: (cli, model, window). codex models run on the codex CLI."""
    # newest version first within each provider, so typing "sol" or "grok" lands on the latest
    newest = sorted(catalog, key=lambda r: (r["provider"], [-n for n in _version(r["id"])]))
    out = [("claude", "", 0)] + [("claude", m, 0) for m in CLAUDE_MODELS] + [("codex", "", 0)]
    out += [("codex", r["id"], r["window"]) for r in newest if r["provider"] == "openai-codex"]
    out += [("pi", f"{r['provider']}/{r['id']}", r["window"]) for r in newest if r["provider"] != "openai-codex"]
    return out


def _version(model: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", model))


def resolve_model(token: str, choices: list[tuple[str, str, int]]) -> tuple[str, str, str] | None:
    """`opus`, `gpt-6-sol`, `grok:medium`, `xai/grok-4.7`, `codex` → (cli, model, thinking)."""
    thinking = ""
    if ":" in token and token.rsplit(":", 1)[1] in THINKING_SHORT:
        token, thinking = token.rsplit(":", 1)
    token = token.strip().lower()
    if token in ("claude", "codex"):
        return token, "", thinking
    exact = [c for c in choices if c[1] and token in (c[1].lower(), c[1].split("/")[-1].lower())]
    hits = exact or [c for c in choices if c[1] and token in c[1].lower()]
    if not hits:
        return None
    cli, model, _ = max(hits, key=lambda c: _version(c[1]))
    return cli, model, thinking


def slot_name(cli: str, model: str, taken: set[str]) -> str:
    """Short slot name: claude-opus-5-5 → opus, gpt-6-sol → sol, xai/grok-4.7 → grok."""
    parts = re.split(r"[-_./:]", model.split("/")[-1].lower()) if model else []
    words = [p for p in parts if p.isalpha() and p not in ("claude", "gpt", "openai", "chat", "latest")]
    base = words[0] if words else (parts[0] if parts and parts[0].isalpha() else cli)
    name, n = base, 2
    while name in taken:
        name, n = f"{base}{n}", n + 1
    return name
