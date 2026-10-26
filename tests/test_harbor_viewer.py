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
        self.output = self.root / "out"
        self.catalog = {"models": [{"id": "model-a", "name": "Model A"}, {"id": "model-b", "name": "Model B"}],
                        "current_model_id": "model-a"}
        self.discovery = self.enterContext(patch("flexbench.harbor.viewer.discover_models", side_effect=lambda _: self.catalog))
        self.client = self.enterContext(TestClient(create_app(self.output, self.profile), base_url="http://127.0.0.1"))
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


if __name__ == "__main__":
    unittest.main()
