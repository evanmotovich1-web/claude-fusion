#!/usr/bin/env python3
"""claude-codex-fusion: one terminal, two coding agents, one fused answer.

Every prompt goes to Claude Code (`claude -p`) and Codex (`codex exec`) in
parallel. A fuser (Claude by default) then reads both drafts, keeps what they
agree on, settles what they disagree on, and prints one answer. Stdlib only.

Modes (switch with a slash command):
  /fuse    both agents answer, then the fuser merges (default)
  /duel    Claude answers, Codex critiques it, Claude revises
  /claude  Claude only        /codex   Codex only
Other commands: /fuser claude|codex, /status, /history, /clear, /help, /quit

Nothing here edits files by itself: both CLIs run in their own read-only or
default sandbox unless you pass --write. Transcripts go to
~/.cache/claude-codex-fusion/<timestamp>.jsonl.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

C = {"reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
     "claude": "\033[38;5;208m", "codex": "\033[38;5;39m", "fuse": "\033[38;5;171m",
     "err": "\033[31m", "ok": "\033[32m"}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = {k: "" for k in C}

BANNER = r"""
   ___ _                 _          __  __   ___          _
  / __| |__ _ _  _ __| |___  \ \/ /  / __|___ __| |_____ __
 | (__| / _` | || / _` / -_)  >  <  | (__/ _ \/ _` / -_) \ /
  \___|_\__,_|\_,_\__,_\___| /_/\_\  \___\___/\__,_\___/_\_\
                    F  U  S  I  O  N
"""

HISTORY_TURNS = 6
TIMEOUT = int(os.environ.get("FUSION_TIMEOUT", "600"))


def c(color: str, text: str) -> str:
    return f"{C[color]}{text}{C['reset']}"


class Spinner:
    def __init__(self, label: str):
        self.label, self._stop = label, threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        frames, i, start = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", 0, time.time()
        while not self._stop.is_set():
            if sys.stdout.isatty():
                sys.stdout.write(f"\r{c('dim', frames[i % 10] + ' ' + self.label)} {time.time()-start:4.0f}s ")
                sys.stdout.flush()
            i += 1
            time.sleep(0.1)
        if sys.stdout.isatty():
            sys.stdout.write("\r\033[K")

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._t.join()


class Agents:
    def __init__(self, cwd: str, write: bool, claude_model: str | None, codex_model: str | None):
        self.cwd, self.write = cwd, write
        self.claude_model, self.codex_model = claude_model, codex_model
        self.has = {"claude": bool(shutil.which("claude")), "codex": bool(shutil.which("codex"))}

    def run(self, who: str, prompt: str) -> str:
        if not self.has[who]:
            return f"[{who} CLI not found on PATH]"
        try:
            return self._claude(prompt) if who == "claude" else self._codex(prompt)
        except subprocess.TimeoutExpired:
            return f"[{who} timed out after {TIMEOUT}s]"

    def _claude(self, prompt: str) -> str:
        cmd = ["claude", "-p", "--output-format", "text"]
        if self.claude_model:
            cmd += ["--model", self.claude_model]
        if self.write:
            cmd += ["--permission-mode", "acceptEdits"]
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           cwd=self.cwd, timeout=TIMEOUT)
        return r.stdout.strip() or f"[claude exit {r.returncode}] {r.stderr.strip()[-800:]}"

    def _codex(self, prompt: str) -> str:
        with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as out:
            path = out.name
        cmd = ["codex", "exec", "--skip-git-repo-check", "--color", "never",
               "--sandbox", "workspace-write" if self.write else "read-only",
               "--output-last-message", path]
        if self.codex_model:
            cmd += ["--model", self.codex_model]
        cmd.append("-")  # read prompt from stdin
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               cwd=self.cwd, timeout=TIMEOUT)
            text = Path(path).read_text().strip() if Path(path).exists() else ""
        finally:
            Path(path).unlink(missing_ok=True)
        return text or r.stdout.strip()[-4000:] or f"[codex exit {r.returncode}] {r.stderr.strip()[-800:]}"


def with_history(history: list[dict], prompt: str) -> str:
    if not history:
        return prompt
    lines = ["Conversation so far (most recent last):"]
    for turn in history[-HISTORY_TURNS:]:
        lines += [f"USER: {turn['prompt']}", f"ASSISTANT: {turn['answer'][:3000]}"]
    return "\n".join(lines) + f"\n\nNEW REQUEST:\n{prompt}"


FUSE_PROMPT = """You are the fuser in a Claude Code + Codex fusion harness.
Two independent agents answered the same request. Produce ONE final answer.
Rules: keep what both agree on; where they disagree, pick the better-supported
side and say why in one line; add anything correct that only one caught; drop
anything unsupported. Do not mention "Draft A/B" in the answer body except in a
final short section titled "Fusion notes" (agreements, disagreements, who won).

REQUEST:
{prompt}

DRAFT A (Claude Code):
{a}

DRAFT B (Codex):
{b}
"""

CRITIQUE_PROMPT = """Review this answer from another coding agent for the request below.
List concrete errors, missing steps, and risky suggestions. Be terse. If it is
correct, say so.

REQUEST:
{prompt}

ANSWER:
{a}
"""

REVISE_PROMPT = """Revise your answer using the reviewer's critique. Accept valid
points, reject wrong ones with one line of reasoning. Output the final answer.

REQUEST:
{prompt}

YOUR ANSWER:
{a}

CRITIQUE (from Codex):
{b}
"""


class Fusion:
    def __init__(self, agents: Agents, fuser: str):
        self.agents, self.fuser, self.mode = agents, fuser, "fuse"
        self.history: list[dict] = []
        log_dir = Path.home() / ".cache" / "claude-codex-fusion"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log = log_dir / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    def other(self, who: str) -> str:
        return "codex" if who == "claude" else "claude"

    def panel(self, who: str, text: str):
        label = {"claude": "Claude Code", "codex": "Codex", "fuse": "FUSED"}[who]
        width = min(shutil.get_terminal_size().columns, 100)
        print(c(who, f"┌─ {label} " + "─" * max(0, width - len(label) - 4)))
        for line in text.splitlines() or [""]:
            print(c(who, "│ ") + line)
        print(c(who, "└" + "─" * (width - 1)))

    def ask(self, prompt: str):
        full = with_history(self.history, prompt)
        rec = {"ts": datetime.now().isoformat(), "mode": self.mode, "prompt": prompt}
        if self.mode in ("claude", "codex"):
            with Spinner(f"{self.mode} thinking"):
                ans = self.agents.run(self.mode, full)
            self.panel(self.mode, ans)
        elif self.mode == "fuse":
            with Spinner("claude + codex in parallel"), cf.ThreadPoolExecutor(2) as ex:
                fa, fb = ex.submit(self.agents.run, "claude", full), ex.submit(self.agents.run, "codex", full)
                a, b = fa.result(), fb.result()
            self.panel("claude", a)
            self.panel("codex", b)
            with Spinner(f"fusing ({self.fuser})"):
                ans = self.agents.run(self.fuser, FUSE_PROMPT.format(prompt=full, a=a, b=b))
            rec.update(claude=a, codex=b)
            self.panel("fuse", ans)
        else:  # duel
            with Spinner("claude drafting"):
                a = self.agents.run("claude", full)
            self.panel("claude", a)
            with Spinner("codex critiquing"):
                b = self.agents.run("codex", CRITIQUE_PROMPT.format(prompt=full, a=a))
            self.panel("codex", b)
            with Spinner("claude revising"):
                ans = self.agents.run("claude", REVISE_PROMPT.format(prompt=full, a=a, b=b))
            rec.update(claude=a, codex=b)
            self.panel("fuse", ans)
        rec["answer"] = ans
        self.history.append(rec)
        with self.log.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def command(self, line: str) -> bool:
        cmd, _, arg = line[1:].partition(" ")
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd in ("fuse", "duel", "claude", "codex"):
            self.mode = cmd
            print(c("ok", f"mode → {cmd}"))
        elif cmd == "fuser" and arg in ("claude", "codex"):
            self.fuser = arg
            print(c("ok", f"fuser → {arg}"))
        elif cmd == "status":
            self.status()
        elif cmd == "history":
            for i, t in enumerate(self.history, 1):
                print(c("dim", f"{i}. [{t['mode']}] {t['prompt'][:80]}"))
        elif cmd == "clear":
            self.history.clear()
            print(c("ok", "history cleared"))
        else:
            print(__doc__.split("Modes")[1].split("Nothing")[0])
        return True

    def status(self):
        h = self.agents.has
        mark = lambda ok: c("ok", "online") if ok else c("err", "missing")
        print(f"  {c('claude', 'Claude Code')}: {mark(h['claude'])}   {c('codex', 'Codex')}: {mark(h['codex'])}")
        print(c("dim", f"  mode={self.mode} fuser={self.fuser} write={self.agents.write} cwd={self.agents.cwd}"))
        print(c("dim", f"  log={self.log}"))


def main() -> int:
    p = argparse.ArgumentParser(prog="claude-codex-fusion", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("prompt", nargs="*", help="one-shot prompt; omit for the interactive shell")
    p.add_argument("--mode", choices=["fuse", "duel", "claude", "codex"], default="fuse")
    p.add_argument("--fuser", choices=["claude", "codex"], default="claude")
    p.add_argument("--write", action="store_true", help="let both agents edit files in cwd")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--claude-model", default=os.environ.get("FUSION_CLAUDE_MODEL"))
    p.add_argument("--codex-model", default=os.environ.get("FUSION_CODEX_MODEL"))
    args = p.parse_args()

    fusion = Fusion(Agents(args.cwd, args.write, args.claude_model, args.codex_model), args.fuser)
    fusion.mode = args.mode
    if args.prompt:
        fusion.ask(" ".join(args.prompt))
        return 0

    print(c("fuse", BANNER))
    fusion.status()
    print(c("dim", "  /help for commands, /quit to leave\n"))
    try:
        import readline  # noqa: F401  (line editing + arrow-key history)
    except ImportError:
        pass
    while True:
        try:
            line = input(c("fuse", f"fusion[{fusion.mode}]› ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.startswith("/"):
            if not fusion.command(line):
                break
            continue
        try:
            fusion.ask(line)
        except KeyboardInterrupt:
            print(c("err", "\n  interrupted"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
