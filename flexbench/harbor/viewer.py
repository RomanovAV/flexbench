"""Local GigaCode launcher alongside the unmodified Harbor results viewer."""

import argparse
import asyncio
import json
from pathlib import Path
import re
import signal
import sys
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from flexbench.models import discover_models
from .cli import prepare_job


class LaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str
    models: list[str] = Field(min_length=1, max_length=500)
    cases: list[str] = Field(min_length=1, max_length=100)
    repeat: int = Field(default=1, ge=1, le=100)
    concurrency: int = Field(default=4, ge=1, le=64)

    @field_validator("request_id")
    @classmethod
    def valid_request_id(cls, value):
        return str(UUID(value))

    @field_validator("models", "cases")
    @classmethod
    def unique_names(cls, values):
        if len(set(values)) != len(values) or any(not v.strip() or len(v) > 200 for v in values):
            raise ValueError("Выберите непустые уникальные ID")
        return values


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def log_tail(path):
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 16384))
            return stream.read().decode("utf-8", "replace")
    except OSError:
        return ""


def create_app(output, profile_path=None):
    import harbor
    from harbor.viewer.server import create_app as harbor_app
    from flexbench.__main__ import ROOT, case_data, profile_data

    output = Path(output).resolve()
    jobs = output / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    static = Path(harbor.__file__).parent / "viewer/static"
    app = harbor_app(jobs, static_dir=None)
    app.title = "FlexBench / Harbor"
    # Replace the generic launcher: it targets arbitrary agents/environments.
    # Result browsing, streaming and job APIs remain Harbor's own routes.
    app.router.routes[:] = [r for r in app.router.routes if not getattr(r, "path", "").startswith("/api/run")]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    processes = {}
    launch_lock = asyncio.Lock()
    catalog_lock = asyncio.Lock()
    profile_path = profile_path or ROOT / "profiles/gigacode-command.json"

    @app.middleware("http")
    async def local_writes(request: Request, call_next):
        if request.url.path.startswith("/api/flexbench") and request.method != "GET":
            origin = request.headers.get("origin")
            if (request.headers.get("x-flexbench-request") != "1"
                    or (origin is not None and origin != str(request.base_url).rstrip("/"))):
                return JSONResponse({"detail": "Запуск разрешён только со страницы FlexBench."}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/flexbench") or request.url.path.rstrip("/") == "/run":
            response.headers["Cache-Control"] = "no-store"
        return response

    def profile():
        value = profile_data(profile_path)
        if value.get("adapter") != "gigacode" or value.get("mode") != "command":
            raise ValueError("Нужен профиль GigaCode в режиме command")
        return value

    async def catalog():
        async with catalog_lock:
            try:
                config = await asyncio.to_thread(profile)
                result = await asyncio.to_thread(discover_models, config["executable"])
                return {**result, "executor_version": config["executor_version"]}
            except (ValueError, OSError, RuntimeError) as error:
                raise HTTPException(503, str(error)) from error

    def bundle(name):
        if not re.fullmatch(r"gigacode-\d{8}T\d{6}-[0-9a-f]{8}", name):
            raise HTTPException(404, "Прогон не найден")
        directory = output / name
        if not (directory / "config.json").is_file():
            raise HTTPException(404, "Прогон не найден")
        return directory

    def receipt(directory):
        return {"job_name": directory.name, "url": "/jobs/" + directory.name,
                "status_url": "/api/flexbench/runs/" + directory.name}

    def status(name):
        directory = bundle(name)
        process = processes.get(name)
        state = read_json(directory / "launcher-state.json", {})
        returncode = process.returncode if process else state.get("returncode")
        running = process is not None and returncode is None
        result = read_json(jobs / name / "result.json", {})
        if process is None and state.get("state") == "running":
            phase = "unobserved"  # Server restarted; do not invent process ownership.
        elif returncode is not None:
            phase = "finished"
        else:
            phase = "running" if running else "unknown"
        stats = result.get("stats", {})
        return {**receipt(directory), "state": phase, "running": running,
                "returncode": returncode, "job_ready": bool(result), "can_stop": running,
                "completed": stats.get("n_completed_trials", 0),
                "total": result.get("n_total_trials") or read_json(directory / "launch-request.json", {}).get("trials"),
                "log_tail": log_tail(directory / "launcher.log"),
                "summary": read_json(jobs / name / "flexbench-summary.json")}

    @app.get("/run", response_class=HTMLResponse)
    @app.get("/run/", response_class=HTMLResponse, include_in_schema=False)
    def launcher():
        return HTMLResponse((Path(__file__).parent / "launch.html").read_text(encoding="utf-8"))

    @app.get("/api/flexbench/options")
    def options():
        ids = sorted(p.parent.name for p in (ROOT / "cases").glob("*/case.json"))
        return {"cases": [{"id": case, "category": case_data(case)[0]["category"]} for case in ids],
                "repeat": 1, "concurrency": 4}

    @app.get("/api/flexbench/models")
    async def models():
        return await catalog()

    @app.post("/api/flexbench/runs", status_code=202)
    async def launch(data: LaunchRequest):
        async with launch_lock:
            payload = data.model_dump()
            # Persist receipts so retrying a timed-out POST never launches twice.
            for record in output.glob("gigacode-*/launch-request.json"):
                previous = read_json(record, {})
                if previous.get("request", {}).get("request_id") == data.request_id:
                    if previous["request"] != payload:
                        raise HTTPException(409, "Этот запрос уже использован с другими настройками")
                    return receipt(record.parent)
            current = await catalog()
            available = {m["id"] for m in current["models"]}
            missing = [m for m in data.models if m not in available]
            if missing:
                raise HTTPException(409, "Модели отсутствуют в текущем каталоге: " + ", ".join(missing) + ". Обновите список.")
            args = argparse.Namespace(profile=profile_path, output=output, models=data.models,
                                      case=data.cases, repeat=data.repeat, concurrency=data.concurrency)
            try:
                directory, config = await asyncio.to_thread(prepare_job, args)
            except (ValueError, OSError, RuntimeError) as error:
                raise HTTPException(422, str(error)) from error
            trials = len(list((directory / "tasks").iterdir())) * len(config.agents) * data.repeat
            (directory / "launch-request.json").write_text(json.dumps({"request": payload, "trials": trials}), encoding="utf-8")
            from .worker import write_state
            write_state(directory, state="starting", returncode=None)
            try:
                with (directory / "launcher.log").open("wb") as log:
                    processes[directory.name] = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "flexbench.harbor.worker", str(directory / "config.json"),
                        stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True)
            except OSError as error:
                write_state(directory, state="finished", returncode=2)
                (directory / "launcher.log").write_text(str(error), encoding="utf-8")
                raise HTTPException(503, "Не удалось запустить Harbor: " + str(error)) from error
            return receipt(directory)

    @app.get("/api/flexbench/runs/{name}")
    def run_status(name: str):
        return status(name)

    @app.get("/api/run/{name}/status")
    def harbor_run_status(name: str):
        # Harbor job pages also poll this endpoint for launcher progress.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise HTTPException(404)
        if (output / name / "launch-request.json").is_file():
            return status(name)
        return {"running": False, "returncode": None, "job_ready": (jobs / name / "result.json").is_file(), "log_tail": ""}

    @app.post("/api/flexbench/runs/{name}/stop")
    def stop(name: str):
        bundle(name)
        process = processes.get(name)
        if process is None or process.returncode is not None:
            raise HTTPException(409, "У этого сервера нет активного процесса для остановки")
        try:
            process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        return {"stopping": True}

    @app.get("/api/flexbench/runs/{name}/{artifact}")
    def report(name: str, artifact: str):
        bundle(name)
        files = {"report": "flexbench-report.md", "summary": "flexbench-summary.json"}
        if artifact not in files:
            raise HTTPException(404)
        path = jobs / name / files[artifact]
        if not path.is_file():
            raise HTTPException(404, "Отчёт ещё не готов")
        return FileResponse(path, filename=files[artifact])

    @app.get("/flexbench-navigation.js")
    def navigation():
        # Harbor's SPA normally navigates to its generic /run without a request.
        # Use a full navigation for our launcher; no patched dependency assets.
        return FileResponse(Path(__file__).parent / "navigation.js", media_type="text/javascript")

    @app.get("/flexbench-launch.js")
    def launch_script():
        return FileResponse(Path(__file__).parent / "launch.js", media_type="text/javascript")

    for name in ("assets", "fonts"):
        app.mount("/" + name, StaticFiles(directory=static / name))

    @app.get("/favicon.ico")
    @app.get("/favicon.svg")
    def favicon(request: Request):
        return FileResponse(static / request.url.path.lstrip("/"))

    @app.get("/{path:path}", response_class=HTMLResponse)
    def harbor_page(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        page = (static / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(page.replace("</head>", '<script src="/flexbench-navigation.js"></script></head>'))

    return app


def serve(output, port=8080, profile=None):
    import uvicorn
    app = create_app(output, profile)
    print("FlexBench / Harbor: http://127.0.0.1:{}/run".format(port), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port)
    return 0
