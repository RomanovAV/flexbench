"""Harbor agent using the installed, already authenticated workstation CLI."""

import asyncio
import json
from typing import Literal

from harbor.agents.base import BaseAgent
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.agents.options import AgentOptions
from harbor.models.agent.context import AgentContext
from pydantic import Field

from flexbench.gigacode import argv, process_output
from flexbench.metrics import normalize, validate_pricing
from .local import LocalEnvironment


class GigaCodeAPIError(NonZeroAgentExitCodeError):
    pass


class GigaCodeResultError(NonZeroAgentExitCodeError):
    pass


class GigaCodeOptions(AgentOptions):
    executable: str = "gigacode"
    approval_mode: Literal["default", "auto-edit"] = "auto-edit"
    allowed_tools: list[str] = Field(default_factory=lambda: ["run_shell_command"])
    pricing: dict | None = None


class GigaCodeAgent(BaseAgent):
    options_model = GigaCodeOptions

    @staticmethod
    def name():
        return "gigacode"

    def version(self):
        return getattr(self, "_version", None)

    async def setup(self, environment):
        if not isinstance(environment, LocalEnvironment):
            raise ValueError("GigaCodeAgent requires flexbench-local")
        if self.mcp_servers or self.skills_dir:
            raise ValueError("Harbor skill/MCP injection is not configured for GigaCode")
        if any(not tool.strip() or tool.startswith("-") for tool in self.options.allowed_tools):
            raise ValueError("allowed_tools must contain tool names")
        if self.options.pricing:
            validate_pricing(self.options.pricing)
        result = await asyncio.wait_for(environment.execute([self.options.executable, "--version"]), 10)
        if result.return_code or not result.stdout.strip():
            raise RuntimeError("GigaCode --version failed; check installation")
        self._version = result.stdout.strip()

    async def run(self, instruction, environment, context):
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (environment.workspace / "TASK.md").write_text(instruction, encoding="utf-8")
        profile = {**self.options.model_dump(), "model": self.model_name}
        execution = {"reason": "interrupted", "return_code": None}
        try:
            result = await environment.execute(argv(profile, instruction),
                                               stdout_path=self.logs_dir / "executor.jsonl",
                                               stderr_path=self.logs_dir / "executor.log")
            execution.update(reason="completed" if result.return_code == 0 else "nonzero_exit",
                             return_code=result.return_code)
        finally:
            # Also written after cancellation so partial telemetry stays partial.
            (self.logs_dir / "execution.json").write_text(json.dumps(execution), encoding="utf-8")
        info, _ = self._measurements(context)
        execution = context.metadata["execution"]
        if execution["reason"] != "completed":
            error = GigaCodeAPIError if info.get("failure") == "api_error" else GigaCodeResultError
            raise error("GigaCode: " + execution["reason"] + "; inspect agent/executor.jsonl and executor.log")

    def _measurements(self, context):
        record = self.logs_dir / "execution.json"
        if not record.exists():
            return {}, None
        execution = json.loads(record.read_text(encoding="utf-8"))
        profile = {**self.options.model_dump(), "model": self.model_name}
        info, payload = process_output(self.logs_dir, profile, execution)
        if payload and execution["reason"] == "interrupted":
            payload["complete"] = False
        pricing = self.options.pricing
        if not self.model_name or info["models"] != [self.model_name]:
            pricing = None
        usage, cost = normalize(payload, pricing) if payload else (None, None)
        if usage:
            context.n_input_tokens = usage["input_tokens"]
            context.n_output_tokens = usage["output_tokens"]
            context.n_cache_tokens = usage["cached_input_tokens"]
        if cost and cost["currency"] == "USD":
            context.cost_usd = cost["amount"]
        context.metadata = {"execution": execution, "gigacode": info, "usage": usage,
                            "cost": cost, "executor_version": self.version(),
                            "isolation": "trusted-local-no-security-boundary"}
        record.write_text(json.dumps(execution), encoding="utf-8")
        (self.logs_dir / "measurements.json").write_text(
            json.dumps(context.metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        from .trajectory import metrics_trajectory
        trajectory = metrics_trajectory(context.metadata, self.model_name, self.version())
        if trajectory:
            (self.logs_dir / "trajectory.json").write_text(
                json.dumps(trajectory, ensure_ascii=False, indent=2), encoding="utf-8")
        return info, payload

    def populate_context_post_run(self, context: AgentContext):
        self._measurements(context)
