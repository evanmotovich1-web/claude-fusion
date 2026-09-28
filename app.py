"""Full-screen app for claude codex fusion (Textual).

Launching takes over the whole terminal:
  splash    animated logo for about a second (any key skips)
  top bar   ✻ FUSION · cwd · stack · writer lock · vault · cost · phase
  left      conversation log (your prompts, fused answers, results)
  right     one live pane per agent: role, stats line, tool/thinking stream
  bottom    input box; typing `/` opens the command menu (↑↓ to scroll,
            Enter/Tab to pick, Esc to close)

The engine is fusion.py; this module only renders it. Commands run in a
worker thread, their printed output is routed into the log.
"""

from __future__ import annotations

import os
import time

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
    ("/fh-opinion", "<prompt>", "every agent answers independently, read-only"),
    ("/fh-fusion", '"<prompt>" "<instruction>"', "read-only workers → one FUSION writer → every agent ACKs"),
    ("/fh-debate", "[--rounds N] <prompt>", "N-round debate between all agents, no judge"),
    ("/fh-collaborate", "<prompt>", "proposals → architect task graph → one writer at a time"),
    ("/fh-only", "<slot> [prompt]", "talk to one agent; without a prompt arms the next message"),
    ("/fh-stack", "[duo|trio|quint|file.json]", "list or switch agent stacks (2-5 agents)"),
    ("/fh-model", "<slot> <model> [thinking]", "change one agent's model for this session"),
    ("/fh-system-prompt", "", "show each agent's appended system prompt"),
    ("/fh-reset", "", "fresh sessions for every agent"),
    ("/fh-cwd", "<path>", "move every agent to another project folder"),
    ("/fh-add-dir", "<path>", "let write-enabled agents also edit this folder"),
    ("/fh", "", "command index"),
    ("/clear", "", "clear the conversation log"),
    ("/quit", "", "leave fusion"),
]
NO_ARGS = {cmd for cmd, hint, _ in MENU if not hint}


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
        title = "Claude × Codex Fusion"
        shown = title[: min(len(title), self.frame)]
        t = Text(shown, style="bold #FFFFFF")
        if self.frame > len(title):
            t.append("\n" + "fuse your agents · AND, not OR", style="#A78BFA")
        self.query_one("#splash-title", Static).update(t)
        if time.time() - self.t0 > 1.6:
            self.dismiss()

    def on_key(self, event):
        self.dismiss()


class AgentPane(Static):
    """One agent: bordered pane in the slot's color with live stats and stream."""

    def __init__(self, slot):
        super().__init__("", classes="agent")
        self.slot = slot

    def on_mount(self):
        s = self.slot
        self.styles.border = ("round", s.color)
        self.border_title = f"{fusion.GLYPH[s.kind]} {s.role} · {s.name}"
        self.border_subtitle = s.model_label

    def refresh_live(self):
        s = self.slot
        running = s.state == "run"
        self.styles.border = ("heavy" if running else "round", s.color)
        rows = max(1, self.size.height - 3)
        width = max(10, self.size.width - 4)
        out = Text.from_ansi(tui.stats_line(s))
        lines = [l for text in s.live for l in tui.wrap(text, width)][-rows:]
        for line in lines:
            out.append("\n")
            out.append_text(Text.from_ansi(line))
        if not lines:
            out.append("\n")
            out.append("waiting for a command" if s.state == "idle" else "", style="dim")
        self.update(out)


class FusionApp(App):
    TITLE = "Claude × Codex Fusion"
    CSS = f"""
    Screen {{ background: {BG}; layers: base menu; }}
    #topbar {{ height: 1; background: #2A1250; color: #E9D5FF; padding: 0 1; }}
    #body {{ height: 1fr; }}
    #log {{ width: 3fr; background: {BG}; border: round #3B1D6E; padding: 0 1; scrollbar-size-vertical: 1; }}
    #agents {{ width: 2fr; min-width: 36; background: {BG}; }}
    .agent {{ height: 1fr; min-height: 5; background: {BG}; padding: 0 1; }}
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

    # layout ---------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Horizontal(id="body"):
            yield RichLog(id="log", wrap=True, markup=False, highlight=False)
            yield Vertical(id="agents")
        yield OptionList(id="menu")
        yield Input(id="prompt")
        yield Static("", id="footer")

    def on_mount(self):
        fusion.print = self.log_print  # route engine output into the log
        self.build_agents()
        self.query_one("#prompt", Input).focus()
        self.set_interval(0.2, self.tick)
        self.greet()
        self.push_screen(Splash())
        self.fit_width()

    def on_resize(self, event):
        self.fit_width()

    def fit_width(self):
        log = self.query_one("#log", RichLog)
        # fusion.py wraps panels to the terminal width; make that the log width
        os.environ["COLUMNS"] = str(max(40, log.size.width - 4 or 80))

    def build_agents(self):
        box = self.query_one("#agents", Vertical)
        box.remove_children()
        box.mount_all([AgentPane(s) for s in self.h.stack])

    def greet(self):
        log = self.query_one("#log", RichLog)
        head = logo_text()
        log.write(head)
        t = Text("Claude × Codex Fusion", style="bold #FFFFFF")
        t.append(f"  v{fusion.VERSION}\n", style="#8B7BB0")
        t.append("Type to talk to the main builder. Type / for every command.\n", style="#C4B5FD")
        for s in self.h.stack:
            ok = bool(fusion.shutil.which(s.cli))
            t.append(f"  {fusion.GLYPH[s.kind]} {s.name:<9}", style=f"bold {s.color}")
            t.append(f"{s.kind:<11}{s.model_label:<28}", style="#C4B5FD")
            t.append("● online\n" if ok else f"● {s.cli} missing\n", style="#4ADE80" if ok else "#F87171")
        log.write(t)

    # live refresh ---------------------------------------------------------
    def tick(self):
        for pane in self.query(AgentPane):
            pane.refresh_live()
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
        self.query_one("#topbar", Static).update(Text("  │  ".join(bits)))
        target = h.armed or h.main
        prompt = self.query_one("#prompt", Input)
        state = "working… (wait for the run to finish)" if self.busy else f"message {target.name} ({target.kind}) · / for commands"
        prompt.placeholder = state
        prompt.border_title = f"{fusion.GLYPH[target.kind]} {target.name}"
        self.query_one("#footer", Static).update(
            "/ commands  ·  ↑↓ history  ·  ctrl+s next agent  ·  ctrl+l clear  ·  ctrl+c twice quit")

    # output ---------------------------------------------------------------
    def log_print(self, *args, sep=" ", end="\n", **_):
        text = sep.join(str(a) for a in args)
        self.call_from_thread(self.query_one("#log", RichLog).write, Text.from_ansi(text))

    # slash menu -----------------------------------------------------------
    def on_input_changed(self, event: Input.Changed):
        value = event.value
        menu = self.query_one("#menu", OptionList)
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

    def on_option_list_option_selected(self, event):
        self.action_menu_pick()
        self.query_one("#prompt", Input).focus()

    def _set_prompt(self, value: str):
        prompt = self.query_one("#prompt", Input)
        prompt.value = value
        prompt.cursor_position = len(value)

    # submitting -----------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted):
        if self._menu_open():
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
        echo = Text("\n❯ ", style="bold #F0ABFC")
        echo.append(line, style="bold #FFFFFF")
        log.write(echo)
        self.busy, self.t_run = True, time.time()
        self.run_worker(lambda: self.run_line(line), thread=True, exclusive=True)

    def run_line(self, line: str):
        h = self.h
        try:
            if line.startswith("/fh-model"):
                self.set_model(line.split()[1:])
            elif line.startswith("/fh-stack") and line.split()[1:]:
                fusion.dispatch(h, line)
                self.call_from_thread(self.build_agents)
            elif line.startswith("/"):
                fusion.dispatch(h, line)
            else:
                h.chat(line)
        except Exception as exc:  # show engine errors instead of dying
            self.log_print(fusion.fg("#F87171", f"✗ {type(exc).__name__}: {exc}"))
        finally:
            self.busy = False

    def set_model(self, args: list[str]):
        if len(args) < 2 or not self.h.slot(args[0]):
            self.log_print(fusion.fg("#FBBF24", "usage: /fh-model <slot> <model> [thinking]  ·  slots: "
                                     + ", ".join(s.name for s in self.h.stack)))
            return
        s = self.h.slot(args[0])
        s.model, s.session, s.started = args[1], None, False
        if len(args) > 2:
            s.thinking = args[2]
        self.log_print(fusion.fg("#4ADE80", f"  {s.name} → {s.label} thinking={s.thinking} (session only)"))
        self.call_from_thread(self.build_agents)

    # keys -----------------------------------------------------------------
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
