import json
from pathlib import Path
import subprocess
import sys

SCRIPT = r'''
import { pathToFileURL } from 'node:url';
const { canRetry, reduceRetry: r } = await import(pathToFileURL(process.argv[1]).href);
const checks = [];
const check = (id, f) => { try { checks.push({id, passed: f() === true}); } catch { checks.push({id, passed:false}); } };
const state = (status='idle', more={}) => Object.freeze({status, requestId:null, error:null, attempts:0, extra:42, ...more});
check('retry_only_failed', () => ['idle','pending','succeeded'].every(s => canRetry(state(s)) === false) && canRetry(state('failed')) === true);
check('start', () => { const s=state('failed',{error:'old',attempts:2}); const n=r(s,Object.freeze({type:'START',requestId:'new'})); return n.status==='pending' && n.error===null && n.attempts===3 && n.requestId==='new' && n.extra===42 && s.error==='old'; });
check('start_idle', () => r(state(), {type:'START',requestId:'r'}).status==='pending');
check('start_invalid', () => [null,'',0].every(id => { const s=state(); return r(s,{type:'START',requestId:id})===s; }));
check('duplicate_start', () => ['pending','succeeded'].every(status => {const s=state(status);return r(s,{type:'START',requestId:'new'})===s;}));
check('stale_result', () => ['SUCCESS','FAILURE'].every(type => {const s=state('pending',{requestId:'new'});return r(s,{type,requestId:'old',error:'x'})===s;}));
check('result_requires_pending', () => ['idle','failed','succeeded'].every(status => ['SUCCESS','FAILURE'].every(type => {const s=state(status,{requestId:'r'});return r(s,{type,requestId:'r'})===s;})));
check('success', () => {const s=state('pending',{requestId:'r',attempts:2,error:'old'});const n=r(s,{type:'SUCCESS',requestId:'r'});return n.status==='succeeded' && n.error===null && n.requestId==='r' && n.attempts===2 && n.extra===42;});
check('failure', () => {const s=state('pending',{requestId:'r',attempts:2});const n=r(s,{type:'FAILURE',requestId:'r',error:'bad'});return n.status==='failed' && n.error==='bad' && n.requestId==='r' && n.attempts===2 && n.extra===42;});
check('failure_fallback', () => [null,'',42].every(error => r(state('pending',{requestId:'r'}),{type:'FAILURE',requestId:'r',error}).error==='Unknown error'));
check('unknown_identity', () => {const s=state();return r(s,null)===s && r(s,{type:'OTHER'})===s;});
console.log(JSON.stringify({checks}));
'''


if __name__ == "__main__":
    result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT,
                             str(Path(sys.argv[1]) / "src/retry-state.mjs")], capture_output=True, text=True)
    if result.returncode:
        print(json.dumps({"checks": [{"id": "loads", "passed": False}], "detail": result.stderr[-4000:]}))
    else:
        data = json.loads(result.stdout)
        print(json.dumps(data))
