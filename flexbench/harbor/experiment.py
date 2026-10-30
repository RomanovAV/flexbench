"""Paired, serial harness experiments over frozen Harbor jobs on one model."""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import uuid

from flexbench.__main__ import ROOT, case_data, profile_data, read, runtime, save
from flexbench.gigacode import DEFAULT_PROMPT_PREFIX
from flexbench.metrics import efficiency
from .cli import build_config, export_tasks
from . import HARBOR_VERSION

SIDES = ("baseline", "candidate")
PROFILE_FIELDS = {"id", "version", "executor", "executor_version", "model", "mode", "adapter",
                  "executable", "approval_mode", "allowed_tools", "timeout_seconds", "instruction_files",
                  "prompt_prefix_file", "pricing", "pricing_by_model"}
VARIABLE_FIELDS = ("instruction_snapshots", "prompt_prefix", "approval_mode", "allowed_tools")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_keys(value, allowed, label):
    if not isinstance(value, dict):
        raise ValueError(label + " must be an object")
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(label + ": unsupported fields: " + ", ".join(sorted(unknown)))


def plan(cases, repeat, seed):
    """Keep each pair adjacent; flip its first variant on subsequent repeats."""
    rng = random.Random(seed)
    first = {case: rng.randrange(2) for case in sorted(cases)}
    rows = []
    for repetition in range(1, repeat + 1):
        ordered = sorted(cases)
        rng.shuffle(ordered)
        for case in ordered:
            sides = SIDES if (first[case] + repetition) % 2 else SIDES[::-1]
            for side in sides:
                rows.append({"index": len(rows) + 1, "case": case, "repeat": repetition, "variant": side,
                             "job_name": "attempt-{:04d}-{}".format(len(rows) + 1, side)})
    return rows


def prepare_experiment(args):
    spec_path = Path(args.spec).resolve()
    spec = read(spec_path)
    validate_keys(spec, {"schema_version", "name", "hypothesis", "baseline", "candidate", "model",
                         "cases", "repeat", "seed"}, "Experiment")
    if type(spec.get("schema_version")) is not int or spec["schema_version"] != 1:
        raise ValueError("Experiment requires schema_version: 1")
    for field in ("name", "hypothesis", *SIDES):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise ValueError("Experiment requires nonempty " + field)
    model = args.model if args.model is not None else spec.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Choose one explicit model with --model or experiment.model")
    repeat = args.repeat if args.repeat is not None else spec.get("repeat", 3)
    seed = spec.get("seed", 0)
    if type(repeat) is not int or not 1 <= repeat <= 100 or type(seed) is not int:
        raise ValueError("repeat must be an integer in 1..100 and seed must be an integer")
    cases = args.case if args.case is not None else spec.get("cases", ["all"])
    known = sorted(p.parent.name for p in (ROOT / "cases").glob("*/case.json"))
    if cases == ["all"]:
        cases = known
    if (not isinstance(cases, list) or not cases or any(not isinstance(c, str) or c not in known for c in cases)
            or len(cases) != len(set(cases))):
        raise ValueError("Choose unique known cases, or all alone")
    profiles = {}
    for side in SIDES:
        path = (spec_path.parent / spec[side]).resolve()
        raw = read(path)
        validate_keys(raw, PROFILE_FIELDS, side + " profile (native system prompt/rules are not supported)")
        if raw.get("model") not in (None, model):
            raise ValueError(side + " profile specifies a different model")
        profile = profile_data(path, model=model)
        profile.setdefault("prompt_prefix", DEFAULT_PROMPT_PREFIX)
        # Effective comparison excludes display IDs, source paths and unused tariffs.
        profile["effective_digest"] = sha(json.dumps({key: profile.get(key) for key in (
            *VARIABLE_FIELDS, "model", "executable", "executor_version", "timeout_seconds", "pricing")},
            sort_keys=True, ensure_ascii=False).encode())
        profiles[side] = profile
    for field in ("executable", "executor_version", "timeout_seconds", "pricing"):
        if profiles["baseline"].get(field) != profiles["candidate"].get(field):
            raise ValueError("Controlled experiment requires identical " + field)
    changes = {key: {side: profiles[side].get(key) for side in SIDES} for key in VARIABLE_FIELDS
               if profiles["baseline"].get(key) != profiles["candidate"].get(key)}
    if not changes:
        raise ValueError("Profiles have identical effective harness settings")
    versions = {case: runtime(case_data(case)[0]) for case in cases}
    directory = Path(args.output).resolve() / ("experiment-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                                               + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    export_tasks(cases, directory / "tasks")
    for folder in ("profiles", "configs", "outcomes"):
        (directory / folder).mkdir()
    for side, profile in profiles.items():
        save(directory / "profiles" / (side + ".json"), profile)
    save(directory / "spec.json", spec)
    schedule = plan(cases, repeat, seed)
    jobs = Path(args.jobs_dir).resolve() if getattr(args, "jobs_dir", None) else directory / "jobs"
    for slot in schedule:
        if getattr(args, "jobs_dir", None):
            slot["job_name"] = directory.name + "-" + slot["job_name"]
        config = build_config(argparse.Namespace(models=[model], repeat=1, concurrency=1),
                              directory / slot["job_name"], profiles[slot["variant"]])
        config.jobs_dir = jobs
        config.datasets[0].path = directory / "tasks"
        config.datasets[0].task_names = [slot["case"]]
        (directory / "configs" / (slot["job_name"] + ".json")).write_text(config.model_dump_json(indent=2), encoding="utf-8")
    frozen = {p.relative_to(directory).as_posix(): sha(p.read_bytes())
              for p in sorted(directory.rglob("*")) if p.is_file()}
    manifest = {"schema_version": 1, "kind": "harness_experiment", "name": spec["name"],
                "hypothesis": spec["hypothesis"], "model": model, "repeat": repeat, "seed": seed,
                "harbor_version": HARBOR_VERSION, "concurrency": 1, "schedule": schedule,
                "jobs_relative_path": os.path.relpath(jobs, directory),
                "cases": {case: read(directory / "tasks" / case / "environment/local.json") for case in cases},
                "runtime": versions, "profiles": profiles, "changes": changes, "frozen_files": frozen,
                "instruction_delivery": "user prompt (-p), prefix and appended instructions; native settings inherited",
                "limitations": ["Open dev suite; repeats are not independent new tasks.",
                                "Global CLI settings, native rules/system prompt and server model revision are unverified.",
                                "Local execution is not a security sandbox.",
                                "Serial adjacent pairs; first variant alternates across repeats. No automatic retries."]}
    save(directory / "experiment.json", manifest)
    save(directory / "state.json", {"state": "prepared"})
    write_experiment_report(directory)
    return directory


def check_frozen(directory, manifest):
    for name, digest in manifest["frozen_files"].items():
        path = directory / name
        if not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink() or sha(path.read_bytes()) != digest:
            raise ValueError("Frozen experiment input changed: " + name)


async def run_experiment(directory):
    from harbor.job import Job
    from harbor.models.job.config import JobConfig
    from .report import write_report
    manifest = read(directory / "experiment.json")
    if read(directory / "state.json")["state"] != "prepared":
        raise ValueError("Experiment already started; create a new experiment to run again")
    check_frozen(directory, manifest)
    save(directory / "state.json", {"state": "running"})
    state = "interrupted"
    try:
        for slot in manifest["schedule"]:
            check_frozen(directory, manifest)
            name = slot["job_name"]
            print("[{}/{}] {} / {} / repeat {}".format(slot["index"], len(manifest["schedule"]),
                                                     slot["variant"], slot["case"], slot["repeat"]), flush=True)
            path = directory / "outcomes" / (name + ".json")
            save(path, {"state": "running"})
            try:
                config = JobConfig.model_validate_json((directory / "configs" / (name + ".json")).read_text())
                job = await Job.create(config)
                result = await job.run()
                write_report(result, job.job_dir)
                save(path, {"state": "finished"})
            except asyncio.CancelledError:
                save(path, {"state": "interrupted"})
                raise
            except Exception as error:
                save(path, {"state": "runner_error", "error": type(error).__name__ + ": " + str(error)})
            write_experiment_report(directory)
        state = "finished"
    finally:
        save(directory / "state.json", {"state": state})
        summary = write_experiment_report(directory)
        print("Report:", directory / "report.md", flush=True)
    if not summary["complete"] or not summary["comparison_valid"]:
        return 2
    return 0 if all(v["successes"] == v["attempts"] for v in summary["variants"].values()) else 1


def read_attempt(directory, slot, manifest):
    from harbor.models.job.result import JobResult
    from harbor.models.trial.result import TrialResult
    from .report import write_report
    name = slot["job_name"]
    outcome = directory / "outcomes" / (name + ".json")
    state = read(outcome) if outcome.exists() else {"state": "not_run"}
    row = {**slot, "status": state["state"], "functional_pass": None, "executor": {},
           "usage": None, "cost": None, "checks": [], "reported_models": [],
           "finished": state["state"] == "runner_error", "evidence_issues": []}
    if "error" in state:
        row["error"] = state["error"]
    result_path = directory / manifest.get("jobs_relative_path", "jobs") / name / "result.json"
    if not result_path.exists():
        return row
    result = JobResult.model_validate_json(result_path.read_text())
    if not result.finished_at:
        return row
    # Harbor persists aggregate job statistics without embedding trial_results.
    # Read authoritative per-trial records, also after an interrupted worker.
    result.trial_results = [TrialResult.model_validate_json(p.read_text())
                            for p in sorted(result_path.parent.glob("*/result.json"))]
    if not result.trial_results:
        row["evidence_issues"].append("Finished job has no saved trial result")
        return row
    if result.n_total_trials != 1 or len(result.trial_results) != 1:
        raise ValueError("Expected one trial for " + name)
    trial = result.trial_results[0]
    if not trial.finished_at:
        return row
    if trial.task_name != slot["case"] or trial.config.agent.model_name != manifest["model"]:
        raise ValueError("Result does not match scheduled case/model: " + name)
    attempt = write_report(result, result_path.parent)["attempts"][0]
    row.update(attempt, finished=True)
    trial_dir = result_path.parent / trial.trial_name
    if not trial_dir.resolve().is_relative_to(result_path.parent.resolve()):
        raise ValueError("Invalid trial path")
    row["evidence_path"] = Path(os.path.relpath(trial_dir, directory)).as_posix()
    grader = trial_dir / "verifier/grader.json"
    if grader.exists():
        try:
            checks = read(grader).get("checks")
            if not isinstance(checks, list) or not checks or any(
                not isinstance(c, dict) or not isinstance(c.get("id"), str) or type(c.get("passed")) is not bool
                for c in checks
            ):
                raise ValueError("Invalid grader checks")
            row["checks"] = [{"id": c["id"], "passed": c["passed"]} for c in checks]
        except (ValueError, OSError, AttributeError) as error:
            row["grading_issue"] = str(error)
    profile = manifest["profiles"][slot["variant"]]
    if row["reported_models"] != [manifest["model"]]:
        row["evidence_issues"].append("Reported model missing or different from requested model")
    if row["executor_version"] != profile["executor_version"]:
        row["evidence_issues"].append("Executor version missing or changed since preparation")
    from flexbench.gigacode import argv
    instruction = (directory / "tasks" / slot["case"] / "instruction.md").read_text()
    instruction = "\n\n".join([instruction, *profile["instruction_snapshots"]])
    invocation = trial_dir / "agent/invocation.json"
    if not invocation.exists() or read(invocation).get("argv") != argv(profile, instruction):
        row["evidence_issues"].append("Dispatched prompt/settings could not be verified")
    return row


def group_stats(rows):
    stats = efficiency(rows)
    stats["finished"] = sum(r["finished"] for r in rows)
    stats["complete"] = stats["finished"] == len(rows)
    if not stats["complete"]:
        stats["pass_rate"] = None
    stats["functional_passes"] = sum(r["functional_pass"] == 1 for r in rows)
    stats["functional_measured"] = sum(r["functional_pass"] is not None for r in rows)
    stats["execution_reasons"] = dict(Counter(r.get("execution_reason") or r["status"] for r in rows))
    stats["failed_checks"] = dict(Counter(c["id"] for r in rows for c in r["checks"] if not c["passed"]))
    return stats


def write_experiment_report(directory):
    manifest = read(directory / "experiment.json")
    check_frozen(directory, manifest)
    attempts = [read_attempt(directory, slot, manifest) for slot in manifest["schedule"]]
    complete = all(r["finished"] for r in attempts)
    issues = [{"job_name": r["job_name"], "issues": r["evidence_issues"]} for r in attempts if r["evidence_issues"]]
    valid = complete and not issues and all(r["status"] != "runner_error" for r in attempts)
    variants = {side: group_stats([r for r in attempts if r["variant"] == side]) for side in SIDES}
    cases = []
    for case in manifest["cases"]:
        groups = {side: group_stats([r for r in attempts if r["variant"] == side and r["case"] == case]) for side in SIDES}
        delta = (100 * (groups["candidate"]["pass_rate"] - groups["baseline"]["pass_rate"])) if valid else None
        cases.append({"case": case, "variants": groups, "delta_percentage_points": delta})
    pairs = []
    for case in manifest["cases"]:
        for repetition in range(1, manifest["repeat"] + 1):
            pair = {r["variant"]: r for r in attempts if r["case"] == case and r["repeat"] == repetition}
            outcomes = {side: pair[side]["status"] == "passed" if pair[side]["finished"] else None for side in SIDES}
            pairs.append({"case": case, "repeat": repetition, "success": outcomes,
                          "change": (int(outcomes["candidate"]) - int(outcomes["baseline"]))
                          if valid else None})
    delta = 100 * (variants["candidate"]["pass_rate"] - variants["baseline"]["pass_rate"]) if valid else None
    summary = {"schema_version": 1, "kind": "harness_comparison", "name": manifest["name"],
               "model": manifest["model"], "complete": complete, "comparison_valid": valid,
               "changes": manifest["changes"], "variants": variants, "cases": cases, "pairs": pairs,
               "delta_percentage_points": delta, "attempts": attempts, "evidence_issues": issues,
               "limitations": manifest["limitations"]}
    save(directory / "summary.json", summary)
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    def number(value):
        return "unknown" if value is None else "{:g}".format(value)
    lines = ["# " + cell(manifest["name"]), "", cell(manifest["hypothesis"]), "",
             "Model: **{}**. Finished: **{}/{}**. Comparison: **{}**.".format(cell(manifest["model"]),
                 sum(r["finished"] for r in attempts), len(attempts), "available" if valid else "incomplete / unverified"),
             "", "Changed settings: " + ", ".join(manifest["changes"]) + ". Full diff: [experiment.json](experiment.json).",
             "", "| Variant | Full success | Checks passed / measured | Seconds, all attempts | Tokens, all attempts |",
             "|---|---:|---:|---:|---:|"]
    for side, stats in variants.items():
        tokens = stats["tokens"]["total_tokens"]
        lines.append("| {} | {}/{} | {} / {} | {} | {} (coverage {}/{}) |".format(
            side, stats["successes"], stats["attempts"], stats["functional_passes"], stats["functional_measured"],
            number(stats["executor_seconds"]["total"]), number(tokens["total"]), tokens["measured_runs"], tokens["attempts"]))
    lines += ["", "Candidate − baseline: **{} percentage points**.".format(number(delta)), "",
              "| Case | Baseline | Candidate | Delta, pp |", "|---|---:|---:|---:|"]
    for item in cases:
        b, c = (item["variants"][side] for side in SIDES)
        lines.append("| {} | {}/{} | {}/{} | {} |".format(cell(item["case"]), b["successes"], b["attempts"],
                     c["successes"], c["attempts"], number(item["delta_percentage_points"])))
    lines += ["", "## Attempts", "", "| Case / repeat | Variant | Status | Functional | Failed checks | Evidence |",
              "|---|---|---|---|---|---|"]
    for row in attempts:
        evidence = "[trial]({})".format(row["evidence_path"]) if "evidence_path" in row else "—"
        lines.append("| {} / {} | {} | {} | {} | {} | {} |".format(cell(row["case"]), row["repeat"],
            row["variant"], cell(row["status"]), row["functional_pass"] if row["functional_pass"] is not None else "unknown",
            cell(row.get("grading_issue") or ", ".join(c["id"] for c in row["checks"] if not c["passed"])), evidence))
    lines += ["", "## Limits", "", *["- " + note for note in manifest["limitations"]],
              "- Unknown measurements are not zero. Partial runs have no comparison delta.",
              "- Token/time coverage, costs by currency, paired outcomes and diagnostics: [summary.json](summary.json)."]
    for issue in issues:
        lines.append("- {}: {}".format(cell(issue["job_name"]), cell("; ".join(issue["issues"]))))
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
