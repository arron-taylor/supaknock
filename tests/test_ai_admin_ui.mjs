import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({window: {}});
vm.runInContext(readFileSync(new URL('../feedback/web/ai-admin.js', import.meta.url), 'utf8'), context);
const compatible = context.aiCompletedIndex;
test('authoring failures distinguish truncation, provider rejection and retained batches', () => {
  assert.match(context.aiAuthoringError({error:'incomplete_response'}), /Maximum response tokens/);
  assert.match(context.aiAuthoringError({error:'request_rejected'}), /provider rejected/);
  const message=context.aiAuthoringError({error:'invalid_response',coverage:{completed_batches:1}});
  assert.match(message, /affected batch/);
  assert.match(message, /Completed batches and their drafts are saved/);
  assert.doesNotMatch(context.aiAuthoringError({error:'invalid_response'}), /stopped before finishing/);
});
test('section prerequisites explain source and model readiness while permitting preserved indices', () => {
  const config={readiness:{ready:false,issues:['answer_model_unverified','knowledge_not_ready']}};
  const source={active:true,approved:false,index_ready:false};
  const reasons=context.aiSectionPrerequisites(config,source);
  assert.equal(reasons.length,3);
  assert.equal(reasons[0].target,'providers');
  assert.match(reasons[1].message,/approve/i);
  assert.match(reasons[2].message,/indexing/i);
  assert.equal(context.aiSectionPrerequisites({readiness:{ready:true,issues:[]}}, {...source,approved:true,index_ready:true,index_status:'failed',index_stale:true}).length,0);
  assert.equal(context.aiSectionPrerequisites({readiness:{ready:true,issues:[]}}, {...source,approved:true,index_ready:true},true).length,1);
});
const before = {
  version: 2, name: 'Support', kind: 'markdown', location: null,
  active: true, approved: false, deleted_at: null,
  latest_document: {id: 'immutable-original'},
  index_status: 'pending', job: {id: 'ingest-job'}, active_revision_id: null,
};
const published = {...before, version: 3, index_status: 'indexed', job: null, active_revision_id: 'indexed-original'};

test('normal indexing completion advances only its observed version without changing the form baseline', () => {
  const original = structuredClone(before);
  assert.equal(compatible(before, published), true);
  assert.equal(compatible({...before, index_status: 'indexing'}, published), true);
  assert.deepEqual(before, original);
  assert.equal(before.approved, false);
});

test('new originals, permission changes, removal and intervening revisions require explicit review', () => {
  const changes = [
    {latest_document: {id: 'replacement-original'}}, {latest_document: null},
    {approved: true}, {active: false}, {deleted_at: '2026-10-03'},
    {name: 'Changed by another admin'}, {kind: 'website'}, {location: 'https://example.test/docs'},
    {version: 4}, {version: 2},
  ];
  for (const change of changes) assert.equal(compatible(before, {...published, ...change}), false, JSON.stringify(change));
});

test('queued replacement, failed refresh or unchanged old index cannot impersonate index publication', () => {
  for (const change of [{job: {id: 'replacement-job'}}, {index_status: 'failed'}, {index_status: 'pending'}, {active_revision_id: null}]) {
    assert.equal(compatible(before, {...published, ...change}), false, JSON.stringify(change));
  }
  assert.equal(compatible({...before, active_revision_id: 'old'}, {...published, active_revision_id: 'old'}), false);
  assert.equal(compatible({...before, job: null}, published), false);
  assert.equal(compatible({...before, index_status: 'indexed'}, published), false);
  assert.equal(compatible({...before, latest_document: null}, published), false);
});

const providerConfig={has_key:true,catalogue:['gpt-6-luna'],settings:{provider:'kodekey',model:'gpt-6-luna',enabled:true,tools:{search_knowledge:true}}};
const draft=context.aiProviderDraft,write=context.aiProviderWrite;
const plain=value=>JSON.parse(JSON.stringify(value));
const providerModels={kodekey:'gpt-6-luna',gemini:'gemini-3.8-flash',mistral:'ministral-14b-2512',groq:'openai/gpt-oss-120b'};

test('switching provider hides previous catalogue and forbids checks using the old saved key',()=>{
  const original=structuredClone(providerConfig);
  for(const [saved,model] of Object.entries(providerModels))for(const selected of Object.keys(providerModels)){
    if(saved===selected)continue;
    const config={...providerConfig,catalogue:[model],settings:{...providerConfig.settings,provider:saved,model}};
    const state=draft(config,selected,model);
    assert.equal(state.changed,true);assert.equal(state.dirty,true);assert.equal(state.hasKey,false);
    assert.deepEqual(plain(state.catalogue),[]);assert.equal(state.canRefresh,false);assert.equal(state.canProbe,false);
  }
  assert.deepEqual(providerConfig,original);
});

test('provider switches explicitly clear credentials and model and disable AI without changing tool permissions',()=>{
  for(const [saved,model] of Object.entries(providerModels))for(const selected of Object.keys(providerModels)){
    if(saved===selected)continue;
    const config={...providerConfig,settings:{...providerConfig.settings,provider:saved,model}};
    assert.deepEqual(plain(write(config,selected,model)),{settings:{provider:selected,model:null,enabled:false},key:null});
    assert.equal(config.settings.tools.search_knowledge,true);
    assert.deepEqual(plain(write(config,selected,model,'new-owner-key')),
      {settings:{provider:selected,model:null,enabled:false},key:'new-owner-key'});
  }
});

test('unsaved model or replacement key prevents accidental checks of saved provider settings',()=>{
  for(const [model,key] of [['another-model',''],['gpt-6-luna','replacement']]){
    const state=draft(providerConfig,'kodekey',model,key);
    assert.equal(state.canRefresh,false);assert.equal(state.canProbe,false);
  }
  const saved=draft(providerConfig,'kodekey','gpt-6-luna');
  assert.equal(saved.canRefresh,true);assert.equal(saved.canProbe,true);
  saved.catalogue.push('not-a-saved-model');
  assert.deepEqual(providerConfig.catalogue,['gpt-6-luna']);
});

test('each provider setup requires explicit key, catalogue, exact model and probe steps',()=>{
  for(const [provider,model] of Object.entries(providerModels)){
    const fresh={has_key:false,catalogue:[],settings:{provider,model:null,enabled:false}};
    assert.equal(draft(fresh,provider,'').canRefresh,false);
    const keyed={...fresh,has_key:true};
    assert.equal(draft(keyed,provider,'').canRefresh,true);
    assert.equal(draft(keyed,provider,'').canProbe,false);
    const listed={...keyed,catalogue:[model]};
    assert.equal(draft(listed,provider,model).canProbe,false);
    const payload=plain(write(listed,provider,model));
    assert.deepEqual(payload,{settings:{provider,model}});
    const selected={...listed,settings:{...listed.settings,...payload.settings}};
    assert.equal(draft(selected,provider,model).canProbe,true);
    assert.equal(selected.settings.enabled,false);
  }
});

test('unchanged KodeKey saves preserve its key while switching back from Gemini removes the Gemini key',()=>{
  assert.deepEqual(plain(write(providerConfig,'kodekey','gpt-6-luna')),{settings:{provider:'kodekey',model:'gpt-6-luna'}});
  const gemini={...providerConfig,settings:{...providerConfig.settings,provider:'gemini',model:'gemini-3.8-flash'}};
  assert.deepEqual(plain(write(gemini,'kodekey','gemini-3.8-flash')),
    {settings:{provider:'kodekey',model:null,enabled:false},key:null});
});

test('99 named keys stay in bounded pages and filter by name, group and effective status',()=>{
  const keys=Array.from({length:99},(_,i)=>({id:'opaque-'+i,label:'Support '+i,quota_group:i%2?'Shared account':'Other account',status:i%3?'ready':'paused'}));
  const first=context.aiKeyPage(keys);assert.equal(first.items.length,25);assert.equal(first.pages,4);assert.equal(first.total,99);
  const last=context.aiKeyPage(keys,'','',99);assert.equal(last.page,3);assert.equal(last.items.length,24);
  const filtered=context.aiKeyPage(keys,'shared ACCOUNT','paused',3);
  assert.equal(filtered.page,0);assert.equal(filtered.items.length,16);
  assert.ok(filtered.items.every(key=>key.quota_group==='Shared account'&&key.status==='paused'));
  assert.equal(context.aiKeyPage(keys,'no matches').total,0);
  assert.equal(keys.length,99);assert.equal(keys[0].id,'opaque-0');
});

test('key writes retain observed versions, preserve exact labels and only send newly supplied secrets',()=>{
  const pool={version:7},key={version:3};
  const values={label:'Work / US',quota_group:'Billing Account A',enabled:false,key:''};
  assert.deepEqual(plain(context.aiKeyWrite(pool,key,values)),{version:7,key_version:3,label:values.label,quota_group:values.quota_group,enabled:false});
  assert.deepEqual(plain(context.aiKeyWrite(pool,null,{...values,key:'new-key'})),{version:7,label:values.label,quota_group:values.quota_group,key:'new-key'});
  assert.equal(pool.version,7);assert.equal(key.version,3);
});

test('health-only updates preserve an open editor while remote configuration or credential changes conflict',()=>{
  const key={id:'Key-A',version:2,status:'cooling_down'},opened={provider:'groq',version:6,key};
  const pool={provider:'groq',version:6,keys:[{...key,status:'recovering'}]};
  assert.equal(context.aiKeyEditorConflict(opened,pool),false);
  for(const update of [{version:7},{provider:'mistral'},{keys:[]},{keys:[{...key,version:3}]}]){
    assert.equal(context.aiKeyEditorConflict(opened,{...pool,...update}),true);
  }
  assert.equal(context.aiKeyEditorConflict({...opened,key:null},pool),false);
  assert.equal(key.status,'cooling_down');
});

test('Groq recommendation requires an actual returned catalogue entry and never changes saved state',()=>{
  const config={settings:{provider:'groq',model:null,enabled:false}},pool={provider:'groq',catalogue:[]};
  assert.equal(context.aiSuggestedModel(config,pool),'');
  assert.equal(context.aiSuggestedModel(config,{...pool,catalogue:['openai/gpt-oss-20b']}),'');
  assert.equal(context.aiSuggestedModel(config,{...pool,catalogue:['openai/gpt-oss-120b']}),'openai/gpt-oss-120b');
  assert.equal(context.aiSuggestedModel(config,{provider:'mistral',catalogue:['openai/gpt-oss-120b']}),'');
  assert.equal(context.aiSuggestedModel({settings:{...config.settings,model:'openai/gpt-oss-20b'}},pool),'openai/gpt-oss-20b');
  assert.deepEqual(config,{settings:{provider:'groq',model:null,enabled:false}});
});

test('permission drafts survive key operations that leave saved permissions unchanged',()=>{
  const baseline={enabled:false,allow_guest_reports:false,tools:{search_knowledge:false,support_link:false}};
  const draft={...baseline,tools:{...baseline.tools,support_link:true}};
  const saved={...baseline,tools:{support_link:false,search_knowledge:false}};
  assert.deepEqual(plain(context.aiPermissionDraftState(saved,baseline,draft)),{dirty:true,conflict:false});
  assert.equal(draft.tools.support_link,true);assert.equal(saved.tools.support_link,false);
});

test('automatic disable or remote permission changes require review without erasing unsaved choices',()=>{
  const baseline={enabled:true,allow_guest_reports:false,tools:{search_knowledge:false}};
  const draft={...baseline,allow_guest_reports:true};
  for(const saved of [{...baseline,enabled:false},{...baseline,tools:{search_knowledge:true}}]){
    assert.deepEqual(plain(context.aiPermissionDraftState(saved,baseline,draft)),{dirty:true,conflict:true});
  }
  assert.equal(draft.enabled,true);assert.equal(draft.allow_guest_reports,true);
  assert.deepEqual(plain(context.aiPermissionDraftState(baseline,baseline,baseline)),{dirty:false,conflict:false});
});

test('crawl summaries distinguish skipped URLs from a failed import and explain next steps', () => {
  assert.equal(context.aiCrawlSummary({fetched_count:3,skipped_count:4,failed_count:0}), '3 pages collected · 4 URLs skipped');
  assert.match(context.aiCrawlSummary({fetched_count:1,failed_count:1}), /1 failed/);
  assert.match(context.aiCrawlReason('redirect_refused'), /final page URL/);
  assert.match(context.aiCrawlReason('login_page'), /Sign-in/);
  assert.doesNotMatch(context.aiCrawlReason('unknown_server_value'), /unknown_server_value/);
  assert.match(context.aiCrawlFailure({error:'empty_page',fetched_count:0}), /No usable pages/);
  const aborted = context.aiCrawlFailure({error:'timeout',fetched_count:2});
  assert.match(aborted, /could not finish/);
  assert.match(aborted, /Previous knowledge has been kept/);
});
