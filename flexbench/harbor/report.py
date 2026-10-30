"""Coverage-aware summary over Harbor's authoritative trial results."""

from collections import defaultdict
import json

from flexbench.metrics import efficiency


def aggregate_attempts(attempts):
    grouped = defaultdict(list)
    for attempt in attempts:
        grouped[attempt["model"]].append(attempt)
    models = [{"model": model, "functional_passes": sum(a["functional_pass"] == 1 for a in group),
               **efficiency(group)} for model, group in grouped.items()]
    return {"efficiency": efficiency(attempts), "models": models, "attempts": attempts}


def write_report(result, directory):
    attempts = []
    for trial in result.trial_results:
        context = trial.agent_result
        metadata = (context.metadata or {}) if context else {}
        rewards = (trial.verifier_result.rewards or {}) if trial.verifier_result else {}
        timing = trial.agent_execution
        seconds = ((timing.finished_at - timing.started_at).total_seconds()
                   if timing and timing.started_at and timing.finished_at else None)
        status = (trial.exception_info.exception_type if trial.exception_info else
                  "passed" if rewards.get("reward") == 1 else "failed")
        attempts.append({"trial": trial.trial_name, "case": trial.task_name,
                         "model": trial.config.agent.model_name,
                         "status": status, "functional_pass": rewards.get("functional_pass"),
                         "executor": {"seconds": seconds},
                         "execution_reason": metadata.get("execution", {}).get("reason"),
                         "executor_version": metadata.get("executor_version"),
                         "usage": metadata.get("usage"), "cost": metadata.get("cost"),
                         "reported_models": metadata.get("gigacode", {}).get("models", [])})
    summary = {"schema_version": 1, "kind": "harbor_job", "job_id": str(result.id),
               "isolation": "trusted-local-no-security-boundary",
               "n_total_trials": result.n_total_trials,
               "job_seconds": (result.finished_at - result.started_at).total_seconds()
                              if result.finished_at and result.started_at else None,
               **aggregate_attempts(attempts)}
    (directory / "flexbench-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = ["# FlexBench / Harbor", "", "Trusted local execution; no OS isolation. Open dev task set.", "",
            "Full success requires passing checks and normal executor completion.", "",
            "| Model | Attempts | Full success | Checks passed | Total tokens |",
            "|---|---:|---:|---:|---:|"]
    for model in summary["models"]:
        tokens = model["tokens"]["total_tokens"]
        total = str(tokens["total"]) if tokens["total"] is not None else "unknown ({}/{})".format(tokens["measured_runs"], tokens["attempts"])
        rows.append("| {} | {} | {} | {} | {} |".format(
            str(model["model"] or "CLI settings").replace("|", "\\|"), model["attempts"],
            model["successes"], model["functional_passes"], total))
    rows += ["", "Complete totals require measurements for every attempt. See flexbench-summary.json for coverage,",
             "timing, cost by currency, and individual failures. Unknown cost is never zero.", ""]
    (directory / "flexbench-report.md").write_text("\n".join(rows), encoding="utf-8")
    return summary
