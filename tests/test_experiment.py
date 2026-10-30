"""Harness experiments: real Harbor, fake CLI, no model requests."""

import argparse
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_harbor as harbor_tests
from flexbench.__main__ import read, save
from flexbench.harbor.experiment import plan, prepare_experiment, run_experiment, write_experiment_report


@unittest.skipIf(harbor_tests.harbor is None, "Install Harbor with Python 3.12+")
class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = harbor_tests.HarborTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        executable = self.fixture.fake()
        self.executable = executable
        # Only the candidate's delivered instruction causes the fake to leave the bug.
        text = executable.read_text().replace("if mode == 'control':", "if 'RULE_CANDIDATE' in a.p:\n    mode = 'incorrect'\nif mode == 'control':")
        executable.write_text(text)
        self.rule = self.root / "rule.md"
        self.rule.write_text("RULE_CANDIDATE")
        self.prefix = self.root / "prefix.md"
        self.prefix.write_text("CUSTOM_PREFIX")
        profile = {"id": "test", "version": 1, "executor": "gigacode", "adapter": "gigacode", "mode": "command",
                   "executable": str(executable), "model": None, "timeout_seconds": 5,
                   "instruction_files": [], "prompt_prefix_file": "prefix.md"}
        save(self.root / "baseline.json", profile)
        save(self.root / "candidate.json", {**profile, "instruction_files": ["rule.md"]})
        self.spec = self.root / "spec.json"
        save(self.spec, {"schema_version": 1, "name": "Rules", "hypothesis": "Measure instruction effect",
                         "baseline": "baseline.json", "candidate": "candidate.json", "model": "model-a",
                         "cases": ["js-retry-state"], "repeat": 2, "seed": 17})
        self.args = argparse.Namespace(spec=str(self.spec), model=None, case=None, repeat=None, output=str(self.root / "output"))

    def run_experiment(self, directory):
        with redirect_stdout(io.StringIO()):
            return asyncio.run(run_experiment(directory))

    def test_frozen_inputs_paired_outcomes_and_report_rebuild(self):
        directory = prepare_experiment(self.args)
        prepared = read(directory / "summary.json")
        self.assertFalse(prepared["complete"])
        self.assertIsNone(prepared["delta_percentage_points"])
        self.assertIsNone(prepared["variants"]["baseline"]["pass_rate"])
        # Original files may change later; jobs must use their frozen text.
        self.rule.write_text("DIFFERENT_RULE")
        self.prefix.write_text("DIFFERENT_PREFIX")
        self.assertEqual(self.run_experiment(directory), 1)
        summary = read(directory / "summary.json")
        self.assertTrue(summary["complete"])
        self.assertTrue(summary["comparison_valid"], summary["evidence_issues"])
        self.assertEqual(summary["delta_percentage_points"], -100)
        self.assertEqual(summary["variants"]["baseline"]["successes"], 2)
        self.assertEqual(summary["variants"]["candidate"]["functional_passes"], 0)
        self.assertTrue(summary["variants"]["candidate"]["failed_checks"])
        self.assertEqual([p["change"] for p in summary["pairs"]], [-1, -1])
        for attempt in summary["attempts"]:
            invocation = read(directory / attempt["evidence_path"] / "agent/invocation.json")
            self.assertTrue(invocation["argv"][-1].startswith("CUSTOM_PREFIX\n\n"))
            self.assertEqual("RULE_CANDIDATE" in invocation["argv"][-1], attempt["variant"] == "candidate")
            self.assertEqual(invocation["model"], "model-a")
        (directory / "summary.json").unlink()
        rebuilt = write_experiment_report(directory)
        self.assertEqual(rebuilt, summary)
        # Broken grader diagnostics must not erase Harbor's saved outcome.
        evidence = directory / summary["attempts"][0]["evidence_path"]
        (evidence / "verifier/grader.json").write_text("{incomplete")
        repaired = write_experiment_report(directory)
        self.assertIn("grading_issue", repaired["attempts"][0])
        self.assertTrue(repaired["complete"])
        self.assertEqual(repaired["delta_percentage_points"], summary["delta_percentage_points"])
        self.assertEqual([a["status"] for a in repaired["attempts"]], [a["status"] for a in summary["attempts"]])
        with self.assertRaisesRegex(ValueError, "already started"):
            self.run_experiment(directory)

    def test_api_failure_keeps_functional_pass_and_cost_coverage(self):
        self.args.repeat = 1
        text = self.executable.read_text().replace("mode = 'incorrect'", "mode = 'api_error'")
        self.executable.write_text(text)
        directory = prepare_experiment(self.args)
        self.assertEqual(self.run_experiment(directory), 1)
        summary = read(directory / "summary.json")
        self.assertTrue(summary["comparison_valid"])
        candidate = summary["variants"]["candidate"]
        self.assertEqual(candidate["successes"], 0)
        self.assertEqual(candidate["functional_passes"], 1)
        self.assertEqual(candidate["execution_reasons"], {"api_error": 1})
        self.assertEqual(candidate["tokens"]["total_tokens"]["total"], 1100)
        self.assertEqual(candidate["cost_measured_runs"], 0)

    def test_model_mismatch_invalidates_comparison_but_preserves_results(self):
        self.args.repeat = 1
        self.executable.write_text(self.executable.read_text().replace("if mode == 'control':", "a.model = 'other-model'\nif mode == 'control':"))
        directory = prepare_experiment(self.args)
        self.assertEqual(self.run_experiment(directory), 2)
        summary = read(directory / "summary.json")
        self.assertTrue(summary["complete"])
        self.assertFalse(summary["comparison_valid"])
        self.assertIsNone(summary["delta_percentage_points"])
        self.assertEqual(summary["variants"]["baseline"]["successes"], 1)

    def test_timeout_retains_grading_and_unknown_usage(self):
        self.args.repeat = 1
        self.executable.write_text(self.executable.read_text().replace("mode = 'incorrect'", "mode = 'timeout'"))
        for side in ("baseline", "candidate"):
            path = self.root / (side + ".json")
            save(path, {**read(path), "timeout_seconds": 1})
        directory = prepare_experiment(self.args)
        self.assertEqual(self.run_experiment(directory), 2)
        summary = read(directory / "summary.json")
        candidate = summary["variants"]["candidate"]
        self.assertEqual(candidate["functional_passes"], 1)
        self.assertEqual(candidate["successes"], 0)
        self.assertEqual(candidate["status_counts"], {"AgentTimeoutError": 1})
        self.assertIsNone(candidate["tokens"]["total_tokens"]["total"])
        self.assertFalse(summary["comparison_valid"])  # CLI never reported its actual model.

    def test_cli_dry_run_and_partial_report_do_not_launch_jobs(self):
        from flexbench.harbor.cli import main
        with redirect_stdout(io.StringIO()), patch("harbor.job.Job.create", side_effect=AssertionError("must not launch")):
            self.assertEqual(main(["experiment", "--spec", str(self.spec), "--repeat", "1",
                                   "--output", self.args.output, "--dry-run"]), 0)
            directory = next(Path(self.args.output).iterdir())
            self.assertEqual(len(read(directory / "experiment.json")["schedule"]), 2)
            self.assertEqual(main(["experiment-report", "--experiment", str(directory)]), 1)

    def test_mutated_snapshot_stops_before_model_execution(self):
        directory = prepare_experiment(self.args)
        (directory / "tasks/js-retry-state/instruction.md").write_text("changed")
        with self.assertRaisesRegex(ValueError, "Frozen experiment input changed"):
            self.run_experiment(directory)
        self.assertFalse((directory / "jobs").exists())

    def test_cancelled_experiment_is_partial_and_not_relaunched(self):
        directory = prepare_experiment(self.args)
        with patch("harbor.job.Job.create", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                self.run_experiment(directory)
        self.assertEqual(read(directory / "state.json")["state"], "interrupted")
        self.assertFalse(read(directory / "summary.json")["complete"])
        with self.assertRaisesRegex(ValueError, "already started"):
            self.run_experiment(directory)

    def test_runner_error_is_visible_and_does_not_erase_other_attempts(self):
        directory = prepare_experiment(self.args)
        with patch("harbor.job.Job.create", side_effect=RuntimeError("setup failed")):
            self.assertEqual(self.run_experiment(directory), 2)
        summary = read(directory / "summary.json")
        self.assertTrue(summary["complete"])
        self.assertFalse(summary["comparison_valid"])
        self.assertEqual(len(summary["attempts"]), 4)
        self.assertTrue(all(a["status"] == "runner_error" for a in summary["attempts"]))

    def test_invalid_and_ineffective_configuration_rejected(self):
        original = read(self.root / "candidate.json")
        for changes, message in [({"system_prompt_file": "rule.md"}, "unsupported fields"),
                                 ({"allowed_tool": []}, "unsupported fields"),
                                 ({"timeout_seconds": 6}, "identical timeout_seconds"),
                                 ({"model": "another"}, "different model"),
                                 ({"instruction_files": []}, "identical effective"),
                                 ({"instruction_files": "rule.md"}, "list of file paths")]:
            with self.subTest(changes=changes):
                save(self.root / "candidate.json", {**original, **changes})
                with self.assertRaisesRegex(ValueError, message):
                    prepare_experiment(self.args)


class ScheduleTests(unittest.TestCase):
    def test_balanced_alternation_and_reproducible_order(self):
        rows = plan(["a", "b", "c"], 4, 11)
        self.assertEqual(rows, plan(["c", "a", "b"], 4, 11))
        self.assertEqual(len(rows), 24)
        for i in range(0, len(rows), 2):
            self.assertEqual(rows[i]["case"], rows[i + 1]["case"])
            self.assertEqual(rows[i]["repeat"], rows[i + 1]["repeat"])
            self.assertNotEqual(rows[i]["variant"], rows[i + 1]["variant"])
        for case in ["a", "b", "c"]:
            first = [r["variant"] for r in rows[::2] if r["case"] == case]
            self.assertEqual(first.count("baseline"), 2)
            self.assertTrue(all(a != b for a, b in zip(first, first[1:])))
