import assert from 'node:assert/strict';
import {updateSelected} from './src/batch-update.mjs';
let calls = 0;
assert.deepEqual(await updateSelected(['a','a'], 1, id => {calls++; return id.toUpperCase();}), [
  {id:'a', status:'fulfilled', value:'A'}
]);
assert.equal(calls, 1);
console.log('Smoke passed');
