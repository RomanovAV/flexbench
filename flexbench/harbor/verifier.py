"""Run the frozen existing grader outside the candidate's working directory."""

import json
import sys

from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier

from .local import LocalEnvironment


class FlexBenchVerifier(BaseVerifier):
    async def verify(self):
        if not isinstance(self.environment, LocalEnvironment):
            raise ValueError("FlexBenchVerifier requires flexbench-local")
        directory = self.trial_paths.verifier_dir
        directory.mkdir(parents=True, exist_ok=True)
        result = await self.environment.execute(
            [sys.executable, str(self.task.paths.tests_dir / "graders/grader.py"), str(self.environment.workspace)],
            cwd=self.task.paths.tests_dir, env={**self.override_env, **(self.verifier_env or {})},
            stdout_path=directory / "grader.json", stderr_path=directory / "grader.log")
        if result.return_code:
            raise RuntimeError("Grader failed; inspect verifier/grader.log")
        data = json.loads((directory / "grader.json").read_text(encoding="utf-8"))
        checks = data.get("checks")
        if not isinstance(checks, list) or not checks or any(
                not isinstance(check, dict) or type(check.get("passed")) is not bool for check in checks):
            raise ValueError("Grader returned invalid checks")
        functional = all(check["passed"] for check in checks)
        execution_path = self.trial_paths.agent_dir / "execution.json"
        execution = json.loads(execution_path.read_text(encoding="utf-8")) if execution_path.exists() else {}
        completed = execution.get("reason") == "completed" and execution.get("return_code") == 0
        rewards = {"reward": int(functional and completed), "functional_pass": int(functional)}
        (directory / "reward.json").write_text(json.dumps(rewards), encoding="utf-8")
        await self.environment.execute(
            ["git", "-c", "core.hooksPath=/dev/null", "diff", "--binary", "HEAD"],
            stdout_path=directory / "candidate.patch", stderr_path=directory / "diff.log")
        return VerifierResult(rewards=rewards)
