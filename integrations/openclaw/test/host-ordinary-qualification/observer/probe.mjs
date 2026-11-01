// Test-only passive process observer. No RPC or authority is registered.
import {appendFileSync,statSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import {definePluginEntry} from 'openclaw/plugin-sdk/plugin-entry';
import {observeFetch} from '../../host-action-qualification/fetch-observer.mjs';
const key=Symbol.for('relaymind.ordinary.fixture.fetch-observer');
export default definePluginEntry({id:'relaymind-ordinary-observer',name:'Ordinary entry passive observer',register(api){
 const record=value=>{let line;try{line=JSON.stringify({...value,monotonic_ns:process.hrtime.bigint().toString()})+'\n';}catch{throw Error('capture_invalid');}const size=Buffer.byteLength(line,'utf8');if(size>1048576||statSync(api.pluginConfig.eventFile).size+size>8388608)throw Error('capture_bound');appendFileSync(api.pluginConfig.eventFile,line,{encoding:'utf8'});};
 api.on('after_tool_call',(event,context)=>{
   if(event?.toolName!=='skills_read')return;
   record({kind:'native_skill_tool',event:{toolName:event.toolName,params:event.params,
     toolCallId:event.toolCallId,runId:event.runId,result:event.result,
     ...(event.error?{error:'native_tool_error'}:{})},
     context:{sessionKey:context?.sessionKey,sessionId:context?.sessionId,
       runId:context?.runId,toolCallId:context?.toolCallId}});
 });
 // Keep one wrapper across plugin reload: production captures fetch at registration.
 // The owned disposable Gateway process ends after the fixture; no live user process is modified.
 if(globalThis[key]) {globalThis[key].record=record;return;}
 const state={record};globalThis[key]=state;
 const original=globalThis.fetch;
 globalThis.fetch=async(url,options)=>{const attempt_id=randomUUID();const observed=observeFetch(original,event=>state.record({...event,attempt_id}));try{return await observed(url,options);}catch(error){state.record({kind:'http_error',attempt_id,path:new URL(url).pathname,aborted:options?.signal?.aborted===true});throw error;}};
}});
