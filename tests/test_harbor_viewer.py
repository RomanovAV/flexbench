"""Browser launch contract tested through FastAPI and actual Harbor workers."""

import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_harbor as fixtures


@unittest.skipIf(fixtures.harbor is None, "Install the Harbor dependency")
class HarborViewerTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from flexbench.harbor.viewer import create_app
        self.helper = fixtures.HarborTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.root = self.helper.root
        self.profile = self.root / "profile.json"
        self.set_profile()
        self.experiments = self.root / "experiments"
        self.experiments.mkdir()
        (self.root / "rule.md").write_text("Check the contract before finishing.")
        self.candidate = self.root / "candidate.json"
        self.candidate.write_text(json.dumps({**json.loads(self.profile.read_text()), "instruction_files": ["rule.md"]}))
        (self.experiments / "test-rules.json").write_text(json.dumps({
            "schema_version": 1, "name": "Rule comparison", "hypothesis": "Test a rule",
            "baseline": "../profile.json", "candidate": "../candidate.json", "cases": ["js-retry-state"], "repeat": 1}))
        self.output = self.root / "out"
        self.catalog = {"models": [{"id": "model-a", "name": "Model A"}, {"id": "model-b", "name": "Model B"}],
                        "current_model_id": "model-a"}
        self.discovery = self.enterContext(patch("flexbench.harbor.viewer.discover_models", side_effect=lambda _: self.catalog))
        self.client = self.enterContext(TestClient(create_app(self.output, self.profile, self.experiments), base_url="http://127.0.0.1"))
        self.headers = {"X-FlexBench-Request": "1", "Origin": "http://127.0.0.1"}

    def set_profile(self, mode="success", timeout=10):
        self.profile.write_text(json.dumps({"id": "browser-fake", "executor": "gigacode", "mode": "command",
            "adapter": "gigacode", "executable": str(self.helper.fake(mode)), "model": None,
            "timeout_seconds": timeout, "instruction_files": []}))

    def payload(self, **changes):
        return {"request_id": str(uuid4()), "models": ["model-a", "model-b"], "cases": ["js-retry-state"],
                "repeat": 1, "concurrency": 2, **changes}

    def launch(self, data):
        return self.client.post("/api/flexbench/runs", json=data, headers=self.headers)

    def wait(self, receipt, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.client.get(receipt["status_url"])
            self.assertEqual(response.status_code, 200, response.text)
            state = response.json()
            if state["returncode"] is not None:
                return state
            time.sleep(.1)
        self.fail("Worker did not finish: " + state["log_tail"])

    def test_form_discovery_and_harbor_history_share_the_server(self):
        form = self.client.get("/run")
        self.assertEqual(form.status_code, 200)
        self.assertIn('id="launch-form"', form.text)
        self.assertEqual(form.headers["cache-control"], "no-store")
        self.assertIn('id="launch-form"', self.client.get("/run/").text)
        self.assertEqual(self.client.get("/flexbench-launch.js").status_code, 200)
        for asset in ("/flexbench-ui.css", "/flexbench-summary.js", "/flexbench-theme.css"):
            self.assertEqual(self.client.get(asset).status_code, 200, asset)
        self.assertIn("Google Sans Code", self.client.get("/flexbench-theme.css").text)
        self.assertIn("/flexbench-theme.css", form.text)
        self.assertIn("/flexbench-summary.js", self.client.get("/").text)
        self.assertIn("/flexbench-navigation.js", self.client.get("/").text)
        self.assertIn("location.assign", self.client.get("/flexbench-navigation.js").text)
        self.assertEqual(self.client.get("/api/jobs").json()["total"], 0)
        self.assertEqual(len(self.client.get("/api/flexbench/options").json()["cases"]), 8)
        models = self.client.get("/api/flexbench/models").json()
        self.assertEqual(models["models"], self.catalog["models"])
        self.assertEqual(models["executor_version"], "fake-26.10.6")
        self.discovery.assert_called_once_with(str(self.helper.root / "fake-gigacode"))
        # The generic Harbor launcher cannot accidentally choose Docker or another agent.
        self.assertEqual(self.client.post("/api/run", json={}).status_code, 405)

    def test_browser_launch_parallel_worker_reports_and_idempotent_retry(self):
        data = self.payload()
        response = self.launch(data)
        self.assertEqual(response.status_code, 202, response.text)
        receipt = response.json()
        retry = self.launch(data)
        self.assertEqual(retry.json(), receipt)
        self.assertEqual(self.discovery.call_count, 1)
        self.assertEqual(len(list(self.output.glob("gigacode-*/config.json"))), 1)
        conflict = self.launch({**data, "repeat": 2})
        self.assertEqual(conflict.status_code, 409)
        state = self.wait(receipt)
        self.assertEqual(state["returncode"], 0, state["log_tail"])
        self.assertEqual(state["summary"]["efficiency"]["successes"], 2)
        self.assertEqual(state["completed"], 2)
        directory = self.output / receipt["job_name"]
        config = json.loads((directory / "config.json").read_text())
        self.assertEqual([a["model_name"] for a in config["agents"]], ["model-a", "model-b"])
        self.assertEqual(config["n_concurrent_trials"], 2)
        self.assertEqual(config["environment"]["import_path"], "flexbench.harbor.local:LocalEnvironment")
        intervals = [json.loads(p.read_text()) for p in (self.output / "jobs" / receipt["job_name"]).glob("*/local/workspace/interval.json")]
        self.assertEqual(len(intervals), 2)
        self.assertLess(max(v[0] for v in intervals), min(v[1] for v in intervals))
        self.assertEqual(self.client.get("/api/jobs").json()["total"], 1)
        native = self.client.get("/api/jobs").json()["items"][0]
        # Harbor reports uncached input separately; the cache must be added once.
        self.assertEqual((native["total_input_tokens"], native["total_cached_input_tokens"],
                          native["total_output_tokens"]), (1600, 400, 200))
        self.assertEqual(self.client.get("/api/jobs/" + receipt["job_name"]).json()["stats"]["n_input_tokens"], 2000)
        native_trials = self.client.get("/api/jobs/" + receipt["job_name"] + "/chart-trials").json()
        self.assertEqual(sum(t["input_tokens"] + t["cached_input_tokens"] + t["output_tokens"] for t in native_trials), 2200)
        measured_path = "/api/flexbench/jobs/" + receipt["job_name"] + "/summary"
        measured = self.client.get(measured_path).json()
        self.assertEqual(measured["efficiency"]["tokens"]["total_tokens"]["total"], 2200)
        trial = self.client.get(measured_path, params={"trial": native_trials[0]["name"]}).json()
        self.assertEqual(trial["efficiency"]["tokens"]["total_tokens"]["total"], 1100)
        self.assertEqual(trial["n_total_trials"], 1)
        self.assertEqual(self.client.get(measured_path, params={"trial": "missing"}).status_code, 404)
        from harbor.models.trajectories.trajectory import Trajectory
        first = native_trials[0]["name"]
        trial_dir = self.output / "jobs" / receipt["job_name"] / first
        atif_path = trial_dir / "agent/trajectory.json"
        url = "/api/jobs/" + receipt["job_name"] + "/trials/" + first + "/trajectory"
        atif = self.client.get(url).json()
        Trajectory.model_validate(atif)
        self.assertEqual(atif["final_metrics"]["total_prompt_tokens"], 1000)
        self.assertEqual(atif["final_metrics"]["total_completion_tokens"], 100)
        self.assertEqual(atif["final_metrics"]["total_cached_tokens"], 200)
        self.assertNotIn("total_cost_usd", atif["final_metrics"])
        self.assertEqual(atif["extra"]["projection"], "session-metrics-only")
        self.assertEqual(atif["steps"][0]["source"], "system")
        self.assertTrue(atif_path.is_file())
        self.assertEqual(self.client.get(receipt["url"]).status_code, 200)
        self.assertEqual(self.client.get(receipt["status_url"] + "/summary").json()["n_total_trials"], 2)
        self.assertIn("Full success", self.client.get(receipt["status_url"] + "/report").text)
        self.assertEqual(self.client.get(receipt["status_url"] + "/config.json").status_code, 404)
        from fastapi.testclient import TestClient
        from flexbench.harbor.viewer import create_app
        with TestClient(create_app(self.output, self.profile), base_url="http://127.0.0.1") as restarted:
            restored = restarted.get(receipt["status_url"]).json()
            self.assertEqual(restored["returncode"], 0)
            self.assertFalse(restored["running"])

    def test_saved_partial_measurements_stay_unknown_and_reads_do_not_change_them(self):
        from flexbench.harbor.report import aggregate_attempts
        name = "gigacode-20261028T120000-01234567"
        directory = self.output / "jobs" / name
        directory.mkdir()
        attempts = [{"trial": "partial", "case": "js-retry-state", "model": "model-a", "status": "failed",
                     "functional_pass": None, "executor": {"seconds": 1}, "cost": None,
                     "usage": {"input_tokens": 5, "output_tokens": 2, "cached_input_tokens": None,
                               "total_tokens": 7, "complete": False}}]
        data = {"n_total_trials": 1, **aggregate_attempts(attempts)}
        path = directory / "flexbench-summary.json"
        path.write_text(json.dumps(data))
        before = path.read_bytes()
        url = "/api/flexbench/jobs/" + name + "/summary"
        summary = self.client.get(url).json()
        self.assertIsNone(summary["efficiency"]["tokens"]["total_tokens"]["total"])
        self.assertEqual(summary["efficiency"]["tokens"]["total_tokens"]["observed_total"], 7)
        self.assertEqual(summary["efficiency"]["tokens"]["total_tokens"]["measured_runs"], 0)
        self.assertEqual(self.client.get(url, params={"trial": "partial"}).json()["efficiency"], data["efficiency"])
        self.assertEqual(path.read_bytes(), before)
        from flexbench.harbor.trajectory import metrics_trajectory
        self.assertIsNone(metrics_trajectory({"usage": attempts[0]["usage"]}))
        self.assertIsNone(metrics_trajectory({"usage": {**attempts[0]["usage"], "complete": True}}))
        path.unlink()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.discovery.assert_not_called()

    def test_stale_models_and_invalid_cases_do_not_start_workers(self):
        # Simulate catalogue drift after a user has selected a model.
        self.catalog["models"] = [{"id": "model-b", "name": "Model B"}]
        response = self.launch(self.payload())
        self.assertEqual(response.status_code, 409)
        self.assertIn("model-a", response.json()["detail"])
        self.assertEqual(list(self.output.glob("gigacode-*")), [])
        invalid = self.launch(self.payload(models=["model-b"], cases=["unknown-case"]))
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(list(self.output.glob("gigacode-*")), [])

    def test_closed_request_schema_and_local_write_boundary(self):
        for changes in ({"models": []}, {"models": ["model-a", "model-a"]}, {"cases": []},
                        {"concurrency": 0}, {"repeat": True}, {"request_id": "bad"},
                        {"profile": "/tmp/other.json"}, {"environment": "docker"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.launch(self.payload(**changes)).status_code, 422)
        for headers in ({}, {**self.headers, "Origin": "https://example.invalid"}):
            self.assertEqual(self.client.post("/api/flexbench/runs", json=self.payload(), headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/run", headers={"Host": "evil.invalid"}).status_code, 400)
        self.discovery.assert_not_called()
        self.assertEqual(list(self.output.glob("gigacode-*")), [])

    def test_catalog_error_is_visible_and_does_not_use_a_cached_list(self):
        self.discovery.side_effect = ValueError("GigaCode catalogue unavailable")
        response = self.client.get("/api/flexbench/models")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "GigaCode catalogue unavailable")
        self.assertEqual(self.launch(self.payload()).status_code, 503)
        self.assertEqual(list(self.output.glob("gigacode-*")), [])

    def test_stop_cancels_worker_and_its_agent_descendant(self):
        self.set_profile("timeout", timeout=60)
        response = self.launch(self.payload(models=["model-a"]))
        self.assertEqual(response.status_code, 202, response.text)
        receipt = response.json()
        deadline = time.monotonic() + 20
        heartbeat = None
        while time.monotonic() < deadline:
            heartbeat = next((self.output / "jobs" / receipt["job_name"]).glob("*/local/workspace/heartbeat"), None)
            if heartbeat:
                break
            time.sleep(.1)
        self.assertIsNotNone(heartbeat)
        stopped = self.client.post(receipt["status_url"] + "/stop", headers=self.headers)
        self.assertEqual(stopped.status_code, 200, stopped.text)
        state = self.wait(receipt)
        self.assertEqual(state["returncode"], 130, state["log_tail"])
        previous = heartbeat.read_text()
        time.sleep(.2)
        self.assertEqual(heartbeat.read_text(), previous)

    def experiment_payload(self, **changes):
        return {"request_id": str(uuid4()), "spec_id": "test-rules", "model": "model-a",
                "cases": ["js-retry-state"], "repeat": 1, **changes}

    def experiment_launch(self, data):
        return self.client.post("/api/flexbench/experiments", json=data, headers=self.headers)

    def test_experiment_picker_worker_comparison_history_and_recovery(self):
        options = self.client.get("/api/flexbench/options").json()
        spec = options["experiments"][0]
        self.assertEqual(spec["id"], "test-rules")
        self.assertEqual(spec["configurations"]["candidate"]["instructions"], ["Check the contract before finishing."])
        data = self.experiment_payload()
        response = self.experiment_launch(data)
        self.assertEqual(response.status_code, 202, response.text)
        receipt = response.json()
        self.assertEqual(self.experiment_launch(data).json(), receipt)
        self.assertEqual(self.discovery.call_count, 1)
        self.assertEqual(self.experiment_launch({**data, "repeat": 2}).status_code, 409)
        state = self.wait(receipt)
        self.assertEqual(state["returncode"], 0, state["log_tail"])
        self.assertEqual(state["completed"], 2)
        self.assertTrue(state["summary"]["comparison_valid"])
        self.assertEqual(state["summary"]["delta_percentage_points"], 0)
        self.assertEqual(self.client.get("/api/jobs").json()["total"], 2)
        for attempt in state["summary"]["attempts"]:
            job = attempt["job_name"]
            self.assertTrue(job.startswith(receipt["job_name"]))
            self.assertEqual(self.client.get("/api/jobs/" + job).status_code, 200)
            self.assertEqual(self.client.get("/api/flexbench/jobs/" + job + "/summary").status_code, 200)
        self.assertEqual(self.client.get("/api/flexbench/experiments").json()[0]["job_name"], receipt["job_name"])
        self.assertEqual(self.client.get(receipt["status_url"] + "/summary").json()["kind"], "harness_comparison")
        self.assertIn("Candidate", self.client.get(receipt["status_url"] + "/report").text)
        self.assertEqual(self.client.get(receipt["status_url"] + "/config").json()["model"], "model-a")
        self.assertEqual(self.client.get(receipt["status_url"] + "/launcher.log").status_code, 404)
        from fastapi.testclient import TestClient
        from flexbench.harbor.viewer import create_app
        with TestClient(create_app(self.output, self.profile, self.experiments), base_url="http://127.0.0.1") as restarted:
            restored = restarted.get(receipt["status_url"]).json()
            self.assertEqual(restored["returncode"], 0)
            self.assertFalse(restored["can_stop"])
            self.assertEqual(restarted.post("/api/flexbench/experiments", json=data, headers=self.headers).json(), receipt)

    def test_experiment_rejects_stale_model_paths_and_unsafe_requests(self):
        for changes in ({"spec_id": "../profile"}, {"models": ["model-a", "model-b"]},
                        {"concurrency": 2}, {"model": ""}, {"repeat": True},
                        {"cases": ["js-retry-state", "js-retry-state"]}, {"request_id": "invalid"}):
            self.assertEqual(self.experiment_launch(self.experiment_payload(**changes)).status_code, 422)
        self.assertEqual(self.experiment_launch(self.experiment_payload(spec_id="missing")).status_code, 422)
        self.assertEqual(self.experiment_launch(self.experiment_payload(model="missing-model")).status_code, 409)
        self.assertEqual(self.client.post("/api/flexbench/experiments", json=self.experiment_payload()).status_code, 403)
        self.assertEqual(self.client.post("/api/flexbench/experiments", json=self.experiment_payload(),
                         headers={**self.headers, "Origin": "https://example.invalid"}).status_code, 403)
        self.assertEqual(list((self.output / "experiments").glob("experiment-*")), [])

    def test_stop_experiment_cancels_current_attempt_and_does_not_start_next(self):
        self.set_profile("timeout", timeout=60)
        self.candidate.write_text(json.dumps({**json.loads(self.profile.read_text()), "instruction_files": ["rule.md"]}))
        response = self.experiment_launch(self.experiment_payload(repeat=2))
        self.assertEqual(response.status_code, 202, response.text)
        receipt = response.json()
        deadline = time.monotonic() + 20
        heartbeat = None
        while time.monotonic() < deadline:
            heartbeat = next((self.output / "jobs").glob("*/*/local/workspace/heartbeat"), None)
            if heartbeat:
                break
            time.sleep(.1)
        self.assertIsNotNone(heartbeat)
        self.assertEqual(self.client.post(receipt["status_url"] + "/stop", headers=self.headers).status_code, 200)
        state = self.wait(receipt)
        self.assertEqual(state["returncode"], 130, state["log_tail"])
        self.assertFalse(state["summary"]["complete"])
        self.assertEqual(len(list((self.output / "jobs").iterdir())), 1)
        previous = heartbeat.read_text()
        time.sleep(.2)
        self.assertEqual(heartbeat.read_text(), previous)


if __name__ == "__main__":
    unittest.main()
