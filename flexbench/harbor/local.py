"""Local Harbor environment for FlexBench tasks; deliberately not a sandbox.

Only file-transfer APIs map Harbor's reserved log paths. Shell commands always
use real host paths. Container tasks, mounts, users and network restrictions are
unsupported and must never silently become host operations.
"""

import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import signal

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities, EnvironmentResourceCapabilities
from harbor.utils.path_filter import filter_paths_by_patterns


class LocalEnvironment(BaseEnvironment):
    @staticmethod
    def type():
        return "flexbench-local"

    @property
    def capabilities(self):
        return EnvironmentCapabilities(mounted=True)

    @classmethod
    def resource_capabilities(cls):
        return EnvironmentResourceCapabilities()

    def _validate_definition(self):
        if not (self.environment_dir / "fixture").is_dir():
            raise ValueError("LocalEnvironment requires an exported FlexBench fixture")
        if (self.task_env_config.docker_image or self.extra_docker_compose_paths
                or any((self.environment_dir / name).exists() for name in ("Dockerfile", "docker-compose.yaml"))):
            raise ValueError("Container definitions cannot run on the workstation")
        if self.task_env_config.workdir or self.task_env_config.healthcheck:
            raise ValueError("Custom workdir/healthcheck is unsupported for local tasks")
        self.local_dir = self.trial_paths.trial_dir.resolve() / "local"
        self.workspace = self.local_dir / "workspace"
        self._processes = set()
        self._paths = {
            "/logs/agent": self.trial_paths.agent_dir.resolve(),
            "/logs/verifier": self.trial_paths.verifier_dir.resolve(),
            "/logs/artifacts": self.trial_paths.host_artifact_path("main", "/logs/artifacts").resolve(),
            "/tests": self.local_dir / "tests",
            "/solution": self.local_dir / "solution",
        }
        for mount in self._mounts:
            target = mount["target"]
            if target not in self._paths or Path(mount["source"]).resolve() != self._paths[target]:
                raise ValueError("LocalEnvironment supports Harbor log mounts only")

    def path(self, value):
        """Resolve a transfer path, rejecting traversal and symlink escapes."""
        value = Path(value)
        if ".." in value.parts:
            raise ValueError("Parent traversal is unsupported")
        for virtual, actual in self._paths.items():
            if value == Path(virtual) or Path(virtual) in value.parents:
                candidate = actual / value.relative_to(virtual)
                if not candidate.resolve().is_relative_to(actual.resolve()):
                    raise ValueError("Transfer path escapes its directory")
                return candidate
        candidate = value if value.is_absolute() else self.workspace / value
        if not candidate.resolve().is_relative_to(self.workspace.resolve()):
            raise ValueError("Transfer path outside the local workspace")
        return candidate

    async def start(self, force_build=False):
        self.local_dir.mkdir(parents=True, exist_ok=False)
        shutil.copytree(self.environment_dir / "fixture", self.workspace)
        for folder in self._paths.values():
            folder.mkdir(parents=True, exist_ok=True)
        # A real repository lets GigaCode use its normal project workflow.
        git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
               "-c", "user.name=FlexBench Fixture", "-c", "user.email=fixture@example.invalid"]
        for command in (["init"], ["symbolic-ref", "HEAD", "refs/heads/main"],
                        ["add", "."], ["commit", "-m", "Initial benchmark fixture"]):
            result = await self.execute(git + command)
            if result.return_code:
                raise RuntimeError("Fixture git failed: " + (result.stderr or result.stdout or ""))

    @staticmethod
    async def _terminate(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), 1)
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    async def stop(self, delete=False):
        for process in list(self._processes):
            await self._terminate(process)
        # Preserve candidate files for review, including after timeouts.

    async def execute(self, argv, *, cwd=None, env=None, stdout_path=None, stderr_path=None):
        """Execute argv on the host, with closed stdin and owned process group."""
        with ExitStack() as stack:
            stdout = stack.enter_context(Path(stdout_path).open("wb")) if stdout_path else asyncio.subprocess.PIPE
            stderr = stack.enter_context(Path(stderr_path).open("wb")) if stderr_path else asyncio.subprocess.PIPE
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd or self.workspace,
                env={**os.environ, **self._startup_env(), **(self._merge_env(env) or {})},
                stdin=asyncio.subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
            self._processes.add(process)
            try:
                out, err = await process.communicate()
                return ExecResult(return_code=process.returncode,
                                  stdout=out.decode("utf-8", "replace") if out else "",
                                  stderr=err.decode("utf-8", "replace") if err else "")
            finally:
                await self._terminate(process)
                self._processes.discard(process)

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        if user is not None or self.default_user is not None:
            raise ValueError("Changing user is unsupported in local execution")
        if any(virtual in command for virtual in self._paths):
            raise ValueError("Use real host paths in local shell commands")
        return await asyncio.wait_for(
            self.execute(["/bin/bash", "-c", command], cwd=cwd, env=env), timeout_sec)

    async def is_dir(self, path, user=None):
        return self.path(path).is_dir()

    async def is_file(self, path, user=None):
        return self.path(path).is_file()

    async def upload_file(self, source_path, target_path):
        target = self.path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)

    async def upload_dir(self, source_dir, target_dir):
        shutil.copytree(source_dir, self.path(target_dir), dirs_exist_ok=True, symlinks=True)

    async def download_file(self, source_path, target_path):
        source, target = self.path(source_path), Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copy2(source, target, follow_symlinks=False)

    async def download_dir(self, source_dir, target_dir):
        source, target = self.path(source_dir), Path(target_dir)
        if source.resolve() != target.resolve():
            shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)

    async def download_dir_filtered(self, source_dir, target_dir, include=None, exclude=None, always_include=None):
        source = self.path(source_dir)
        names = [str(p.relative_to(source)) for p in source.rglob("*") if p.is_file() and not p.is_symlink()]
        selected = set(filter_paths_by_patterns(names, include=include, exclude=exclude))
        selected.update(n for n in (always_include or []) if n in names)
        for name in selected:
            await self.download_file(str(Path(source_dir) / name), Path(target_dir) / name)
