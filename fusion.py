#!/usr/bin/env python3
"""claude codex fusion: the Fusion Pi harness, rebuilt over Claude Code + Codex.

Design, commands, and prompts copied from disler/fusion-harness (the Pi
extension behind `fusion` on the Mac; MIT, see prompts/LICENSE-fusion-harness).
Two slots instead of Pi models:

  claude  ARCHITECT       Claude Code CLI (`claude -p`)
  codex   BUILDER (Main)  Codex CLI (`codex exec`)

Raw chat goes to the Main builder only. Fan-out happens only through /fh-*
commands. Every slot gets the fusion-pi communication contract
(prompts/COMMUNICATION.md) appended to its system prompt.

Single-writer invariant: opinion and debate are read-only for every slot.
/fh-fusion workers are read-only and one fresh FUSION agent is the sole writer.
/fh-collaborate serializes write tasks. --read-only removes every write.

Run artifacts: ~/.cache/claude-codex-fusion/runs/<command>-<timestamp>/.
Stdlib only.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"
CACHE = Path.home() / ".cache" / "claude-codex-fusion"
TIMEOUT = int(os.environ.get("FUSION_TIMEOUT", "1800"))
HANDOFF_MAX = 60_000
BACKGROUND = os.environ.get("FUSION_BG", "#1B0B33")  # fusion pi deep purple
TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

READ_ONLY_TOOLS = "Read Grep Glob LS"
WRITE_TOOLS = "Read Grep Glob LS Edit Write MultiEdit Bash"


# ── terminal ──────────────────────────────────────────────────────────────


def fg(hex_color: str, text: str, bold: bool = False) -> str:
    if not TTY:
        return text
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return f"\033[{'1;' if bold else ''}38;2;{r};{g};{b}m{text}\033[0m"


def dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if TTY else text


GREEN, AMBER, RED, LABEL = "#4ADE80", "#FBBF24", "#F87171", "#E9D5FF"


def paint_background(on: bool):
    if not TTY or os.environ.get("FUSION_NO_BG"):
        return
    sys.stdout.write(f"\033]11;{BACKGROUND}\007" if on else "\033]111\007")
    if on:
        sys.stdout.write("\033]0;Claude × Codex Fusion\007")
    sys.stdout.flush()


def width() -> int:
    return max(60, shutil.get_terminal_size((120, 40)).columns)


def wrap(text: str, w: int) -> list[str]:
    out: list[str] = []
    for line in text.splitlines() or [""]:
        out += textwrap.wrap(line, w, replace_whitespace=False, drop_whitespace=False) or [""]
    return out


def header(title: str, sub: str = ""):
    print()
    print(fg(LABEL, f"FUSION HARNESS · {title}", bold=True))
    if sub:
        print(dim(f"  {sub}"))


def panel(slot: "Slot | None", title: str, text: str, color: str = LABEL):
    color = slot.color if slot else color
    w = width()
    print(fg(color, f"┌─ {title} " + "─" * max(0, w - len(title) - 4)))
    for line in wrap(text, w - 2):
        print(fg(color, "│ ") + line)
    print(fg(color, "└" + "─" * (w - 1)))


def grid(cols: list[tuple["Slot", str, str]]):
    """AgentGrid: slot answers side by side, stacked when the terminal is narrow."""
    w = width()
    if len(cols) < 2 or w < 110:
        for slot, title, text in cols:
            panel(slot, title, text)
        return
    cw = (w - 3 * (len(cols) - 1)) // len(cols)
    wrapped = [wrap(text, cw) for _, _, text in cols]
    heads = [fg(s.color, t[:cw].ljust(cw), bold=True) for s, t, _ in cols]
    sep = dim(" │ ")
    print(sep.join(heads))
    print(sep.join(fg(s.color, "─" * cw) for s, _, _ in cols))
    for i in range(max(len(x) for x in wrapped)):
        print(sep.join((x[i] if i < len(x) else "").ljust(cw) for x in wrapped))


class Spinner:
    def __init__(self, label: str):
        self.label, self.status, self._stop = label, {}, threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def set(self, name: str, state: str):
        self.status[name] = state

    def _run(self):
        frames, i, t0 = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", 0, time.time()
        while not self._stop.is_set():
            if TTY:
                states = " ".join(f"{k}:{v}" for k, v in self.status.items())
                line = f"{frames[i % 10]} {self.label} {time.time() - t0:4.0f}s {states}"
                sys.stdout.write("\r\033[K" + dim(line[: width() - 1]))
                sys.stdout.flush()
            i += 1
            time.sleep(0.1)
        if TTY:
            sys.stdout.write("\r\033[K")

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._t.join()


# ── prompts ───────────────────────────────────────────────────────────────


def prompt_file(name: str) -> str:
    return (PROMPTS / name).read_text()


def fill(name: str, **values: str) -> str:
    text = prompt_file(name)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


# ── slots ─────────────────────────────────────────────────────────────────


@dataclass
class Slot:
    name: str
    cli: str  # claude | codex
    model: str = ""  # empty = the CLI's own default
    thinking: str = "high"
    architect: bool = False
    primary: bool = False
    color: str = "#A78BFA"
    append_system_prompt: list[str] = field(default_factory=lambda: ["COMMUNICATION.md"])
    session: str | None = None  # resumed conversation id
    started: bool = False

    @property
    def role(self) -> str:
        return "ARCHITECT" if self.architect else "BUILDER (Main)" if self.primary else "BUILDER"

    @property
    def label(self) -> str:
        return f"{self.name}({self.model or self.cli + '-default'})"

    def appended(self) -> str:
        parts = []
        for item in self.append_system_prompt:
            p = PROMPTS / item
            parts.append(p.read_text() if p.exists() else item)
        return "\n\n".join(parts)


DEFAULT_STACK = [
    Slot("claude", "claude", thinking="high", architect=True, color="#A78BFA"),
    Slot("codex", "codex", thinking="high", primary=True, color="#F59E0B"),
]


def load_stack(path: str | None) -> list[Slot]:
    if not path:
        return [Slot(**{k: v for k, v in s.__dict__.items() if k not in ("session", "started")}) for s in DEFAULT_STACK]
    raw = json.loads(Path(path).read_text())
    base = Path(path).parent
    slots = []
    for item in raw:
        asp = item.get("append_system_prompt", ["COMMUNICATION.md"])
        asp = [asp] if isinstance(asp, str) else asp
        item["append_system_prompt"] = [str(base / a) if (base / a).exists() else a for a in asp]
        slots.append(Slot(**item))
    if sum(s.architect for s in slots) != 1 or sum(s.primary and not s.architect for s in slots) != 1:
        raise SystemExit("stack needs exactly one architect and exactly one non-architect primary")
    if not 2 <= len(slots) <= 5 or len({s.name for s in slots}) != len(slots):
        raise SystemExit("stack needs 2-5 slots with unique names")
    return slots


def roster(stack: list[Slot]) -> str:
    return "\n".join(f"- [{s.name.upper()}] {s.role} · {s.cli}/{s.model or 'default'} · thinking={s.thinking}" for s in stack)


# ── runner ────────────────────────────────────────────────────────────────


@dataclass
class Result:
    slot: Slot
    text: str
    ok: bool
    seconds: float
    session: str | None = None


def run_slot(slot: Slot, prompt: str, cwd: str, write: bool, fresh: bool = False,
             system: str = "") -> Result:
    """One headless turn. fresh=True uses a throwaway session (the FUSION agent)."""
    t0 = time.time()
    if not shutil.which(slot.cli):
        return Result(slot, f"{slot.cli} CLI not found on PATH", False, 0.0)
    try:
        if slot.cli == "claude":
            text, ok, sid = _claude(slot, prompt, cwd, write, fresh, system)
        else:
            text, ok, sid = _codex(slot, prompt, cwd, write, fresh, system)
    except subprocess.TimeoutExpired:
        return Result(slot, f"timed out after {TIMEOUT}s", False, time.time() - t0)
    if ok and not fresh:
        slot.session, slot.started = sid or slot.session, True
    return Result(slot, text, ok, time.time() - t0, sid)


def _claude(slot, prompt, cwd, write, fresh, system):
    cmd = ["claude", "-p", "--output-format", "json"]
    if slot.model:
        cmd += ["--model", slot.model]
    appended = "\n\n".join(x for x in (system, slot.appended()) if x)
    if appended:
        cmd += ["--append-system-prompt", appended]
    if write:
        cmd += ["--permission-mode", "acceptEdits", "--allowedTools", WRITE_TOOLS]
    else:
        cmd += ["--allowedTools", READ_ONLY_TOOLS,
                "--disallowedTools", "Edit Write MultiEdit NotebookEdit Bash"]
    if not fresh and slot.session and slot.started:
        cmd += ["--resume", slot.session]
    elif not fresh:
        slot.session = str(uuid.uuid4())
        cmd += ["--session-id", slot.session]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd, timeout=TIMEOUT)
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
        ok = not data.get("is_error") and r.returncode == 0
        return data.get("result", "").strip() or "(empty answer)", ok, data.get("session_id")
    except (json.JSONDecodeError, IndexError):
        return f"claude exit {r.returncode}: {(r.stderr or r.stdout).strip()[-800:]}", False, None


def _codex(slot, prompt, cwd, write, fresh, system):
    # Codex exec has no append-system-prompt flag, so the contract rides on the
    # first turn of each session (same as the Mac's self-compact launchers).
    resume = not fresh and slot.session and slot.started
    if not resume:
        head = "\n\n".join(x for x in (system, slot.appended()) if x)
        if head:
            prompt = f"<system_instructions>\n{head}\n</system_instructions>\n\n{prompt}"
    out = CACHE / f"codex-last-{uuid.uuid4().hex}.txt"
    common = ["--skip-git-repo-check", "--json", "--output-last-message", str(out)]
    if slot.model:
        common += ["--model", slot.model]
    if slot.thinking:
        common += ["-c", f"model_reasoning_effort={slot.thinking}"]
    if resume:
        cmd = ["codex", "exec", *common, "resume", slot.session, "-"]
    else:
        cmd = ["codex", "exec", *common, "--sandbox", "workspace-write" if write else "read-only", "-"]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd, timeout=TIMEOUT)
        text = out.read_text().strip() if out.exists() else ""
    finally:
        out.unlink(missing_ok=True)
    sid = None
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = ev.get("thread_id") or ev.get("session_id") or sid
    if r.returncode != 0 or not text:
        return f"codex exit {r.returncode}: {(r.stderr or r.stdout).strip()[-800:]}", False, sid
    return text, True, sid


def fan_out(jobs: list[tuple[Slot, str]], cwd: str, label: str) -> list[Result]:
    with Spinner(label) as sp, cf.ThreadPoolExecutor(len(jobs)) as ex:
        for s, _ in jobs:
            sp.set(s.name, "…")

        def one(slot, prompt):
            res = run_slot(slot, prompt, cwd, write=False)
            sp.set(slot.name, "✓" if res.ok else "✗")
            return res

        return list(ex.map(lambda j: one(*j), jobs))


def stat(r: Result) -> str:
    return f"{'✓' if r.ok else '✗'} {r.slot.role} | {r.slot.label} | {r.seconds:.0f}s"


# ── harness ───────────────────────────────────────────────────────────────


class Harness:
    def __init__(self, stack: list[Slot], cwd: str, read_only: bool):
        self.stack, self.cwd, self.read_only = stack, cwd, read_only
        self.bar = False
        self.armed: Slot | None = None
        CACHE.mkdir(parents=True, exist_ok=True)

    @property
    def main(self) -> Slot:
        return next(s for s in self.stack if s.primary and not s.architect)

    @property
    def architect(self) -> Slot:
        return next(s for s in self.stack if s.architect)

    def slot(self, name: str) -> Slot | None:
        return next((s for s in self.stack if s.name == name.lower()), None)

    def run_dir(self, command: str) -> Path:
        d = CACHE / "runs" / f"{command}-{datetime.now():%Y%m%dT%H%M%S}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "stack.json").write_text(json.dumps(
            [{"name": s.name, "cli": s.cli, "model": s.model, "role": s.role} for s in self.stack], indent=2))
        return d

    def write_ok(self) -> bool:
        return not self.read_only

    # raw chat → Main builder (or the armed /fh-only slot)
    def chat(self, text: str):
        slot = self.armed or self.main
        self.armed = None
        with Spinner(f"{slot.name} thinking"):
            r = run_slot(slot, text, self.cwd, write=self.write_ok())
        panel(slot, stat(r), r.text)

    def cmd_opinion(self, prompt: str):
        header("/fh-opinion", f"prompt: {prompt[:100]}")
        d = self.run_dir("fh-opinion")
        jobs = [(s, fill("USER_PROMPT_OPINION.md", SLOT_NAME=s.name, MODEL=s.label, ROSTER=roster(self.stack), PROMPT=prompt))
                for s in self.stack]
        results = fan_out(jobs, self.cwd, "all agents answering read-only")
        for r in results:
            (d / f"{r.slot.name}.md").write_text(r.text)
        grid([(r.slot, stat(r), r.text) for r in results])
        print(dim(f"  artifacts: {d}"))

    def cmd_fusion(self, prompt: str, instruction: str):
        header("/fh-fusion", f"prompt: {prompt[:100]}")
        instruction = instruction or prompt_file("USER_PROMPT_FUSION_DEFAULT_INSTRUCTION.md").strip()
        d = self.run_dir("fh-fusion")
        run_id = d.name
        jobs = [(s, fill("USER_PROMPT_FUSION_WORKER.md", SLOT_NAME=s.name, MODEL=s.label, ROSTER=roster(self.stack), PROMPT=prompt))
                for s in self.stack]
        results = fan_out(jobs, self.cwd, "workers researching read-only")
        grid([(r.slot, stat(r), r.text) for r in results])

        ok = [r for r in results if r.ok]
        per = max(2_000, HANDOFF_MAX // max(1, len(ok)))
        manifest = []
        for r in results:
            path = d / "sources" / f"{r.slot.name}.md"
            path.parent.mkdir(exist_ok=True)
            path.write_text(r.text)
            manifest.append({"slot": r.slot.name, "model": r.slot.label, "status": "ok" if r.ok else "failed",
                             "artifact": str(path), "excerpt": r.text[:per]})
        (d / "manifest.json").write_text(json.dumps(manifest, indent=2))
        if not ok:
            print(fg(RED, "✗ every source failed; nothing to fuse"))
            return
        source_block = "\n\n".join(
            f"<<<SOURCE [{m['slot'].upper()}] {m['model']} status={m['status']} artifact={m['artifact']}>>>\n{m['excerpt']}\n<<<END SOURCE>>>"
            for m in manifest)
        fuser = self.architect
        merge = fill("USER_PROMPT_FUSION_MERGE.md", MODEL=fuser.label, THINKING=fuser.thinking,
                     SOURCE_COUNT=len(ok), PROMPT=prompt, FUSION_INSTRUCTION=instruction,
                     ARTIFACTS_DIR=d, MANIFEST_PATH=d / "manifest.json", SOURCE_MANIFEST=source_block)
        with Spinner(f"FUSION agent ({fuser.name}, fresh session, sole writer={self.write_ok()})"):
            fused = run_slot(fuser, merge, self.cwd, write=self.write_ok(), fresh=True,
                             system=prompt_file("SYSTEM_PROMPT_FUSION.md"))
        (d / "fused.md").write_text(fused.text)
        srcs = fg(LABEL, " ⊕ ").join(fg(r.slot.color, r.slot.label) for r in results)
        print(fg(GREEN, "⧉ FUSED", bold=True) + dim(" ← ") + srcs + dim(f"   {stat(fused)}"))
        panel(None, "FUSED", fused.text, GREEN if fused.ok else RED)
        print(dim(f"  fused by {fuser.role} model {fuser.label} (fresh session)"))
        if fused.ok:
            self._context_sync(run_id, fused.text, d)
        print(dim(f"  artifacts: {d}"))

    def _context_sync(self, run_id: str, fused: str, d: Path):
        h = hashlib.sha256(fused.encode()).hexdigest()
        ack = fill("USER_PROMPT_FUSION_CONTEXT_ACK.md", RUN_ID=run_id, FUSED_HASH=h, FUSED_RESULT=fused)
        results = fan_out([(s, ack) for s in self.stack], self.cwd, "context sync")
        want = f"ACK FUSION {run_id}"
        acks = [{"slot": r.slot.name, "status": "acknowledged" if want in r.text else "missing", "reply": r.text[:200]}
                for r in results]
        (d / "context-sync.json").write_text(json.dumps(acks, indent=2))
        if all(a["status"] == "acknowledged" for a in acks):
            print(fg(GREEN, "✓ CONTEXT SYNC — all agents acknowledged", bold=True))
        else:
            print(fg(AMBER, "⚠ CONTEXT SYNC INCOMPLETE", bold=True) + dim(
                "  " + ", ".join(f"{a['slot']}={a['status']}" for a in acks)))

    def cmd_debate(self, prompt: str, rounds: int):
        header("/fh-debate", f"{rounds} rounds · prompt: {prompt[:90]}")
        d = self.run_dir("fh-debate")
        prev: dict[str, Result] = {}
        for rnd in range(1, rounds + 1):
            jobs = []
            for s in self.stack:
                if rnd > 1 and not prev.get(s.name, Result(s, "", False, 0)).ok:
                    continue  # failed agents drop out
                if rnd == 1:
                    p = fill("USER_PROMPT_DEBATE_OPENING.md", SLOT_NAME=s.name, MODEL=s.label,
                             ROSTER=roster(self.stack), ROUNDS=rounds, PROMPT=prompt)
                else:
                    others = "\n\n".join(
                        f"<<<OPINION [{r.slot.name.upper()}] {r.slot.label} round {rnd - 1}>>>\n{r.text}\n<<<END OPINION>>>"
                        for n, r in prev.items() if n != s.name and r.ok)
                    name = "USER_PROMPT_DEBATE_CLOSING.md" if rnd == rounds else "USER_PROMPT_DEBATE_REBUTTAL.md"
                    left = rounds - rnd
                    p = fill(name, SLOT_NAME=s.name, MODEL=s.label, ROUND=rnd, ROUNDS=rounds, PREV_ROUND=rnd - 1,
                             PROMPT=prompt, OTHER_OPINIONS=others[:HANDOFF_MAX],
                             ROUNDS_LEFT=f"{left} round{'' if left == 1 else 's'} remain after this one, then every surviving agent gives a closing opinion.")
                jobs.append((s, p))
            if not jobs:
                break
            results = fan_out(jobs, self.cwd, f"round {rnd}/{rounds}")
            for r in results:
                (d / f"round-{rnd}").mkdir(exist_ok=True)
                (d / f"round-{rnd}" / f"{r.slot.name}.md").write_text(r.text)
            prev = {r.slot.name: r for r in results}
            if rnd == rounds:
                srcs = dim(" ⚔ ").join(fg(r.slot.color, r.slot.label) for r in results)
                print(fg(LABEL, "⚔ CLOSING STATEMENTS", bold=True) + dim(" · ") + srcs +
                      dim(f"   after {rounds} round{'s' if rounds != 1 else ''} of cross-examination"))
                print(dim("  no judge — every closing opinion survived the debate; compare coalitions, concessions, and remaining disagreements"))
            else:
                print(fg(LABEL, f"round {rnd}/{rounds}", bold=True))
            grid([(r.slot, stat(r), r.text) for r in results])
        print(dim(f"  artifacts: {d}"))

    def cmd_collaborate(self, prompt: str):
        header("/fh-collaborate", f"prompt: {prompt[:100]}")
        d = self.run_dir("fh-collaborate")
        (d / "proposals").mkdir()
        (d / "reports").mkdir()
        jobs = [(s, fill("USER_PROMPT_COLLAB_PROPOSE.md", SLOT_NAME=s.name, MODEL=s.label, ROSTER=roster(self.stack), PROMPT=prompt))
                for s in self.stack]
        proposals = fan_out(jobs, self.cwd, "independent proposals (read-only)")
        for r in proposals:
            (d / "proposals" / f"{r.slot.name}.md").write_text(r.text)
        grid([(r.slot, "PROPOSAL · " + stat(r), r.text) for r in proposals])

        arch = self.architect
        coord = prompt_file("SYSTEM_PROMPT_COLLAB_COORDINATOR.md")
        plan_path = d / "plan.json"
        delegate = fill("USER_PROMPT_COLLAB_DELEGATE.md", COLLAB_DIR=d, ASSIGNEE_IDS=", ".join(s.name for s in self.stack),
                        PLAN_PATH=plan_path, ROSTER=roster(self.stack), PROMPT=prompt)
        with Spinner(f"architect ({arch.name}) merging plans"):
            r = run_slot(arch, delegate, self.cwd, write=False, system=coord)
        tasks = parse_plan(r.text, {s.name for s in self.stack})
        if not tasks:
            panel(arch, "✗ ARCHITECT PLAN INVALID", r.text, RED)
            return
        plan_path.write_text(json.dumps({"tasks": tasks}, indent=2))
        panel(arch, "◆ ARCHITECT · delegation plan", "\n".join(
            f"{t['id']:>5} [{t['assignee']}] {t['mode']:5} ← {','.join(t['depends_on']) or '-'}  {t['description']}" for t in tasks))

        done: dict[str, str] = {}
        pending = list(tasks)
        while pending:
            ready = [t for t in pending if all(dep in done for dep in t["depends_on"])]
            if not ready:
                print(fg(RED, "✗ dependency deadlock; stopping"))
                return
            # every ready read task runs in parallel; at most one write task (single writer token)
            batch = [t for t in ready if t["mode"] == "read"]
            writes = [t for t in ready if t["mode"] == "write"]
            if writes:
                batch.append(writes[0])
            # a slot runs its own tasks one at a time
            seen, wave = set(), []
            for t in batch:
                if t["assignee"] not in seen:
                    seen.add(t["assignee"])
                    wave.append(t)
            with Spinner("tasks " + " ".join(f"{t['id']}:{t['assignee']}" for t in wave)), cf.ThreadPoolExecutor(len(wave)) as ex:
                futs = {ex.submit(self._execute, t, done, prompt, d): t for t in wave}
                for f in cf.as_completed(futs):
                    t = futs[f]
                    res = f.result()
                    done[t["id"]] = res.text
                    pending.remove(t)
                    (d / "reports" / f"{t['id']}-{t['assignee']}.md").write_text(res.text)
                    panel(res.slot, f"TASK {t['id']} · {t['mode']} · {stat(res)}", res.text)

        final = fill("USER_PROMPT_COLLAB_COORDINATE.md", REPORTS_DIR=d / "reports", PLAN_PATH=plan_path, PROMPT=prompt)
        with Spinner(f"architect ({arch.name}) final integration, sole writer={self.write_ok()}"):
            r = run_slot(arch, final, self.cwd, write=self.write_ok(), system=coord)
        (d / "final.md").write_text(r.text)
        panel(arch, "◆ ARCHITECT · integration · " + stat(r), r.text)
        print(dim(f"  artifacts: {d}"))

    def _execute(self, task: dict, done: dict, prompt: str, d: Path) -> Result:
        slot = self.slot(task["assignee"])
        write = task["mode"] == "write" and self.write_ok()
        handoff = "\n\n".join(f"<<<REPORT {dep}>>>\n{done[dep]}\n<<<END REPORT>>>" for dep in task["depends_on"])
        contract = ("WRITE TASK: you hold the harness's sole writer token. Full tools are enabled, and no other writer is active."
                    if write else "READ-ONLY TASK: use read/grep/find/ls only; do not mutate the project.")
        p = fill("USER_PROMPT_COLLAB_EXECUTE.md", SLOT_NAME=slot.name, MODEL=slot.label, TASK_ID=task["id"],
                 TASK_DESCRIPTION=task["description"], TASK_OUTPUTS="\n".join(task.get("outputs", [])) or "-",
                 HANDOFF=(handoff or "No upstream reports; inspect the current project state.")[:HANDOFF_MAX],
                 MODE_CONTRACT=contract, PROMPT=prompt)
        return run_slot(slot, p, self.cwd, write=write)

    # ── small commands ──

    def cmd_fh(self, arg: str):
        if arg in ("on", "off"):
            self.bar = arg == "on"
        print(fg(LABEL, "FUSION HARNESS · commands", bold=True))
        for c, h in COMMANDS:
            print(f"  {fg(LABEL, c.ljust(40))} {dim(h)}")
        print(dim(f"  aliases (expanded by the models): scr eli foc ref · model bar {'on' if self.bar else 'off'}"))

    def print_bar(self):
        for s in self.stack:
            mark = "online" if shutil.which(s.cli) else "missing"
            print(fg(s.color, f"  {'◆' if s.architect else '●'} {s.role:<15}| {s.label:<28}| thinking={s.thinking:<7}| {mark}"))
        print(dim(f"  cwd={self.cwd} · writes={'off' if self.read_only else 'single-writer'}"))

    def cmd_only(self, arg: str):
        name, _, prompt = arg.partition(" ")
        slot = self.slot(name) if name else None
        if not slot:
            print(fg(RED, f"slots: {', '.join(s.name for s in self.stack)}"))
            return
        if prompt.strip():
            with Spinner(f"{slot.name} thinking"):
                r = run_slot(slot, prompt, self.cwd, write=self.write_ok())
            panel(slot, stat(r), r.text)
        elif self.armed is slot:
            self.armed = None
            print(dim(f"  disarmed {slot.name}"))
        else:
            self.armed = slot
            print(dim(f"  next plain input goes to {slot.name} once"))

    def cmd_model(self):
        for i, s in enumerate(self.stack, 1):
            print(fg(s.color, f"  {i}. {s.name} ({s.label})"))
        try:
            s = self.stack[int(input("  slot #: ")) - 1]
            model = input(f"  model for {s.name} [{s.model or 'default'}] (blank keeps): ").strip()
            thinking = input(f"  thinking [{s.thinking}] (low|medium|high|xhigh, blank keeps): ").strip()
        except (ValueError, IndexError, EOFError):
            print(dim("  unchanged"))
            return
        if model:
            s.model, s.session, s.started = model, None, False
        if thinking:
            s.thinking = thinking
        print(fg(GREEN, f"  {s.name} → {s.label} thinking={s.thinking} (session only)"))

    def cmd_system_prompt(self):
        header("/fh-system-prompt — what each role runs with")
        grid([(s, f"{s.role} · {s.label}", (
            "base: Claude Code default system prompt" if s.cli == "claude" else "base: Codex default instructions (appended text rides on the first turn)")
            + "\n\n" + s.appended()) for s in self.stack])

    def cmd_reset(self):
        for s in self.stack:
            s.session, s.started = None, False
        self.armed = None
        print(fg(GREEN, "  fresh sessions for every slot"))


COMMANDS = [
    ("/fh [on|off]", "command index; on/off toggles the model bar above the prompt"),
    ("/fh-opinion <prompt>", "every slot answers independently, read-only"),
    ('/fh-fusion "<prompt>" "<instruction>"', "read-only workers → one fresh FUSION agent writes → every slot ACKs"),
    ("/fh-debate [--rounds N] <prompt>", "N-round read-only debate, no judge (default 3)"),
    ("/fh-collaborate <prompt>", "proposals → architect DAG → tasks with one writer at a time → integration"),
    ("/fh-only [slot] [prompt]", "talk to one slot; without a prompt arms the next input"),
    ("/fh-model", "slot → model → thinking picker (session only)"),
    ("/fh-system-prompt", "every slot's effective appended system prompt"),
    ("/fh-reset", "fresh sessions for every slot"),
    ("/quit", "leave"),
]


def parse_plan(text: str, names: set[str]) -> list[dict] | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        tasks = json.loads(m.group(0))["tasks"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    ids = {t.get("id") for t in tasks}
    for t in tasks:
        t.setdefault("depends_on", [])
        t.setdefault("outputs", [])
        t["assignee"] = str(t.get("assignee", "")).lower()
        if t["assignee"] not in names or t.get("mode") not in ("read", "write") or not set(t["depends_on"]) <= ids:
            return None
    return tasks


def split_quoted(arg: str) -> list[str]:
    try:
        return shlex.split(arg)
    except ValueError:
        return [arg]


def dispatch(h: Harness, line: str) -> bool:
    cmd, _, arg = line.partition(" ")
    arg = arg.strip()
    if cmd in ("/quit", "/exit", "/q"):
        return False
    if cmd == "/fh":
        h.cmd_fh(arg)
    elif cmd == "/fh-opinion" and arg:
        h.cmd_opinion(arg)
    elif cmd == "/fh-fusion" and arg:
        parts = split_quoted(arg)
        h.cmd_fusion(parts[0] if len(parts) > 1 else arg, parts[1] if len(parts) > 1 else "")
    elif cmd == "/fh-debate" and arg:
        m = re.match(r"--rounds\s+(\d+)\s+(.*)", arg, re.S)
        h.cmd_debate(m.group(2) if m else arg, max(1, int(m.group(1))) if m else 3)
    elif cmd == "/fh-collaborate" and arg:
        h.cmd_collaborate(arg)
    elif cmd == "/fh-only":
        h.cmd_only(arg)
    elif cmd == "/fh-model":
        h.cmd_model()
    elif cmd == "/fh-system-prompt":
        h.cmd_system_prompt()
    elif cmd == "/fh-reset":
        h.cmd_reset()
    else:
        h.cmd_fh("")
    return True


BANNER = r"""
   ___ _                 _        __  __   ___          _
  / __| |__ _ _  _ __| |___  \ \/ /  / __|___ __| |_____ __
 | (__| / _` | || / _` / -_)  >  <  | (__/ _ \/ _` / -_) \ /
  \___|_\__,_|\_,_\__,_\___| /_/\_\  \___\___/\__,_\___/_\_\
          F U S I O N   H A R N E S S  ·  AND, not OR
"""


def main() -> int:
    p = argparse.ArgumentParser(prog="claude-codex-fusion", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("line", nargs="*", help="one-shot input, e.g. '/fh-opinion why is this slow'")
    p.add_argument("--fh-config", help="JSON stack file (2-5 slots); default is claude architect + codex main")
    p.add_argument("--read-only", action="store_true", help="no slot may write, including the FUSION agent")
    p.add_argument("--cwd", default=os.getcwd())
    args = p.parse_args()

    h = Harness(load_stack(args.fh_config), args.cwd, args.read_only)
    if args.line:
        line = " ".join(args.line)
        dispatch(h, line) if line.startswith("/") else h.chat(line)
        return 0

    paint_background(True)
    try:
        print(fg(LABEL, BANNER))
        h.print_bar()
        print(dim("  raw chat → Main builder only · /fh for commands · /quit to leave\n"))
        try:
            import readline  # noqa: F401
        except ImportError:
            pass
        while True:
            if h.bar:
                h.print_bar()
            target = h.armed.name if h.armed else "main"
            try:
                line = input(fg(LABEL, f"fusion[{target}]› ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            try:
                if line.startswith("/"):
                    if not dispatch(h, line):
                        break
                else:
                    h.chat(line)
            except KeyboardInterrupt:
                print(fg(RED, "\n  interrupted"))
    finally:
        paint_background(False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
