"""Thin job/configuration layer. Harbor owns scheduling, retries and results."""

import argparse
import asyncio
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from . import HARBOR_VERSION


def require_harbor():
    try:
        actual = version("harbor")
    except PackageNotFoundError:
        raise ValueError("Harbor is not installed in the current Python environment ({}). "
                         "Dependency installation must finish successfully before run/view: "
                         "python -m pip install -e .".format(sys.executable)) from None
    if actual != HARBOR_VERSION:
        raise ValueError("FlexBench requires harbor=={}; installed {}".format(HARBOR_VERSION, actual))


def export_tasks(selected, destination):
    from flexbench.__main__ import case_data
    destination.mkdir(parents=True, exist_ok=False)
    for case_id in selected:
        case, source, grader = case_data(case_id)
        task = destination / case_id
        (task / "environment").mkdir(parents=True)
        (task / "tests/graders").mkdir(parents=True)
        shutil.copytree(source / "fixture", task / "environment/fixture")
        shutil.copy2(source / "task.md", task / "instruction.md")
        shutil.copy2(grader, task / "tests/graders/grader.py")
        # Two graders compare immutable files with their original fixture.
        # Preserve their relative layout; these files stay outside workspace.
        shutil.copytree(source / "fixture", task / "tests/cases" / case_id / "fixture")
        (task / "tests/test.sh").write_text(
            '#!/bin/sh\nset -eu\nexec "${FLEXBENCH_PYTHON:-python3}" "$(dirname "$0")/graders/grader.py" '
            '"${FLEXBENCH_WORKSPACE:?Set FLEXBENCH_WORKSPACE to the candidate directory}"\n', encoding="utf-8")
        (task / "environment/local.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
        (task / "task.toml").write_text(
            'schema_version = "1.4"\n'
            '[metadata]\ncase_id = ' + json.dumps(case_id) + '\ncase_version = ' + str(case["version"]) +
            '\ncase_digest = ' + json.dumps(case["digest"]) + '\n'
            '[agent]\ntimeout_sec = 600\n'
            '[verifier]\nenvironment_mode = "shared"\ntimeout_sec = ' + str(case["grader_timeout_seconds"]) + '\n'
            '[environment]\nnetwork_mode = "public"\n', encoding="utf-8")
    return destination


def build_config(args, directory, profile):
    from harbor.models.job.config import JobConfig
    agents = []
    for model in args.models or [profile.get("model")]:
        pricing = profile.get("pricing_by_model", {}).get(model)
        if pricing is None and model == profile.get("model"):
            pricing = profile.get("pricing")
        agents.append({"import_path": "flexbench.harbor.gigacode:GigaCodeAgent", "model_name": model,
                       "override_timeout_sec": profile["timeout_seconds"],
                       "kwargs": {"executable": profile["executable"], "approval_mode": profile["approval_mode"],
                                  "allowed_tools": profile["allowed_tools"], "pricing": pricing}})
    return JobConfig.model_validate({
        "job_name": directory.name, "jobs_dir": str(directory.parent / "jobs"),
        "n_attempts": args.repeat, "n_concurrent_trials": args.concurrency,
        "retry": {"max_retries": 0}, "agents": agents,
        "environment": {"import_path": "flexbench.harbor.local:LocalEnvironment", "delete": False},
        "verifier": {"import_path": "flexbench.harbor.verifier:FlexBenchVerifier"},
        "datasets": [{"path": str(directory / "tasks")}],
        "extra_instructions": profile.get("instruction_snapshots", []),
    })


async def run_job(config):
    from harbor.job import Job
    from .report import write_report
    job = await Job.create(config)
    result = await job.run()
    write_report(result, job.job_dir)
    # Harbor rewards and infrastructure errors are independent dimensions.
    success = len(result.trial_results) == result.n_total_trials and all(
        trial.exception_info is None and trial.verifier_result is not None
        and (trial.verifier_result.rewards or {}).get("reward") == 1 for trial in result.trial_results)
    print("Results:", job.job_dir)
    print("Report:", job.job_dir / "flexbench-report.md")
    print("Summary:", job.job_dir / "flexbench-summary.json")
    return 0 if success else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Harbor + installed GigaCode, local execution without containers")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Launch GigaCode attempts concurrently through Harbor")
    run.add_argument("--profile", help="Native GigaCode profile (defaults to profiles/gigacode-command.json)")
    run.add_argument("--case", nargs="+", default=["all"])
    run.add_argument("--models", nargs="+")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--output", default="artifacts/harbor")
    run.add_argument("--dry-run", action="store_true", help="Freeze tasks and validate config without calling a model")
    view = commands.add_parser("view", help="Open Harbor's local results viewer")
    view.add_argument("--output", default="artifacts/harbor")
    view.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    try:
        require_harbor()
        if args.command == "view":
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            jobs = Path(args.output).resolve() / "jobs"
            jobs.mkdir(parents=True, exist_ok=True)
            return subprocess.call([sys.executable, "-m", "harbor.cli.main", "view",
                                    str(jobs), "--jobs", "--port", str(args.port)])
        from flexbench.__main__ import ROOT, case_data, profile_data, runtime
        if args.repeat < 1 or args.concurrency < 1:
            raise ValueError("repeat and concurrency must be positive")
        if args.models and (any(not m.strip() for m in args.models) or len(set(args.models)) != len(args.models)):
            raise ValueError("Select nonempty unique model IDs")
        ids = sorted(p.parent.name for p in (ROOT / "cases").glob("*/case.json"))
        selected = ids if args.case == ["all"] else args.case
        if len(set(selected)) != len(selected) or any(case not in ids for case in selected):
            raise ValueError("Select unique known case IDs, or all alone")
        profile = profile_data(args.profile or ROOT / "profiles/gigacode-command.json")
        if profile.get("adapter") != "gigacode" or profile["mode"] != "command":
            raise ValueError("Harbor run requires a native GigaCode command profile")
        runtimes = {case: runtime(case_data(case)[0]) for case in selected}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        directory = Path(args.output).resolve() / ("gigacode-" + stamp)
        export_tasks(selected, directory / "tasks")
        config = build_config(args, directory, profile)
        (directory / "config.json").write_text(config.model_dump_json(indent=2), encoding="utf-8")
        (directory / "inputs.json").write_text(json.dumps({"harbor_version": HARBOR_VERSION,
            "executor_version": profile["executor_version"], "runtime": runtimes,
            "profile_digest": profile["digest"], "isolation": "trusted-local-no-security-boundary"}, indent=2), encoding="utf-8")
        print("Config:", directory / "config.json", flush=True)
        print("Viewer: python -m flexbench harbor view --output " + str(Path(args.output).resolve()), flush=True)
        if args.dry_run:
            print("Validated; no model calls. Trials:", len(selected) * len(config.agents) * args.repeat)
            return 0
        return asyncio.run(run_job(config))
    except KeyboardInterrupt:
        print("Interrupted; inspect saved Harbor results before starting another run", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 2
