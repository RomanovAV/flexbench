"""GigaCode subprocess contract reused from the neighboring GigaFlex project.

See docs/GIGACODE_ADAPTER.md for source revision and accounting assumptions.
"""

import json
from pathlib import Path
import re
import shutil
import subprocess

from .metrics import normalize


APPROVAL_UNAVAILABLE = "requires user approval but cannot execute in non-interactive mode"
API_ERROR = re.compile(r"\A\s*\[?API Error:\s*\d{3}(?:\s+[^\r\n\]]*)?\]?\s*\Z", re.IGNORECASE)


def configure(profile):
    if profile["mode"] != "command":
        raise ValueError("The gigacode adapter requires mode: command")
    if profile.get("command"):
        raise ValueError("The gigacode adapter builds command argv; do not also set command")
    executable = profile.get("executable", "gigacode")
    if not isinstance(executable, str) or not executable:
        raise ValueError("GigaCode executable must be a nonempty string")
    resolved = shutil.which(executable)
    if resolved is None:
        raise ValueError("GigaCode executable not found: {}. Install/authenticate GigaCode on this machine or set executable.".format(executable))
    model = profile.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("GigaCode model must be a nonempty string or null (use CLI settings)")
    approval = profile.setdefault("approval_mode", "auto-edit")
    if approval not in ("default", "auto-edit"):
        raise ValueError("GigaCode benchmark approval_mode must be default or auto-edit; plan cannot execute tasks")
    allowed = profile.setdefault("allowed_tools", ["run_shell_command"])
    if not isinstance(allowed, list) or not all(isinstance(tool, str) and tool.strip() and not tool.startswith("-") for tool in allowed):
        raise ValueError("GigaCode allowed_tools must be a list of tool names")
    version = subprocess.run([resolved, "--version"], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=10)
    if version.returncode or not version.stdout.strip():
        raise ValueError("GigaCode --version failed; check the executable installation: "
                         + (version.stderr or version.stdout).strip())
    # Keep the executable's invocation path: some installations use symlink wrappers.
    profile["executable"] = str(Path(resolved).absolute())
    profile["executor_version"] = version.stdout.strip()


def argv(profile, task):
    prompt = ("Выполни задание ниже в текущей рабочей папке. Изменяй файлы здесь; "
              "не читай файлы вне рабочей папки. Выполни доступные проверки.\n\n" + task)
    command = [profile["executable"], "--approval-mode=" + profile["approval_mode"]]
    if profile["allowed_tools"]:
        command += ["--allowed-tools", *profile["allowed_tools"]]
    command += ["--output-format", "stream-json"]
    if profile.get("model"):
        command += ["--model", profile["model"]]
    # Keep options before the explicit prompt: GigaCode's query/array parsing is
    # order sensitive (documented and tested in the neighboring GigaFlex).
    return command + ["-p", prompt]


def nonnegative_int(value):
    return value if type(value) is int and value >= 0 else None


def process_output(run, profile, execution):
    """Use only the terminal result's session usage; never sum message snapshots."""
    path = run / "executor.jsonl"
    info = {"output_file": path.name, "telemetry_issues": [], "models": [],
            "event_types": [], "session_id": None, "result_events": 0}
    result = None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if isinstance(event_type, str) and event_type not in info["event_types"]:
                    info["event_types"].append(event_type)
                if isinstance(event.get("session_id"), str):
                    info["session_id"] = event["session_id"]
                model = None
                if event_type == "system":
                    model = event.get("model")
                elif event_type == "assistant" and isinstance(event.get("message"), dict):
                    model = event["message"].get("model")
                if isinstance(model, str) and model and model not in info["models"]:
                    info["models"].append(model)
                if event_type == "result":
                    info["result_events"] += 1
                    result = event
    except OSError:
        info["telemetry_issues"].append("GigaCode event stream unavailable")
    try:
        stderr = (run / "executor.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        stderr = ""
    result_text = result.get("result", "") if result else ""
    result_text = result_text if isinstance(result_text, str) else ""
    failure = None
    if APPROVAL_UNAVAILABLE in stderr or APPROVAL_UNAVAILABLE in result_text:
        failure = "approval_unavailable"
    elif API_ERROR.fullmatch(result_text) or API_ERROR.fullmatch(stderr):
        failure = "api_error"
    elif result and (result.get("error") or result.get("is_error") is True
                     or str(result.get("subtype", "")).startswith("error")):
        failure = "reported_error"
    elif info["result_events"] != 1:
        failure = "missing_result" if result is None else "ambiguous_result"
    if failure:
        info["failure"] = failure
        if execution["reason"] == "completed":
            execution["reason"] = failure
    if info["result_events"] != 1:
        info["telemetry_issues"].append("GigaCode needs exactly one final result event; see executor.jsonl and executor.log")
        return info, None
    stats = result.get("stats")
    if isinstance(stats, dict) and isinstance(stats.get("models"), dict) and stats["models"]:
        info["models"] = list(stats["models"])
    for key in ("duration_ms", "duration_api_ms"):
        info[key] = nonnegative_int(result.get(key))
    if profile.get("model") and info["models"] and profile["model"] not in info["models"]:
        info["telemetry_issues"].append("Configured GigaCode model differs from models reported by the CLI")
    usage = result.get("usage")
    if not isinstance(usage, dict):
        info["telemetry_issues"].append("GigaCode final result has no usage object")
        return info, None
    payload = {"schema_version": 1, "source": "gigacode stream-json final result.usage",
               "complete": execution["reason"] not in ("timeout", "launch_error"),
               "usage": {"input_tokens": usage.get("input_tokens"),
                         "output_tokens": usage.get("output_tokens"),
                         "cached_input_tokens": usage.get("cache_read_input_tokens"),
                         "total_tokens": usage.get("total_tokens")}}
    try:
        normalize(payload)
    except ValueError as error:
        info["telemetry_issues"].append("Invalid GigaCode final usage: " + str(error))
        return info, None
    return info, payload
