"""Subprocess contract tests using a fake CLI; these do not call an LLM."""

import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from flexbench.__main__ import grade, main, prepare, profile_data, read, run_executor, save
from flexbench.reporting import write_report


FINAL = {"type": "result", "subtype": "success", "session_id": "test-session",
         "result": "PRIVATE_AGENT_RESPONSE", "duration_ms": 1400, "duration_api_ms": 1100,
         "usage": {"input_tokens": 1000, "output_tokens": 100,
                   "cache_read_input_tokens": 200, "total_tokens": 1100},
         "stats": {"models": {"test-model": {}}}}
PRICING = {"currency": "USD", "source": "test-only tariff", "input_per_million": 2,
           "output_per_million": 8, "cached_input_per_million": .5}
FIX = """
p = Path('pom.xml')
p.write_text(p.read_text().replace('3.2.0', '3.2.1'))
p = Path('api/pom.xml')
p.write_text(p.read_text().replace('3.2.0', '${framework.version}'))
"""


class GigaCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def profile(self, body=None, **overrides):
        executable = self.root / "fake-gigacode"
        executable.write_text("#!" + sys.executable + "\n" + '''
import argparse
import json
from pathlib import Path
import sys
if sys.argv[1:] == ['--version']:
    print('test-cli-1.0')
    raise SystemExit(0)
parser = argparse.ArgumentParser()
parser.add_argument('--approval-mode')
parser.add_argument('--allowed-tools', nargs='+')
parser.add_argument('--output-format')
parser.add_argument('--model')
parser.add_argument('-p', required=True)
args = parser.parse_args()
assert args.output_format == 'stream-json'
assert args.approval_mode == 'auto-edit'
assert args.allowed_tools == ['run_shell_command']
assert sys.argv[-2] == '-p', 'all options must precede the prompt'
assert sys.stdin.read() == '', 'stdin must be closed in non-interactive mode'
Path('invocation.json').write_text(json.dumps({'argv': sys.argv[1:], 'cwd': str(Path.cwd())}))
print('private stderr diagnostic', file=sys.stderr, flush=True)
''' + (body if body is not None else FIX + "\nprint(" + repr(json.dumps(FINAL)) + ")\n"))
        executable.chmod(0o755)
        path = self.root / "profile.json"
        save(path, {"id": "test-native", "executor": "gigacode", "mode": "command",
                    "adapter": "gigacode", "executable": str(executable), "model": None,
                    "executor_version": None, "timeout_seconds": 5, "instruction_files": [], **overrides})
        return path

    def attempt(self, profile):
        run = prepare("maven-version-alignment", profile, self.root / "runs")
        run_executor(run)
        return run, grade(run)

    def test_native_pipeline_passes_collects_final_usage_and_keeps_logs_separate(self):
        body = FIX + "\nprint('startup banner')\n"
        body += "print(" + repr(json.dumps({"type": "system", "model": "alias", "session_id": "test-session"})) + ")\n"
        body += "print(" + repr(json.dumps({"type": "assistant", "message": {"model": "test-model", "usage": FINAL["usage"]}})) + ")\n"
        body += "print(" + repr(json.dumps(FINAL)) + ")\n"
        run, result = self.attempt(self.profile(body, model="test-model", pricing=PRICING))
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["success_within_time_budget"])
        self.assertEqual(result["profile"]["executor_version"], "test-cli-1.0")
        self.assertEqual(result["usage"]["input_tokens"], 1000)  # no sum with assistant snapshot
        self.assertEqual(result["usage"]["total_tokens"], 1100)
        self.assertTrue(result["usage"]["complete"])
        self.assertAlmostEqual(result["cost"]["amount"], .0025)
        self.assertEqual(result["adapter_info"]["models"], ["test-model"])
        self.assertEqual(result["adapter_info"]["duration_api_ms"], 1100)
        self.assertEqual(result["adapter_info"]["session_id"], "test-session")
        invocation = read(run / "workspace/invocation.json")
        self.assertEqual(invocation["cwd"], str(run / "workspace"))
        self.assertIn("--model", invocation["argv"])
        self.assertTrue(invocation["argv"][-1].endswith((run / "workspace/TASK.md").read_text()))
        self.assertNotIn("private stderr", (run / "executor.jsonl").read_text())
        self.assertIn("private stderr", (run / "executor.log").read_text())
        self.assertEqual({e["file"] for e in result["log_evidence"]}, {"executor.jsonl", "executor.log"})
        output = write_report(None, [[result]], self.root / "report")
        exported = (output / "summary.json").read_text() + (output / "report.md").read_text()
        self.assertNotIn("PRIVATE_AGENT_RESPONSE", exported)
        self.assertNotIn("private stderr", exported)
        self.assertNotIn("org.example.framework", exported)
        self.assertIn("test-model", exported)

    def test_default_model_does_not_pass_a_model_flag(self):
        run, result = self.attempt(self.profile())
        self.assertNotIn("--model", read(run / "workspace/invocation.json")["argv"])
        self.assertIsNone(result["profile"]["model"])
        self.assertEqual(result["adapter_info"]["models"], ["test-model"])

    def test_structured_and_api_errors_cannot_pass_with_exit_zero(self):
        for patch_value, reason in (({"result": "[API Error: 404 Model not found]"}, "api_error"),
                                    ({"is_error": True}, "reported_error"),
                                    ({"subtype": "error_max_turns"}, "reported_error"),
                                    ({"result": "requires user approval but cannot execute in non-interactive mode"}, "approval_unavailable")):
            with self.subTest(patch=patch_value):
                payload = {**FINAL, **patch_value}
                _, result = self.attempt(self.profile(FIX + "\nprint(" + repr(json.dumps(payload)) + ")\n"))
                self.assertEqual(result["status"], "executor_" + reason)
                self.assertTrue(result["functional_pass"])
                self.assertFalse(result["success_within_time_budget"])
                self.assertTrue(result["usage"]["complete"])  # complete failed session still has costs

    def test_nonzero_exit_preserves_usage_and_original_exit_reason(self):
        body = FIX + "\nprint(" + repr(json.dumps(FINAL)) + ")\nraise SystemExit(7)\n"
        _, result = self.attempt(self.profile(body))
        self.assertEqual(result["status"], "executor_nonzero_exit")
        self.assertEqual(result["executor"]["exit_code"], 7)
        self.assertTrue(result["usage"]["complete"])

    def test_missing_duplicate_and_malformed_result_never_pass(self):
        for output, reason in (("print('ordinary text')", "missing_result"),
                               ("print('{broken')", "missing_result"),
                               ("print(" + repr(json.dumps(FINAL)) + ")\n" +
                                "print(" + repr(json.dumps(FINAL)) + ")", "ambiguous_result")):
            with self.subTest(reason=reason):
                _, result = self.attempt(self.profile(FIX + "\n" + output))
                self.assertEqual(result["status"], "executor_" + reason)
                self.assertIsNone(result["usage"])

    def test_timeout_without_final_event_has_unknown_usage(self):
        body = FIX + "\nprint(" + repr(json.dumps({"type": "assistant", "message": {"usage": FINAL["usage"]}})) + ", flush=True)\nimport time; time.sleep(5)"
        _, result = self.attempt(self.profile(body, timeout_seconds=.3))
        self.assertEqual(result["status"], "executor_timeout")
        self.assertIsNone(result["usage"])

    def test_timeout_after_result_preserves_partial_usage(self):
        body = FIX + "\nprint(" + repr(json.dumps(FINAL)) + ", flush=True)\nimport time; time.sleep(5)"
        _, result = self.attempt(self.profile(body, timeout_seconds=.3, pricing=PRICING))
        self.assertEqual(result["status"], "executor_timeout")
        self.assertEqual(result["usage"]["total_tokens"], 1100)
        self.assertFalse(result["usage"]["complete"])
        self.assertFalse(result["cost"]["complete"])

    def test_bad_or_absent_usage_does_not_break_functional_grade(self):
        for usage in (None, {"input_tokens": -1, "output_tokens": 1},
                      {"input_tokens": 1, "output_tokens": 1, "total_tokens": 100}):
            with self.subTest(usage=usage):
                payload = {**FINAL, "usage": usage}
                _, result = self.attempt(self.profile(FIX + "\nprint(" + repr(json.dumps(payload)) + ")"))
                self.assertEqual(result["status"], "passed")
                self.assertIsNone(result["usage"])
                self.assertTrue(result["measurement_issues"])

    def test_unknown_cache_is_not_zero_and_blocks_discount_estimate(self):
        payload = copy.deepcopy(FINAL)
        payload["usage"].pop("cache_read_input_tokens")
        _, result = self.attempt(self.profile(FIX + "\nprint(" + repr(json.dumps(payload)) + ")", pricing=PRICING))
        self.assertIsNone(result["usage"]["cached_input_tokens"])
        self.assertIsNone(result["cost"])

    def test_multiple_or_unexpected_models_do_not_use_single_model_price(self):
        for models in ({"other": {}}, {"test-model": {}, "other": {}}):
            payload = {**FINAL, "stats": {"models": models}}
            _, result = self.attempt(self.profile(FIX + "\nprint(" + repr(json.dumps(payload)) + ")",
                                                 model="test-model", pricing=PRICING))
            self.assertIsNone(result["cost"])
            self.assertEqual(result["usage"]["total_tokens"], 1100)
            self.assertTrue(any("pricing" in issue for issue in result["measurement_issues"]))

    def test_cli_run_prints_report_without_runbook(self):
        profile = self.profile()
        stdout, stderr = io.StringIO(), io.StringIO()
        output = self.root / "auto"
        with patch.object(sys, "argv", ["flexbench", "run", "--profile", str(profile), "--case",
                                         "maven-version-alignment", "--output", str(output)]), redirect_stdout(stdout), redirect_stderr(stderr):
            status = main()
        self.assertEqual(status, 0, stderr.getvalue())
        self.assertIn("Report:", stdout.getvalue())
        self.assertNotIn("RUNBOOK", stdout.getvalue())
        summary = read(next(output.glob("report-*/summary.json")))
        self.assertEqual(summary["efficiency"]["current"]["tokens"]["input_tokens"]["total"], 1000)

    def test_invalid_adapter_configuration_fails_early(self):
        for override in ({"executable": "/not-installed/gigacode"}, {"adapter": "unknown"},
                         {"approval_mode": "plan"}, {"command": ["anything"]},
                         {"allowed_tools": "run_shell_command"}, {"model": ""}, {"mode": "manual"}):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    profile_data(self.profile(**override))


if __name__ == "__main__":
    unittest.main()
