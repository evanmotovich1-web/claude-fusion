"""Hermes-style tool motion for every fusion agent.

While a tool runs, the lane redraws a braille spinner and an elapsed time,
the same frames Hermes uses. When the tool finishes, that line settles to a
check or a cross and stops moving. FUSION_NO_MOTION=1 keeps a still bullet.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

# Hermes KawaiiSpinner 'dots' frames (agent/display.py).
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
MARK = "\x1eFTOOL:"
clock = time.time


@dataclass
class ToolUse:
    id: str
    name: str
    preview: str
    started: float
    state: str = "run"  # run | done | fail


@dataclass
class Board:
    uses: dict[str, ToolUse] = field(default_factory=dict)

    def clear(self) -> None:
        self.uses.clear()

    def begin(self, tid: str, name: str, preview: str) -> tuple[bool, str]:
        """Start or update a call. The bool is True only the first time, so a
        streamed tool_use that arrives twice is counted once."""
        tid = tid or f"t{len(self.uses)}"
        name = (name or "tool").strip() or "tool"
        preview = " ".join((preview or "").split())
        existing = self.uses.get(tid)
        if existing and existing.state == "run":
            if preview:
                existing.preview = preview
            if name and name != "tool":
                existing.name = name
            return False, marker(tid)
        self.uses[tid] = ToolUse(tid, name, preview, clock())
        return True, marker(tid)

    def finish(self, tid: str, ok: bool = True) -> tuple[str, str] | None:
        use = self.uses.get(tid)
        if not use or use.state != "run":
            return None
        use.state = "done" if ok else "fail"
        return marker(tid), paint_use(use, clock())

    def settle_open(self, ok: bool = True) -> list[tuple[str, str]]:
        out = []
        for tid, use in list(self.uses.items()):
            if use.state != "run":
                continue
            use.state = "done" if ok else "fail"
            out.append((marker(tid), paint_use(use, clock())))
        return out

    def paint(self, line: str, now: float | None = None) -> str:
        if not line.startswith(MARK):
            return line
        use = self.uses.get(line[len(MARK):])
        if not use:
            return ""
        return paint_use(use, clock() if now is None else now)


def marker(tid: str) -> str:
    return f"{MARK}{tid}"


def motion_on() -> bool:
    return os.environ.get("FUSION_NO_MOTION") != "1"


def glyph(state: str, now: float) -> str:
    if state == "done":
        return "✓"
    if state == "fail":
        return "✗"
    if not motion_on():
        return "●"
    return SPIN[int(now * 8) % len(SPIN)]


def paint_use(use: ToolUse, now: float) -> str:
    elapsed = max(0.0, now - use.started)
    g = glyph(use.state, now)
    detail = f"  {use.preview}" if use.preview else ""
    body = f"┊ {g} {use.name}{detail}  ({elapsed:.1f}s)"
    if use.state == "run":
        return _fg("#FDE047", body)
    if use.state == "fail":
        return _fg("#F87171", body)
    return _fg("#4ADE80", body)


def _fg(hex_color: str, text: str) -> str:
    if os.environ.get("NO_COLOR"):
        return text
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return f"\033[38;2;{r};{g};{b}m{text}\033[0m"


def note_claude_tool(slot, part: dict, short) -> None:
    tid = str(part.get("id") or "")
    name = str(part.get("name") or "tool")
    if name.startswith("mcp__"):
        name = ".".join(name[5:].split("__", 1))
    slot.show_tool(tid, name, short(part.get("input") or {}))


def note_claude_user(slot, ev: dict) -> None:
    content = (ev.get("message") or {}).get("content") or []
    if isinstance(content, str):
        return
    for part in content:
        if isinstance(part, dict) and part.get("type") == "tool_result":
            slot.finish_tool(str(part.get("tool_use_id") or ""), ok=not part.get("is_error"))


def note_codex_item(slot, ev: dict, short) -> None:
    item = ev.get("item") or {}
    kind = item.get("type") or item.get("item_type") or ""
    event = ev.get("type") or ""
    if event == "item.started" and kind == "command_execution":
        slot.show_tool(_codex_id(item, kind), "$", str(item.get("command") or "")[:100])
    elif event == "item.started" and kind == "mcp_tool_call":
        name = f"{item.get('server', '')}.{item.get('tool', '')}".strip(".")
        slot.show_tool(_codex_id(item, kind), name or "mcp", short(item.get("arguments") or {}))
    elif event == "item.started" and kind == "web_search":
        slot.show_tool(_codex_id(item, kind), "web_search", str(item.get("query") or "")[:100])
    elif event == "item.completed" and kind in ("command_execution", "mcp_tool_call", "web_search"):
        ok = _codex_ok(item)
        tid = _codex_id(item, kind)
        if slot.finish_tool(tid, ok=ok) is None and kind == "command_execution":
            slot.show_tool(tid, "$", str(item.get("command") or "")[:100])
            slot.finish_tool(tid, ok=ok)
    elif event == "item.completed" and kind == "file_change":
        paths = ", ".join(c.get("path", "") for c in item.get("changes") or [])
        tid = _codex_id(item, kind) or f"edit-{paths}"
        slot.show_tool(tid, "edit", paths)
        slot.finish_tool(tid, ok=True)


def note_pi_event(slot, ev: dict, short) -> None:
    kind = ev.get("type")
    if kind == "tool_execution_start":
        tid = str(ev.get("toolCallId") or ev.get("toolName") or "")
        slot.show_tool(tid, str(ev.get("toolName") or "tool"), short(ev.get("args") or {}))
    elif kind == "tool_execution_end":
        tid = str(ev.get("toolCallId") or ev.get("toolName") or "")
        ok = not ev.get("isError")
        if slot.finish_tool(tid, ok=ok) is None:
            slot.show_tool(tid, str(ev.get("toolName") or "tool"), "")
            slot.finish_tool(tid, ok=ok)


def _codex_id(item: dict, kind: str) -> str:
    if item.get("id"):
        return str(item["id"])
    hint = item.get("command") or item.get("tool") or item.get("query") or ""
    return f"{kind}:{hint}"


def _codex_ok(item: dict) -> bool:
    if item.get("status") in ("failed", "error"):
        return False
    code = item.get("exit_code")
    return code in (None, 0)
