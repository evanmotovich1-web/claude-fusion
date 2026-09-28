"""Live dashboard for claude codex fusion (stdlib only).

Layouts, picked by how many slots are running:
  2-3 agents  D1 columns: one pane per agent side by side
  4-5 agents  D2 tabs: one large pane for the selected agent + task board
Keys while a command runs: 1-5 select a pane, Tab cycles, c/t force
columns/tabs, Ctrl-C interrupts.

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
        # results printed by the command are held until the live view closes
        sys.stdout = self._buffer
        if TTY:
            if not self.compact:
                self.out.write("\033[?1049h\033[?25l")
            self._cbreak(True)
            self._thread.start()
        return self

    def __exit__(self, *_):
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
            elif ch in "ct":
                self.force = {"c": "columns", "t": "tabs"}[ch]

    # drawing --------------------------------------------------------------
    def layout(self) -> str:
        if self.force:
            return self.force
        return "columns" if len(self.h.stack) <= 3 else "tabs"

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
        if self.layout() == "columns":
            out += self.columns(cols, body_h)
        else:
            out += self.tabs(cols, body_h)
        out += board
        out.append(dim(fit(" 1-5 pane · Tab next · c columns · t tabs · Ctrl-C interrupt", cols)))
        self.out.write("\033[H" + "\n".join(fit(line, cols) for line in out[:rows]) + "\033[J")
        self.out.flush()

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
