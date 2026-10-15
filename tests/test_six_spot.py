"""The sixth Claude Codex Fusion seat is /fh-stack five, and every command seats it."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion


NAMES = {"opus", "sol", "astra", "grok", "argon", "seek"}


class SixSpotTest(unittest.TestCase):
    def test_fh_stack_five_loads_six_agents(self):
        slots = fusion.load_stack("five")
        self.assertEqual([s.name for s in slots], ["opus", "sol", "astra", "grok", "argon", "seek"])
        self.assertEqual(sum(s.architect for s in slots), 1)
        self.assertEqual(sum(s.primary and not s.architect for s in slots), 1)
        self.assertEqual(slots[-1].model, "deepseek/deepseek-v4-pro")

    def test_seven_agents_are_refused(self):
        raw = json.loads((fusion.STACKS / "five.json").read_text())
        raw.append({"name": "extra", "cli": "pi", "model": "xai/grok-4.7", "thinking": "high", "color": "#FB7185"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seven.json"
            path.write_text(json.dumps(raw))
            with self.assertRaises(SystemExit):
                fusion.load_stack(str(path))

    def test_opinion_debate_fusion_and_collaborate_include_the_sixth(self):
        slots = fusion.load_stack("five")
        vault = fusion.Vault(enabled=False)
        harness = fusion.Harness(slots, tempfile.gettempdir(), True, vault)
        seen = {}

        def capture(h, jobs):
            seen[len(seen)] = [slot.name for slot, _ in jobs]
            return [fusion.Result(slot, "no", False, 0) for slot, _ in jobs]

        with patch.object(fusion, "fan_out", capture), patch.object(fusion, "run_slot", lambda *a, **k: fusion.Result(slots[0], "not json", False, 0)):
            harness.cmd_opinion("what should change")
            opinion = seen[0]
            harness.cmd_debate("what should change", 1)
            debate = seen[1]
            harness.cmd_fusion("what should change", "")
            fusion_jobs = seen[2]
            harness.cmd_collaborate("what should change")
            collab = seen[3]
        for jobs in (opinion, debate, fusion_jobs, collab):
            self.assertEqual(set(jobs), NAMES)
        self.assertIn("most likely to miss", fusion.prompt_file("USER_PROMPT_OPINION.md"))
        self.assertIn("beat the best single source", fusion.prompt_file("USER_PROMPT_FUSION_MERGE.md"))
        self.assertIn("quiet slot", fusion.prompt_file("USER_PROMPT_COLLAB_DELEGATE.md"))


if __name__ == "__main__":
    unittest.main()
