"""Actual Harbor jobs with a fake GigaCode executable; no API/model calls."""

import argparse
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    import harbor
except ImportError:
    harbor = None


@unittest.skipIf(harbor is None, "Install the Harbor dependency with Python 3.12+")
class HarborTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flexbench harbor ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fake(self, mode="success"):
        from reference_solutions import JS
        executable = self.root / "fake-gigacode"
        executable.write_text("#!" + sys.executable + "\n" + '''
import argparse, json, os, subprocess, sys, time
from pathlib import Path
if sys.argv[1:] == ['--version']:
    print('fake-26.10.6')
    raise SystemExit(0)
p = argparse.ArgumentParser()
p.add_argument('--approval-mode')
p.add_argument('--allowed-tools', nargs='+')
p.add_argument('--output-format')
p.add_argument('--model')
p.add_argument('-p')
a = p.parse_args()
assert a.approval_mode == 'auto-edit'
assert a.allowed_tools == ['run_shell_command']
assert a.output_format == 'stream-json'
assert sys.argv[-2] == '-p'
assert sys.stdin.read() == ''
assert Path('TASK.md').read_text() in a.p
assert not Path('grader.py').exists()
started = time.time()
''' + "\nmode = " + repr(mode) + "\n" + "solution = " + repr(JS) + "\n" +
            "child_program = " + repr("import time\nfrom pathlib import Path\nwhile True:\n Path('heartbeat').write_text(str(time.time()))\n time.sleep(.05)\n") + "\n" +
            "controls = " + repr(str(Path(__file__).parent.resolve())) + "\n" + '''
if mode == 'control':
    sys.path.insert(0, controls)
    import reference_solutions, expanded_solutions
    case_id = Path.cwd().parent.parent.name.split('__')[0]
    module = reference_solutions if case_id in ['java-nearest-activity','js-retry-state','maven-version-alignment'] else expanded_solutions
    module.apply(case_id, Path.cwd())
elif mode != 'incorrect':
    Path('src/retry-state.mjs').write_text(solution)
if mode == 'timeout':
    child = subprocess.Popen([sys.executable, '-c', child_program])
    Path('child.pid').write_text(str(child.pid))
    time.sleep(60)
time.sleep(.25)
Path('interval.json').write_text(json.dumps([started,time.time()]))
if mode == 'missing':
    raise SystemExit(0)
usage = {'input_tokens':1000, 'output_tokens':100, 'cache_read_input_tokens':200, 'total_tokens':1100}
print(json.dumps({'type':'assistant','message':{'model':a.model, 'usage':usage}}))
print(json.dumps({'type':'result','subtype':'success','result': '[API Error: 408 status code (no body)]' if mode == 'api_error' else 'done',
    'usage':usage,'stats':{'models':{a.model:{}}}}))
if mode == 'nonzero':
    raise SystemExit(7)
''', encoding="utf-8")
        executable.chmod(0o755)
        return executable

    def config(self, mode="success", models=None, timeout=5):
        from flexbench.harbor.cli import build_config, export_tasks
        directory = self.root / "input"
        export_tasks(["js-retry-state"], directory / "tasks")
        args = argparse.Namespace(models=models or ["model-a"], repeat=1, concurrency=2)
        return build_config(args, directory, {
            "executable": str(self.fake(mode)), "approval_mode": "auto-edit", "allowed_tools": ["run_shell_command"],
            "timeout_seconds": timeout, "instruction_snapshots": [], "model": "model-a",
            "pricing": {"currency":"USD", "source":"test-only", "input_per_million":2,
                        "cached_input_per_million":.5, "output_per_million":8}})

    def run_job(self, config):
        from harbor.job import Job
        async def run():
            job = await Job.create(config)
            return await job.run()
        with redirect_stdout(io.StringIO()):
            return asyncio.run(run())

    def trial_dir(self, trial):
        return self.root / "jobs/input" / trial.trial_name

    def test_real_harbor_parallel_models_and_final_usage(self):
        result = self.run_job(self.config(models=["model-a", "model-b"]))
        self.assertEqual(result.n_total_trials, 2)
        self.assertEqual(len(result.trial_results), 2)
        intervals = []
        for trial in result.trial_results:
            self.assertIsNone(trial.exception_info)
            self.assertEqual(trial.verifier_result.rewards, {"reward":1,"functional_pass":1})
            self.assertEqual(trial.agent_result.n_input_tokens, 1000)
            self.assertEqual(trial.agent_result.n_cache_tokens, 200)
            self.assertEqual(trial.agent_result.n_output_tokens, 100)
            self.assertTrue(trial.agent_result.metadata["usage"]["complete"])
            if trial.config.agent.model_name == "model-a":
                self.assertAlmostEqual(trial.agent_result.cost_usd, .0025)
            else:
                self.assertIsNone(trial.agent_result.cost_usd)
            directory = self.trial_dir(trial)
            intervals.append(json.loads((directory / "local/workspace/interval.json").read_text()))
            self.assertTrue((directory / "verifier/grader.json").exists())
        self.assertLess(max(v[0] for v in intervals), min(v[1] for v in intervals))
        from flexbench.harbor.report import write_report
        summary = write_report(result, self.root / "jobs/input")
        self.assertEqual(summary["efficiency"]["tokens"]["total_tokens"]["total"], 2200)
        self.assertIsNone(summary["efficiency"]["cost_by_currency"]["USD"]["total"])
        self.assertEqual(summary["efficiency"]["cost_by_currency"]["USD"]["measured_runs"], 1)
        from fastapi.testclient import TestClient
        from harbor.viewer.server import create_app
        static_dir = Path(harbor.__file__).parent / "viewer/static"
        with TestClient(create_app(self.root / "jobs", static_dir=static_dir)) as client:
            self.assertEqual(client.get("/").status_code, 200)
            response = client.get("/api/jobs")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], 1)
            detail = client.get("/api/jobs/input")
            self.assertEqual(detail.status_code, 200)

    def test_api_error_still_grades_saved_files_but_is_not_success(self):
        result = self.run_job(self.config("api_error"))
        trial = result.trial_results[0]
        self.assertEqual(trial.exception_info.exception_type, "GigaCodeAPIError")
        self.assertEqual(trial.verifier_result.rewards, {"reward":0,"functional_pass":1})
        self.assertEqual(trial.agent_result.n_input_tokens, 1000)

    def test_missing_result_nonzero_and_incorrect_are_distinct(self):
        for mode, functional, exception in [("missing", 1, "GigaCodeResultError"),
                                             ("nonzero", 1, "GigaCodeResultError"),
                                             ("incorrect", 0, None)]:
            with self.subTest(mode=mode):
                # Each job has its own input and trial directories.
                self.root = Path(self.temp.name) / mode
                self.root.mkdir()
                trial = self.run_job(self.config(mode)).trial_results[0]
                self.assertEqual(trial.verifier_result.rewards, {"reward":0,"functional_pass":functional})
                self.assertEqual(trial.exception_info.exception_type if trial.exception_info else None, exception)
                if mode == "missing":
                    self.assertIsNone(trial.agent_result.n_input_tokens)

    def test_timeout_kills_descendants_and_grades_saved_code(self):
        trial = self.run_job(self.config("timeout", timeout=1)).trial_results[0]
        self.assertEqual(trial.exception_info.exception_type, "AgentTimeoutError")
        self.assertEqual(trial.verifier_result.rewards, {"reward":0,"functional_pass":1})
        self.assertIsNone(trial.agent_result.n_input_tokens)
        heartbeat = self.trial_dir(trial) / "local/workspace/heartbeat"
        previous = heartbeat.read_text()
        time.sleep(.2)
        self.assertEqual(heartbeat.read_text(), previous, "descendant survived timeout")

    def test_all_exported_tasks_load_without_containers_or_solutions(self):
        from flexbench.__main__ import ROOT
        from flexbench.harbor.cli import export_tasks
        from harbor.models.task.task import Task
        ids = sorted(p.parent.name for p in (ROOT / "cases").glob("*/case.json"))
        directory = export_tasks(ids, self.root / "tasks")
        self.assertEqual(len(ids), 8)
        for case in ids:
            task = Task(directory / case)
            self.assertEqual(task.config.metadata["case_id"], case)
            self.assertIsNone(task.config.environment.docker_image)
            self.assertFalse((task.paths.environment_dir / "fixture/grader.py").exists())
            self.assertFalse(task.paths.solution_dir.exists())

    def test_existing_eight_control_solutions_pass_through_harbor(self):
        from flexbench.__main__ import ROOT
        from flexbench.harbor.cli import export_tasks
        config = self.config("control")
        task_dir = self.root / "input/tasks"
        import shutil
        shutil.rmtree(task_dir)
        ids = sorted(p.parent.name for p in (ROOT / "cases").glob("*/case.json"))
        export_tasks(ids, task_dir)
        result = self.run_job(config)
        self.assertEqual(len(result.trial_results), 8)
        for trial in result.trial_results:
            with self.subTest(case=trial.task_name):
                self.assertIsNone(trial.exception_info)
                self.assertEqual(trial.verifier_result.rewards, {"reward":1,"functional_pass":1})

    def test_cli_dry_run_freezes_inputs_without_prompting_and_rejects_bad_limits(self):
        from flexbench.harbor.cli import main
        profile = self.root / "profile.json"
        profile.write_text(json.dumps({"id":"fake", "executor":"gigacode", "mode":"command",
            "adapter":"gigacode", "executable":str(self.fake()), "model":None,
            "timeout_seconds":5, "instruction_files":[]}))
        output = self.root / "out"
        args = ["run", "--profile",str(profile), "--case","js-retry-state",
                "--models","model-a","model-b", "--output",str(output), "--dry-run"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
        self.assertFalse((output / "jobs").exists())
        config = json.loads(next(output.glob("*/config.json")).read_text())
        self.assertEqual(len(config["agents"]), 2)
        self.assertEqual(config["n_concurrent_trials"], 4)
        self.assertEqual(config["retry"]["max_retries"], 0)
        self.assertIsNone(config["environment"]["type"])
        self.assertEqual(config["environment"]["import_path"], "flexbench.harbor.local:LocalEnvironment")
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(args + ["--concurrency","0"]), 2)

    def test_local_transfers_reject_escape_and_network_restrictions(self):
        from flexbench.harbor.cli import export_tasks
        from flexbench.harbor.local import LocalEnvironment
        from harbor.models.task.config import EnvironmentConfig, NetworkPolicy, NetworkMode
        from harbor.models.trial.paths import TrialPaths
        directory = export_tasks(["js-retry-state"], self.root / "tasks")
        kwargs = {"environment_dir": directory / "js-retry-state/environment", "environment_name":"test",
                  "session_id":"test", "trial_paths":TrialPaths(self.root / "trial"),
                  "task_env_config":EnvironmentConfig()}
        env = LocalEnvironment(**kwargs)
        with self.assertRaises(ValueError):
            env.path("../outside")
        with self.assertRaises(ValueError):
            env.path("/etc/passwd")
        with self.assertRaises((ValueError, RuntimeError)):
            LocalEnvironment(**kwargs, network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))

    def test_missing_harbor_reports_incomplete_installation_not_python_age(self):
        from importlib.metadata import PackageNotFoundError
        from flexbench.harbor.cli import require_harbor
        with patch("flexbench.harbor.cli.version", side_effect=PackageNotFoundError("harbor")):
            with self.assertRaisesRegex(ValueError, "Dependency installation must finish successfully") as caught:
                require_harbor()
        self.assertIn(sys.executable, str(caught.exception))
        self.assertNotIn("Python 3.12+", str(caught.exception))
