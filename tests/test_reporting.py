import json
from pathlib import Path
import tempfile
import unittest

from flexbench.__main__ import ROOT, attach_log, compare, grade, observe, prepare, read
from flexbench.reporting import diagnose, write_report
from reference_solutions import apply


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = ROOT / "profiles/gigacode-manual.json"

    def attempt(self, side="runs"):
        return prepare("maven-version-alignment",self.profile,self.root / side)

    def test_observations_and_log_are_snapshotted(self):
        run = self.attempt()
        observe(run,"human_intervention","Helped start the tool")
        observe(run,"intervention_summary","One intervention, no manual code edits")
        source = self.root / "chat.txt"
        source.write_text("Session output")
        attach_log(run,source)
        result = grade(run)
        self.assertEqual(len(result["observations"]),2)
        self.assertEqual(result["log_evidence"][0]["source"],"operator_export")
        self.assertEqual(diagnose(result)["reported_intervention_events"],1)
        source.write_text("Changed")
        self.assertEqual((run / "session.log").read_text(),"Session output")
        with self.assertRaisesRegex(ValueError,"sealed"):
            observe(run,"note","late note")
        self.assertEqual(read(run / "result.json")["observations"],result["observations"])

    def test_no_events_does_not_mean_no_interventions(self):
        report = diagnose(grade(self.attempt()))
        self.assertFalse(report["intervention_summary_reported"])
        self.assertTrue(any("вмешательств" in gap for gap in report["evidence_gaps"]))

    def test_failure_is_evidence_and_cause_is_hypothesis(self):
        result = grade(self.attempt())
        report = diagnose(result)
        self.assertEqual({d["area"] for d in report["directions"]},{"cross_file"})
        self.assertTrue(all(d["evidence"] for d in report["directions"]))
        self.assertIn("Возможно",report["directions"][0]["hypothesis"])
        result["status"]="executor_timeout"
        self.assertEqual([d["area"] for d in diagnose(result)["directions"]],["execution"])

    def test_report_shows_regression_without_exporting_raw_content(self):
        baseline, candidate = self.attempt("baseline"), self.attempt("candidate")
        apply("maven-version-alignment",baseline / "workspace")
        observe(candidate,"note","PRIVATE_NOTE_NOT_FOR_SUMMARY")
        source = self.root / "chat.txt"
        source.write_text("PRIVATE_LOG_NOT_FOR_SUMMARY")
        attach_log(candidate,source)
        groups=[[grade(baseline)],[grade(candidate)]]
        comparison=compare(self.root / "baseline",self.root / "candidate")
        output=write_report(comparison,groups,self.root / "report")
        summary=json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["macro_delta_percentage_points"],-100)
        combined=(output / "report.md").read_text()+(output / "summary.json").read_text()
        self.assertNotIn("PRIVATE_NOTE",combined)
        self.assertNotIn("PRIVATE_LOG",combined)
        self.assertIn("Гипотеза",combined)
        self.assertIn("-100.0",combined)
        with self.assertRaisesRegex(ValueError,"already exists"):
            write_report(comparison,groups,output)

    def test_log_cannot_be_overwritten(self):
        run=self.attempt()
        source=self.root / "chat.txt"
        source.write_text("first")
        attach_log(run,source)
        source.write_text("second")
        with self.assertRaisesRegex(ValueError,"already exists"):
            attach_log(run,source)
        self.assertEqual((run / "session.log").read_text(),"first")


if __name__ == "__main__":
    unittest.main()
