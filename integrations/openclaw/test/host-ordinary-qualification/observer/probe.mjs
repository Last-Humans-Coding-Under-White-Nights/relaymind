// Test-only passive process observer. No RPC or authority is registered.
import {appendFileSync,statSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import {definePluginEntry} from 'openclaw/plugin-sdk/plugin-entry';
import {observeFetch} from '../../host-action-qualification/fetch-observer.mjs';
const key=Symbol.for('relaymind.ordinary.fixture.fetch-observer');
export default definePluginEntry({id:'relaymind-ordinary-observer',name:'Ordinary entry passive observer',register(api){
 const record=value=>{if(statSync(api.pluginConfig.eventFile).size>1048576)throw Error('capture_bound');appendFileSync(api.pluginConfig.eventFile,JSON.stringify({...value,monotonic_ns:process.hrtime.bigint().toString()})+'\n',{encoding:'utf8'});};
 // Keep one wrapper across plugin reload: production captures fetch at registration.
 // The owned disposable Gateway process ends after the fixture; no live user process is modified.
 if(globalThis[key]) {globalThis[key].record=record;return;}
 const state={record};globalThis[key]=state;
 const original=globalThis.fetch;
 globalThis.fetch=async(url,options)=>{const attempt_id=randomUUID();const observed=observeFetch(original,event=>state.record({...event,attempt_id}));try{return await observed(url,options);}catch(error){state.record({kind:'http_error',attempt_id,path:new URL(url).pathname,aborted:options?.signal?.aborted===true});throw error;}};
}});
