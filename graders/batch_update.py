import json
from pathlib import Path
import subprocess
import sys

SCRIPT = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const root = process.argv[1];
const {mapLimit} = await import(pathToFileURL(root + '/src/map-limit.mjs').href);
const {updateSelected} = await import(pathToFileURL(root + '/src/batch-update.mjs').href);
const checks = [];
const turn = () => new Promise(resolve => setImmediate(resolve));
const gate = () => {let resolve;const promise = new Promise(r => {resolve=r;});return {promise,resolve};};
async function check(id, f) {
 let timer;
 try {await Promise.race([Promise.resolve().then(f), new Promise((_, reject) => {timer=setTimeout(() => reject(new Error('Check did not settle')), 1500);})]);checks.push({id,passed:true});}
 catch {checks.push({id,passed:false});} finally {clearTimeout(timer);}
}
await check('settled_order_and_index', async () => {
 const items=Object.freeze([5,3,9]); const seen=[];
 const result=await mapLimit(items,2,(x,i)=>{seen.push([x,i]);return x+i;});
 assert.deepEqual(result,[{status:'fulfilled',value:5},{status:'fulfilled',value:4},{status:'fulfilled',value:11}]);
 assert.deepEqual(seen,[[5,0],[3,1],[9,2]]);
});
await check('pool_capacity_and_refill', async () => {
 const gates=Array.from({length:4},gate); const started=[];
 const result=mapLimit([0,1,2,3],2,i=>{started.push(i);return gates[i].promise;});
 try {
  await turn();assert.deepEqual(started,[0,1]);
  gates[1].resolve('one');await turn();assert.deepEqual(started,[0,1,2]);
  gates[2].resolve('two');await turn();assert.deepEqual(started,[0,1,2,3]);
 } finally {gates.forEach((g,i)=>g.resolve('v'+i));}
 assert.deepEqual(await result,[{status:'fulfilled',value:'v0'},{status:'fulfilled',value:'one'},{status:'fulfilled',value:'two'},{status:'fulfilled',value:'v3'}]);
});
await check('single_slot', async () => {
 const gates=[gate(),gate()];const started=[];const p=mapLimit([0,1],1,i=>{started.push(i);return gates[i].promise;});
 try{await turn();assert.deepEqual(started,[0]);gates[0].resolve(0);await turn();assert.deepEqual(started,[0,1]);}
 finally{gates.forEach((g,i)=>g.resolve(i));} await p;
});
await check('sync_throw_continues', async () => {
 const reason={code:7};const seen=[];
 const r=await mapLimit([0,1,2],1,i=>{seen.push(i);if(i===1)throw reason;return i;});
 assert.deepEqual(seen,[0,1,2]);assert.deepEqual(r,[{status:'fulfilled',value:0},{status:'rejected',reason},{status:'fulfilled',value:2}]);assert.equal(r[1].reason,reason);
});
await check('async_rejection_continues', async () => {
 const error=new Error('expected');const r=await mapLimit([0,1,2],2,async i=>{if(i===0)throw error;return i;});
 assert.equal(r[0].reason,error);assert.deepEqual(r.slice(1),[{status:'fulfilled',value:1},{status:'fulfilled',value:2}]);
});
await check('invalid_concurrency_before_work', async () => {
 for(const n of [0,-1,1.5,NaN,Infinity,'2',true,Number.MAX_SAFE_INTEGER+1]){
  let calls=0;await assert.rejects(()=>mapLimit([1],n,()=>{calls++;}),RangeError);assert.equal(calls,0);
  await assert.rejects(()=>mapLimit([],n,()=>{}),RangeError);
 }
});
await check('invalid_pool_arguments', async () => {
 for(const items of [null,{},'abc'])await assert.rejects(()=>mapLimit(items,1,()=>{}),TypeError);
 for(const worker of [null,{},1])await assert.rejects(()=>mapLimit([],1,worker),TypeError);
});
await check('empty_and_large_limit', async () => {
 let calls=0;assert.deepEqual(await mapLimit([],1,()=>{calls++;}),[]);assert.equal(calls,0);
 assert.deepEqual(await mapLimit([1],99,x=>x),[{status:'fulfilled',value:1}]);
});
await check('independent_pools', async () => {
 const [a,b]=await Promise.all([mapLimit([1,2],1,async x=>x*2),mapLimit([8],3,async x=>x+1)]);
 assert.deepEqual(a,[{status:'fulfilled',value:2},{status:'fulfilled',value:4}]);assert.deepEqual(b,[{status:'fulfilled',value:9}]);
});
await check('batch_deduplication_and_order', async () => {
 const ids=Object.freeze(['b','a','b','c','a']);const calls=[];
 const result=await updateSelected(ids,2,id=>{calls.push(id);return id.toUpperCase();});
 assert.deepEqual(calls,['b','a','c']);assert.deepEqual(result,['b','a','c'].map(id=>({id,status:'fulfilled',value:id.toUpperCase()})));
});
await check('batch_partial_failure', async () => {
 const error={message:'failure'};const r=await updateSelected(['a','b','c'],1,id=>{if(id==='b')throw error;return id;});
 assert.deepEqual(r,[{id:'a',status:'fulfilled',value:'a'},{id:'b',status:'rejected',reason:error},{id:'c',status:'fulfilled',value:'c'}]);assert.equal(r[1].reason,error);
});
await check('batch_capacity_refill_and_out_of_order', async () => {
 const gates=Array.from({length:3},gate);const started=[];
 const p=updateSelected(['a','b','c','a'],2,id=>{started.push(id);return gates['abc'.indexOf(id)].promise;});
 try{await turn();assert.deepEqual(started,['a','b']);gates[1].resolve('B');await turn();assert.deepEqual(started,['a','b','c']);}
 finally{gates.forEach((g,i)=>g.resolve('v'+i));}
 assert.deepEqual(await p,[{id:'a',status:'fulfilled',value:'v0'},{id:'b',status:'fulfilled',value:'B'},{id:'c',status:'fulfilled',value:'v2'}]);
});
await check('batch_validate_all_ids_before_work', async () => {
 for(const bad of [null,'','  ',1,{},false]){
  let calls=0;await assert.rejects(()=>updateSelected(['valid',bad],1,()=>{calls++;}),TypeError);assert.equal(calls,0);
 }
 for(const ids of [null,{},'x'])await assert.rejects(()=>updateSelected(ids,1,()=>{}),TypeError);
});
await check('batch_validate_options_even_empty', async () => {
 for(const n of [0,-1,1.5,Infinity,'1',true,Number.MAX_SAFE_INTEGER+1])await assert.rejects(()=>updateSelected([],n,()=>{}),RangeError);
 for(const update of [null,{},3])await assert.rejects(()=>updateSelected([],1,update),TypeError);
 let calls=0;assert.deepEqual(await updateSelected([],1,()=>{calls++;}),[]);assert.equal(calls,0);
});
await check('literal_ids_and_independent_batches', async () => {
 const ids=['a','a ','__proto__','a'];let calls=0;const f=id=>{calls++;return id;};
 const a=await updateSelected(ids,2,f);const b=await updateSelected(ids,2,f);
 assert.equal(calls,6);assert.deepEqual(a,b);assert.deepEqual(a.map(x=>x.id),['a','a ','__proto__']);assert.deepEqual(ids,['a','a ','__proto__','a']);
});
console.log(JSON.stringify({checks}));
'''


if __name__ == "__main__":
    result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT, str(Path(sys.argv[1]).resolve())],
                            capture_output=True, text=True)
    if result.returncode:
        print(json.dumps({"checks": [{"id": "loads", "passed": False}], "detail": result.stderr[-4000:]}))
    else:
        print(json.dumps(json.loads(result.stdout)))
