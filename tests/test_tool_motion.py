"""Tool-motion frames for Claude, Codex, and Pi. No model calls."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tool_motion as tm


class FakeSlot:
    def __init__(self):
        self.tools = 0
        self.live = []
        self.motions = tm.Board()

    def show_tool(self, tid, name, preview):
        counted, mark = self.motions.begin(tid, name, preview)
        if counted:
            self.tools += 1
            self.live.append(mark)

    def finish_tool(self, tid, ok=True):
        pair = self.motions.finish(tid, ok=ok)
        if not pair:
            return None
        mark, settled = pair
        for i, line in enumerate(self.live):
            if line == mark:
                self.live[i] = settled
                return settled
        self.live.append(settled)
        return settled

    def painted(self, now=None):
        return [self.motions.paint(line, now) for line in self.live]


class ToolMotionTest(unittest.TestCase):
    def setUp(self):
        self._clock = tm.clock
        self.t = 10.0
        tm.clock = lambda: self.t
        os.environ.pop("FUSION_NO_MOTION", None)
        os.environ.pop("NO_COLOR", None)

    def tearDown(self):
        tm.clock = self._clock

    def test_spinner_advances_then_settles_for_every_cli(self):
        slot = FakeSlot()
        tm.note_claude_tool(slot, {"id": "c1", "name": "Read", "input": {"file_path": "src/api.ts"}}, _short)
        tm.note_codex_item(slot, {"type": "item.started", "item": {"id": "x1", "type": "command_execution", "command": "ls"}}, _short)
        tm.note_pi_event(slot, {"type": "tool_execution_start", "toolCallId": "p1", "toolName": "grep", "args": {"pattern": "slot"}}, _short)
        self.assertEqual(slot.tools, 3)
        first = slot.painted(10.0)
        second = slot.painted(10.2)
        self.assertEqual(len(first), 3)
        self.assertTrue(all("┊" in line for line in first))
        self.assertIn("Read", first[0])
        self.assertIn("src/api.ts", first[0])
        self.assertIn("ls", first[1])
        self.assertIn("grep", first[2])
        self.assertNotEqual(_glyph(first[0]), _glyph(second[0]))
        self.assertIn(tm.SPIN[0], first[0])
        self.assertIn(tm.SPIN[1], second[0])

        tm.note_claude_user(slot, {"message": {"content": [{"type": "tool_result", "tool_use_id": "c1"}]}})
        tm.note_codex_item(slot, {"type": "item.completed", "item": {"id": "x1", "type": "command_execution", "exit_code": 0}}, _short)
        tm.note_pi_event(slot, {"type": "tool_execution_end", "toolCallId": "p1", "isError": True}, _short)
        settled = slot.painted(12.0)
        self.assertIn("✓", settled[0])
        self.assertIn("✓", settled[1])
        self.assertIn("✗", settled[2])
        self.assertNotIn(tm.SPIN[0], settled[0])

    def test_claude_tool_use_is_counted_once(self):
        slot = FakeSlot()
        part = {"id": "c1", "name": "Bash", "input": {}}
        tm.note_claude_tool(slot, part, _short)
        part["input"] = {"command": "pwd"}
        tm.note_claude_tool(slot, part, _short)
        self.assertEqual(slot.tools, 1)
        self.assertIn("pwd", slot.painted(10.0)[0])

    def test_slot_paints_a_moving_tool_line(self):
        import fusion
        s = fusion.Slot("codex", "codex", primary=True)
        s.show_tool("x", "web_search", "hermes spinner")
        first = s.painted_lines()
        self.t = 10.2
        second = s.painted_lines()
        self.assertIn("web_search", first[0])
        self.assertIn(tm.SPIN[0], first[0])
        self.assertIn(tm.SPIN[1], second[0])
        s.finish_tool("x", ok=True)
        self.assertIn("✓", s.painted_lines()[0])

    def test_reduced_motion_holds_a_still_bullet(self):
        os.environ["FUSION_NO_MOTION"] = "1"
        slot = FakeSlot()
        tm.note_pi_event(slot, {"type": "tool_execution_start", "toolCallId": "p", "toolName": "read", "args": {}}, _short)
        a = slot.painted(10.0)[0]
        b = slot.painted(11.0)[0]
        self.assertIn("●", a)
        self.assertIn("●", b)
        self.assertNotIn(tm.SPIN[0], a)


def _short(obj) -> str:
    if isinstance(obj, dict):
        return " ".join(f"{k}={v}" for k, v in list(obj.items())[:2])
    return str(obj)


def _glyph(line: str) -> str:
    for frame in tm.SPIN:
        if frame in line:
            return frame
    return ""


if __name__ == "__main__":
    unittest.main()
