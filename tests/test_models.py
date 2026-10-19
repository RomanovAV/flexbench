import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from flexbench.__main__ import main, read, save
from flexbench.models import ModelCatalogError, discover_models, parse_catalog


LEGACY = {"sessionId": "catalog-only", "models": {"currentModelId": "model-a", "availableModels": [
    {"modelId": "model-a", "name": "Модель A"}, {"modelId": "model-b", "name": "Model B"}]}}


class CatalogParserTests(unittest.TestCase):
    def test_legacy_and_grouped_config_selectors(self):
        self.assertEqual(parse_catalog(LEGACY), {"models": [{"id": "model-a", "name": "Модель A"},
                                                           {"id": "model-b", "name": "Model B"}], "current_model_id": "model-a"})
        catalog = parse_catalog({"configOptions": [{"id": "provider-model", "category": "model", "type": "select",
            "currentValue": "a", "options": [{"group": "provider", "options": [
                {"value": "a", "name": "A"}, {"value": "a", "name": "Duplicate"},
                {"value": "bad\nID", "name": "Invalid"}]}, {"value": "b", "name": "B"}]}]})
        self.assertEqual(catalog, {"models": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}], "current_model_id": "a"})

    def test_config_selector_has_priority_and_unlisted_current_is_not_invented(self):
        catalog = parse_catalog({**LEGACY, "configOptions": [{"id": "model", "type": "select", "currentValue": "unknown",
                                                             "options": [{"value": "new", "name": "New"}]}]})
        self.assertEqual(catalog["models"], [{"id": "new", "name": "New"}])
        self.assertIsNone(catalog["current_model_id"])

    def test_missing_catalog_control_characters_and_oversized_catalog(self):
        for value in (None, {}, {"models": {"availableModels": []}},
                      {"configOptions": [{"id": "mode", "type": "select", "options": [{"value": "plan", "name": "Plan"}]}]},
                      {"models": {"availableModels": [{"modelId": "bad\x00ID", "name": "Bad"}]}},
                      {"models": {"availableModels": [{"modelId": "x" * 201, "name": "Bad"}]}}):
            with self.subTest(value=value), self.assertRaises(ModelCatalogError):
                parse_catalog(value)
        with self.assertRaisesRegex(ModelCatalogError, "500"):
            parse_catalog({"models": {"availableModels": [{"modelId": str(i), "name": str(i)} for i in range(501)]}})


class ModelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"

    def fake_cli(self, mode="legacy"):
        cli = self.root / ("fake-" + mode)
        log = self.root / (mode + ".jsonl")
        cli.write_text("#!" + sys.executable + "\n" + '''
import json, os, signal, sys, time
from pathlib import Path
if sys.argv[1:] == ['--version']:
    print('test-cli-1')
    raise SystemExit(0)
''' + "mode=" + repr(mode) + "\nlog=Path(" + repr(str(log)) + ")\nresult=" + repr(LEGACY) + '\n' + '''
def record(value):
    with log.open('a') as stream: stream.write(json.dumps(value) + '\\n')
def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)
record({'pid':os.getpid(), 'cwd':str(Path.cwd()), 'argv':sys.argv[1:]})
if mode == 'hang': signal.signal(signal.SIGTERM, signal.SIG_IGN)
denied=0
for line in sys.stdin:
    message=json.loads(line); record(message)
    if message.get('method') == 'initialize':
        if mode == 'hang':
            while True: time.sleep(1)
        if mode == 'exit':
            print('TOKEN_SECRET', file=sys.stderr, flush=True)
            raise SystemExit(7)
        if mode in ('stdout-flood', 'stderr-flood'):
            stream=sys.stdout if mode == 'stdout-flood' else sys.stderr
            stream.write('x'*(2*1024*1024+1)); stream.flush()
            while True: time.sleep(1)
        if mode == 'auth':
            send({'jsonrpc':'2.0','id':1,'error':{'code':-32000,'message':'TOKEN_SECRET authentication required'}})
            continue
        print('CLI startup banner', flush=True)
        send({'jsonrpc':'2.0','id':999,'result':{'protocolVersion':1}})
        send({'jsonrpc':'2.0','id':1,'result':{'protocolVersion':2 if mode == 'version' else 1}})
    elif message.get('method') == 'session/new':
        if mode == 'missing':
            send({'jsonrpc':'2.0','id':2,'result':{'sessionId':'empty'}}); continue
        if mode == 'deny':
            send({'jsonrpc':'2.0','id':'permission','method':'session/request_permission','params':{}})
            send({'jsonrpc':'2.0','id':'file','method':'fs/read_text_file','params':{'path':'/private/TOKEN_SECRET'}})
            continue
        if mode == 'modern':
            result={'sessionId':'catalog-only','configOptions':[{'category':'model','type':'select',
                'currentValue':'a','options':[{'value':'a','name':'Модель A'}]}]}
        # Split a UTF-8 character across writes to exercise byte framing.
        raw=(json.dumps({'jsonrpc':'2.0','id':2,'result':result}, ensure_ascii=False)+'\\n').encode()
        split=raw.index('М'.encode())+1
        os.write(1,raw[:split]); time.sleep(.01); os.write(1,raw[split:])
    elif message.get('id') in ('permission','file'):
        denied+=1
        if denied == 2: send({'jsonrpc':'2.0','id':2,'result':result})
    elif message.get('method'):
        raise SystemExit(90)
''')
        cli.chmod(0o755)
        return cli, log

    def assert_cleaned_up(self, log):
        records = [json.loads(line) for line in log.read_text().splitlines()]
        started = records[0]
        self.assertFalse(Path(started["cwd"]).exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(started["pid"], 0)
        return records

    def test_discovery_uses_only_initialize_and_isolated_session_then_cleans_up(self):
        cli, log = self.fake_cli()
        catalog = discover_models(str(cli))
        self.assertEqual(catalog["models"][0]["name"], "Модель A")
        records = self.assert_cleaned_up(log)
        started, initialize, session = records
        self.assertEqual(started["argv"], ["--acp", "--approval-mode=plan"])
        self.assertEqual(initialize["params"]["clientCapabilities"], {})
        self.assertEqual(session["params"], {"cwd": started["cwd"], "mcpServers": []})
        self.assertEqual([initialize["method"], session["method"]], ["initialize", "session/new"])

    def test_modern_discovery_and_permission_denial(self):
        cli, log = self.fake_cli("modern")
        self.assertEqual(discover_models(str(cli))["current_model_id"], "a")
        self.assert_cleaned_up(log)
        cli, log = self.fake_cli("deny")
        self.assertEqual(len(discover_models(str(cli))["models"]), 2)
        records = self.assert_cleaned_up(log)
        responses = {r["id"]: r for r in records[1:] if "method" not in r}
        self.assertEqual(responses["permission"]["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(responses["file"]["error"]["code"], -32601)
        self.assertFalse(any(r.get("method") in ("session/prompt", "authenticate") for r in records))

    def test_failures_do_not_expose_raw_diagnostics_or_leave_processes(self):
        for mode in ("missing", "version", "auth", "exit", "stdout-flood", "stderr-flood"):
            with self.subTest(mode=mode):
                cli, log = self.fake_cli(mode)
                with self.assertRaises(ModelCatalogError) as caught:
                    discover_models(str(cli), timeout=3)
                self.assertNotIn("TOKEN_SECRET", str(caught.exception))
                self.assert_cleaned_up(log)

    def test_timeout_kills_unresponsive_cli_and_next_request_still_works(self):
        cli, log = self.fake_cli("hang")
        with self.assertRaisesRegex(ModelCatalogError, "вовремя"):
            discover_models(str(cli), timeout=1)
        self.assert_cleaned_up(log)
        cli, log = self.fake_cli("legacy")
        self.assertEqual(len(discover_models(str(cli))["models"]), 2)
        self.assert_cleaned_up(log)
        for timeout in (0, -1, float("nan"), True):
            with self.assertRaises(ModelCatalogError):
                discover_models(str(cli), timeout=timeout)
        with self.assertRaisesRegex(ModelCatalogError, "не найден"):
            discover_models("/not-installed/gigacode")

    def test_cleanup_falls_back_to_signaling_the_child_when_group_is_denied(self):
        cli, log = self.fake_cli()
        with patch("flexbench.models.os.killpg", side_effect=PermissionError):
            self.assertEqual(len(discover_models(str(cli))["models"]), 2)
        self.assert_cleaned_up(log)

    def test_cli_lists_or_exports_catalog_without_starting_a_benchmark(self):
        cli, _ = self.fake_cli()
        profile = self.root / "profile.json"
        save(profile, {"id": "test-catalog", "executor": "gigacode", "mode": "command", "adapter": "gigacode",
                       "executable": str(cli), "timeout_seconds": 10})
        output = self.root / "catalog/models.json"
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["flexbench", "models", "--profile", str(profile), "--json", "--output", str(output)]), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(), 0, stderr.getvalue())
        catalog = json.loads(stdout.getvalue())
        self.assertEqual(read(output), catalog)
        self.assertEqual(catalog["executor_version"], "test-cli-1")
        self.assertEqual(catalog["current_model_id"], "model-a")
        self.assertEqual(len(catalog["models"]), 2)
        self.assertFalse(list(self.root.rglob("run.json")))
        self.assertFalse(list(self.root.rglob("result.json")))


if __name__ == "__main__":
    unittest.main()
