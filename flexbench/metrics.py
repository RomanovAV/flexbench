"""Portable executor measurements and coverage-aware efficiency aggregates."""

from collections import Counter
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import statistics


TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens")


def nonnegative(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(name + " must be a finite nonnegative number")
    return value


def label(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a nonempty string")
    return value


def currency(value):
    if not isinstance(value, str) or len(value) != 3 or not value.isascii() or not value.isalpha() or not value.isupper():
        raise ValueError("currency must be a three-letter uppercase code")
    return value


def validate_pricing(pricing):
    if not isinstance(pricing, dict):
        raise ValueError("pricing must be an object")
    currency(pricing.get("currency"))
    label(pricing.get("source"), "pricing.source")
    for key in ("input_per_million", "output_per_million", "cached_input_per_million"):
        nonnegative(pricing.get(key), "pricing." + key)
    return {key: pricing[key] for key in (
        "currency", "source", "input_per_million", "output_per_million", "cached_input_per_million")}


def normalize(payload, pricing=None):
    """Input tokens include cached input. One record covers the entire attempt."""
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("Measurements require schema_version: 1")
    source = label(payload.get("source"), "source")
    complete = payload.get("complete")
    if type(complete) is not bool:
        raise ValueError("Measurements require a boolean complete flag")
    usage, cost = payload.get("usage"), payload.get("cost")
    if usage is None and cost is None:
        raise ValueError("Measurements require usage or cost")
    if usage is not None:
        if not isinstance(usage, dict):
            raise ValueError("usage must be an object")
        for key in TOKEN_FIELDS:
            value = usage.get(key)
            if key in ("input_tokens", "output_tokens") or value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError(key + " must be a nonnegative integer")
        cached = usage.get("cached_input_tokens")
        if cached is not None and cached > usage["input_tokens"]:
            raise ValueError("cached_input_tokens exceeds input_tokens")
        total = usage["input_tokens"] + usage["output_tokens"]
        if usage.get("total_tokens") is not None and usage["total_tokens"] != total:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        usage = {key: usage.get(key) for key in TOKEN_FIELDS}
        usage.update(total_tokens=total, source=source, complete=complete)
    if cost is not None:
        if not isinstance(cost, dict):
            raise ValueError("cost must be an object")
        cost = {"amount": nonnegative(cost.get("amount"), "cost.amount"),
                "currency": currency(cost.get("currency")), "source": source,
                "method": "reported", "complete": complete}
    elif usage is not None and pricing is not None:
        pricing = validate_pricing(pricing)
        cached = usage["cached_input_tokens"]
        # Unknown cache usage is safe only when both input rates are identical.
        if cached is not None or pricing["cached_input_per_million"] == pricing["input_per_million"]:
            cached = cached or 0
            amount = ((usage["input_tokens"] - cached) * Decimal(str(pricing["input_per_million"]))
                      + cached * Decimal(str(pricing["cached_input_per_million"]))
                      + usage["output_tokens"] * Decimal(str(pricing["output_per_million"]))) / Decimal(1_000_000)
            cost = {"amount": nonnegative(float(amount), "calculated cost"), "currency": pricing["currency"],
                    "source": pricing["source"], "method": "estimated", "complete": complete,
                    "pricing": pricing}
    return usage, cost


def collect(path, pricing=None, origin="executor_adapter"):
    """Bad/missing telemetry must not erase the independent correctness result."""
    path = Path(path)
    usage, cost, evidence = None, None, None
    issues = []
    try:
        if path.is_symlink():
            raise ValueError("Measurements file must not be a symlink")
        raw = path.read_bytes()
        evidence = {"file": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "origin": origin}
        usage, cost = normalize(json.loads(raw), pricing)
    except FileNotFoundError:
        issues.append("Measurements file unavailable")
    except (ValueError, OSError, OverflowError) as error:
        issues.append("Invalid measurements: " + str(error))
    if usage is None:
        issues.append("Token usage unknown")
    elif not usage["complete"]:
        issues.append("Token usage is partial")
    if cost is None:
        issues.append("Cost unknown (provide reported cost or sufficient usage and pricing)")
    elif not cost["complete"]:
        issues.append("Cost is partial")
    return {"usage": usage, "cost": cost, "measurement_evidence": evidence,
            "measurement_issues": issues}


def coverage(values, attempts, complete=None):
    known = [value for value in values if value is not None]
    complete = complete if complete is not None else [value is not None for value in values]
    observed = float(sum(Decimal(str(v)) for v in known)) if known else None
    measured = sum(value is not None and flag for value, flag in zip(values, complete))
    return {"total": observed if measured == attempts else None, "observed_total": observed,
            "measured_runs": measured, "available_runs": len(known), "attempts": attempts}


def efficiency(results):
    attempts = len(results)
    successes = sum(r["status"] == "passed" for r in results)
    times = [(r.get("executor") or {}).get("seconds") for r in results]
    known_times = [t for t in times if t is not None]
    usage = [r.get("usage") or {} for r in results]
    tokens = {}
    for key in TOKEN_FIELDS:
        values = [u.get(key) for u in usage]
        stats = coverage(values, attempts, [u.get("complete", False) for u in usage])
        # Token counts are exact integers, even above float's integer precision.
        stats["observed_total"] = sum(v for v in values if v is not None) if any(v is not None for v in values) else None
        stats["total"] = stats["observed_total"] if stats["measured_runs"] == attempts else None
        tokens[key] = stats
    costs = [r.get("cost") or {} for r in results]
    currencies = sorted({c["currency"] for c in costs if c})
    money = {}
    for code in currencies:
        values = [c.get("amount") if c.get("currency") == code else None for c in costs]
        stats = coverage(values, attempts, [c.get("complete", False) for c in costs])
        stats["per_successful_attempt"] = stats["total"] / successes if successes and stats["total"] is not None else None
        stats["methods"] = dict(Counter(c["method"] for c in costs if c.get("currency") == code))
        money[code] = stats
    return {"attempts": attempts, "successes": successes, "pass_rate": successes / attempts if attempts else None,
            "status_counts": dict(Counter(r["status"] for r in results)),
            "executor_seconds": {**coverage(times, attempts),
                                 "median_observed": statistics.median(known_times) if known_times else None},
            "tokens": tokens, "cost_by_currency": money,
            "cost_measured_runs": sum(bool(c) and c.get("complete", False) for c in costs)}
