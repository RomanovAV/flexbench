"""Local browser progress without a server, external assets, or raw transcripts."""

from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import time
from urllib.parse import quote
import webbrowser


PHASES = {"pending": "Ожидание", "preparing": "Подготовка", "executing": "GigaCode / исполнитель",
          "grading": "Проверка результата", "passed": "Успех", "failed": "Неуспешно",
          "reporting": "Подготовка отчёта", "interrupted": "Прервано", "finished": "Отчёты готовы", "error": "Ошибка выполнения"}
STATES = {"running": "Выполняется", "finished": "Завершено", "interrupted": "Прервано", "error": "Ошибка запуска"}


def escaped(value):
    return html.escape("—" if value is None else str(value), quote=True)


def atomic_text(path, content):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


class GigaCodeActivity:
    """Tail complete JSONL lines; never show text, tool arguments or results."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.pending = b""
        self.stats = {"events": 0, "tool_calls": 0, "tool_results": 0,
                      "activity": "Ожидание событий CLI", "reported_models": []}

    def poll(self):
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                raw = stream.read()
                self.offset = stream.tell()
        except FileNotFoundError:
            return dict(self.stats)
        lines = (self.pending + raw).split(b"\n")
        self.pending = lines.pop()
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue
            self.stats["events"] += 1
            kind = event.get("type")
            message = event.get("message")
            model = event.get("model") if kind == "system" else (
                message.get("model") if isinstance(message, dict) else None)
            if isinstance(model, str) and model not in self.stats["reported_models"]:
                self.stats["reported_models"].append(model)
            if kind == "system":
                self.stats["activity"] = "Сеанс CLI начат"
            elif kind == "assistant":
                self.stats["activity"] = "Ответ модели"
            elif kind == "result":
                self.stats["activity"] = "Итог сеанса получен"
            for block in self.tool_blocks(event):
                if block["type"] == "tool_use":
                    self.stats["tool_calls"] += 1
                    name = block.get("name")
                    self.stats["activity"] = "Инструмент: " + (name[:80] if isinstance(name, str) else "вызов")
                else:
                    self.stats["tool_results"] += 1
                    self.stats["activity"] = "Результат инструмента" + (" (ошибка)" if block.get("is_error") is True else "")
        return dict(self.stats)

    @staticmethod
    def tool_blocks(value):
        if isinstance(value, dict):
            if value.get("type") in ("tool_use", "tool_result"):
                yield value
            for child in value.values():
                yield from GigaCodeActivity.tool_blocks(child)
        elif isinstance(value, list):
            for child in value:
                yield from GigaCodeActivity.tool_blocks(child)


class BrowserProgress:
    def __init__(self, folder, plan, open_browser=False):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=False)
        self.started = time.monotonic()
        self.current = None
        self.attempt_started = None
        self.state = {"schema_version": 1, "state": "running", "created_at": datetime.now(timezone.utc).isoformat(),
                      "phase": "preparing", "attempts": [{**item, "phase": "pending"} for item in plan],
                      "completed": 0, "successes": 0, "failures": 0, "total": len(plan), "reports": []}
        self.write()
        print("Progress:", (self.folder / "index.html").as_uri(), flush=True)
        if open_browser:
            try:
                webbrowser.open((self.folder / "index.html").as_uri())
            except (webbrowser.Error, OSError):
                pass  # The printed local file remains available.

    def start(self, index):
        self.current = index
        self.attempt_started = time.monotonic()
        self.state["attempts"][index]["phase"] = "preparing"
        self.state["phase"] = "preparing"
        self.write()

    def phase(self, phase, run=None):
        self.state["phase"] = phase
        if self.current is not None:
            current = self.state["attempts"][self.current]
            current["phase"] = phase
            if run is not None:
                current["run"] = str(run)
        self.write()

    def tick(self, seconds, activity=None):
        if self.current is not None:
            row = self.state["attempts"][self.current]
            if activity is not None:
                row.update(activity)
            row["phase_seconds"] = round(seconds, 1)
            row["elapsed_seconds"] = round(time.monotonic() - self.attempt_started, 1)
        self.write()

    def finish_attempt(self, result):
        row = self.state["attempts"][self.current]
        passed = result["status"] == "passed"
        row.update(phase="passed" if passed else "failed", status=result["status"],
                   elapsed_seconds=round(time.monotonic() - self.attempt_started, 1),
                   executor_seconds=(result.get("executor") or {}).get("seconds"),
                   tokens=(result.get("usage") or {}).get("total_tokens"),
                   reported_models=(result.get("adapter_info") or {}).get("models", []))
        self.state["completed"] += 1
        self.state["successes" if passed else "failures"] += 1
        self.current = None
        self.write()

    def finish(self, state="finished", reports=(), error=None):
        self.state["state"] = state
        self.state["phase"] = state
        self.state["reports"] = [{"label": label, "path": os.path.relpath(str(Path(path).resolve()), self.folder)}
                                 for label, path in reports]
        if error:
            self.state["error"] = error
        if self.current is not None:
            self.state["attempts"][self.current]["phase"] = "interrupted"
        self.write()

    def write(self):
        self.state["elapsed_seconds"] = round(time.monotonic() - self.started, 1)
        self.state["current"] = self.current
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_text(self.folder / "progress.json", json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")
        atomic_text(self.folder / "index.html", render(self.state))


def render(state):
    percent = 100 * state["completed"] / state["total"] if state["total"] else 0
    refresh = '<meta http-equiv="refresh" content="2">' if state["state"] == "running" else ""
    rows = []
    for index, row in enumerate(state["attempts"]):
        phase = row["phase"]
        current = index == state.get("current")
        model = row.get("model") or "Настройки CLI"
        status = row.get("status") or PHASES.get(phase, phase)
        rows.append('<tr class="{}"><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
            "active" if current else phase, escaped(model), escaped(row["case"]), row["repeat"], escaped(status),
            escaped(row.get("elapsed_seconds", "—")), escaped(row.get("tokens", "—")), escaped(row.get("tool_calls", "—"))))
    current = state["attempts"][state["current"]] if state.get("current") is not None else None
    active = '<p class="muted">{}</p>'.format(escaped(PHASES.get(state["phase"], state["phase"])))
    if current:
        active = '<h2>{} <span class="muted">/</span> {}</h2><p>{} · повтор {} · {} с</p><p class="activity">{}</p><p class="muted">События: {} · вызовы инструментов: {} · ответы инструментов: {}</p>'.format(
            escaped(current.get("model") or "Настройки CLI"), escaped(current["case"]),
            escaped(PHASES.get(current["phase"], current["phase"])), current["repeat"], escaped(current.get("elapsed_seconds", 0)),
            escaped(current.get("activity", "Ожидание")), current.get("events", 0), current.get("tool_calls", 0), current.get("tool_results", 0))
    if state.get("error"):
        active += '<p class="error">{}</p>'.format(escaped(state["error"]))
    links = "".join('<a href="{}">{}</a>'.format(escaped(quote(report["path"], safe="/")), escaped(report["label"])) for report in state["reports"])
    return '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">''' + refresh + '''<title>FlexBench · Прогресс</title><style>
:root{color-scheme:dark;font-family:ui-sans-serif,system-ui,sans-serif;background:#0b1220;color:#e8edf6}*{box-sizing:border-box}body{margin:0;padding:40px 24px}main{max-width:1200px;margin:auto}header{display:flex;justify-content:space-between;align-items:center;gap:16px}h1{font-size:30px;margin:0}h2{font-size:22px;margin:0 0 14px}.tag{background:#1c304d;border-radius:30px;padding:8px 16px;color:#9bccff}.muted{color:#8e9db5}p{line-height:1.5}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:28px 0}.card,section{background:#121e30;border:1px solid #26354c;border-radius:14px;padding:22px}.number{display:block;font-size:30px;font-weight:650;margin-top:8px}.green{color:#64dca7}.red,.error{color:#ff9c9c}.bar{height:10px;background:#25364c;border-radius:8px;overflow:hidden}.fill{height:100%;background:#64b5ff}section{margin:22px 0}.activity{color:#b8d9ff}nav{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}a{color:#97caff;text-decoration:none;border:1px solid #365675;padding:10px 14px;border-radius:8px}a:hover{background:#203651}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:14px 12px;text-align:left;border-bottom:1px solid #25354b;white-space:nowrap}th{color:#8e9db5;font-weight:500}.active{background:#173a54}.passed td:nth-child(4){color:#64dca7}.failed td:nth-child(4){color:#ff9c9c}footer{margin-top:20px;font-size:13px}@media(max-width:650px){.cards{grid-template-columns:repeat(2,1fr)}body{padding:24px 12px}header{align-items:flex-start}}
</style></head><body><main><header><div><h1>FlexBench</h1><p class="muted">Прогресс выполнения</p></div><span class="tag">''' + escaped(STATES[state["state"]]) + '''</span></header><div class="cards"><div class="card">Завершено<span class="number">''' + str(state["completed"]) + " / " + str(state["total"]) + '''</span></div><div class="card">Успешно<span class="number green">''' + str(state["successes"]) + '''</span></div><div class="card">Неуспешно<span class="number red">''' + str(state["failures"]) + '''</span></div><div class="card">Время прогона<span class="number">''' + str(state["elapsed_seconds"]) + ''' с</span></div></div><div class="bar"><div class="fill" style="width:''' + str(percent) + '''%"></div></div><p class="muted">''' + "{:.0f}%".format(percent) + ''' попыток завершено. Длительность текущего ответа модели заранее неизвестна.</p><section>''' + active + '''</section><nav>''' + links + '''</nav><div class="table-wrap"><table><thead><tr><th>Модель</th><th>Задача</th><th>Повтор</th><th>Статус</th><th>Время, с</th><th>Токены</th><th>Инструменты</th></tr></thead><tbody>''' + "".join(rows) + '''</tbody></table></div><footer class="muted">Обновлено: ''' + escaped(state["updated_at"]) + ''' · Страница обновляется каждые 2 секунды во время прогона. Тексты ответов и команды инструментов здесь не отображаются.</footer></main></body></html>'''
