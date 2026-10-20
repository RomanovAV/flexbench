import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flexbench.__main__ import ROOT, compare, execute, grade, pilot, prepare, read, run_executor, save
from reference_solutions import apply

IDS = ["java-nearest-activity", "js-retry-state", "maven-version-alignment"]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = ROOT / "profiles/gigacode-manual.json"

    def prepare(self, case_id="maven-version-alignment", directory=None, profile=None):
        return prepare(case_id, profile or self.profile, directory or self.root / "runs")

    def command_profile(self, code, timeout=10):
        p = self.root / "command.json"
        save(p, {"id":"test-only", "executor":"test-only", "model":None,
                 "mode":"command", "command":[sys.executable,"-c",code],
                 "timeout_seconds":timeout,"instruction_files":[]})
        return p

    def test_originals_fail_and_reference_solutions_pass(self):
        for case_id in IDS:
            with self.subTest(case=case_id):
                bad = self.prepare(case_id)
                self.assertEqual(grade(bad)["status"], "acceptance_failure")
                good = self.prepare(case_id)
                apply(case_id, good / "workspace")
                result = grade(good)
                self.assertEqual(result["status"], "passed", result)
                self.assertIsNone(result["success_within_time_budget"])
                self.assertIsNone(result["executor"])

    def test_java_boundary_mutation_is_rejected(self):
        run = self.prepare(IDS[0])
        apply(IDS[0], run / "workspace")
        src = run / "workspace/src/ActivityService.java"
        src.write_text(src.read_text().replace("!a.scheduledAt().isBefore(now)", "a.scheduledAt().isAfter(now)"))
        result = grade(run)
        self.assertEqual(result["status"], "acceptance_failure")
        self.assertFalse(next(c["passed"] for c in result["evaluation"]["checks"] if c["id"]=="inclusive_now"))

    def test_stale_response_mutation_is_rejected(self):
        run = self.prepare(IDS[1])
        apply(IDS[1], run / "workspace")
        src = run / "workspace/src/retry-state.mjs"
        src.write_text(src.read_text().replace(" || e.requestId !== s.requestId", ""))
        self.assertEqual(grade(run)["status"], "acceptance_failure")

    def test_partial_dependency_bump_is_rejected(self):
        run = self.prepare()
        src = run / "workspace/pom.xml"
        src.write_text(src.read_text().replace("3.2.0", "3.2.1"))
        self.assertEqual(grade(run)["status"], "acceptance_failure")

    def test_results_cannot_be_overwritten(self):
        run = self.prepare()
        grade(run)
        original = (run / "result.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "sealed"):
            grade(run)
        self.assertEqual((run / "result.json").read_bytes(), original)

    def test_case_version_change_is_rejected(self):
        run = self.prepare()
        record = read(run / "run.json")
        record["case"]["digest"] = "changed"
        save(run / "run.json", record)
        with self.assertRaisesRegex(ValueError, "changed"):
            grade(run)

    def test_workspace_isolation_and_no_grader_copy(self):
        first, second = self.prepare(), self.prepare()
        (first / "workspace/pom.xml").write_text("changed")
        self.assertIn("3.2.0", (second / "workspace/pom.xml").read_text())
        self.assertFalse((first / "workspace/graders").exists())
        self.assertTrue((first / "workspace/.git").is_dir())

    def test_prepare_supports_git_without_init_branch_option(self):
        actual_run = subprocess.run

        def older_git(argv, *args, **kwargs):
            if argv[0] == "git" and "init" in argv:
                if "-b" in argv or "--initial-branch" in argv:
                    if kwargs.get("check"):
                        raise subprocess.CalledProcessError(129, argv, output="", stderr="error: unknown switch `b'\n")
                    return subprocess.CompletedProcess(argv, 129, stdout="", stderr="error: unknown switch `b'\n")
                # A user's default branch must not change fixture branch names.
                argv = argv[:1] + ["-c", "init.defaultBranch=legacy-default"] + argv[1:]
            return actual_run(argv, *args, **kwargs)

        with patch("flexbench.__main__.subprocess.run", side_effect=older_git):
            run = self.prepare()
        branch = actual_run(["git", "-C", str(run / "workspace"), "symbolic-ref", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(branch, "refs/heads/main")
        self.assertEqual(read(run / "run.json")["state"], "prepared")
        self.assertEqual(grade(run)["status"], "acceptance_failure")

    def test_fixture_git_failure_includes_actual_diagnostic(self):
        actual_run = subprocess.run

        def failing_git(argv, *args, **kwargs):
            if argv[0] == "git" and "init" in argv:
                return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: fixture permission denied\n")
            return actual_run(argv, *args, **kwargs)

        with patch("flexbench.__main__.subprocess.run", side_effect=failing_git):
            with self.assertRaisesRegex(ValueError, "fixture permission denied"):
                self.prepare()

    def test_instruction_snapshot_and_delivery(self):
        profile = ROOT / "profiles/gigacode-boundaries-manual.json"
        run = self.prepare(profile=profile)
        record = read(run / "run.json")
        self.assertIn("граничные", record["profile"]["instruction_snapshots"][0])
        self.assertIn("граничные", (run / "workspace/TASK.md").read_text())

    def test_command_timeout_is_not_acceptance_failure(self):
        run = self.prepare(profile=self.command_profile("import time; time.sleep(5)", .1))
        run_executor(run)
        result = grade(run)
        self.assertEqual(result["status"], "executor_timeout")
        self.assertFalse(result["success_within_time_budget"])

    def test_command_nonzero_is_preserved(self):
        run = self.prepare(profile=self.command_profile("raise SystemExit(7)"))
        run_executor(run)
        result = grade(run)
        self.assertEqual(result["status"], "executor_nonzero_exit")
        self.assertEqual(result["executor"]["exit_code"], 7)

    def test_successful_command_pipeline(self):
        code = "from pathlib import Path; p=Path('pom.xml'); p.write_text(p.read_text().replace('3.2.0','3.2.1')); p=Path('api/pom.xml'); p.write_text(p.read_text().replace('3.2.0','${framework.version}'))"
        run = self.prepare(profile=self.command_profile(code))
        run_executor(run)
        result = grade(run)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["success_within_time_budget"])
        self.assertGreater(result["executor"]["seconds"], 0)

    def test_grader_failure_is_not_task_failure(self):
        run = self.prepare()
        with patch("flexbench.__main__.execute", return_value={"reason":"timeout","seconds":1,"exit_code":None}):
            result = grade(run)
        self.assertEqual(result["status"],"grader_error")
        self.assertIsNone(result["functional_pass"])

    def test_launch_error_is_recorded(self):
        result = execute(["/does-not-exist/flexbench-fixture"], self.root, self.root / "log", 1)
        self.assertEqual(result["reason"], "launch_error")

    def test_interrupted_run_cannot_be_graded_as_success(self):
        run = self.prepare()
        record = read(run / "run.json")
        record["state"] = "running"
        save(run / "run.json", record)
        with self.assertRaisesRegex(ValueError, "interrupted"):
            grade(run)

    def test_comparison_and_repeat_count_guard(self):
        baseline, candidate = self.root / "baseline", self.root / "candidate"
        bad = self.prepare(directory=baseline)
        good = self.prepare(directory=candidate)
        apply(IDS[2], good / "workspace")
        grade(bad)
        grade(good)
        report = compare(baseline,candidate)
        self.assertEqual(report["macro_delta_percentage_points"], 100)
        grade(self.prepare(directory=baseline))
        with self.assertRaisesRegex(ValueError, "Incompatible"):
            compare(baseline,candidate)

    def test_comparison_rejects_same_run_on_both_sides(self):
        run = self.prepare()
        grade(run)
        with self.assertRaisesRegex(ValueError, "overlap"):
            compare(run,run)

    def test_candidate_symlink_rejected(self):
        run = self.prepare()
        (run / "workspace/outside").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "symlink"):
            grade(run)

    def test_pilot_prepares_all_cases_in_independent_sessions(self):
        folder = pilot(self.profile, ROOT / "profiles/gigaflex-manual.json", self.root)
        records = read(folder / "pilot.json")["runs"]
        expected = 2 * len(list((ROOT / "cases").glob("*/case.json")))
        self.assertEqual(len(records), expected)
        self.assertEqual(len({r["run"] for r in records}), expected)
        self.assertEqual((folder / "RUNBOOK.md").read_text().count("flexbench grade"), expected)
        with self.assertRaisesRegex(ValueError, "Unfinished"):
            compare(folder / "baseline",folder / "candidate")

    def test_grading_snapshots_candidate(self):
        run = self.prepare()
        apply(IDS[2],run / "workspace")
        result = grade(run)
        (run / "workspace/pom.xml").write_text("changed after grading")
        self.assertIn("3.2.1", (run / "candidate/pom.xml").read_text())
        self.assertEqual(read(run / "result.json")["candidate_digest"],result["candidate_digest"])
        self.assertTrue((run / "changes.patch").exists())


if __name__ == "__main__":
    unittest.main()
