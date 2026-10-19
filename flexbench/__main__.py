"""Standard-library pilot runner. Local execution is not a security sandbox."""

import argparse
import copy
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import shlex
import signal
import statistics
import subprocess
import sys
import time
import uuid

from .metrics import collect, efficiency, normalize, validate_pricing

ROOT = Path(__file__).resolve().parents[1]
OBSERVATION_KINDS = ("note", "human_intervention", "intervention_summary", "verification", "obstacle", "context_loss", "executor_claim")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fingerprint(paths):
    pieces = []
    for name, path in sorted(paths):
        if path.is_symlink():
            raise ValueError("Symlinks are not supported: " + str(path))
        pieces.append(name.encode() + b"\0" + path.read_bytes())
    return digest(b"\0".join(pieces))


def case_data(case_id):
    folder = ROOT / "cases" / case_id
    if folder.parent != ROOT / "cases" or not (folder / "case.json").is_file():
        raise ValueError("Unknown case: " + case_id)
    case = read(folder / "case.json")
    if case["id"] != case_id:
        raise ValueError("Case ID mismatch")
    if type(case.get("version")) is not int or case["version"] < 1:
        raise ValueError("Case version must be a positive integer")
    if not (folder / "task.md").is_file() or not (folder / "fixture").is_dir():
        raise ValueError("Case requires task.md and fixture/")
    if any(p.is_symlink() for p in folder.rglob("*")):
        raise ValueError("Case fixtures must not contain symlinks")
    grader = ROOT / "graders" / case["grader"]
    if grader.parent != ROOT / "graders" or not grader.is_file():
        raise ValueError("Invalid grader")
    paths = [(str(p.relative_to(folder)), p) for p in folder.rglob("*") if p.is_file()]
    paths.append(("grader", grader))
    case["digest"] = fingerprint(paths)
    return case, folder, grader


def profile_data(path, model=None):
    path = Path(path).resolve()
    profile = read(path)
    if model is not None:
        if profile.get("adapter") != "gigacode" or profile.get("mode") != "command":
            raise ValueError("--models requires a command profile with adapter: gigacode")
        original_model = profile.get("model")
        profile["model"] = model
        # Do not silently apply a default model's tariff to another model.
        if original_model != model:
            profile.pop("pricing", None)
    model_prices = profile.get("pricing_by_model", {})
    if not isinstance(model_prices, dict) or not all(isinstance(key, str) and key.strip() for key in model_prices):
        raise ValueError("pricing_by_model must map model IDs to pricing objects")
    for price in model_prices.values():
        validate_pricing(price)
    if isinstance(profile.get("model"), str) and profile["model"] in model_prices:
        profile["pricing"] = model_prices[profile["model"]]
    if profile.get("mode") not in ("manual", "command"):
        raise ValueError("Profile mode must be manual or command")
    adapter = profile.get("adapter")
    if adapter is not None and adapter != "gigacode":
        raise ValueError("Unknown executor adapter: " + str(adapter))
    for key in ("id", "executor"):
        if not isinstance(profile.get(key), str) or not profile[key]:
            raise ValueError("Missing profile " + key)
    timeout = profile.get("timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    command = profile.get("command", [])
    if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command):
        raise ValueError("command must be an argv array")
    if profile["mode"] == "command" and not command and adapter is None:
        raise ValueError("Configure the verified executor command first")
    if "pricing" in profile:
        profile["pricing"] = validate_pricing(profile["pricing"])
    if adapter == "gigacode":
        from .gigacode import configure
        configure(profile)
    contents = []
    for filename in profile.get("instruction_files", []):
        contents.append((path.parent / filename).resolve().read_text(encoding="utf-8"))
    profile["instruction_snapshots"] = contents
    profile["digest"] = digest(json.dumps(profile, sort_keys=True).encode())
    return profile


def runtime(case):
    versions = {"python": sys.version.split()[0]}
    for program in case["requires"]:
        if not shutil.which(program):
            raise ValueError("Required executable not found: " + program)
        probe = subprocess.run([program, "-version" if program in ("java", "javac") else "--version"],
                               capture_output=True, text=True, timeout=5)
        if probe.returncode:
            raise ValueError("Runtime probe failed: " + program)
        versions[program] = (probe.stdout + probe.stderr).splitlines()[0]
    return versions


def prepare(case_id, profile_path, output, repeat=1, profile_snapshot=None):
    case, folder, _ = case_data(case_id)
    profile = copy.deepcopy(profile_snapshot) if profile_snapshot is not None else profile_data(profile_path)
    versions = runtime(case)
    if not shutil.which("git"):
        raise ValueError("Git is required to create a fixture repository")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:10]
    run = Path(output).resolve() / run_id
    run.mkdir(parents=True, exist_ok=False)
    workspace = run / "workspace"
    shutil.copytree(folder / "fixture", workspace)
    task = (folder / "task.md").read_text(encoding="utf-8")
    if profile["instruction_snapshots"]:
        task += "\n\n## Дополнительные инструкции этого прогона\n\n" + "\n\n".join(profile["instruction_snapshots"])
    (workspace / "TASK.md").write_text(task, encoding="utf-8")
    git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
           "-c", "user.name=FlexBench Fixture", "-c", "user.email=fixture@example.invalid", "-C", str(workspace)]

    def fixture_git(arguments):
        completed = subprocess.run(git + arguments, capture_output=True, text=True)
        if completed.returncode:
            diagnostic = (completed.stderr or completed.stdout or "No diagnostic output").strip()
            raise ValueError("Fixture git {} failed (exit {}): {}".format(
                shlex.join(arguments), completed.returncode, diagnostic))
        return completed.stdout.strip()

    # Avoid `git init -b`, unavailable in older Git installations. Set the
    # unborn branch explicitly so user defaults still cannot affect fixtures.
    for arguments in (["init"], ["symbolic-ref", "HEAD", "refs/heads/main"],
                      ["add", "."], ["commit", "-m", "Initial benchmark fixture"]):
        fixture_git(arguments)
    initial_commit = fixture_git(["rev-parse", "HEAD"])
    record = {
        "schema_version": 1, "run_id": run_id, "repeat": repeat, "case": case, "profile": profile,
        "runtime": versions, "task_digest": digest(task.encode()),
        "created_at": datetime.now(timezone.utc).isoformat(), "state": "prepared",
        "initial_commit": initial_commit,
        "executor": None, "isolation": "trusted-local-no-security-boundary",
        "instruction_delivery": "TASK.md user-level instructions; native rules/system prompt not configured",
    }
    save(run / "run.json", record)
    return run


def execute(argv, cwd, log_path, timeout, stderr_path=None, on_tick=None):
    """Bound the complete process group, including descendants on timeout/exit."""
    start = time.monotonic()
    try:
        errors = Path(stderr_path).open("wb") if stderr_path is not None else nullcontext(subprocess.STDOUT)
        with errors as stderr, Path(log_path).open("wb") as log:
            process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=stderr,
                                       start_new_session=True)
            try:
                while True:
                    remaining = timeout - (time.monotonic() - start)
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    if on_tick is not None:
                        on_tick(time.monotonic() - start)
                    try:
                        code = process.wait(timeout=min(1, remaining) if on_tick is not None else remaining)
                        break
                    except subprocess.TimeoutExpired:
                        if on_tick is None:
                            raise
                if on_tick is not None:
                    on_tick(time.monotonic() - start)
                reason = "completed" if code == 0 else "nonzero_exit"
            except subprocess.TimeoutExpired:
                reason, code = "timeout", None
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        return {"reason": reason, "exit_code": code, "seconds": time.monotonic() - start}
    except OSError as error:
        return {"reason": "launch_error", "exit_code": None, "seconds": time.monotonic() - start, "error": str(error)}


def run_executor(run, on_tick=None):
    record = read(run / "run.json")
    if record["profile"]["mode"] != "command":
        raise ValueError("Manual profile: use prepare, solve in its workspace, then grade")
    if record["state"] != "prepared":
        raise ValueError("This attempt was already started; create a new attempt")
    record["state"] = "running"
    save(run / "run.json", record)
    profile = record["profile"]
    if profile.get("adapter") == "gigacode":
        from .gigacode import argv, process_output
        command = argv(profile, (run / "workspace/TASK.md").read_text(encoding="utf-8"))
        record["executor"] = execute(command, run / "workspace", run / "executor.jsonl", profile["timeout_seconds"],
                                     stderr_path=run / "executor.log", on_tick=on_tick)
        info, payload = process_output(run, profile, record["executor"])
        record["adapter_info"] = info
        if payload is not None:
            save(run / "measurements.json", payload)
            pricing = profile.get("pricing")
            if len(info["models"]) > 1 or (profile.get("model") and info["models"] and profile["model"] not in info["models"]):
                pricing = None
                info["telemetry_issues"].append("Single-model pricing cannot be applied to multiple or unexpected reported models")
            measurements = collect(run / "measurements.json", pricing, "gigacode_final_result")
        else:
            measurements = {"usage": None, "cost": None, "measurement_evidence": None,
                            "measurement_issues": ["Token usage unknown", "Cost unknown"]}
        measurements["measurement_issues"].extend(info["telemetry_issues"])
        record["measurements"] = measurements
    else:
        replacements = {"{workspace}": str(run / "workspace"), "{task_file}": str(run / "workspace/TASK.md"),
                        "{measurements_file}": str(run / "measurements.json")}
        command = profile["command"]
        for key, value in replacements.items():
            command = [arg.replace(key, value) for arg in command]
        record["executor"] = execute(command, run / "workspace", run / "executor.log", profile["timeout_seconds"], on_tick=on_tick)
        record["measurements"] = collect(run / "measurements.json", profile.get("pricing"))
    record["state"] = "executed"
    save(run / "run.json", record)


def writable_attempt(run):
    run = Path(run).resolve()
    record = read(run / "run.json")
    if (run / "result.json").exists() or record["state"] not in ("prepared","executed"):
        raise ValueError("Observations/logs must be attached before grading; the result is sealed or the run is active")
    return run


def observe(run, kind, text):
    run = writable_attempt(run)
    if kind not in OBSERVATION_KINDS or not isinstance(text,str) or not text.strip():
        raise ValueError("Observation needs a supported kind and nonempty text")
    event = {"timestamp":datetime.now(timezone.utc).isoformat(),"kind":kind,
             "text":text,"source":"operator_reported"}
    with (run / "observations.jsonl").open("a",encoding="utf-8") as target:
        target.write(json.dumps(event,ensure_ascii=False)+"\n")


def attach_log(run, source):
    run = writable_attempt(run)
    destination = run / "session.log"
    if destination.exists():
        raise ValueError("Session log already exists; it will not be overwritten")
    source = Path(source)
    if not source.is_file():
        raise ValueError("Session log must be an existing file")
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        shutil.copyfileobj(incoming,outgoing)
    return destination


def attach_metrics(run, source):
    run = writable_attempt(run)
    record = read(run / "run.json")
    if record["profile"]["mode"] != "manual":
        raise ValueError("Command measurements are captured automatically at executor exit")
    raw = Path(source).read_bytes()
    normalize(json.loads(raw), record["profile"].get("pricing"))
    destination = run / "measurements.json"
    with destination.open("xb") as target:
        target.write(raw)
    return destination


def grade(run, on_tick=None):
    run = Path(run).resolve()
    if (run / "result.json").exists():
        raise ValueError("Result is sealed; create another attempt instead of overwriting")
    record = read(run / "run.json")
    if record["state"] not in ("prepared", "executed"):
        raise ValueError("Run is incomplete/interrupted; inspect it and create a new attempt")
    if record["profile"]["mode"] == "command" and record["state"] != "executed":
        raise ValueError("Command profile must execute before grading")
    case, _, grader = case_data(record["case"]["id"])
    if case["digest"] != record["case"]["digest"]:
        raise ValueError("Case/grader changed since preparation; create a fresh attempt")
    # Reject symlinks in the candidate tree; this is hygiene, not sandboxing.
    if any(p.is_symlink() for p in (run / "workspace").rglob("*")):
        raise ValueError("Candidate workspace contains a symlink")
    observation_file = run / "observations.jsonl"
    observations = [json.loads(line) for line in observation_file.read_text(encoding="utf-8").splitlines() if line.strip()] if observation_file.exists() else []
    if any(e.get("kind") not in OBSERVATION_KINDS or not isinstance(e.get("text"),str) for e in observations):
        raise ValueError("Invalid observations.jsonl")
    record["observations"] = observations
    record["log_evidence"] = [{"file":name,"sha256":digest((run / name).read_bytes()),"source":source}
                              for name,source in (("executor.log","runner_stderr" if record["profile"].get("adapter") == "gigacode" else "runner_stdout"),
                                                  ("executor.jsonl","runner_json"),("session.log","operator_export"))
                              if (run / name).is_file()]
    record["state"] = "grading"
    save(run / "run.json", record)
    snapshot = run / "candidate"
    shutil.copytree(run / "workspace", snapshot, ignore=shutil.ignore_patterns(".git"))
    files = [(str(p.relative_to(snapshot)),p) for p in snapshot.rglob("*") if p.is_file()]
    record["candidate_digest"] = fingerprint(files)
    patch = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(run / "workspace"),
                            "diff", "--no-ext-diff", "--no-textconv", record["initial_commit"], "--"],
                           capture_output=True, timeout=10)
    (run / "changes.patch").write_bytes(patch.stdout)
    record["patch_available"] = patch.returncode == 0
    record["state"] = "grading"
    save(run / "run.json", record)
    check = execute([sys.executable, str(grader), str(snapshot)], ROOT,
                    run / "grader.log", case["grader_timeout_seconds"], on_tick=on_tick)
    evaluation, error = None, None
    if check["reason"] == "completed":
        try:
            evaluation = read(run / "grader.log")
            checks = evaluation["checks"]
            if not checks or not all(isinstance(c["id"], str) and type(c["passed"]) is bool for c in checks):
                raise ValueError("Invalid check records")
            if len({c["id"] for c in checks}) != len(checks):
                raise ValueError("Duplicate check IDs")
        except (ValueError, KeyError, TypeError) as exc:
            error, evaluation = str(exc), None
    else:
        error = check["reason"]
    after = [(str(p.relative_to(snapshot)),p) for p in snapshot.rglob("*") if p.is_file()]
    if fingerprint(after) != record["candidate_digest"]:
        error, evaluation = "Candidate changed during grading", None
    functional_pass = all(c["passed"] for c in evaluation["checks"]) if evaluation else None
    executor = record["executor"]
    completed = executor is not None and executor["reason"] == "completed"
    manual = record["profile"]["mode"] == "manual"
    status = "grader_error" if evaluation is None else (
        "passed" if functional_pass else "acceptance_failure")
    if executor and not completed:
        status = "executor_" + executor["reason"]
    measurements = (collect(run / "measurements.json", record["profile"].get("pricing"), "operator_reported")
                    if manual else record.get("measurements") or collect(run / "measurements.json", record["profile"].get("pricing")))
    result = {
        **record, "state": "finished", "status": status, "functional_pass": functional_pass,
        "success_within_time_budget": None if manual else completed and functional_pass is True,
        "evaluation": evaluation, "grader_error": error, "grader_seconds": check["seconds"],
        **measurements,
        "measurement_limitations": measurements["measurement_issues"] + ["No protected grading sandbox"]
            + (["Manual execution duration, external configuration and interventions are unverified"] if manual else []),
    }
    save(run / "result.json", result)
    record["state"] = "finished"
    save(run / "run.json", record)
    return result


def result_files(directory):
    directory = Path(directory)
    if directory.is_file():
        return [directory]
    runs = [directory] if (directory / "run.json").exists() else [p.parent for p in directory.glob("*/run.json")]
    unfinished = [str(run) for run in runs if not (run / "result.json").is_file()]
    if unfinished:
        raise ValueError("Unfinished runs must not be silently excluded: " + ", ".join(unfinished))
    return sorted(run / "result.json" for run in runs)


def manual_batch(case_ids, profile, output, repeats):
    folder = Path(output).resolve() / ("manual-" + uuid.uuid4().hex[:10])
    folder.mkdir(parents=True)
    lines = ["# FlexBench: прогон одного профиля", "",
             "Для каждой попытки открой workspace в новом сеансе исполнителя и передай: «Выполни TASK.md в этой рабочей папке».",
             "После остановки исполнителя приложи журнал через attach-log, сведения о вмешательствах через observe.",
             "Если есть usage/стоимость за весь сеанс, импортируй их через attach-metrics до grade (формат описан в README).",
             "Ручное время не измеряется; импортированные метрики считаются сообщением оператора.", ""]
    runs = []
    for case_id in case_ids:
        for repeat in range(1, repeats + 1):
            run = prepare(case_id, profile, folder, repeat)
            runs.append(str(run))
            lines += ["## {} / повтор {}".format(case_id, repeat), "",
                      "Workspace: `{}`".format(run / "workspace"), "", "```bash",
                      "python3 -m flexbench grade --run " + shlex.quote(str(run)), "```", ""]
    lines += ["## Отчёт после оценки всех попыток", "", "```bash",
              "python3 -m flexbench report --runs {} --output {}".format(
                  shlex.quote(str(folder)), shlex.quote(str(folder / "report"))), "```", ""]
    (folder / "RUNBOOK.md").write_text("\n".join(lines), encoding="utf-8")
    save(folder / "batch.json", {"runs": runs, "type": "manual-single-profile"})
    return folder


def command_batch(case_ids, profile_path, output, repeats, base_profile, models=None, open_progress=False):
    from .progress import BrowserProgress, GigaCodeActivity
    from .reporting import write_model_report, write_report

    if models is not None:
        if not models or not all(isinstance(m, str) and m.strip() for m in models) or len(set(models)) != len(models):
            raise ValueError("--models must contain unique nonempty model IDs")
        if base_profile.get("adapter") != "gigacode":
            raise ValueError("--models requires a command profile with adapter: gigacode")
        profiles = [profile_data(profile_path, model=model) for model in models]
    else:
        profiles = [base_profile]
    root = Path(output).resolve()
    if models is not None:
        root /= "matrix-" + uuid.uuid4().hex[:10]
    destinations = [root / ("model-{:02d}".format(i+1)) if models is not None else root for i in range(len(profiles))]
    plan = []
    for case_index, case_id in enumerate(case_ids):
        for repeat in range(1, repeats+1):
            # Rotate which model starts each pair to reduce a fixed order bias.
            first = (case_index + repeat - 1) % len(profiles)
            for offset in range(len(profiles)):
                group = (first + offset) % len(profiles)
                plan.append({"group": group, "model": profiles[group].get("model"), "case": case_id, "repeat": repeat})
    progress_folder = root / ("progress" if models is not None else "progress-" + uuid.uuid4().hex[:10])
    progress = BrowserProgress(progress_folder, plan, open_browser=open_progress)
    groups = [[] for _ in profiles]
    try:
        for index, item in enumerate(plan):
            group = item["group"]
            progress.start(index)
            run = prepare(item["case"], profile_path, destinations[group], item["repeat"], profile_snapshot=profiles[group])
            print("[{}/{}] {} / {} / repeat {}: {}".format(index+1, len(plan), item["model"] or "CLI settings",
                  item["case"], item["repeat"], run), file=sys.stderr, flush=True)
            progress.phase("executing", run=run)
            activity = GigaCodeActivity(run / "executor.jsonl") if profiles[group].get("adapter") == "gigacode" else None
            run_executor(run, on_tick=lambda seconds: progress.tick(seconds, activity.poll() if activity else None))
            progress.phase("grading")
            result = grade(run, on_tick=progress.tick)
            groups[group].append(result)
            progress.finish_attempt(result)
            print(json.dumps({"run": str(run), "status": result["status"], "model": item["model"]}), flush=True)
        progress.phase("reporting")
        if models is not None:
            report = write_model_report(models, groups, root / "report")
        else:
            report = write_report(None, groups, root / ("report-" + uuid.uuid4().hex[:10]))
        progress.finish(reports=[("Итоговый отчёт", report / "report.md"), ("Summary JSON", report / "summary.json")])
        print("Report:", report / "report.md")
        print("Summary:", report / "summary.json")
    except (Exception, KeyboardInterrupt) as error:
        progress.finish("interrupted" if isinstance(error, KeyboardInterrupt) else "error",
                        error="Прогон прерван пользователем" if isinstance(error, KeyboardInterrupt) else str(error))
        raise
    return 1 if any(r["status"] != "passed" for group in groups for r in group) else 0


def pilot(baseline, candidate, output):
    if any(profile_data(p)["mode"] != "manual" for p in (baseline,candidate)):
        raise ValueError("pilot prepares manual sessions; use run for command profiles")
    folder = Path(output).resolve() / ("pilot-" + uuid.uuid4().hex[:10])
    folder.mkdir(parents=True, exist_ok=False)
    lines = ["# Первый пилот FlexBench", "", "Это проверка работоспособности на трёх приближённых задачах.",
             "Для каждого пункта открой указанную рабочую папку в новом сеансе нужного инструмента.",
             "Передай задание: «Выполни TASK.md в этой рабочей папке. Не читай файлы вне неё».",
             "После завершения исполнителя выполни команду grade из корня flexbench.",
             "Перед grade добавь через observe итог о вмешательствах (включая явное отсутствие), через attach-log — доступный журнал сеанса.",
             "Не запускай grade до выполнения задачи. Не исправляй результат вручную перед оценкой.",
             "Ручной режим проверяет результат; время, модель и вмешательства независимо не измеряются.", ""]
    runs = []
    ids = sorted(p.parent.name for p in (ROOT / "cases").glob("*/case.json"))
    for i, case_id in enumerate(ids):
        pair = [("baseline",baseline),("candidate",candidate)]
        if i % 2:
            pair.reverse()
        for side, profile in pair:
            run = prepare(case_id,profile,folder / side)
            record = read(run / "run.json")
            runs.append({"side":side,"case":case_id,"run":str(run)})
            lines += ["## {} / {} / {}".format(case_id,side,record["profile"]["id"]), "",
                      "Рабочая папка: `{}`".format(run / "workspace"), "", "```bash",
                      "python3 -m flexbench grade --run '{}'".format(str(run).replace("'","'\"'\"'")), "```", ""]
    lines += ["## Сравнение после всех шести оценок", "", "```bash",
              "python3 -m flexbench compare --baseline '{}' --candidate '{}'".format(
                  str(folder / "baseline").replace("'","'\"'\"'"),str(folder / "candidate").replace("'","'\"'\"'")), "```", "",
              "## Отчёт для совместного разбора", "", "```bash",
              "python3 -m flexbench report --baseline '{}' --candidate '{}' --output '{}'".format(
                  str(folder / "baseline").replace("'","'\"'\"'"),str(folder / "candidate").replace("'","'\"'\"'"),
                  str(folder / "report").replace("'","'\"'\"'")), "```", "",
              "Не используйте эту тройку как holdout после настройки на ней. Перед grade можно импортировать токены/стоимость через attach-metrics."]
    (folder / "RUNBOOK.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    save(folder / "pilot.json",{"runs":runs,"type":"manual-synthetic-pilot"})
    return folder


def compare(baseline, candidate):
    groups = [[read(p) for p in result_files(folder)] for folder in (baseline, candidate)]
    if not all(groups):
        raise ValueError("Both directories must contain result.json records")
    for group in groups:
        if len({r["profile"]["digest"] for r in group}) != 1:
            raise ValueError("Each side must contain exactly one profile configuration")
        if len({r["run_id"] for r in group}) != len(group):
            raise ValueError("Duplicate run IDs in comparison")
    if {r["run_id"] for r in groups[0]} & {r["run_id"] for r in groups[1]}:
        raise ValueError("Baseline and candidate overlap")
    def matching_key(r):
        return (r["case"]["id"], r["case"]["digest"], json.dumps(r["runtime"],sort_keys=True),
                r["profile"]["timeout_seconds"], r["profile"]["mode"])
    if Counter(map(matching_key, groups[0])) != Counter(map(matching_key, groups[1])):
        raise ValueError("Incompatible cases, grader/fixture versions, runtimes, limits, modes or repeat counts")
    rows = []
    for case_id in sorted({r["case"]["id"] for r in groups[0]}):
        pair = [[r for r in group if r["case"]["id"] == case_id] for group in groups]
        rates = [sum(r["status"] == "passed" for r in group) / len(group) for group in pair]
        check_ids = sorted({c["id"] for group in pair for r in group
                            for c in (r.get("evaluation") or {}).get("checks",[])})
        details = {}
        for check_id in check_ids:
            details[check_id] = []
            for group in pair:
                outcomes = [next((c["passed"] for c in (r.get("evaluation") or {}).get("checks",[])
                                  if c["id"]==check_id),None) for r in group]
                details[check_id].append({"passed":outcomes.count(True),"failed":outcomes.count(False),
                                          "unavailable":outcomes.count(None)})
        seconds = [[r["executor"]["seconds"] for r in group if r.get("executor")] for group in pair]
        rows.append({"case": case_id, "runs_per_side": len(pair[0]), "baseline_pass_rate": rates[0],
                     "candidate_pass_rate": rates[1], "delta_percentage_points": 100 * (rates[1]-rates[0]),
                     "executor_median_seconds_all_attempts": [statistics.median(s) if s else None for s in seconds],
                     "checks_baseline_candidate": details})
    profiles = [g[0]["profile"] for g in groups]
    changed = {k: {"baseline": profiles[0].get(k), "candidate": profiles[1].get(k)}
               for k in sorted(set(profiles[0]) | set(profiles[1])) if profiles[0].get(k) != profiles[1].get(k)}
    return {"cases": rows, "macro_delta_percentage_points": sum(r["delta_percentage_points"] for r in rows)/len(rows),
            "efficiency": [efficiency(group) for group in groups],
            "profile_changes": changed,
            "status_counts": [dict(Counter(r["status"] for r in group)) for group in groups],
            "warnings": ["Exploratory synthetic pilot, not evidence of production effectiveness",
                         "Manual results do not measure time-budget success" ]
                + (["Model/executor version unknown; cannot isolate the effect of rules or executor"]
                   if any((not r["profile"].get("model") and not (r.get("adapter_info") or {}).get("models"))
                          or not r["profile"].get("executor_version") for group in groups for r in group) else [])}


def main():
    parser = argparse.ArgumentParser(description="FlexBench local pilot (Python 3.9+, macOS/Linux)")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("validate")
    command = commands.add_parser("pilot")
    command.add_argument("--baseline",default=str(ROOT / "profiles/gigacode-manual.json"))
    command.add_argument("--candidate",default=str(ROOT / "profiles/gigaflex-manual.json"))
    command.add_argument("--output",default="artifacts")
    for verb in ("prepare", "run"):
        command = commands.add_parser(verb)
        if verb == "run":
            command.add_argument("--case", nargs="+", default=["all"], help="Case IDs, or all (default)")
        else:
            command.add_argument("--case", required=True, help="Case ID")
        command.add_argument("--profile", required=True)
        command.add_argument("--output", default="artifacts/runs")
        if verb == "run":
            command.add_argument("--repeat", type=int, default=1)
            command.add_argument("--models", nargs="+", help="Exact GigaCode model IDs; run the same cases on each model")
            command.add_argument("--open-progress", action="store_true", help="Open the live progress page in the default browser")
    command = commands.add_parser("grade")
    command.add_argument("--run", required=True)
    for verb in ("compare", "report"):
        command = commands.add_parser(verb)
        command.add_argument("--baseline", required=verb == "compare")
        command.add_argument("--candidate", required=verb == "compare")
        if verb == "report":
            command.add_argument("--runs", help="Single-profile results directory or result.json")
            command.add_argument("--output",required=True)
    command = commands.add_parser("observe")
    command.add_argument("--run",required=True)
    command.add_argument("--kind",choices=OBSERVATION_KINDS,default="note")
    command.add_argument("--text",required=True)
    command = commands.add_parser("attach-log")
    command.add_argument("--run",required=True)
    command.add_argument("--file",required=True)
    command = commands.add_parser("attach-metrics")
    command.add_argument("--run", required=True)
    command.add_argument("--file", required=True)
    args = parser.parse_args()
    try:
        ids = sorted(p.parent.name for p in (ROOT / "cases").glob("*/case.json"))
        if args.action == "validate":
            for case_id in ids:
                case, _, _ = case_data(case_id)
                print(json.dumps({"case": case_id, "digest": case["digest"], "runtime": runtime(case)}))
        elif args.action == "pilot":
            folder = pilot(args.baseline,args.candidate,args.output)
            print("Open:",folder / "RUNBOOK.md")
        elif args.action == "prepare":
            run = prepare(args.case, args.profile, args.output)
            print(run)
            print("Workspace:", run / "workspace")
            print("Task:", run / "workspace/TASK.md")
        elif args.action == "run":
            if args.repeat < 1:
                raise ValueError("repeat must be positive")
            selected = ids if args.case == ["all"] else args.case
            if not selected or len(set(selected)) != len(selected) or any(case not in ids for case in selected):
                raise ValueError("Select unique known case IDs, or all alone")
            profile = profile_data(args.profile)
            # Fail before launching any executor if a selected case cannot run locally.
            for case_id in selected:
                case, _, _ = case_data(case_id)
                runtime(case)
            if profile["mode"] == "manual":
                if args.models is not None or args.open_progress:
                    raise ValueError("--models and --open-progress require a command profile")
                folder = manual_batch(selected, args.profile, args.output, args.repeat)
                print("Open:", folder / "RUNBOOK.md")
                return 0
            return command_batch(selected, args.profile, args.output, args.repeat, profile,
                                 models=args.models, open_progress=args.open_progress)
        elif args.action == "grade":
            result = grade(args.run)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "passed" else 1
        elif args.action == "compare":
            print(json.dumps(compare(args.baseline,args.candidate), ensure_ascii=False, indent=2))
        elif args.action == "report":
            from .reporting import write_report
            if args.runs and not (args.baseline or args.candidate):
                comparison = None
                groups = [[read(p) for p in result_files(args.runs)]]
            elif args.baseline and args.candidate and not args.runs:
                comparison = compare(args.baseline,args.candidate)
                groups = [[read(p) for p in result_files(folder)] for folder in (args.baseline,args.candidate)]
            else:
                raise ValueError("report requires either --runs or both --baseline and --candidate")
            folder = write_report(comparison,groups,args.output)
            print("Report:",folder / "report.md")
            print("Summary:",folder / "summary.json")
        elif args.action == "observe":
            observe(args.run,args.kind,args.text)
            print("Observation saved")
        elif args.action == "attach-log":
            print("Session log saved:",attach_log(args.run,args.file))
        elif args.action == "attach-metrics":
            print("Measurements saved:", attach_metrics(args.run, args.file))
    except KeyboardInterrupt:
        print("Interrupted; inspect the progress page and saved attempts before creating a new run", file=sys.stderr)
        return 130
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
