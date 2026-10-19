import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from flexbench.__main__ import ROOT, attach_metrics, compare, grade, main, prepare, read, run_executor, save
from flexbench.metrics import collect, efficiency, normalize, validate_pricing
from flexbench.reporting import write_report


PRICING = {"currency": "USD", "source": "test tariff (not a real provider price)",
           "input_per_million": 2, "cached_input_per_million": .5, "output_per_million": 8}
PAYLOAD = {"schema_version": 1, "source": "test executor session export", "complete": True,
           "usage": {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 100}}


class MetricUnitTests(unittest.TestCase):
    def test_cache_is_not_double_counted_and_tariff_is_preserved(self):
        usage, cost = normalize(PAYLOAD, PRICING)
        self.assertEqual(usage["total_tokens"], 1100)
        self.assertAlmostEqual(cost["amount"], .0025)
        self.assertEqual(cost["method"], "estimated")
        self.assertEqual(cost["pricing"], PRICING)

    def test_reported_cost_takes_precedence_and_can_exist_without_tokens(self):
        payload = {**PAYLOAD, "cost": {"amount": .12, "currency": "EUR"}}
        self.assertEqual(normalize(payload, PRICING)[1]["amount"], .12)
        payload.pop("usage")
        usage, cost = normalize(payload, PRICING)
        self.assertIsNone(usage)
        self.assertEqual(cost["method"], "reported")

    def test_missing_cache_is_unknown_and_prevents_discount_estimate(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["usage"].pop("cached_input_tokens")
        usage, cost = normalize(payload, PRICING)
        self.assertIsNone(usage["cached_input_tokens"])
        self.assertIsNone(cost)
        same_rate = {**PRICING, "cached_input_per_million": 2}
        self.assertAlmostEqual(normalize(payload, same_rate)[1]["amount"], .0028)

    def test_invalid_values_rejected(self):
        for key, value in (("input_tokens", -1), ("input_tokens", True), ("output_tokens", 1.5),
                           ("output_tokens", float("nan")), ("cached_input_tokens", 1001),
                           ("total_tokens", 1300)):
            with self.subTest(key=key, value=value):
                payload = copy.deepcopy(PAYLOAD)
                payload["usage"][key] = value
                with self.assertRaises(ValueError):
                    normalize(payload)
        for amount in (-1, True, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                normalize({**PAYLOAD, "cost": {"amount": amount, "currency": "USD"}})
        for patch_value in ({"schema_version": True}, {"complete": None}, {"source": ""},
                            {"usage": []}, {"usage": None}):
            with self.assertRaises(ValueError):
                normalize({**PAYLOAD, **patch_value})
        for patch_value in ({"output_per_million": -1}, {"currency": "usd"}, {"source": ""}):
            with self.assertRaises(ValueError):
                validate_pricing({**PRICING, **patch_value})

    def result(self, status="passed", payload=None):
        usage, cost = normalize(payload or PAYLOAD, PRICING)
        return {"status": status, "usage": usage, "cost": cost, "executor": {"seconds": 2}}

    def test_failed_attempt_cost_is_included_in_cost_per_success(self):
        stats = efficiency([self.result(), self.result("executor_timeout")])
        self.assertEqual(stats["pass_rate"], .5)
        self.assertAlmostEqual(stats["cost_by_currency"]["USD"]["per_successful_attempt"], .005)
        self.assertEqual(stats["tokens"]["total_tokens"]["total"], 2200)

    def test_unknown_partial_and_mixed_currency_do_not_become_full_totals(self):
        first = self.result()
        for second in (
            {"status": "acceptance_failure"},
            self.result(payload={**PAYLOAD, "complete": False}),
            self.result(payload={**PAYLOAD, "cost": {"amount": 1, "currency": "EUR"}}),
        ):
            with self.subTest(second=second):
                stats = efficiency([first, second])
                self.assertIsNone(stats["cost_by_currency"]["USD"]["total"])
                self.assertIsNone(stats["cost_by_currency"]["USD"]["per_successful_attempt"])
                self.assertIsNotNone(stats["cost_by_currency"]["USD"]["observed_total"])
        partial = efficiency([first, self.result(payload={**PAYLOAD, "complete": False})])
        self.assertEqual(partial["tokens"]["input_tokens"]["measured_runs"], 1)
        self.assertEqual(partial["tokens"]["input_tokens"]["available_runs"], 2)
        self.assertIsNone(partial["tokens"]["input_tokens"]["total"])
        self.assertEqual(partial["tokens"]["input_tokens"]["observed_total"], 2000)

    def test_zero_and_no_success_are_distinguished(self):
        zero = {**PAYLOAD, "usage": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}}
        stats = efficiency([self.result("acceptance_failure", zero)])
        self.assertEqual(stats["cost_by_currency"]["USD"]["total"], 0)
        self.assertIsNone(stats["cost_by_currency"]["USD"]["per_successful_attempt"])
        self.assertEqual(stats["tokens"]["total_tokens"]["measured_runs"], 1)


class MetricIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = ROOT / "profiles/gigacode-manual.json"

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["flexbench", *map(str, args)]), redirect_stdout(stdout), redirect_stderr(stderr):
            status = main()
        return status, stdout.getvalue(), stderr.getvalue()

    def command_profile(self, raw=None, finish="", timeout=5):
        code = "from pathlib import Path; import sys; "
        if raw is not None:
            code += "Path(sys.argv[1]).write_text(" + repr(raw) + "); "
        code += finish or "pass"
        path = self.root / "command.json"
        save(path, {"id": "metrics-test", "executor": "test", "model": "test-model", "executor_version": "1",
                    "mode": "command", "command": [sys.executable, "-c", code, "{measurements_file}"],
                    "timeout_seconds": timeout, "pricing": PRICING})
        return path

    def test_command_capture_is_sealed_before_grade(self):
        profile = self.command_profile(json.dumps(PAYLOAD), "raise SystemExit(7)")
        run = prepare("maven-version-alignment", profile, self.root / "runs")
        run_executor(run)
        (run / "measurements.json").write_text("tampered after executor exit")
        result = grade(run)
        self.assertEqual(result["status"], "executor_nonzero_exit")
        self.assertEqual(result["usage"]["input_tokens"], 1000)
        self.assertAlmostEqual(result["cost"]["amount"], .0025)
        self.assertEqual(result["measurement_evidence"]["origin"], "executor_adapter")

    def test_timeout_preserves_partial_telemetry(self):
        payload = {**PAYLOAD, "complete": False}
        profile = self.command_profile(json.dumps(payload), "import time; time.sleep(5)", .3)
        run = prepare("maven-version-alignment", profile, self.root / "runs")
        run_executor(run)
        result = grade(run)
        self.assertEqual(result["status"], "executor_timeout")
        self.assertFalse(result["usage"]["complete"])
        self.assertIn("Cost is partial", result["measurement_issues"])

    def test_missing_and_invalid_metrics_do_not_break_grading(self):
        for raw in (None, "{broken", json.dumps({**PAYLOAD, "cost": {"amount": -1, "currency": "USD"}})):
            profile = self.command_profile(raw)
            run = prepare("maven-version-alignment", profile, self.root / "runs")
            run_executor(run)
            result = grade(run)
            self.assertEqual(result["status"], "acceptance_failure")
            self.assertIsNone(result["usage"])
            self.assertIsNone(result["cost"])
            self.assertTrue(result["measurement_issues"])

    def test_manual_import_is_validated_and_cannot_overwrite_or_edit_result(self):
        run = prepare("maven-version-alignment", self.profile, self.root / "runs")
        source = self.root / "metrics.json"
        source.write_text("bad")
        with self.assertRaises(ValueError):
            attach_metrics(run, source)
        self.assertFalse((run / "measurements.json").exists())
        save(source, {**PAYLOAD, "cost": {"amount": 1.2, "currency": "USD"}})
        self.assertEqual(self.cli("attach-metrics", "--run", run, "--file", source)[0], 0)
        with self.assertRaises(FileExistsError):
            attach_metrics(run, source)
        result = grade(run)
        self.assertEqual(result["cost"]["amount"], 1.2)
        self.assertEqual(result["measurement_evidence"]["origin"], "operator_reported")
        self.assertIsNone(efficiency([result])["executor_seconds"]["total"])
        with self.assertRaisesRegex(ValueError, "sealed"):
            attach_metrics(run, source)

    def test_run_generates_own_report_even_when_tasks_fail_and_output_is_reused(self):
        profile = self.command_profile(json.dumps(PAYLOAD))
        output = self.root / "runs"
        # An earlier, unfinished attempt must not pollute this invocation's report.
        prepare("maven-version-alignment", profile, output)
        status, stdout, stderr = self.cli("run", "--profile", profile, "--case", "maven-version-alignment",
                                          "--repeat", "2", "--output", output)
        self.assertEqual(status, 1, stderr)
        summary = read(next(output.glob("report-*/summary.json")))
        self.assertEqual(summary["kind"], "single")
        self.assertEqual(summary["efficiency"]["current"]["attempts"], 2)
        self.assertEqual(summary["efficiency"]["current"]["tokens"]["input_tokens"]["total"], 2000)
        self.assertIn("[2/2]", stderr)
        self.assertIn("Report:", stdout)
        self.assertEqual(self.cli("report", "--runs", output, "--output", self.root / "incomplete")[0], 2)

    def test_successful_run_returns_zero_and_reports_cost(self):
        fix = "p=Path('pom.xml'); p.write_text(p.read_text().replace('3.2.0','3.2.1')); p=Path('api/pom.xml'); p.write_text(p.read_text().replace('3.2.0','${framework.version}'))"
        profile = self.command_profile(json.dumps(PAYLOAD), fix)
        output = self.root / "success"
        status, _, stderr = self.cli("run", "--profile", profile, "--case", "maven-version-alignment", "--output", output)
        self.assertEqual(status, 0, stderr)
        summary = read(next(output.glob("report-*/summary.json")))
        self.assertAlmostEqual(summary["efficiency"]["current"]["cost_by_currency"]["USD"]["per_successful_attempt"], .0025)

    def test_manual_batch_and_single_report_for_legacy_results(self):
        status, _, stderr = self.cli("run", "--profile", self.profile, "--case", "maven-version-alignment",
                                    "--repeat", "2", "--output", self.root)
        self.assertEqual(status, 0, stderr)
        folder = next(self.root.glob("manual-*"))
        self.assertIn("flexbench report --runs", (folder / "RUNBOOK.md").read_text())
        for run in read(folder / "batch.json")["runs"]:
            result = grade(run)
            # Old v1 results had no measurement metadata and null usage/cost.
            for key in ("measurement_evidence", "measurement_issues"):
                result.pop(key)
            save(Path(run) / "result.json", result)
        status, _, stderr = self.cli("report", "--runs", folder, "--output", folder / "report")
        self.assertEqual(status, 0, stderr)
        summary = read(folder / "report/summary.json")
        self.assertIsNone(summary["efficiency"]["current"]["tokens"]["total_tokens"]["total"])
        self.assertIn("неизвестно", (folder / "report/report.md").read_text())

    def test_selection_and_report_mode_validation(self):
        for cases in (("all", "maven-version-alignment"), ("unknown",), ("maven-version-alignment",) * 2):
            status, _, _ = self.cli("run", "--profile", self.profile, "--case", *cases, "--output", self.root / "unused")
            self.assertEqual(status, 2)
            self.assertFalse((self.root / "unused").exists())
        self.assertEqual(self.cli("report", "--runs", "x", "--baseline", "y", "--output", "z")[0], 2)
        with self.assertRaisesRegex(ValueError, "nonempty"):
            write_report(None, [[]], self.root / "empty")

    def test_comparison_uses_the_same_efficiency_and_keeps_raw_payload_private(self):
        groups = []
        for side, amount in (("baseline", 1.2), ("candidate", .6)):
            run = prepare("maven-version-alignment", self.profile, self.root / side)
            source = self.root / (side + ".json")
            save(source, {**PAYLOAD, "cost": {"amount": amount, "currency": "USD"},
                          "private_raw_log": "PRIVATE_METRIC_LOG"})
            attach_metrics(run, source)
            result = grade(run)
            result["evaluation"]["checks"][0]["detail"] = "PRIVATE_CHECK_LOG"
            groups.append([result])
        comparison = compare(self.root / "baseline", self.root / "candidate")
        folder = write_report(comparison, groups, self.root / "comparison")
        summary = read(folder / "summary.json")
        for index, side in enumerate(("baseline", "candidate")):
            self.assertEqual(summary["efficiency"][side], comparison["efficiency"][index])
        self.assertEqual(summary["efficiency"]["candidate"]["cost_by_currency"]["USD"]["total"], .6)
        self.assertNotIn("PRIVATE_", (folder / "summary.json").read_text())
        self.assertIn("Эффективность: baseline", (folder / "report.md").read_text())

    def test_report_rejects_mixed_profiles_versions_and_duplicate_attempts(self):
        result = grade(prepare("maven-version-alignment", self.profile, self.root / "runs"))
        for change in ("profile", "case", "duplicate"):
            second = copy.deepcopy(result)
            if change != "duplicate":
                second["run_id"] += "-other"
                second[change]["digest"] = "different"
            with self.subTest(change=change), self.assertRaises(ValueError):
                write_report(None, [[result, second]], self.root / change)


if __name__ == "__main__":
    unittest.main()
