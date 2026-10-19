import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from flexbench.__main__ import ROOT, compare, main, profile_data, read, save
from flexbench.reporting import write_model_report


PRICE = {"currency": "USD", "source": "test tariff", "input_per_million": 2,
         "cached_input_per_million": .5, "output_per_million": 8}


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = self.root / "calls.jsonl"

    def profile(self, **overrides):
        executable = self.root / "fake-gigacode"
        executable.write_text("#!" + sys.executable + "\n" + '''
import argparse, json, sys
from pathlib import Path
if sys.argv[1:] == ['--version']:
    print('test-1')
    raise SystemExit(0)
p=argparse.ArgumentParser()
p.add_argument('--approval-mode')
p.add_argument('--allowed-tools', nargs='+')
p.add_argument('--output-format')
p.add_argument('--model')
p.add_argument('-p')
args=p.parse_args()
''' + "with Path(" + repr(str(self.calls)) + ").open('a') as stream: stream.write(json.dumps(args.model) + '\\n')\n" + '''
src=Path('pom.xml'); src.write_text(src.read_text().replace('3.2.0','3.2.1'))
src=Path('api/pom.xml'); src.write_text(src.read_text().replace('3.2.0','${framework.version}'))
model=args.model or 'default-test-model'
result='[API Error: 404 Model not found]' if model == 'bad-model' else 'PRIVATE_RESPONSE'
print(json.dumps({'type':'result','result':result,'usage':{'input_tokens':1000,'output_tokens':100,
    'cache_read_input_tokens':200,'total_tokens':1100},'stats':{'models':{model:{}}}}))
''')
        executable.chmod(0o755)
        path = self.root / "profile.json"
        save(path, {"id": "batch-test", "executor": "gigacode", "mode": "command", "adapter": "gigacode",
                    "executable": str(executable), "model": None, "timeout_seconds": 5, **overrides})
        return path

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["flexbench", *map(str, args)]), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_models_complete_their_suite_one_at_a_time_in_requested_order(self):
        profile = self.profile(pricing_by_model={"model-a": PRICE, "model-b": {**PRICE, "input_per_million": 4}})
        original = profile.read_bytes()
        output = self.root / "matrix"
        with patch("flexbench.progress.webbrowser.open") as opener:
            code, stdout, stderr = self.cli("run", "--profile", profile, "--models", "model-a", "model-b", "--repeat", "2",
                                            "--case", "maven-version-alignment", "--open-progress", "--output", output)
        self.assertEqual(code, 0, stderr)
        opener.assert_called_once()
        self.assertIn("Progress: file:", stdout)
        matrix = next(output.glob("matrix-*"))
        summary = read(matrix / "report/summary.json")
        self.assertEqual(summary["kind"], "model_matrix")
        self.assertEqual([r["requested_model"] for r in summary["models"]], ["model-a", "model-b"])
        self.assertEqual([json.loads(line) for line in self.calls.read_text().splitlines()], ["model-a", "model-a", "model-b", "model-b"])
        for row in summary["models"]:
            self.assertEqual(row["efficiency"]["successes"], 2)
            self.assertEqual(row["efficiency"]["tokens"]["total_tokens"]["total"], 2200)
            self.assertTrue(row["reported_model_matches"])
            self.assertEqual(len(row["summary"]["metadata"]), 2)
        self.assertAlmostEqual(summary["models"][0]["efficiency"]["cost_by_currency"]["USD"]["total"], .005)
        self.assertAlmostEqual(summary["models"][1]["efficiency"]["cost_by_currency"]["USD"]["total"], .0082)
        progress = read(matrix / "progress/progress.json")
        self.assertEqual([attempt["model"] for attempt in progress["attempts"]], ["model-a", "model-a", "model-b", "model-b"])
        self.assertEqual((progress["completed"], progress["successes"], progress["total"]), (4, 4, 4))
        self.assertEqual(progress["state"], "finished")
        self.assertIn("report.md", progress["reports"][0]["path"])
        self.assertNotIn("PRIVATE_RESPONSE", json.dumps(summary))
        self.assertEqual(profile.read_bytes(), original)
        self.assertEqual(compare(matrix / "model-01", matrix / "model-02")["macro_delta_percentage_points"], 0)

    def test_one_failed_model_does_not_hide_other_model_or_skip_report(self):
        output = self.root / "matrix"
        code, _, stderr = self.cli("run", "--profile", self.profile(), "--models", "bad-model", "good-model",
                                    "--case", "maven-version-alignment", "--output", output)
        self.assertEqual(code, 1, stderr)
        matrix = next(output.glob("matrix-*"))
        summary = read(matrix / "report/summary.json")
        self.assertEqual(summary["models"][0]["efficiency"]["status_counts"], {"executor_api_error": 1})
        self.assertEqual(summary["models"][1]["efficiency"]["successes"], 1)
        state = read(matrix / "progress/progress.json")
        self.assertEqual((state["completed"], state["failures"], state["successes"]), (2, 1, 1))

    def test_tariff_is_not_copied_to_a_different_model(self):
        profile = self.profile(model="model-a", pricing=PRICE)
        self.assertIn("pricing", profile_data(profile, model="model-a"))
        self.assertNotIn("pricing", profile_data(profile, model="model-b"))

    def test_duplicate_models_and_manual_matrix_fail_before_preparation(self):
        output = self.root / "unused"
        code, _, _ = self.cli("run", "--profile", self.profile(), "--models", "model-a", "model-a",
                              "--case", "maven-version-alignment", "--output", output)
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        code, _, _ = self.cli("run", "--profile", ROOT / "profiles/gigacode-manual.json", "--models", "model-a",
                              "--case", "maven-version-alignment", "--output", output)
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())

    def test_interruption_marks_progress_and_preserves_attempt(self):
        output = self.root / "interrupted"
        with patch("flexbench.__main__.run_executor", side_effect=KeyboardInterrupt):
            code, _, _ = self.cli("run", "--profile", self.profile(), "--case", "maven-version-alignment", "--output", output)
        self.assertEqual(code, 130)
        state = read(next(output.glob("progress-*/progress.json")))
        self.assertEqual(state["state"], "interrupted")
        self.assertEqual(state["completed"], 0)
        self.assertEqual(state["attempts"][0]["phase"], "interrupted")
        self.assertTrue(Path(state["attempts"][0]["run"]).is_dir())

    def test_matrix_report_rejects_unmatched_case_versions(self):
        output = self.root / "matrix"
        code, _, stderr = self.cli("run", "--profile", self.profile(), "--models", "model-a", "model-b",
                                    "--case", "maven-version-alignment", "--output", output)
        self.assertEqual(code, 0, stderr)
        matrix = next(output.glob("matrix-*"))
        groups = [[read(next((matrix / side).glob("*/result.json")))] for side in ("model-01", "model-02")]
        bad = copy.deepcopy(groups)
        bad[1][0]["case"]["digest"] = "other"
        with self.assertRaisesRegex(ValueError, "identical"):
            write_model_report(["model-a", "model-b"], bad, self.root / "invalid")
        # Observed model differences must remain visible even when quality passes.
        groups[1][0]["adapter_info"]["models"] = ["unexpected-model"]
        report = write_model_report(["model-a", "model-b"], groups, self.root / "mismatch")
        row = read(report / "summary.json")["models"][1]
        self.assertFalse(row["reported_model_matches"])
        self.assertTrue(row["warnings"])


if __name__ == "__main__":
    unittest.main()
