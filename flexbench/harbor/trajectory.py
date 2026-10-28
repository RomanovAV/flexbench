"""ATIF view of recorded session totals; deliberately not a tool-trace converter."""

from harbor.models.trajectories.trajectory import Trajectory


def metrics_trajectory(metadata, model=None, version=None):
    usage = metadata.get("usage")
    if not usage or usage.get("complete") is not True:
        return None
    values = [usage.get(key) for key in ("input_tokens", "output_tokens", "cached_input_tokens")]
    if any(type(value) is not int or value < 0 for value in values) or values[2] > values[0]:
        return None
    cost = metadata.get("cost") or {}
    info = metadata.get("gigacode") or {}
    reported = info.get("models", [])
    # Attribute only a model actually reported by the CLI.
    reported_model = reported[0] if len(reported) == 1 else None
    model = reported_model or (model if not reported else None)
    data = {
        "agent": {"name": "gigacode", "version": version or metadata.get("executor_version") or "unknown",
                  "model_name": model},
        "session_id": info.get("session_id"),
        "steps": [{"step_id": 1, "source": "system", "message":
            "Итоговые измерения сессии GigaCode. Это сводка, а не пошаговая история работы агента. "
            "Исходные события CLI сохранены в Agent → executor.jsonl."}],
        "notes": "Session metrics projection only. The single informational step is not an agent/model call. "
                 "Native CLI events have not been converted to ATIF steps.",
        "final_metrics": {"total_prompt_tokens": values[0], "total_completion_tokens": values[1],
                          "total_cached_tokens": values[2],
                          "total_cost_usd": cost.get("amount") if cost.get("complete") is True and cost.get("currency") == "USD" else None},
        "extra": {"projection": "session-metrics-only", "source": usage.get("source"),
                  "cost_method": cost.get("method")},
    }
    return Trajectory.model_validate(data).to_json_dict()
