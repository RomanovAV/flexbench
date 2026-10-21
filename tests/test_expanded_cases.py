import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flexbench.__main__ import ROOT, case_data, grade, prepare, read, runtime
from flexbench.reporting import CHECK_DIMENSIONS, diagnose
from expanded_solutions import IDS, apply


class ExpandedCasesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def workspace(self, case_id):
        folder = self.root / ('workspace-' + str(len(list(self.root.iterdir()))))
        shutil.copytree(ROOT / 'cases' / case_id / 'fixture', folder)
        return folder

    def evaluate(self, case_id, workspace):
        case, _, grader = case_data(case_id)
        process = subprocess.run([sys.executable, str(grader), str(workspace)], capture_output=True, text=True,
                                 timeout=case['grader_timeout_seconds'])
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result['checks'])
        self.assertEqual(len({c['id'] for c in result['checks']}), len(result['checks']))
        return result

    def assert_rejected(self, case_id, path, change, check_id):
        workspace = self.workspace(case_id)
        apply(case_id, workspace)
        target = workspace / path
        original = target.read_text()
        mutated = change(original)
        self.assertNotEqual(mutated, original, 'Mutation must alter the control implementation')
        target.write_text(mutated)
        result = self.evaluate(case_id, workspace)
        by_id = {c['id']: c['passed'] for c in result['checks']}
        self.assertFalse(by_id.get(check_id, True), result)
        self.assertNotIn('compiles', [c['id'] for c in result['checks'] if not c['passed']], result)
        self.assertNotIn('loads', by_id, result)
        diagnosis = diagnose({'status':'acceptance_failure', 'functional_pass':False, 'case':{'id':case_id},
                              'run_id':'mutated-control', 'evaluation':result, 'profile':{'id':'control'}, 'observations':[]})
        self.assertTrue(any('mutated-control:check:' + check_id in direction['evidence']
                            for direction in diagnosis['directions']), diagnosis)

    def test_originals_fail_and_controls_pass_through_runner(self):
        for case_id in IDS:
            with self.subTest(case=case_id):
                bad = prepare(case_id, ROOT / 'profiles/gigacode-manual.json', self.root / 'runs')
                result = grade(bad)
                self.assertEqual(result['status'], 'acceptance_failure', result)
                self.assertNotIn('compiles', [c['id'] for c in result['evaluation']['checks'] if not c['passed']])
                self.assertNotIn('loads', [c['id'] for c in result['evaluation']['checks']])
                good = prepare(case_id, ROOT / 'profiles/gigacode-manual.json', self.root / 'runs')
                apply(case_id, good / 'workspace')
                result = grade(good)
                self.assertEqual(result['status'], 'passed', result)
                self.assertTrue(all(c['id'] in CHECK_DIMENSIONS[case_id] for c in result['evaluation']['checks']))
                self.assertEqual(read(good / 'result.json')['candidate_digest'], result['candidate_digest'])
                self.assertFalse((good / 'workspace/graders').exists())
                self.assertFalse((good / 'workspace/expanded_solutions.py').exists())
                command = ['node', 'smoke.mjs'] if case_id == 'js-batch-update' else [sys.executable, 'smoke.py']
                smoke = subprocess.run(command, cwd=good / 'workspace', capture_output=True, text=True, timeout=20)
                self.assertEqual(smoke.returncode, 0, smoke.stderr)

    def test_reset_rejects_partial_fixes(self):
        path = 'src/TaskResetService.java'
        mutations = [
            (lambda s:s.replace('if(!command.admin() && !task.owner().equals(command.actor())) throw new SecurityException();',''), 'unauthorized'),
            (lambda s:s.replace('if(command.requestId().equals(task.lastResetId())) return task;', ''), 'idempotent_retry'),
            (lambda s:s.replace('command.requestId(),task.payload()', 'command.requestId(),null'), 'preserve_payload'),
            (lambda s:s.replace('if(task.version()==Long.MAX_VALUE) throw new IllegalStateException();',''), 'version_overflow'),
        ]
        for change, check_id in mutations:
            with self.subTest(check=check_id):
                self.assert_rejected('java-task-reset', path, change, check_id)

    def test_recommendations_reject_partial_fixes(self):
        mutations = [
            (lambda s:s.replace('c.revision()>old.revision()', 'c.revision()>=old.revision()'), 'first_equal_revision'),
            (lambda s:s.replace('c.dueAt().isBefore(now)', '!c.dueAt().isAfter(now)'), 'carryover_and_boundary'),
            (lambda s:s.replace('if(used.getOrDefault(key,0)>=index.get(key).quota()) continue;', ''), 'quota_per_pair'),
            (lambda s:s.replace('new Key(c.taskType(),c.source())', 'new Key(c.taskType(),"S")'), 'composite_rule'),
        ]
        for change, check_id in mutations:
            with self.subTest(check=check_id):
                self.assert_rejected('java-recommendation-rules', 'src/RecommendationSelector.java', change, check_id)

    def test_batch_rejects_partial_fixes(self):
        mutations = [
            ('src/map-limit.mjs', lambda s:s.replace('Math.min(concurrency, items.length)', 'items.length'), 'pool_capacity_and_refill'),
            ('src/map-limit.mjs', lambda s:s.replace('results[index] = {status:\'rejected\', reason};', 'throw reason;'), 'async_rejection_continues'),
            ('src/batch-update.mjs', lambda s:s.replace('[...new Set(ids)]', '[...ids]'), 'batch_deduplication_and_order'),
            ('src/batch-update.mjs', lambda s:s.replace('...result', "status:'fulfilled', value:result.value"), 'batch_partial_failure'),
        ]
        for path, change, check_id in mutations:
            with self.subTest(check=check_id):
                self.assert_rejected('js-batch-update', path, change, check_id)

    def test_recommendations_reject_normalization_and_blank_ids(self):
        def normalize_keys(source):
            return source.replace('new Key(r.taskType(),r.source())', 'new Key(r.taskType().trim(),r.source().trim())').replace(
                'new Key(c.taskType(),c.source())', 'new Key(c.taskType().trim(),c.source().trim())')

        mutations = [
            (lambda s:s.replace('invalid(c.id())', '(c.id()==null || c.id().isEmpty())'), 'skip_invalid_candidate_id'),
            (normalize_keys, 'literal_rule_matching'),
            (normalize_keys, 'distinct_literal_rule_keys'),
            (lambda s:s.replace('winners.get(c.id())', 'winners.get(c.id().trim())').replace('winners.put(c.id(),c)', 'winners.put(c.id().trim(),c)'), 'literal_candidate_ids'),
            (lambda s:s.replace('c==null || ', ''), 'skip_null_candidate'),
            (lambda s:s.replace(' || c.dueAt()==null', ''), 'skip_null_due_at'),
        ]
        for change, check_id in mutations:
            with self.subTest(check=check_id):
                self.assert_rejected('java-recommendation-rules', 'src/RecommendationSelector.java', change, check_id)

    def test_recommendation_grader_upgrade_changes_the_case_version_and_digest(self):
        case, _, _ = case_data('java-recommendation-rules')
        self.assertEqual(case['version'], 2)
        # Digest of v1 in the supplied, sealed target-machine report.
        self.assertNotEqual(case['digest'], 'f9595f47ba1ea24ae6289376abd0d43a9320560aae0cdb0d9989653111a36fa0')

    def test_indexes_reject_partial_fixes(self):
        mutations = [
            (lambda s:s.replace('owner_id, status, scheduled_at, id', 'status, scheduled_at, owner_id, id'), 'next_plan'),
            (lambda s:s.replace('ON activities(owner_id, status, scheduled_at, id)', "ON activities(owner_id, status, scheduled_at, id) WHERE status='PLANNED'"), 'window_plan'),
            (lambda s:s.replace('CREATE INDEX', 'CREATE UNIQUE INDEX'), 'only_activity_indexes_added'),
            (lambda s:s + "\nDELETE FROM activities WHERE id=1;", 'data_preserved'),
        ]
        for change, check_id in mutations:
            with self.subTest(check=check_id):
                self.assert_rejected('sql-activity-indexes', 'migration.sql', change, check_id)

    def test_packaging_rejects_partial_fixes(self):
        mutations = [
            ('deploy.env', lambda s:s.replace('17.0.13', '17.0.12'), 'deployment_image_and_port'),
            ('entrypoint.sh', lambda s:s.replace('"$@"', '$@'), 'quoted_paths_and_arguments'),
            ('entrypoint.sh', lambda s:s.replace('*[!0-9]*|0*', '*[!0-9]*'), 'invalid_heap_before_launch'),
            ('entrypoint.sh', lambda s:s.replace('exec "${JAVA_BIN:-java}" "$@"', '"${JAVA_BIN:-java}" "$@"\nexit 0'), 'exec_process_identity'),
        ]
        for path, change, check_id in mutations:
            with self.subTest(check=check_id):
                self.assert_rejected('service-launch-contract', path, change, check_id)

    def test_source_provenance_present(self):
        for case_id in IDS:
            case, _, _ = case_data(case_id)
            self.assertEqual(case['fidelity'], 'synthetic-derived')
            self.assertTrue(case['source']['repository'])
            self.assertTrue(case['source']['evidence'])
            self.assertIn('Designed benchmark', case['source']['contract'])

    def test_sqlite_version_is_recorded(self):
        import sqlite3
        case, _, _ = case_data('sql-activity-indexes')
        self.assertEqual(runtime(case)['sqlite'], sqlite3.sqlite_version)

    def test_invalid_runtime_module_declarations_fail_preflight(self):
        case = json.loads((ROOT / 'cases/sql-activity-indexes/case.json').read_text())
        for modules in (None, 'sqlite3', ['sqlite3', 'sqlite3'], ['unknown'], [{}]):
            with self.subTest(modules=modules), patch('flexbench.__main__.read', return_value={**case, 'runtime_modules':modules}):
                with self.assertRaisesRegex(ValueError, 'runtime_modules'):
                    case_data('sql-activity-indexes')

    def test_destructive_sql_is_a_failed_acceptance_check(self):
        self.assert_rejected('sql-activity-indexes', 'migration.sql', lambda s:s+'\nDROP TABLE activities;', 'schema_usable')


if __name__ == '__main__':
    unittest.main()
