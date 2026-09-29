"""Full-screen app for claude codex fusion (Textual).

Launching takes over the whole terminal:
  splash    animated logo for about a second (any key skips)
  top bar   ✻ FUSION · cwd · stack · writer lock · vault · cost · phase
  left      conversation log (your prompts, fused answers, results)
  right     one live pane per agent: role, stats line, tool/thinking stream
  bottom    input box; typing `/` opens the command menu (↑↓ to scroll,
            Enter/Tab to pick, Esc to close); under it the model bar, one
            line per agent: role · model · context bar · tps · spend
  run       while a command runs, one lane per running agent fills the
            screen side by side (Pi's live columns); the log returns after

/fusion N [models…] seats N agents (2-5); missing models are picked from a
menu of the claude, codex, and Pi model lists.

The engine is fusion.py; this module only renders it. Commands run in a
worker thread, their printed output is routed into the log.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from rich import box
from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

import fusion
import tui

BG = fusion.BACKGROUND
LABEL = fusion.LABEL
DIM = "#5B4B7B"
GREEN = "#4ADE80"
RED = "#F87171"

BIG_LOGO = [
    "███████╗██╗   ██╗███████╗██╗ ██████╗ ███╗   ██╗",
    "██╔════╝██║   ██║██╔════╝██║██╔═══██╗████╗  ██║",
    "█████╗  ██║   ██║███████╗██║██║   ██║██╔██╗ ██║",
    "██╔══╝  ██║   ██║╚════██║██║██║   ██║██║╚██╗██║",
    "██║     ╚██████╔╝███████║██║╚██████╔╝██║ ╚████║",
    "╚═╝      ╚═════╝ ╚══════╝╚═╝ ╚═════╝ ╚═╝  ╚═══╝",
]
GRADIENT = ["#C4B5FD", "#A78BFA", "#C084FC", "#E879F9", "#F0ABFC", "#FBBF24", "#F59E0B", "#FBBF24", "#F0ABFC", "#C084FC"]

# every command the menu offers: (command, argument hint, description)
MENU = [
    ("/fusion", "[2-5] [model…]", "how many agents, then each one's model (opens a picker)"),
    ("/fh-opinion", "<prompt>", "every agent answers independently, read-only"),
    ("/fh-fusion", '"<prompt>" "<instruction>"', "read-only workers → one FUSION writer → every agent ACKs"),
    ("/fh-debate", "[--rounds N] <prompt>", "N-round debate between all agents, no judge"),
    ("/fh-collaborate", "<prompt>", "proposals → architect task graph → one writer at a time"),
    ("/fh-only", "<slot> [prompt]", "talk to one agent; without a prompt arms the next message"),
    ("/fh-stack", "[duo|trio|quint|file.json]", "list or switch agent stacks (2-5 agents)"),
    ("/fh-model", "[slot] [model] [thinking]", "pick an agent's model from every configured provider"),
    ("/fh-system-prompt", "", "show each agent's appended system prompt"),
    ("/fh-reset", "", "fresh sessions for every agent"),
    ("/fh-cwd", "<path>", "move every agent to another project folder"),
    ("/fh-add-dir", "<path>", "let write-enabled agents also edit this folder"),
    ("/fh", "", "command index"),
    ("/clear", "", "clear the conversation log"),
    ("/quit", "", "leave fusion"),
]
# multi-agent commands the architect sums up afterwards (side panel + last log panel)
NARROW = 110  # below this many columns the side panel moves under the log
SUMMARY_COMMANDS = ("/fh-opinion", "/fh-fusion", "/fh-debate", "/fh-collaborate")
NO_ARGS = {cmd for cmd, hint, _ in MENU if not hint} | {"/fh-model", "/fusion"}  # these open a picker on their own


def logo_text(frame: int = 0) -> Text:
    """Your logo from ~/.config/claude-codex-fusion/logo.txt, else FUSION with a moving color sweep."""
    try:
        raw = fusion.LOGO_FILE.read_text().rstrip("\n")
        if raw.strip():
            return Text.from_ansi(raw)
    except OSError:
        pass
    out = Text()
    for line in BIG_LOGO:
        for x, ch in enumerate(line):
            color = GRADIENT[((x // 4) - frame) % len(GRADIENT)]
            out.append(ch, style=f"bold {color}")
        out.append("\n")
    return out


class Splash(Screen):
    """Animated logo, then hands over to the main screen."""

    DEFAULT_CSS = f"""
    Splash {{ align: center middle; background: {BG}; }}
    #splash-logo {{ width: auto; height: auto; content-align: center middle; }}
    #splash-title {{ width: auto; margin-top: 1; content-align: center middle; }}
    """

    def __init__(self):
        super().__init__()
        self.frame = 0
        self.t0 = time.time()

    def compose(self) -> ComposeResult:
        with Vertical(id="splash-box"):
            yield Static(logo_text(), id="splash-logo")
            yield Static("", id="splash-title")

    def on_mount(self):
        self.query_one("#splash-box").styles.width = "auto"
        self.query_one("#splash-box").styles.height = "auto"
        self.set_interval(0.07, self.tick)

    def tick(self):
        self.frame += 1
        self.query_one("#splash-logo", Static).update(logo_text(self.frame))
        title = "Claude × LLMs Fusion"
        shown = title[: min(len(title), self.frame)]
        t = Text(shown, style="bold #FFFFFF")
        if self.frame > len(title):
            t.append("\n" + "fuse your agents · AND, not OR", style="#A78BFA")
        self.query_one("#splash-title", Static).update(t)
        if time.time() - self.t0 > 1.6:
            self.dismiss()

    def on_key(self, event):
        self.dismiss()


class FollowLog(RichLog):
    """Follows new output only while you are at the bottom; scrolling up holds your place.
    Keeps what it showed and redraws it when its width changes, so every line re-wraps."""

    follow = True
    KEEP = 3000  # entries kept for reflow

    def __init__(self, *args, **kwargs):
        super().__init__(*args, min_width=20, **kwargs)  # RichLog's default draws nothing under 78 columns
        self.entries: list = []  # renderables, or callables(width) -> renderable
        self.drawn_width = 0
        self.replaying = False

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if not self.replaying:
            self.follow = new_value >= self.max_scroll_y - 1

    def content_width(self) -> int:
        return max(20, self.scrollable_content_region.width or 80)

    def write(self, content, width=None, expand=False, shrink=True, scroll_end=None, animate=False):
        if not self.replaying:
            self.entries.append(content)
            del self.entries[: -self.KEEP]
            if not self.drawn_width:  # size not known yet: the first reflow draws everything once
                return self
        shown = content(self.content_width()) if callable(content) else content
        return super().write(shown, width, expand, shrink, self.follow if scroll_end is None else scroll_end, animate)

    def clear(self):
        self.entries = []
        return super().clear()

    def on_resize(self, event) -> None:
        if self.size.width and self.size.width != self.drawn_width:
            # first draw at once; after that let a drag settle, then redraw once
            self.set_timer(0.12 if self.drawn_width else 0.01, self.reflow)

    def reflow(self):
        if not self.size.width or self.size.width == self.drawn_width:
            return
        self.drawn_width = self.size.width
        follow, ratio = self.follow, (self.scroll_y / self.max_scroll_y) if self.max_scroll_y else 1.0
        self.replaying = True
        super().clear()
        for entry in self.entries:
            self.write(entry, scroll_end=False)
        self.replaying = False
        if follow:
            self.scroll_end(animate=False, immediate=False)
        else:
            self.call_after_refresh(lambda: self.scroll_to(y=round(ratio * self.max_scroll_y), animate=False))


PANEL_FLOW = 6  # live steps shown per agent in the side panel (Pi shows 8 in its widget)


def agent_block(s, width: int, flow: int | None = None) -> Text:
    """Pi's agent block: label, then the working line and its latest steps (one line each,
    cut with …), a green stat block once done, a red reason on failure, or waiting."""
    out = Text(f"{fusion.GLYPH[s.kind]} {s.role} | {s.name}", style=f"bold {s.color}")
    out.append(" | ", style=DIM)
    out.append(s.model_label, style=s.color)
    if s.state == "done":
        for i, line in enumerate(stat_lines(s)):
            out.append(("\n✓ " if i == 0 else "\n  ") + line, style=GREEN)
        return out
    out.append("\n")
    out.append_text(Text.from_ansi(tui.stats_line(s)))
    if s.state == "fail":  # the reason, not just its last line (Pi puts the hint last)
        lines = [Text.from_ansi(l).plain for l in s.live if l.strip()]
        start = max((i for i, l in enumerate(lines) if l.startswith("✗")), default=max(0, len(lines) - 1))
        for line in lines[start : start + 4] or ["✗ failed"]:
            t = Text(line, style=RED)
            t.truncate(width, overflow="ellipsis")
            out.append("\n")
            out.append_text(t)
        return out
    if s.state != "run":
        return out
    lines = [l for l in s.live if l.strip()]
    for line in lines[-flow:] if flow else lines:
        t = Text.from_ansi(line)
        if t.plain.startswith("▸"):
            t.stylize("#E9D5FF")
        elif t.plain.startswith("▹"):
            t.stylize("italic #8B7BB0")
        t.truncate(width, overflow="ellipsis")
        out.append("\n")
        out.append_text(t)
    return out


class AgentPane(Static):
    """One agent in the side panel, in Pi's block format; the architect also shows the pinned summary."""

    def __init__(self, slot):
        super().__init__("", classes="agent")
        self.slot = slot

    def refresh_live(self):
        s = self.slot
        block = agent_block(s, max(10, self.size.width), PANEL_FLOW)
        summary = getattr(self.app, "summary", None)
        if s.architect and summary and s.state != "run":
            block.append("\n◆ SUMMARY", style=f"bold {s.color}")
            self.update(Group(block, Markdown(summary)))
        else:
            self.update(block)


class LanePane(VerticalScroll):
    """One agent's lane while a run is going (Pi's live column): label, state, live flow.
    Scrolls back through the whole stream; follows new lines while you are at the bottom."""

    follow = True

    def __init__(self, slot):
        super().__init__(classes="lane")
        self.slot = slot
        self.body = Static("")

    def compose(self) -> ComposeResult:
        yield self.body

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self.follow = new_value >= self.max_scroll_y - 1

    def refresh_live(self):
        s = self.slot
        out = agent_block(s, max(10, self.size.width - 3))
        self.body.update(out)
        if self.follow:
            self.scroll_end(animate=False, immediate=False)


def stat_lines(s) -> list[str]:
    """Pi's done-agent stat block, one value per line."""
    lines = [f"TIME: {s.seconds:.1f}s"]
    if s.tokens_in:
        lines.append(f"TOKENS IN: {tui.human(s.tokens_in)}")
    if s.tokens_out:
        lines.append(f"TOKENS OUT: {tui.human(s.tokens_out)}")
    if s.tokens_out and s.seconds:
        lines.append(f"TPS: {s.tokens_out / s.seconds:.0f}")
    if s.tools:
        lines.append(f"TOOLS: {s.tools}")
    if s.cost:
        lines.append(f"COST: ${s.cost:.4f}")
    return lines


def final_answers(d: Path) -> list[tuple[str, str]]:
    """The last word of a run: collab final.md / fused.md, else the last debate round, else each opinion."""
    for name in ("final.md", "fused.md"):
        if (d / name).exists():
            return [(name[:-3], (d / name).read_text())]
    rounds = sorted(d.glob("round-*"), key=lambda p: int(p.name.split("-")[-1]) if p.name.split("-")[-1].isdigit() else 0)
    base = rounds[-1] if rounds else d
    return [(p.stem, p.read_text()) for p in sorted(base.glob("*.md")) if p.name != "summary.md"]


def roster_table(slots, kind) -> Table:
    """Agent rows as auto-sizing columns, so they re-fit when the window is resized."""
    table = Table.grid(padding=(0, 2), pad_edge=True)
    for _ in range(4):
        table.add_column(no_wrap=True)
    table.columns[2].overflow = "ellipsis"
    table.columns[2].no_wrap = True
    for s in slots:
        ok = bool(fusion.shutil.which(s.cli))
        table.add_row(Text(f"  {fusion.GLYPH[s.kind]} {s.name}", style=f"bold {s.color}"),
                      Text(kind(s), style="#C4B5FD"), Text(s.model_label, style="#C4B5FD"),
                      Text("● online" if ok else f"● {s.cli} missing", style=GREEN if ok else RED))
    return table


TASK_GLYPH = {"queued": ("○", DIM), "run": ("◐", "#FDE047"), "done": ("✓", GREEN), "fail": ("✗", RED)}


def task_board(h, width: int, rows: int = 8) -> Text:
    """Pi's collaborate board: `⇄ TASKS · 1/5 settled · reads overlap · ONE writer at a time`,
    then one row per task: state · id · assignee · mode · what it is doing · description."""
    tasks = h.tasks
    settled = sum(t.get("state") in ("done", "fail") for t in tasks)
    out = Text(f"⇄ TASKS · {settled}/{len(tasks)} settled · reads overlap · ONE writer at a time", style="bold #E9D5FF",
               no_wrap=True, overflow="ellipsis")
    for t in tasks[:rows]:
        state = t.get("state", "queued")
        glyph, color = TASK_GLYPH.get(state, ("?", DIM))
        doing = {"queued": "pending", "run": "writing" if t["mode"] == "write" else "reading", "done": "done", "fail": "failed"}.get(state, state)
        slot = h.slot(t["assignee"])
        row = Text("  ")
        row.append(glyph, style=color)
        row.append(f" {t['id']} · ", style="#C4B5FD")
        row.append(t["assignee"], style=f"bold {slot.color if slot else '#FFFFFF'}")
        row.append(f" · {t['mode']} · ", style="#C4B5FD")
        row.append(doing, style=color)
        row.append(" · " + " ".join(str(t.get("description", "")).split()), style="#E9D5FF")
        row.truncate(width, overflow="ellipsis")
        out.append("\n")
        out.append_text(row)
    if len(tasks) > rows:
        out.append(f"\n  … {len(tasks) - rows} more", style=DIM)
    return out


def window_label(window: int) -> str:
    if not window:
        return "window reported after its first turn"
    return f"{window / 1_000_000:g}M ctx" if window >= 1_000_000 else f"{window / 1000:g}K ctx"


class FusionApp(App):
    TITLE = "Claude × LLMs Fusion"
    CSS = f"""
    Screen {{ background: {BG}; layers: base menu; }}
    #topbar {{ height: 1; background: #2A1250; color: #E9D5FF; padding: 0 1; }}
    #body {{ height: 1fr; }}
    #lanes {{ height: 1fr; display: none; background: {BG}; layout: vertical; padding: 0 1; }}
    .lane {{ width: 1fr; height: 1fr; background: {BG}; margin-bottom: 1; scrollbar-size-vertical: 1; }}
    .lane > Static {{ height: auto; }}
    #modelbar {{ height: auto; padding: 0 2; background: {BG}; }}
    #tasks {{ height: auto; padding: 0 2; background: {BG}; display: none; border-top: solid #3B1D6E; }}
    #log {{ width: 3fr; background: {BG}; border: round #3B1D6E; padding: 0 1; scrollbar-size-vertical: 1; }}
    #body.narrow {{ layout: vertical; }}
    #body.narrow #log {{ width: 1fr; height: 3fr; }}
    #body.narrow #agents {{ width: 1fr; height: 2fr; min-width: 0; border-left: none; border-top: solid #3B1D6E; }}
    #agents {{ width: 2fr; min-width: 36; background: {BG}; padding: 0 1; border-left: solid #3B1D6E; scrollbar-size-vertical: 1; }}
    .agent {{ height: auto; background: {BG}; margin-bottom: 1; }}
    #menu {{ height: auto; max-height: 14; display: none; background: #231044; border: round #A78BFA; margin: 0 1; }}
    #menu > .option-list--option-highlighted {{ background: #A78BFA; color: #120826; text-style: bold; }}
    #prompt {{ margin: 0 1; background: #1F0D3D; border: round #A78BFA; }}
    #prompt:focus {{ border: round #F0ABFC; }}
    #footer {{ height: 1; color: #8B7BB0; padding: 0 2; }}
    """
    BINDINGS = [
        Binding("up", "menu_up", show=False, priority=True),
        Binding("down", "menu_down", show=False, priority=True),
        Binding("tab", "menu_pick", show=False, priority=True),
        Binding("escape", "menu_close", show=False, priority=True),
        Binding("ctrl+c", "quit_twice", show=False, priority=True),
        Binding("pageup", "scroll_view(-1)", show=False, priority=True),
        Binding("pagedown", "scroll_view(1)", show=False, priority=True),
        Binding("shift+up", "scroll_view(-0.1)", show=False, priority=True),
        Binding("shift+down", "scroll_view(0.1)", show=False, priority=True),
        Binding("ctrl+l", "clear_log", show=False),
        Binding("ctrl+s", "next_slot", show=False),
    ]

    def __init__(self, harness):
        super().__init__()
        self.h = harness
        self.h.app = self
        self.busy = False
        self.history: list[str] = []
        self.hpos = 0
        self.t_run = 0.0
        self.last_ctrl_c = 0.0
        self.menu_items: list[tuple[str, str, str]] = []
        self.summary: str | None = None  # architect's sum-up of the last multi-agent run
        self.catalog: list[dict] = []  # Pi's model registry, loaded in the background
        self.pick: dict | None = None  # open picker: title, rows, on_pick, typed
        self.pick_items: list[tuple[str, Text, object]] = []
        self.perf: dict[int, list] = {}  # per slot: [session tokens out, running seconds, last seen, live_epoch]
        self.t_tick = time.time()
        self.ticks = 0

    # layout ---------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Horizontal(id="body"):
            yield FollowLog(id="log", wrap=True, markup=False, highlight=False)
            yield VerticalScroll(id="agents")
        yield Horizontal(id="lanes")
        yield OptionList(id="menu")
        yield Input(id="prompt")
        yield Static("", id="tasks")
        yield Static("", id="modelbar")
        yield Static("", id="footer")

    def on_mount(self):
        fusion.print = self.log_print  # route engine output into the log
        fusion.panel = self.log_panel  # boxes that re-wrap on resize, not fixed-width text art
        fusion.grid = self.log_grid
        self.build_agents()
        self.query_one("#prompt", Input).focus()
        self.set_interval(0.2, self.tick)
        threading.Thread(target=self.load_catalog, daemon=True).start()
        self.greet()
        self.push_screen(Splash())
        self.fit_width()

    def on_resize(self, event):
        self.fit_width()
        self.query_one("#body").set_class(self.size.width < NARROW, "narrow")

    def fit_width(self):
        log = self.query_one("#log", RichLog)
        if not log.size.width:  # hidden while the run lanes are up
            return
        # fusion.py wraps panels to the terminal width; make that the log width
        os.environ["COLUMNS"] = str(max(40, log.size.width - 4 or 80))

    def build_agents(self):
        box = self.query_one("#agents", VerticalScroll)
        box.remove_children()
        box.mount_all([AgentPane(s) for s in self.h.stack])
        lanes = self.query_one("#lanes", Horizontal)
        lanes.remove_children()
        lanes.mount_all([LanePane(s) for s in self.h.stack])

    def load_catalog(self):
        self.catalog = tui.pi_models(fusion.CACHE)

    def greet(self):
        log = self.query_one("#log", RichLog)
        # the big logo when it fits, a one-line mark when the log is narrow
        log.write(lambda w: logo_text() if w >= len(BIG_LOGO[0]) + 2 else Text("✻ FUSION", style="bold #F0ABFC"))
        t = Text("Claude × LLMs Fusion", style="bold #FFFFFF")
        t.append(f"  v{fusion.VERSION}\n", style="#8B7BB0")
        t.append("Type to talk to the main builder. Type / for every command.", style="#C4B5FD")
        log.write(Group(t, roster_table(self.h.stack, lambda s: s.kind)))

    # live refresh ---------------------------------------------------------
    def tick(self):
        for pane in self.query(AgentPane):
            pane.refresh_live()
        self.ticks += 1
        self.query_one("#body").set_class(self.size.width < NARROW, "narrow")
        if self.ticks % 5 == 0:
            self.fit_width()  # keep engine panels at the log's width, not only after a resize
        self.track_perf()
        self.focus_lanes()
        self.query_one("#modelbar", Static).update(self.model_bar())
        board = self.query_one("#tasks", Static)
        show = bool(self.busy and self.h.tasks)  # the /fh-collaborate task graph while it runs
        if board.display != show:
            board.display = show
        if show:
            board.update(task_board(self.h, max(20, board.size.width - 4 or self.size.width - 4)))
        h = self.h
        cwd = h.cwd.replace(os.path.expanduser("~"), "~")
        cost = sum(s.cost for s in h.stack)
        v = h.vault
        extra = f" +{len(fusion.EXTRA_DIRS)} dirs" if fusion.EXTRA_DIRS else ""
        bits = [f"✻ FUSION", cwd + extra, f"{len(h.stack)} agents",
                f"✎ {h.writer}" if h.writer else "✎ none",
                f"vault {v.hits} hits" if v.live else "vault off",
                f"${cost:.2f}" if cost else "$—"]
        if self.busy:
            bits.append(f"{tui.mmss(time.time() - self.t_run)}")
            if h.dash and h.dash.phase:
                bits.append(h.dash.phase)
        top = Text("  │  ".join(bits) if self.size.width >= 100 else " │ ".join(bits), no_wrap=True, overflow="ellipsis")
        self.query_one("#topbar", Static).update(top)
        target = h.armed or h.main
        prompt = self.query_one("#prompt", Input)
        state = "working… (wait for the run to finish)" if self.busy else f"message {target.name} ({target.kind}) · / for commands"
        if self.pick:
            state = f"{self.pick['title']} · type to filter · ↑↓ Enter · Esc cancels"
        prompt.placeholder = state
        prompt.border_title = f"{fusion.GLYPH[target.kind]} {target.name}"
        keys = ("/ commands  ·  ↑↓ history  ·  wheel/PgUp/PgDn scroll  ·  ctrl+s next agent  ·  ctrl+l clear  ·  ctrl+c twice quit"
                if self.size.width >= 120 else "/ cmds · ↑↓ history · PgUp/PgDn · ctrl+s agent · ctrl+c×2 quit")
        self.query_one("#footer", Static).update(Text(keys, no_wrap=True, overflow="ellipsis"))

    def track_perf(self):
        """Session tps per slot (output tokens over running seconds), plus codex context from its rollout."""
        now = time.time()
        dt, self.t_tick = now - self.t_tick, now
        for s in self.h.stack:
            p = self.perf.setdefault(id(s), [0, 0.0, 0, s.live_epoch])
            if s.live_epoch != p[3]:  # reset_live zeroed the counters at the start of a run
                p[2], p[3] = 0, s.live_epoch
            # a drop within the same run is a correction (claude's result event replaces streamed partials), so it subtracts
            p[0] = max(0, p[0] + s.tokens_out - p[2])
            p[2] = s.tokens_out
            if s.state == "run":
                p[1] += dt
            if s.cli == "codex" and s.thread and self.ticks % 5 == 0:
                got = tui.codex_context(s.thread)
                if got:
                    s.ctx_tokens, s.ctx_window = got

    def focus_lanes(self):
        """While a command runs, its agents' lanes are the only thing on screen."""
        h, dash = self.h, self.h.dash
        body, lanes = self.query_one("#body"), self.query_one("#lanes", Horizontal)
        focus = dash is not None
        if body.display == focus:
            body.display, lanes.display = not focus, focus
        if not focus:
            return
        if dash.force == "tabs" and dash.selected < len(h.stack):
            shown = [h.stack[dash.selected]]
        else:
            shown = [s for s in h.stack if s.state != "idle"] or list(h.stack)
        # one stacked column like Pi: running agents share the height, finished ones stay compact
        for pane in self.query(LanePane):
            on = pane.slot in shown
            if pane.display != on:
                pane.display = on
            if on:
                height = "1fr" if pane.slot.state == "run" else "auto"
                if str(pane.styles.height) != height:
                    pane.styles.height = height
                pane.refresh_live()

    def model_bar(self) -> Text:
        """Pi's model bar: `◆ ARCHITECT | name | model (hi) | [██--------] 12% | 87 tps | $0.0123`.
        Narrower screens drop the role word, then the model, so every row stays on one line."""
        stack = self.h.stack
        room = max(20, self.query_one("#modelbar").size.width - 4 or self.size.width - 4)
        rows = []
        for s in stack:
            out_tokens, secs = self.perf.get(id(s), [0, 0.0])[:2]
            # thinking only shows where the CLI is actually given it (codex, pi)
            model = s.model_label + (tui.thinking_tag(s.thinking) if s.cli != "claude" else "")
            rows.append({
                "s": s, "glyph": fusion.GLYPH[s.kind], "role": s.role, "name": s.name, "model": model,
                "bar": tui.ctx_bar(s.ctx_tokens, tui.context_window(s, self.catalog)),
                "perf": f"{round(out_tokens / secs)} tps | ${s.cost:.4f}" if out_tokens and secs else f"— tps | ${s.cost:.4f}",
            })
        tiers = [("role", "name", "model", "bar", "perf"), ("name", "model", "bar", "perf"), ("name", "bar", "perf"), ("name", "bar")]
        for cols in tiers:
            widths = {c: max(len(r[c]) for r in rows) for c in cols}
            if sum(widths.values()) + 2 + 3 * (len(cols) - 1) <= room:
                break
        out = Text(no_wrap=True, overflow="ellipsis")
        for i, r in enumerate(rows):
            if i:
                out.append("\n")
            out.append(r["glyph"] + " ", style=r["s"].color)
            for j, c in enumerate(cols):
                if j:
                    out.append(" | ", style=DIM)
                out.append(r[c].ljust(widths[c]) if j < len(cols) - 1 else r[c], style=r["s"].color)
        return out

    # output ---------------------------------------------------------------
    def log_print(self, *args, sep=" ", end="\n", **_):
        text = sep.join(str(a) for a in args)
        self.call_from_thread(self.query_one("#log", RichLog).write, Text.from_ansi(text))

    def log_panel(self, slot, title: str, text: str, color: str = LABEL):
        """fusion.panel for the app: the same titled box, drawn at whatever width the log has."""
        color = slot.color if slot else color
        box_ = Panel(Text.from_ansi(text), title=Text.from_ansi(title), title_align="left",
                     border_style=color, box=box.SQUARE, padding=(0, 1))
        self.call_from_thread(self.query_one("#log", RichLog).write, box_)

    def log_grid(self, cols):
        """fusion.grid for the app: role header, stats line, answer indented; re-wraps on resize."""
        log = self.query_one("#log", RichLog)
        for slot, title, text in cols:
            head = Text(f"\n{fusion.GLYPH[slot.kind]} {slot.role} | {slot.name} ", style=f"bold {slot.color}")
            head.append(f"| {slot.model_label}", style=DIM)
            head.append("\n")
            head.append_text(Text.from_ansi(tui.stats_line(slot)))
            self.call_from_thread(log.write, Group(head, Padding(Text.from_ansi(text), (0, 0, 0, 2))))

    # slash menu -----------------------------------------------------------
    def on_input_changed(self, event: Input.Changed):
        value = event.value
        menu = self.query_one("#menu", OptionList)
        if self.pick:
            self.show_picker(value)
            return
        if value.startswith("/") and " " not in value:
            items = [m for m in MENU if m[0].startswith(value)] or []
            presets = []
            self.menu_items = items + presets
            menu.clear_options()
            for cmd, hint, desc in self.menu_items:
                label = Text(f"{cmd:<18}", style="bold #F0ABFC")
                label.append(f"{hint:<30}", style="#C4B5FD")
                label.append(desc, style="#8B7BB0")
                menu.add_option(Option(label))
            menu.display = bool(self.menu_items)
            if self.menu_items:
                menu.highlighted = 0
        else:
            menu.display = False

    def _menu_open(self) -> bool:
        return self.query_one("#menu", OptionList).display

    def action_menu_up(self):
        if self._menu_open():
            self.query_one("#menu", OptionList).action_cursor_up()
        elif self.history:
            self.hpos = max(0, self.hpos - 1)
            self._set_prompt(self.history[self.hpos])

    def action_menu_down(self):
        if self._menu_open():
            self.query_one("#menu", OptionList).action_cursor_down()
        elif self.history:
            self.hpos = min(len(self.history), self.hpos + 1)
            self._set_prompt(self.history[self.hpos] if self.hpos < len(self.history) else "")

    def action_menu_pick(self):
        if self.pick:
            self.pick_current()
            return
        if not self._menu_open():
            return
        menu = self.query_one("#menu", OptionList)
        idx = menu.highlighted or 0
        cmd = self.menu_items[idx][0]
        menu.display = False
        if cmd in NO_ARGS:
            self._set_prompt("")
            self.submit(cmd)
        else:
            self._set_prompt(cmd + " ")

    def action_menu_close(self):
        self.query_one("#menu", OptionList).display = False
        if self.pick:
            self.close_picker()
            self._set_prompt("")
            self.query_one("#log", RichLog).write(Text("  picker closed · nothing changed", style="#8B7BB0"))

    # pickers: /fusion and /fh-model walk a menu one step at a time ---------
    def open_picker(self, title: str, rows: list[tuple[str, Text, object]], on_pick, typed=None):
        """rows are (search text, label, value); typed turns free text into a value when nothing matches."""
        self.pick = {"title": title, "rows": rows, "on_pick": on_pick, "typed": typed}
        self._set_prompt("")
        self.show_picker("")

    def close_picker(self):
        menu = self.query_one("#menu", OptionList)
        menu.display, menu.border_title, self.pick = False, "", None

    def show_picker(self, needle: str):
        menu = self.query_one("#menu", OptionList)
        needle = needle.strip().lower()
        self.pick_items = [r for r in self.pick["rows"] if needle in r[0].lower()]
        menu.clear_options()
        for _, label, _ in self.pick_items:
            menu.add_option(Option(label))
        menu.border_title = self.pick["title"]
        menu.display = bool(self.pick_items)
        if self.pick_items:
            menu.highlighted = 0

    def pick_current(self):
        menu = self.query_one("#menu", OptionList)
        typed = self.query_one("#prompt", Input).value.strip()
        if self.pick_items:
            value = self.pick_items[menu.highlighted or 0][2]
        else:
            value = self.pick["typed"](typed) if typed and self.pick["typed"] else None
            if value is None:
                return
        on_pick = self.pick["on_pick"]
        self.close_picker()
        self._set_prompt("")
        on_pick(value)

    def model_rows(self, color: str) -> list[tuple[str, Text, object]]:
        """Every configured provider: claude CLI, codex CLI, and each provider in Pi's registry."""
        rows = []
        for cli, model, window in tui.model_choices(self.catalog):
            name = model or f"{cli}-default"
            label = Text(f"{name:<36}", style=f"bold {color}")
            label.append(f"{cli:<8}", style="#C4B5FD")
            label.append(window_label(window), style="#8B7BB0")
            rows.append((f"{name} {cli}", label, (cli, model, "")))
        return rows

    def resolve_typed(self, text: str):
        return tui.resolve_model(text, tui.model_choices(self.catalog))

    # /fusion N: seat N agents, each on the model you pick
    def pick_role(self, k: int) -> str:
        return "ARCHITECT" if k == 0 else "BUILDER (Main)" if k == 1 else "BUILDER"

    def start_fusion(self, args: list[str]):
        log = self.query_one("#log", RichLog)
        if not args:  # no number: ask how many, then walk the model picker
            now = len(self.h.stack)
            rows = []
            for n in range(2, 6):
                label = Text(f"{n} agents", style="bold #F0ABFC")
                if n == now:
                    label.append("   current", style="#8B7BB0")
                rows.append((str(n), label, n))
            self.open_picker("/fusion · how many agents", rows, lambda n: self.fusion_step(n, []))
            return
        if not args[0].isdigit():
            t = Text("  /fusion [N] [model…]  seats N agents (2-5); models you leave out are picked from a menu\n"
                     "  e.g. /fusion  ·  /fusion 3  ·  /fusion 3 opus sol grok  ·  model:medium sets thinking\n",
                     style="#C4B5FD")
            t.append("  now: " + " · ".join(s.label for s in self.h.stack), style="#8B7BB0")
            log.write(t)
            return
        n = int(args[0])
        if not 2 <= n <= 5:
            log.write(Text("  the engine runs 2 to 5 agents per run", style="#FBBF24"))
            return
        chosen = []
        for token in args[1 : n + 1]:
            got = self.resolve_typed(token)
            if not got:
                log.write(Text(f"  no model matches '{token}' · /fusion {n} alone opens the picker", style="#FBBF24"))
                return
            chosen.append(got)
        self.fusion_step(n, chosen)

    def fusion_step(self, n: int, chosen: list):
        k = len(chosen)
        if k == n:
            self.apply_fusion(chosen)
            return
        self.open_picker(f"agent {k + 1} of {n} · {self.pick_role(k)}", self.model_rows(tui.PALETTE[k]),
                         lambda v: self.fusion_step(n, chosen + [v]), self.resolve_typed)

    def apply_fusion(self, chosen: list[tuple[str, str, str]]):
        taken: set[str] = set()
        slots = []
        for i, (cli, model, thinking) in enumerate(chosen):
            name = tui.slot_name(cli, model, taken)
            taken.add(name)
            slots.append(fusion.Slot(name, cli, model=model, thinking=thinking or "high",
                                     architect=i == 0, primary=i == 1, color=tui.PALETTE[i]))
        h = self.h
        h.stack, h.tasks, h.armed = slots, [], None
        self.perf.clear()
        self.build_agents()
        head = Text(f"  fusion {len(slots)} · fresh sessions", style=f"bold {GREEN}")
        self.query_one("#log", RichLog).write(Group(head, roster_table(slots, lambda s: f"{s.role}  {s.cli}")))

    # /fh-model [slot] [model] [thinking]: agent → model → thinking, from every configured provider
    def start_model(self, args: list[str]):
        h, log = self.h, self.query_one("#log", RichLog)
        if args and not h.slot(args[0]):
            log.write(Text(f"  no agent named '{args[0]}' · agents: " + ", ".join(s.name for s in h.stack), style="#FBBF24"))
            return
        if not args:
            rows = []
            for s in h.stack:
                label = Text(f"{fusion.GLYPH[s.kind]} {s.name:<10}", style=f"bold {s.color}")
                label.append(f"{s.role:<16}", style="#C4B5FD")
                label.append(s.model_label, style="#8B7BB0")
                rows.append((f"{s.name} {s.model_label}", label, s))
            self.open_picker("/fh-model · which agent", rows, self.model_step)
            return
        s = h.slot(args[0])
        if len(args) == 1:
            self.model_step(s)
            return
        got = self.resolve_typed(args[1]) or (s.cli, args[1], "")  # unknown names go to the agent's own CLI
        self.apply_model(s, got[0], got[1], args[2] if len(args) > 2 else got[2])

    def model_step(self, s):
        self.open_picker(f"/fh-model · {s.name} · model", self.model_rows(s.color),
                         lambda v: self.thinking_step(s, v), self.resolve_typed)

    def thinking_step(self, s, choice):
        cli, model, thinking = choice
        if thinking or cli == "claude":  # the claude CLI is not given a thinking level
            self.apply_model(s, cli, model, thinking)
            return
        rows = []
        for level in ("low", "medium", "high", "xhigh"):
            label = Text(f"{level:<10}", style=f"bold {s.color}")
            if level == s.thinking:
                label.append("current", style="#8B7BB0")
            rows.append((level, label, level))
        self.open_picker(f"/fh-model · {s.name} · thinking", rows, lambda lv: self.apply_model(s, cli, model, lv))

    def apply_model(self, s, cli: str, model: str, thinking: str = ""):
        s.cli, s.model = cli, model
        s.session, s.started, s.thread = None, False, None
        s.ctx_tokens = s.ctx_window = 0
        if thinking:
            s.thinking = thinking
        self.build_agents()
        tag = f" thinking={s.thinking}" if cli != "claude" else ""
        self.query_one("#log", RichLog).write(
            Text(f"  {s.name} → {cli} · {s.model_label}{tag} (fresh session, this session only)", style=GREEN))

    def on_option_list_option_selected(self, event):
        self.action_menu_pick()
        self.query_one("#prompt", Input).focus()

    def _set_prompt(self, value: str):
        prompt = self.query_one("#prompt", Input)
        prompt.value = value
        prompt.cursor_position = len(value)

    # submitting -----------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted):
        if self.pick or self._menu_open():
            self.action_menu_pick()
            return
        line = event.value.strip()
        if not line:
            return
        self._set_prompt("")
        self.submit(line)

    def submit(self, line: str):
        log = self.query_one("#log", RichLog)
        if self.busy:
            log.write(Text("  a run is still going; wait for it to finish", style="#FBBF24"))
            return
        self.history.append(line)
        self.hpos = len(self.history)
        if line in ("/quit", "/exit", "/q"):
            self.exit()
            return
        if line == "/clear":
            self.action_clear_log()
            return
        if line.split()[0] in ("/fusion", "/fh-model"):
            echo = Text("\n❯ ", style="bold #F0ABFC")
            echo.append(line, style="bold #FFFFFF")
            log.write(echo)
            start = self.start_fusion if line.split()[0] == "/fusion" else self.start_model
            start(line.split()[1:])
            return
        echo = Text("\n❯ ", style="bold #F0ABFC")
        echo.append(line, style="bold #FFFFFF")
        log.write(echo, scroll_end=True)
        if line.split()[0] in SUMMARY_COMMANDS:
            self.summary = None
        self.busy, self.t_run = True, time.time()
        self.run_worker(lambda: self.run_line(line), thread=True, exclusive=True)

    def run_line(self, line: str):
        h = self.h
        try:
            if line.startswith("/fh-stack") and line.split()[1:]:
                fusion.dispatch(h, line)
                self.call_from_thread(self.build_agents)
            elif line.startswith("/"):
                fusion.dispatch(h, line)
                if line.split()[0] in SUMMARY_COMMANDS:
                    self.summarize(line.split()[0], line[len(line.split()[0]):].strip())
            else:
                h.chat(line)
        except Exception as exc:  # show engine errors instead of dying
            self.log_print(fusion.fg("#F87171", f"✗ {type(exc).__name__}: {exc}"))
        finally:
            self.busy = False

    def summarize(self, cmd: str, request: str):
        """The architect reads every agent's final answer from the run folder and sums it up."""
        runs = sorted((fusion.CACHE / "runs").glob(f"{cmd[1:]}-*"), key=lambda p: p.stat().st_mtime)
        if not runs or runs[-1].stat().st_mtime < self.t_run - 1:
            return
        d = runs[-1]
        answers = final_answers(d)
        if not answers:
            return
        h, a = self.h, self.h.architect
        body = "\n\n".join(f"[{name.upper()}]\n{text[:6000]}" for name, text in answers)
        prompt = fusion.fill("USER_PROMPT_ARCHITECT_SUMMARY.md", SLOT_NAME=a.name, MODEL=a.label,
                             COMMAND=cmd[1:], PROMPT=request, ANSWERS=body)
        self.log_print(fusion.dim(f"  {a.name} is summing up {len(answers)} answer(s)…"))
        r = fusion.run_slot(a, prompt, h.cwd, write=False, fresh=True, harness=h)
        if not r.ok:
            self.log_print(fusion.fg(RED, f"  ✗ summary failed: {r.text[:200]}"))
            return
        (d / "summary.md").write_text(r.text)
        self.summary = r.text
        fusion.panel(a, f"◆ SUMMARY · {fusion.stat(r)}", r.text)

    # keys -----------------------------------------------------------------
    def action_scroll_view(self, pages: float):
        """PgUp/PgDn (and shift+↑↓ for a few lines) scroll the log, or every lane during a run."""
        if self.query_one("#lanes").display:
            views = [p for p in self.query(LanePane) if p.display]
        else:
            views = [self.query_one("#log", FollowLog)]
        for v in views:
            step = max(1, v.size.height - 2) if abs(pages) >= 1 else 3
            v.scroll_relative(y=step if pages > 0 else -step, animate=False)

    def action_clear_log(self):
        self.query_one("#log", RichLog).clear()

    def action_next_slot(self):
        stack = self.h.stack
        cur = self.h.armed or self.h.main
        nxt = stack[(stack.index(cur) + 1) % len(stack)]
        self.h.armed = None if nxt is self.h.main else nxt

    def action_quit_twice(self):
        now = time.time()
        if now - self.last_ctrl_c < 1.5:
            self.exit()
        else:
            self.last_ctrl_c = now
            self.query_one("#log", RichLog).write(Text("  press ctrl+c again to quit", style="#8B7BB0"))


def run(harness) -> int:
    FusionApp(harness).run()
    return 0
