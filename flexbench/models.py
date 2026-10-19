"""Read GigaCode's ACP model catalog using the Teamytime discovery contract."""

import json
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path


MAX_OUTPUT = 2 * 1024 * 1024
INVALID_CATALOG = "GigaCode не передал каталог моделей через ACP. ID можно указать вручную через --models."


class ModelCatalogError(ValueError):
    pass


def valid_text(value):
    return (isinstance(value, str) and 0 < len(value) <= 200 and value.strip() == value
            and not re.search(r"[\x00-\x1f\x7f]", value))


def parse_catalog(value):
    if not isinstance(value, dict):
        raise ModelCatalogError(INVALID_CATALOG)
    models = {}

    def add(identifier, name):
        if valid_text(identifier) and valid_text(name) and identifier not in models:
            models[identifier] = {"id": identifier, "name": name}
            if len(models) > 500:
                raise ModelCatalogError("GigaCode передал слишком большой каталог моделей (более 500).")

    current = None
    options = value.get("configOptions")
    if isinstance(options, list):
        config = next((item for item in options[:100] if isinstance(item, dict)
                       and (item.get("category") == "model" or item.get("id") == "model")
                       and item.get("type") == "select"), None)
        if config and isinstance(config.get("options"), list):
            for option in config["options"]:
                if not isinstance(option, dict):
                    continue
                entries = option["options"] if isinstance(option.get("options"), list) else [option]
                for entry in entries:
                    if isinstance(entry, dict):
                        add(entry.get("value"), entry.get("name"))
            current = config.get("currentValue")
    legacy = value.get("models")
    if not models and isinstance(legacy, dict) and isinstance(legacy.get("availableModels"), list):
        for option in legacy["availableModels"]:
            if isinstance(option, dict):
                add(option.get("modelId"), option.get("name"))
        current = legacy.get("currentModelId")
    if not models:
        raise ModelCatalogError(INVALID_CATALOG)
    return {"models": list(models.values()), "current_model_id": current if valid_text(current) and current in models else None}


def discover_models(command="gigacode", timeout=15):
    """Initialize + session/new only: no prompt, authentication or client tools."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ModelCatalogError("Тайм-аут каталога должен быть положительным конечным числом.")
    executable = shutil.which(command)
    if executable is None:
        raise ModelCatalogError("GigaCode не найден. Проверьте executable в профиле и установку CLI.")
    with tempfile.TemporaryDirectory(prefix="flexbench-models-") as temporary:
        directory = str(Path(temporary).resolve())
        process = None
        try:
            process = subprocess.Popen([executable, "--acp", "--approval-mode=plan"], cwd=directory,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True, bufsize=0)
            with selectors.DefaultSelector() as selector:
                for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                os.set_blocking(process.stdin.fileno(), False)
                outgoing = bytearray()
                writing = False

                def send(message):
                    nonlocal writing
                    outgoing.extend(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
                    if len(outgoing) > MAX_OUTPUT:
                        raise ModelCatalogError("Слишком много запросов ACP при получении каталога.")
                    if not writing:
                        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                        writing = True

                send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": 1, "clientCapabilities": {},
                    "clientInfo": {"name": "flexbench-model-catalog", "version": "1"}}})
                deadline = time.monotonic() + timeout
                pending = bytearray()
                byte_count = 0
                stage = 1
                readers = 2
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ModelCatalogError("GigaCode не ответил на запрос каталога вовремя. Проверьте вход в CLI и повторите команду.")
                    if readers == 0:
                        raise ModelCatalogError("GigaCode завершил соединение до получения каталога. Проверьте поддержку --acp и авторизацию.")
                    for key, _ in selector.select(remaining):
                        if key.data == "stdin":
                            try:
                                sent = os.write(process.stdin.fileno(), outgoing)
                            except BlockingIOError:
                                continue
                            except OSError:
                                raise ModelCatalogError("GigaCode закрыл соединение ACP. Проверьте поддержку --acp и авторизацию.") from None
                            del outgoing[:sent]
                            if not outgoing:
                                selector.unregister(process.stdin)
                                writing = False
                            continue
                        try:
                            chunk = os.read(key.fileobj.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            readers -= 1
                            continue
                        byte_count += len(chunk)
                        if byte_count > MAX_OUTPUT:
                            raise ModelCatalogError("Слишком большой ответ GigaCode при загрузке моделей.")
                        if key.data == "stderr":
                            continue  # Drain diagnostics but never print raw CLI output/credentials.
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            try:
                                message = json.loads(line)
                            except (ValueError, RecursionError):
                                continue  # CLI banners are not protocol responses.
                            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                                continue
                            identifier = message.get("id")
                            if isinstance(message.get("method"), str):
                                if type(identifier) is int or valid_text(identifier):
                                    if message["method"] == "session/request_permission":
                                        send({"jsonrpc": "2.0", "id": identifier,
                                              "result": {"outcome": {"outcome": "cancelled"}}})
                                    else:
                                        send({"jsonrpc": "2.0", "id": identifier, "error": {
                                            "code": -32601, "message": "Not supported by the model catalog client"}})
                                continue
                            if type(identifier) is not int or identifier != stage:
                                continue
                            if message.get("error"):
                                raise ModelCatalogError("GigaCode отклонил запрос ACP. Проверьте авторизацию и поддержку каталога в вашей версии CLI.")
                            result = message.get("result")
                            if not isinstance(result, dict):
                                raise ModelCatalogError("Некорректный ответ GigaCode на запрос ACP.")
                            if stage == 1:
                                if type(result.get("protocolVersion")) is not int or result["protocolVersion"] != 1:
                                    raise ModelCatalogError("Версия ACP установленного GigaCode не поддерживается для получения моделей.")
                                stage = 2
                                send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
                                      "params": {"cwd": directory, "mcpServers": []}})
                            else:
                                if not isinstance(result.get("sessionId"), str) or not result["sessionId"]:
                                    raise ModelCatalogError("GigaCode не создал служебную сессию для каталога моделей.")
                                return parse_catalog(result)
        except OSError:
            raise ModelCatalogError("Не удалось запустить GigaCode для получения каталога моделей. Проверьте executable и установку CLI.") from None
        finally:
            if process is not None:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()

                def stop(signum):
                    try:
                        os.killpg(process.pid, signum)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        # Sandboxed macOS can deny killpg for an already-exited
                        # group. Reap it, or signal our own child directly.
                        if process.poll() is None:
                            try:
                                process.send_signal(signum)
                            except ProcessLookupError:
                                pass

                stop(signal.SIGTERM)
                try:
                    process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    pass
                finally:
                    stop(signal.SIGKILL)
                    process.wait()
