import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from flexbench.__main__ import execute, read
from flexbench.progress import BrowserProgress, GigaCodeActivity


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_tail_handles_partial_lines_without_counting_events_twice(self):
        path = self.root / "events.jsonl"
        tail = GigaCodeActivity(path)
        self.assertEqual(tail.poll()["events"], 0)
        event = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "run_shell_command",
                            "input": {"command": "PRIVATE_COMMAND"}}]}}).encode()
        path.write_bytes(b"not json\n" + event[:20])
        self.assertEqual(tail.poll()["events"], 0)
        with path.open("ab") as stream:
            stream.write(event[20:] + b"\n")
        self.assertEqual(tail.poll()["tool_calls"], 1)
        self.assertEqual(tail.poll()["tool_calls"], 1)
        self.assertNotIn("PRIVATE_COMMAND", json.dumps(tail.poll()))

    def test_browser_page_escapes_values_and_finishes_with_report_links(self):
        plan = [{"model": '<script>alert("x")</script>', "case": "case-a", "repeat": 1},
                {"model": "model-b", "case": "case-a", "repeat": 1}]
        progress = BrowserProgress(self.root / "progress", plan)
        progress.start(0)
        progress.phase("executing")
        progress.tick(2, {"activity": "tool <unsafe>", "tool_calls": 1})
        html = (progress.folder / "index.html").read_text()
        self.assertIn('http-equiv="refresh"', html)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('tool &lt;unsafe&gt;', html)
        progress.finish_attempt({"status": "executor_timeout", "executor": {"seconds": 2}})
        state = read(progress.folder / "progress.json")
        self.assertEqual(state["failures"], 1)
        self.assertEqual(state["completed"], 1)
        self.assertEqual(state["attempts"][1]["phase"], "pending")
        report = self.root / "report with spaces.md"
        report.write_text("report")
        progress.finish(reports=[("Report", report)])
        html = (progress.folder / "index.html").read_text()
        self.assertNotIn('http-equiv="refresh"', html)
        self.assertIn('../report%20with%20spaces.md', html)
        self.assertEqual(read(progress.folder / "progress.json")["state"], "finished")

    def test_browser_opens_only_when_requested(self):
        with patch("flexbench.progress.webbrowser.open") as opener:
            BrowserProgress(self.root / "closed", [])
            opener.assert_not_called()
            progress = BrowserProgress(self.root / "open", [], open_browser=True)
            opener.assert_called_once_with((progress.folder / "index.html").as_uri())

    def test_live_events_reach_browser_before_subprocess_finishes(self):
        progress = BrowserProgress(self.root / "progress", [{"model": "test", "case": "case-a", "repeat": 1}])
        progress.start(0)
        progress.phase("executing")
        path = self.root / "events.jsonl"
        tail = GigaCodeActivity(path)
        observations = []

        def tick(seconds):
            progress.tick(seconds, tail.poll())
            state = read(progress.folder / "progress.json")
            observations.append((seconds, state["attempts"][0].get("tool_calls", 0), state["completed"]))

        event = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "read_file", "input": {"path": "PRIVATE_PATH"}}]}}
        code = "import json,time; print(" + repr(json.dumps(event)) + ", flush=True); time.sleep(1.6)"
        result = execute([sys.executable, "-c", code], self.root, path, 5, on_tick=tick)
        self.assertEqual(result["reason"], "completed")
        self.assertTrue(any(seconds < 1.5 and tools == 1 and completed == 0 for seconds, tools, completed in observations))
        self.assertNotIn("PRIVATE_PATH", (progress.folder / "progress.json").read_text())


if __name__ == "__main__":
    unittest.main()
