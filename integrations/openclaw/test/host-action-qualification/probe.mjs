// TEST ONLY: same production factories, real host authenticated-request context.
import { readFileSync, appendFileSync, statSync } from 'node:fs';
import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';
import { parsePluginConfig } from '../../dist/config.js';
import { nativeRecommendationDependencies, nativeAdapterRevision } from '../../dist/recommendation-native.js';
import { createNativeActionHandler } from '../../dist/recommendation-native-actions.js';
import { createNativeSkillHandler } from '../../dist/recommendation-native-skill.js';
export default definePluginEntry({
  id: 'relaymind-action-qualification', name: 'Disposable production-factory action qualification',
  register(api) {
    const record = value => { if (statSync(api.pluginConfig.eventFile).size > 1024*1024) throw Error('capture_bound'); appendFileSync(api.pluginConfig.eventFile, JSON.stringify(value)+'\n', {encoding:'utf8'}); };
    const fetcher = globalThis.fetch;
    globalThis.fetch = async (url, options) => {
      record({kind:'http_attempt', method:options?.method ?? 'GET', path:new URL(url).pathname});
      const response = await fetcher(url, options);
      record({kind:'http', path:new URL(url).pathname, status:response.status});
      return response;
    };
    const options = {scope:'operator.write',profileAccess:'required',sessionAccess:{mode:'write'}};
    for (const [name, factory] of [['relaymind.recommendations',createNativeActionHandler],['relaymind.skill',createNativeSkillHandler]]) {
      api.registerGatewayMethod(name, async request => {
        const config = parsePluginConfig(JSON.parse(readFileSync(api.pluginConfig.operatorFile, 'utf8')));
        record({kind:'dispatch', method:name, requestId:request.req.id, implementation_revision:nativeAdapterRevision(),
          profileId:request.client?.preparedSessionProfile?.profileId, connectionId:request.client?.presenceKey,
          authenticatedControlUi:request.client?.internal?.authenticatedControlUi === true,
          sessionId:request.sessionAccessAuthority?.target?.sessionId});
        const signal = request.client?.connectionSignal;
        const onAbort = () => record({kind:'connection_retired',requestId:request.req.id,connectionId:request.client?.presenceKey,aborted:signal?.aborted===true});
        signal?.addEventListener('abort',onAbort,{once:true});
        try {
          return await factory(nativeRecommendationDependencies(api,config))({...request, respond:(ok,payload,error) => {
            record({kind:'result',requestId:request.req.id,ok,error_code:error?.code??null,reason:error?.message??null});
            request.respond(ok,payload,error);
          }});
        } finally { signal?.removeEventListener('abort',onAbort); }
      }, options);
    }
  }
});
