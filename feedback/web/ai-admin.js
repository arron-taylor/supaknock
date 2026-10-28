"use strict";
// Only an index publication may advance an open form's optimistic version.
// Permission/import changes require the administrator to review a fresh source.
function aiCompletedIndex(previous, next) {
  const sameEvidence = previous.latest_document?.id &&
    previous.latest_document.id === next.latest_document?.id &&
    ['name', 'kind', 'location', 'active', 'approved', 'deleted_at'].every(key => previous[key] === next[key]);
  return Boolean(sameEvidence && next.version === previous.version + 1 &&
    ['pending', 'indexing'].includes(previous.index_status) && previous.job && !next.job &&
    next.index_status === 'indexed' && next.active_revision_id &&
    next.active_revision_id !== previous.active_revision_id);
}
function aiSectionPrerequisites(config, source, changed=false) {
  const messages={
    answer_key_missing:['Select an answer API key.','providers'],
    answer_model_missing:['Choose and save an answer model.','providers'],
    answer_model_unavailable:['Refresh answer models and select an available model.','providers'],
    answer_model_unverified:['Verify the selected answer model.','providers'],
    answer_tools_unverified:['Verify the model’s tool access, or turn assistant tools off.','providers'],
    embedding_model_not_ready:['Choose a working embedding provider in AI runtime.','runtime']
  };
  const items=(config.readiness?.issues||[]).filter(code=>code!=='knowledge_not_ready').map(code=>{
    const [message,target]=messages[code]||['Refresh AI settings and review model readiness.','providers'];return {message,target};
  });
  if(changed)items.push({message:'This source changed. Refresh source status before generating.',target:null});
  if(source.deleted_at||!source.active)items.push({message:'Activate this source before generating sections.',target:null});
  if(!source.approved)items.push({message:'Review the original source, check Approved for AI answers, and save source permissions.',target:null});
  if(!source.index_ready)items.push({message:'Wait for a successful crawl and indexing. If the import failed, refresh the website or replace the Markdown original.',target:null});
  return items;
}
function aiCrawlReason(code) {
  const reasons={login_page:'Sign-in page — public content only',empty_page:'No readable content (the page may need JavaScript)',redirect_refused:'Redirect skipped; use the final page URL',robots_denied:'The website does not allow this page to be indexed',robots_unavailable:'Could not verify robots.txt for this website',invalid_robots:'The website’s robots rules could not be read',challenge_page:'Bot check or browser challenge',error_page:'The website returned an error page',http_failed:'Page unavailable or requires access',network_failed:'Could not connect to this page',blocked_destination:'Destination is not a permitted public address',unexpected_type:'Not an HTML page',unexpected_encoding:'Unsupported response compression',response_too_large:'Page exceeds the download size limit',text_limit:'Page exceeds the text size limit',invalid_text:'Page text could not be read',invalid_sitemap:'Sitemap could not be read; other pages can still import',byte_limit:'The import reached its total download limit',total_text_limit:'The import reached its total text limit',discovery_limit:'The import reached its discovery limit',timeout:'The import exceeded its time limit',cancelled:'Import cancelled'};
  return reasons[code] || 'The page could not be imported';
}
function aiCrawlSummary(run) {
  return `${run.fetched_count||0} ${run.fetched_count===1?'page':'pages'} collected · ${run.skipped_count||0} ${run.skipped_count===1?'URL':'URLs'} skipped`+(run.failed_count ? ` · ${run.failed_count} failed` : '');
}
function aiCrawlFailure(run) {
  return (run.fetched_count ? 'This import could not finish. ' : 'No usable pages were found. ')+aiCrawlReason(run.error)+'. Previous knowledge has been kept. Review the skipped URLs or try a more direct support-page URL.';
}
function aiAuthoringError(run) {
  const messages={
    incomplete_response:'The provider stopped before finishing its response. Retry unfinished batches, or increase Maximum response tokens in Settings → Usage limits and start a new generation.',
    request_rejected:'The provider rejected this request. Review the selected model and provider access before retrying.',
    invalid_response:'The provider response could not be validated. No drafts from the affected batch were saved.',
    budget_exhausted:'The project’s daily AI allowance is exhausted. Wait for the next UTC day or update the allowance before starting a new generation.',
    rate_limited:'The provider is limiting requests. Wait before retrying unfinished batches.',
    timeout:'The provider did not respond in time. You can retry unfinished batches.'
  };
  return (messages[run.error]||'Generation stopped: '+run.error)+(run.coverage?.completed_batches?' Completed batches and their drafts are saved.':'');
}
// Unsaved provider/key/model inputs must never drive a check of old credentials.
function aiProviderDraft(config, provider, model, key='') {
  const changed=provider!==config.settings.provider;
  const dirty=changed || Boolean(key) || (model || null)!==config.settings.model;
  return {changed,dirty,hasKey:!changed&&config.has_key,
    catalogue:changed?[]:[...config.catalogue],
    canRefresh:!dirty&&config.has_key,
    canProbe:!dirty&&config.has_key&&Boolean(config.settings.model)};
}
function aiProviderWrite(config, provider, model, key='') {
  const changed=provider!==config.settings.provider;
  return {settings:{provider,model:changed?null:(model||null),...(changed?{enabled:false}:{})},
    ...(key?{key}:changed?{key:null}:{})};
}
function aiKeyPage(keys, query='', status='', page=0, size=25) {
  const term=query.toLocaleLowerCase();
  const matches=keys.filter(key=>(!status||key.status===status)&&(!term||[key.label,key.quota_group].some(value=>String(value).toLocaleLowerCase().includes(term))));
  const pages=Math.max(1,Math.ceil(matches.length/size)),current=Math.max(0,Math.min(page,pages-1));
  return {items:matches.slice(current*size,(current+1)*size),total:matches.length,pages,page:current};
}
function aiKeyWrite(pool, key, values) {
  return {version:pool.version,...(key?{key_version:key.version,enabled:values.enabled}:{}),
    label:values.label,quota_group:values.quota_group,...(values.key?{key:values.key}:{})};
}
function aiKeyEditorConflict(opened, pool) {
  const current=opened.key&&pool.keys.find(key=>key.id===opened.key.id);
  return opened.provider!==pool.provider||opened.version!==pool.version||Boolean(opened.key&&(!current||current.version!==opened.key.version));
}
function aiSuggestedModel(config, pool) {
  return config.settings.model || (config.settings.provider==='groq'&&pool.provider==='groq'&&pool.catalogue.includes('openai/gpt-oss-120b')?'openai/gpt-oss-120b':'');
}
function aiPermissionDraftState(saved, baseline, draft) {
  const signature=value=>JSON.stringify([value.enabled,value.allow_guest_reports,Object.entries(value.tools).sort(([a],[b])=>a.localeCompare(b))]);
  const original=signature(baseline);
  return {dirty:signature(draft)!==original,conflict:signature(saved)!==original};
}
function aiAnswerChoices(library, provider, ids) {
  const keys=library.keys.filter(key=>key.provider===provider&&['generation','both'].includes(key.purpose));
  const selected=keys.filter(key=>ids.includes(key.id));
  // Every selected key must list the exact model we will verify.
  const models=selected.length?[...new Set(selected[0].catalogue)].filter(model=>selected.every(key=>key.catalogue.includes(model))).sort():[];
  return {keys,selected,models};
}
async function aiSaveAnswerSetup(request, base, initial, pool, library, draft, progress=()=>{}) {
  const choices=aiAnswerChoices(library,draft.provider,draft.keyIds);
  const disconnect=draft.disconnect===true&&!draft.keyIds.length&&draft.provider===initial.settings.provider&&draft.model===null;
  if((!draft.keyIds.length&&!disconnect)||choices.selected.length!==draft.keyIds.length)throw new Error('Choose an account key for this provider.');
  if(choices.selected.some(key=>!key.enabled))throw new Error('Uncheck paused keys before verifying, or enable them in account settings.');
  if(!disconnect&&!choices.models.includes(draft.model))throw new Error('Choose a model available to every selected key.');
  const changedProvider=draft.provider!==initial.settings.provider;
  const changed=changedProvider||draft.model!==initial.settings.model||JSON.stringify([...draft.keyIds].sort())!==JSON.stringify(pool.keys.map(key=>key.id).sort());
  progress('Checking saved settings…');
  const current=await request(base),currentLibrary=await request('/ai/keys');
  if(current.version!==initial.version||currentLibrary.version!==library.version)throw new Error('Settings or account keys changed. Reload saved setup and review your choices.');
  let config=initial,libraryVersion=library.version;
  if(changed){
    progress('Saving your choices…');
    config=await request(base,'PUT',{version:config.version,settings:{...config.settings,provider:draft.provider,model:changedProvider?null:draft.model,enabled:false}});
    // Switching provider detaches bindings and advances the library once.
    if(changedProvider)libraryVersion++;
  }
  pool=await request(base+'/keys/selection','PUT',{version:config.version,library_version:libraryVersion,key_ids:draft.keyIds});
  // Use only the version returned by our own write, never adopt a concurrent edit.
  config={...config,version:pool.version};
  if(config.settings.model!==draft.model){
    config=await request(base,'PUT',{version:config.version,settings:{...config.settings,model:draft.model,enabled:false}});
    pool={...pool,version:config.version};
  }
  for(const [index,id] of draft.keyIds.entries()){
    const key=pool.keys.find(key=>key.id===id);
    progress('Verifying '+key.label+' ('+(index+1)+' of '+draft.keyIds.length+')…');
    pool=await request(base+'/keys/'+encodeURIComponent(id)+'/model/probe','POST',{version:pool.version,key_version:key.version,tools:draft.tools});
    if(!pool.keys.find(key=>key.id===id)?.eligible)throw new Error('The model did not pass the required checks. Review its tool support and try again.');
  }
  config=await request(base);
  if(config.version!==pool.version)throw new Error('Settings changed during verification. Reload saved setup to review the result.');
  return {config,pool};
}
// Native administration only. All source/model text is inserted as text nodes.
window.SuggestionAI = (() => {
  const tools = [
    ['search_knowledge', 'Search again when needed', 'Every answer already uses approved knowledge. This optional tool lets the assistant look up more information during a conversation.'],
    ['support_link', 'Provide a support link', 'Return a validated source link.'],
    ['request_human', 'Request human assistance', 'Let the assistant offer a handoff. Visitors can still choose a human directly when this is off.'],
    ['create_feature_request', 'Prepare feature requests', 'Prepare a draft for the visitor to review and confirm.'],
    ['create_bug_report', 'Prepare bug reports', 'Prepare a draft for the visitor to review and confirm.'],
  ];
  const busyStates = ['queued', 'running', 'downloading', 'verifying', 'removing'];
  const replace = (target, ...children) => target.replaceChildren(...children.flat().filter(child=>child!==null && child!==undefined));
  const hint = text => el('p', {class:'hint'}, text);
  const loading = (caption='Loading AI support…') => el('div', {class:'ai-loading',role:'status'},
    el('p',{class:'hint'},caption),el('div',{class:'ai-skeleton','aria-hidden':'true'},
      ...Array.from({length:3},()=>el('span'))));
  async function leaveKnowledge(scope){return ![...scope.querySelectorAll('form[data-edited=true]')].some(f=>!f.dataset.busy)||await confirmChange('Leave unsaved edits?', 'Your saved knowledge is unchanged. Unsaved edits in this view will be discarded.', 'Leave edits');}
  function namedAction(text,label,action){const b=button(text,action);b.setAttribute('aria-label',label);return b;}
  function searchForm(fields,caption,submit,extra=null){const f=form(fields,caption,submit);f.classList.add('ai-search-form');if(extra)f.append(extra);return f;}
  const row = (...children) => el('div', {class:'actions'}, children);
  const date = value => value ? new Date(value).toLocaleString() : 'Not yet';
  const bytes = value => value < 1024 ? value+' bytes' : (value < 1048576 ? (value/1024).toLocaleString(undefined,{maximumFractionDigits:1})+' KiB' : (value/1048576).toLocaleString(undefined,{maximumFractionDigits:1})+' MiB');
  const lines = text => text.split('\n').map(x=>x.trim()).filter(Boolean);
  const nil = text => text.trim() || null;
  function requestIdentity() {
    let previous=null, id=null;
    const identify=body=>{const serialized=JSON.stringify(body);if(serialized!==previous){previous=serialized;id=crypto.randomUUID();}return id;};
    identify.reset=()=>{previous=null;};return identify;
  }
  const encode = encodeURIComponent;
  const stateNames = {active:'Active',not_downloaded:'Not installed', downloading:'Downloading', verifying:'Checking model', ready:'Ready', removing:'Removing', queued:'Queued', running:'In progress', partial:'Partially complete', failed:'Failed', cancelled:'Cancelled', succeeded:'Complete', empty:'No index', pending:'Waiting to index', indexing:'Indexing', indexed:'Indexed', inactive:'Inactive', deleted:'Deleted', fetched:'Fetched', skipped:'Skipped'};
  const state = name => el('span', {class:'pill '+name}, stateNames[name] || name);
  const sourceState = source => el('span', {class:'pill '+source.index_status, 'data-ai-source-state':source.id}, stateNames[source.index_status] || source.index_status);
  function external(text, href) {
    try {
      const url = new URL(href, location.origin);
      if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return el('span', {}, text);
      return el('a', {href:url.href, target:'_blank', rel:'noopener noreferrer', referrerpolicy:'no-referrer'}, text + ' ↗');
    } catch { return el('span', {}, text); }
  }
  function original(title, content) {
    return el('details', {class:'ai-original'}, el('summary', {}, title), el('pre', {tabindex:'0'}, content));
  }
  function citations(items) {
    return el('div', {class:'ai-citations'}, items.map(c=>el('details', {},
      el('summary', {}, c.title || 'Original source'),
      hint(c.kind === 'website' ? 'Website original' : 'Markdown original'),
      el('p', {class:'content'}, c.excerpt),
      c.url ? external('Open source', c.url) : hint('Uploaded source • available to administrators'),
    )));
  }
  let closeConfirmation=null;
  function confirmChange(title, message, actionLabel, target=root) {
    closeConfirmation?.(false);
    return new Promise(resolve=> {
      const previous=document.activeElement, id='ai-confirm-'+crypto.randomUUID();
      const area=el('section',{class:'panel ai-confirm',role:'alertdialog','aria-labelledby':id+'-title','aria-describedby':id+'-message',tabindex:'-1'},
        el('h3',{id:id+'-title'},title),el('p',{id:id+'-message'},message));
      function finish(confirmed){
        area.remove();if(closeConfirmation===finish)closeConfirmation=null;
        resolve(confirmed);
        setTimeout(()=>{if(closeConfirmation)return;if(previous?.isConnected&&!previous.disabled)previous.focus({preventScroll:true});else root.focus({preventScroll:true});},0);
      }
      closeConfirmation=finish;
      area.append(row(button('Cancel',()=>finish(false)),button(actionLabel,()=>finish(true),'danger')));
      area.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();finish(false);}});
      target.append(area);area.scrollIntoView({block:'center'});area.focus({preventScroll:true});
    });
  }
  function retainButtonFocus(box) {
    const focused=document.activeElement;
    const label=box.contains(focused)&&focused.tagName==='BUTTON'?focused.textContent:null;
    return ()=>{if(label)[...box.querySelectorAll('button')].find(b=>b.textContent===label)?.focus({preventScroll:true});};
  }
  function failure(box, error, retry) {
    replace(box,el('p', {class:'error', role:'alert'}, error.message || 'Unable to load this information.'), button('Try again', retry));
  }
  const watchers=new WeakMap();
  function periodic(node, read, draw, pending, interval=3000) {
    const previous=watchers.get(node);if(previous)clearTimeout(previous.timer);
    const watch={timer:null};watchers.set(node,watch);
    async function tick() {
      if (!node.isConnected || watchers.get(node)!==watch) return;
      if (!document.hidden) {
        try { const value = await read(); if (!node.isConnected) return; draw(value); if (!pending(value)) return; }
        catch (error) { node.append(el('p', {class:'error', role:'alert'}, 'Live updates stopped. Refresh to try again. ' + error.message)); return; }
      }
      watch.timer = setTimeout(tick, interval);
    }
    watch.timer = setTimeout(tick, interval);
    window.addEventListener('pagehide', ()=>clearTimeout(watch.timer), {once:true});
  }
  function tabs(entries, prefix="") {
    const nav = el('div', {class:'settings-tabs ai-tabs', role:'tablist', 'aria-label':'AI support settings'});
    const views = entries.map(([key,title,load], i)=> {
      const view = el('div', {id:'ai-'+key, role:'tabpanel', 'aria-labelledby':'ai-tab-'+key, tabindex:'0', hidden:true});
      const b = el('button', {id:'ai-tab-'+key, role:'tab', 'aria-controls':view.id, 'aria-selected':'false', tabindex:'-1', type:'button', onclick:()=>activate(i,true)}, title);
      nav.append(b); return view;
    });
    const loaded = new Set();
    function activate(i, focus=false, updateUrl=true) {
      views.forEach((v,j)=> {v.hidden=i!==j; nav.children[j].setAttribute('aria-selected', String(i===j));nav.children[j].tabIndex=i===j?0:-1;});
      if (focus) nav.children[i].focus();
      if(updateUrl) history.replaceState(null,'','#'+prefix+entries[i][0]);
      if(loaded.has(i))views[i]._refresh?.().catch(error=>notify(error.message));
      if (!loaded.has(i)) {
        loaded.add(i); const v=views[i];
        const load = async()=> {v.setAttribute('aria-busy','true');replace(v,loading('Loading '+entries[i][1].toLowerCase()+'…'));try {await entries[i][2](v);} catch(error) {failure(v,error,load);}finally{v.querySelector(':scope > .ai-loading')?.remove();v.setAttribute('aria-busy','false');}};
        load();
      }
    }
    nav.addEventListener('keydown', event=> {
      const i=[...nav.children].indexOf(document.activeElement); if(i<0)return;
      const next={ArrowRight:(i+1)%entries.length,ArrowLeft:(i+entries.length-1)%entries.length,Home:0,End:entries.length-1}[event.key];
      if(next!==undefined){event.preventDefault();activate(next,true);}
    });
    const fromHash=()=>{const hash=location.hash.slice(1).replace(/sections$/, 'knowledge/drafts');if(prefix&&!hash.startsWith(prefix)&&hash!==prefix.slice(0,-1))return;activate(Math.max(0,entries.findIndex(e=>e[0]===hash.slice(prefix.length).split('/')[0])),false,false);};
    window.addEventListener('hashchange',fromHash);window.addEventListener('popstate',fromHash);fromHash();
    return [nav,...views];
  }
  async function runtime() {
    if (!requireAdmin()) return;
    root.classList.add('ai-admin');
    let current = await api('/ai/runtime');
    const statusBox=el('div', {'aria-live':'polite'}), actionBox=el('div');
    const embeddingBox=el('div');
    await embeddingSettings(embeddingBox);
    const token=field('Your Hugging Face access token', 'token','password','',{required:true,autocomplete:'off',maxlength:2048});
    const downloadPanel=panel('Install model');
    const download=form([
      hint('Use your own Hugging Face account to install the model on this server. SupaKnock does not include a shared access token.'),
      el('ol',{},
        el('li',{},'Sign in to Hugging Face and accept the license agreement on the ',external('EmbeddingGemma model page',current.license_url),'.'),
        el('li',{},'Create a ',external('Hugging Face access token','https://huggingface.co/settings/tokens'),' in that same account with read access to this model.'),
        el('li',{},'Paste your token below, then choose Download model to this server.')),
      token,
      hint('Your token is encrypted while the download is in progress and removed when it completes, fails, or is cancelled. It is not needed to run the installed model.'),
    ], 'Download model to this server', async(data)=> {
      const value=data.get('token');token.querySelector('input').value='';
      current=await api('/ai/runtime/download','POST',{version:current.version,token:value});
      draw(current);watch();notify('Download requested');
    });
    function draw(value) {
      const restoreFocus=retainButtonFocus(actionBox);
      current=value;
      const active=['downloading','verifying','removing'].includes(value.state);
      replace(statusBox,el('div',{class:'ai-status-line'},state(value.state),el('span',{},value.model_id)),
        hint('Model files: '+bytes(value.download_bytes)+' • stored in the server’s persistent data volume.'),
        active ? el('progress',{max:value.total_bytes || value.download_bytes,value:value.progress_bytes,'aria-label':'Model download progress'}) : null,
        active ? hint(bytes(value.progress_bytes)+' of '+bytes(value.total_bytes || value.download_bytes)) : null,
        value.error ? el('p',{class:'error',role:'alert'},value.error==='model_access_required'
          ? 'Hugging Face denied model access. Accept the model license in your own account, then retry with a token from that account that can read this model.'
          : 'Operation failed: '+value.error+'. Review access and available server storage before retrying.') : null,
        hint('Last update: '+date(value.updated_at)));
      downloadPanel.hidden=active || value.state==='ready';
      replace(actionBox,row(button('Refresh status',async()=>{draw(await api('/ai/runtime'));if(['downloading','verifying','removing'].includes(current.state))watch();}),
        ['downloading','verifying'].includes(value.state) ? button('Cancel download',async()=>{draw(await api('/ai/runtime/cancel','POST',{version:current.version}));notify('Download cancelled');}) : null,
        value.active_cache_id && !active ? button('Remove installed model',async()=>{
          if(!await confirmChange('Remove the embedding model?', 'This removes local model files and disables AI for every project. Original knowledge remains stored. Removal cannot be cancelled after it starts.', 'Remove model'))return;
          draw(await api('/ai/runtime/remove','POST',{version:current.version}));watch();
        },'danger') : null));
      restoreFocus();
    }
    function watch(){periodic(statusBox,()=>api('/ai/runtime'),draw,v=>['downloading','verifying','removing'].includes(v.state));}
    replace(root,link('← Administration','/admin','crumb'),hero('Installation','AI runtime','Choose how this server searches your knowledge. Each project keeps its own sources.'),embeddingBox,
      panel('EmbeddingGemma',statusBox,actionBox),downloadPanel,
      panel('How local embeddings run',hint('Local embeddings run inside this installation. Model weights are downloaded separately and persist across container restarts. Page views never start a download.'),
        hint('The local worker uses two CPU threads, batches of four, an eight-request queue, and a 60-second request deadline. Human chat remains available during model installation or failure. Removing the model turns AI off; reinstalling does not turn it back on.')));
    downloadPanel.append(download);
    draw(current); if(['downloading','verifying','removing'].includes(current.state))watch();
  }
  async function embeddingSettings(box) {
    let saved=await api('/ai/embeddings');
    function draw(){
      const cfg=saved.config||{};
      const mode=select('Embedding provider','embedding_mode',[['local','Local EmbeddingGemma'],['external','External OpenAI-compatible endpoint']],saved.mode);
      const externalFields=el('div',{},
        field('Embedding API base URL','base_url','url',cfg.base_url||'',{placeholder:'https://api.openai.com/v1',maxlength:1024}),
        field('Embedding model ID','model','text',cfg.model||'',{placeholder:'text-embedding-3-small',maxlength:256}),
        field('Embedding dimensions','dimensions','number',cfg.dimensions||1536,{min:1,max:4096}),
        checkbox('Send dimensions to the provider','send_dimensions',cfg.send_dimensions||false),
        field('Embedding API key','embedding_key','password','',{autocomplete:'off',maxlength:4096}),
        field('Query prefix (optional)','query_prefix','text',cfg.query_prefix||'',{maxlength:256}),
        field('Document prefix (optional)','document_prefix','text',cfg.document_prefix||'',{maxlength:256}),
        hint('Use the dimensions and prefixes your model expects. Leave the key empty only for an endpoint without authentication. Re-enter the key when saving; saved secrets are never displayed.'),
        hint('Approved passages and search questions are sent to this endpoint. Your knowledge and vectors stay in your database. Private endpoints require ALLOW_INTERNAL_AI_ENDPOINTS=true on the server.'));
      const toggle=()=>{externalFields.hidden=mode.querySelector('select').value!=='external';};toggle();mode.querySelector('select').addEventListener('change',toggle);
      const fields=form([mode,externalFields,hint('Changing embeddings turns AI off for every project and rebuilds approved knowledge. Turn AI back on after indexing finishes. Your original sources remain saved.')], 'Verify and save embeddings',async data=>{
        const selected=data.get('embedding_mode');
        const body={version:saved.version,mode:selected};
        if(selected==='external')Object.assign(body,{key:data.get('embedding_key')||'',config:{base_url:data.get('base_url'),model:data.get('model'),dimensions:Number(data.get('dimensions')),send_dimensions:data.has('send_dimensions'),query_prefix:data.get('query_prefix')||'',document_prefix:data.get('document_prefix')||''}});
        if(!await confirmChange('Change embeddings for every project?',selected==='external'?'Two synthetic checks will run first. Approved passages and search questions will then be shared with this endpoint. AI will stay off until you re-enable it after indexing.':'AI will turn off while approved knowledge is rebuilt with the local model. The model must be installed below.','Verify and save'))return;
        fields.querySelector('[name=embedding_key]').value='';
        saved=await api('/ai/embeddings','PUT',body);draw();notify('Embedding settings saved. '+saved.reindex.queued+' sources queued.'+(saved.reindex.remaining?' Use Queue remaining sources after those finish.':''));
      });
      replace(box,panel('Knowledge search',hint('Using '+(saved.mode==='local'?'local EmbeddingGemma':saved.profile.model_id+' at '+saved.config.base_url)),fields,
        row(button('Queue remaining sources',async()=>{const result=await api('/ai/embeddings/reindex','POST',{version:saved.version});notify(result.queued+' queued; '+result.remaining+' remaining.');}),button('Reload embedding settings',async()=>{saved=await api('/ai/embeddings');draw();}))));
    }
    draw();
  }
  async function accountKeys() {
    if(!requireAdmin())return;
    root.classList.add('ai-admin');
    const [generators,deciders]=await Promise.all([api('/ai/providers'),api('/ai/decision-providers')]);
    const names=new Map([...deciders,...generators].map(p=>[p.id,p.name]));
    let library=await api('/ai/keys'),opened=null,page=0;
    const list=el('div'),editor=el('div',{class:'ai-key-editor'}),summary=hint('');
    const search=field('Find an account key','search','search','',{placeholder:'Name or account group'});
    const providerFilter=select('Filter provider','filter',[['','All providers'],...[...names].map(([id,name])=>[id,name])],'');
    const pageInfo=el('span'),previous=button('Previous',()=>{page--;draw();}),next=button('Next',()=>{page++;draw();});
    function draw(){
      summary.textContent=library.keys.length+' account keys. Projects choose which keys to use; adding a key never enables AI.';
      const filtered=library.keys.filter(key=>!providerFilter.querySelector('select').value||key.provider===providerFilter.querySelector('select').value);
      const result=aiKeyPage(filtered,search.querySelector('input').value,'',page);page=result.page;
      replace(list,...result.items.map(key=>el('article',{class:'ai-key-row'},el('div',{},el('h3',{},key.label),
        el('div',{class:'ai-key-facts'},el('span',{},names.get(key.provider)),el('span',{},key.purpose==='both'?'Answers & decisions':key.purpose==='decision'?'Topic decisions':'Answers'),el('span',{},key.status==='saved'?'Saved':key.status.replaceAll('_',' ')),el('span',{},key.quota_group)),
        hint(key.projects.length?'Used by '+key.projects.map(p=>p.name+' ('+(p.role==='decision'?'decisions':'answers')+')').join(', '):'No projects selected'),
        key.retry_at?hint('Retry after '+date(key.retry_at*1000)):null),button('Manage '+key.label,()=>edit(key)))));
      if(!result.total)replace(list,hint(library.keys.length?'No keys match your search.':'No account keys yet. Add a provider key to begin.'));
      pageInfo.textContent='Page '+(page+1)+' of '+result.pages;previous.disabled=page===0;next.disabled=page+1>=result.pages;
    }
    async function reload(){library=await api('/ai/keys');draw();}
    function close(){opened=null;replace(editor);}
    async function edit(key){
      if(opened&&!await confirmChange('Discard unsaved key edits?','Your saved credentials are unchanged.','Open key'))return;
      opened=key?.id||'new';
      const baseline=library.version;
      const provider=select('Provider','provider',[...names].map(([id,name])=>[id,name]),key?.provider||generators[0].id);
      provider.querySelector('select').disabled=Boolean(key);
      const roleBox=el('div'),secretBox=el('div');
      function fields(){
        const id=provider.querySelector('select').value,gen=generators.some(p=>p.id===id),dec=deciders.some(p=>p.id===id);
        replace(roleBox,select('Use this key for','purpose',[...(gen?[['generation','Answers']]:[]),...(dec?[['decision','Topic decisions']]:[]),...(gen&&dec?[['both','Answers and topic decisions']]:[])],key?.purpose||(gen?'generation':'decision')));
        replace(secretBox,id==='cloudflare'?field('Cloudflare account ID','account_id','text','',{required:!key,pattern:'[0-9a-f]{32}',minlength:32,maxlength:32,autocomplete:'off'}):null,
          field(id==='cloudflare'?'Cloudflare Workers AI API token':key?'Replacement API key (optional)':'API key','key','password','',{required:!key&&id!=='custom',maxlength:4096,autocomplete:'off'}),
          id==='cloudflare'?hint('Use a Workers AI token with Account → Workers AI → Edit permission. R2 access keys do not work. To replace credentials, enter both the account ID and token; their account group is derived automatically.'):
          field('Provider account group','quota_group','text',key?.quota_group||'Default account',{required:true,maxlength:80}),
          id==='custom'?field('API base URL','base_url','url','',{required:!key,placeholder:'https://api.example.com/v1',maxlength:1024}):null,
          id==='custom'?field('Exact model IDs (comma-separated)','models','text','',{required:!key,placeholder:'my-chat-model'}):null,
          id==='custom'?hint('Enter the base URL including /v1 if your server uses it. To replace this connection, supply its URL, model IDs and key together. Leave the key empty only for a server without authentication. Private endpoints require ALLOW_INTERNAL_AI_ENDPOINTS=true on the server.'):null,
          hint('Keys for the same provider account must use the same group so they share quota cooldowns. Secrets are encrypted and never displayed.'));
      }
      provider.querySelector('select').addEventListener('change',fields);fields();
      const formBox=form([field('Key name','label','text',key?.label||'',{required:true,maxlength:80}),provider,roleBox,secretBox,
        key?checkbox('Key is available to selected projects','enabled',key.enabled):null,
        hint(key?.projects.length?'Changes affect: '+key.projects.map(p=>p.name).filter((n,i,a)=>a.indexOf(n)===i).join(', ')+'. In-progress AI work is cancelled; replacing credentials clears all model verification. Removing a role detaches its project selections.':'Projects must explicitly select this key before using it.')],key?'Save account key':'Add account key',async data=>{
          const raw=data.get('key'),id=key?.provider||data.get('provider');
          const body={version:baseline,label:data.get('label'),quota_group:data.get('quota_group')||key?.quota_group||'Default account',...(raw?{key:raw}:{}),...(id==='custom'&&(!key||data.get('base_url'))?{key:raw||'',base_url:data.get('base_url'),models:String(data.get('models')||'').split(',').map(x=>x.trim()).filter(Boolean)}:{}),...(id==='cloudflare'&&data.get('account_id')?{account_id:data.get('account_id')}:{}),
            ...(key?{key_version:key.version,enabled:data.has('enabled'),purpose:data.get('purpose')}:{provider:id,purpose:data.get('purpose')})};
          formBox.querySelector('[name=key]').value='';
          if(key?.projects.length&&!await confirmChange('Update shared account key?','This affects '+key.projects.map(p=>p.name).filter((n,i,a)=>a.indexOf(n)===i).join(', ')+'. Review the selected projects before continuing.','Save shared key'))return;
          library=await api('/ai/keys'+(key?'/'+encode(key.id):''),key?'PUT':'POST',body);close();draw();notify('Account key saved');
        });
      const actions=key?row(...(key.purpose!=='decision'?[button('Refresh answer models',async()=>{library=await api('/ai/keys/'+encode(key.id)+'/models/refresh','POST',{version:baseline,key_version:key.version,role:'generation'});draw();notify('Answer models refreshed');})]:[]),
        ...(key.purpose!=='generation'?[button('Refresh decision models',async()=>{library=await api('/ai/keys/'+encode(key.id)+'/models/refresh','POST',{version:baseline,key_version:key.version,role:'decision'});draw();notify('Decision models refreshed');})]:[]),
        button('Retry account after cooldown',async()=>{if(!await confirmChange('Retry this provider account?','This resets the shared cooldown for every key and project in '+key.quota_group+'. It does not grant access or model verification.','Retry account'))return;library=await api('/ai/keys/'+encode(key.id)+'/retry','POST',{version:baseline,key_version:key.version});close();draw();}),
        button('Delete account key',async()=>{if(!await confirmChange('Delete this shared key?',key.projects.length?'Affected projects: '+key.projects.map(p=>p.name).join(', ')+'. An enabled topic gate keeps refusing when its keys are unavailable.':'This removes the encrypted credential from the library.','Delete account key'))return;library=await api('/ai/keys/'+encode(key.id),'DELETE',{version:baseline,key_version:key.version});close();draw();},'danger')):null;
      replace(editor,panel(key?'Manage '+key.label:'Add provider credentials',formBox,actions,
        key?hint('Verified models: '+(key.verified_models.map(e=>e.model+' ('+e.role+')').join(', ')||'None yet. Select this key and verify a model in project AI support.')):null,row(button('Cancel key edits',close))));
      editor.scrollIntoView({block:'start'});editor.querySelector('[name=label]').focus();
    }
    search.querySelector('input').addEventListener('input',()=>{page=0;draw();});providerFilter.querySelector('select').addEventListener('change',()=>{page=0;draw();});
    replace(root,link('← Administration','/admin','crumb'),hero('Account settings','AI providers','Save provider credentials once, then choose keys and models for each project.'),
      panel('Provider API keys',summary,row(button('Add provider key',()=>edit(null),''),button('Reload account keys',async()=>{if(opened&&!await confirmChange('Reload account keys?','Unsaved key edits will be discarded.','Reload keys'))return;close();await reload();},'quiet-button')),el('div',{class:'ai-columns'},search,providerFilter),list,row(previous,pageInfo,next),editor));draw();
  }
  async function keySelection(box,provider,role,selected,onSave,onDirty=()=>{}) {
    const library=await api('/ai/keys');
    const candidates=library.keys.filter(key=>key.provider===provider&&[role,'both'].includes(key.purpose));
    const search=field('Find a '+(role==='decision'?'decision':'answer')+' account key','find_key','search','',{placeholder:'Name or account group'});
    const choices=el('div',{class:'ai-key-choices'},candidates.map(key=>el('div',{class:'ai-account-choice'},checkbox(key.label,'key_ids',selected.includes(key.id),key.id),hint(key.quota_group+' · '+key.status.replaceAll('_',' ')))));
    const fields=form([search,hint('Only checked account keys can be used by this project. Paused keys keep their selection but cannot run requests.'),
      choices,
      !candidates.length?hint('No account keys are available for this provider and role. Add one in account settings, then reload this section.'):null],
      'Save selected '+(role==='decision'?'decision':'answer')+' keys',async data=>{await onSave(data.getAll('key_ids'),library.version);});
    fields.append(row(link('Manage account API keys →','/admin/ai/providers','button secondary'),fields.querySelector('button[type=submit]')));
    search.querySelector('input').addEventListener('input',()=>{const term=search.querySelector('input').value.toLocaleLowerCase();[...choices.children].forEach((row,index)=>row.hidden=![candidates[index].label,candidates[index].quota_group].some(value=>value.toLocaleLowerCase().includes(term)));});
    fields.addEventListener('input',event=>{if(event.target.name==='key_ids')onDirty();});
    replace(box,fields);
  }
  async function project(ref, target=root, embedded=false) {
    if (!requireAdmin()) return;
    target.classList.add('ai-admin');
    const p=await api('/projects/'+encode(ref));
    const base='/projects/'+encode(p.id)+'/ai';
    let config=await api(base);
    const summary=el('div',{class:'ai-overview','aria-live':'polite'});
    function showSummary(){replace(summary,state(config.settings.enabled?'active':'inactive'),el('span',{},config.settings.enabled?'AI is enabled for this project':'AI is off for this project'),hint('Human support remains available.'));}
    async function reloadConfig(){config=await api(base);showSummary();return config;}
    async function save(changes, key={}) {config=await api(base,'PUT',{version:config.version,settings:{...config.settings,...changes},...key});showSummary();return config;}
    async function getBoards(){let out=[],offset=0;for(;;){const page=await api('/projects/'+encode(p.id)+'/boards?limit=100&offset='+offset);out.push(...page.items);if(!page.has_more)return out;offset+=100;}}
    async function setup(box, mode="settings") {
      const boards=await getBoards();
      let pool=await api(base+'/keys'),operationPending=false,permissionEditor=null,destinationsDirty=false,limitsDirty=false;
      const permissionsBox=el('div'),destinationsBox=el('div'),limitsBox=el('div'),usageBox=el('div');
      const poolChanged=()=>pool.version!==config.version||pool.provider!==config.settings.provider;
      const setOperation=pending=>{operationPending=pending;permissionEditor?.update();};
      const refreshPool=async()=>{pool=await api(base+'/keys');return pool;};
      function permissionForm(force=false){
        if(!force&&permissionEditor?.dirty()){permissionEditor.update();return;}
        const snapshot=settings=>({enabled:settings.enabled,allow_guest_reports:settings.allow_guest_reports,tools:Object.fromEntries(tools.map(([id])=>[id,settings.tools[id]]))});
        const baseline=snapshot(config.settings),notice=el('p',{class:'hint',role:'status'}),readiness=el('div',{class:'ai-readiness','aria-live':'polite'});
        const verify=button('Check model for these permissions',async()=>{
          if(operationPending) return;
          const key=pool.keys.find(k=>k.enabled && k.model_available);
          if(!key)throw new Error('Select an available account key and model in Models & keys first.');
          setOperation(true);
          try{await api(base+'/keys/'+encode(key.id)+'/model/probe','POST',{version:config.version,key_version:key.version,tools:Object.values(values().tools).some(Boolean)});await reloadConfig();await refreshPool();notify('Model check finished. Review readiness, then save your permissions.');}
          finally{setOperation(false);}
        });
        let fields;
        const values=()=>({enabled:fields.querySelector('[name=enabled]').checked,allow_guest_reports:fields.querySelector('[name=guests]').checked,
          tools:Object.fromEntries(tools.map(([id])=>[id,fields.querySelector('[name='+id+']').checked]))});
        const draftState=()=>aiPermissionDraftState(snapshot(config.settings),baseline,values());
        const reload=button('Reload saved permissions',async()=>{
          if(operationPending)throw new Error('Wait for the current key operation to finish.');
          if(poolChanged())throw new Error('Reload saved settings above to review the current configuration.');
          setOperation(true);
          try{if(draftState().dirty&&!await confirmChange('Discard unsaved permission changes?', 'The latest saved permissions shown by this page will replace your unsaved choices.', 'Reload permissions'))return;permissionForm(true);}
          finally{setOperation(false);}
        });
        fields=form([
          checkbox('Enable AI support for this project','enabled',config.settings.enabled),
          hint('Enabling requires working embeddings, verified chat-provider access, and approved indexed knowledge. Turning AI off cancels unfinished AI work; human controls stay available.'),
          el('fieldset',{},el('legend',{},'Assistant tools — each permission is separate'),tools.map(([id,title,description])=>el('div',{class:'ai-toggle'},checkbox(title,id,config.settings.tools[id]),hint(description)))),
          checkbox('Allow guest reports from website chat','guests',config.settings.allow_guest_reports),
          hint('Guest reports also require the relevant report tool, a destination board, and the visitor’s confirmation. With guest reports off, visitors can copy their draft and continue on your feedback page.'),
          hint('Guests stay anonymous in the supplied widget. They are directed to your feedback page to follow updates; this does not promise email notifications, account claiming, or draft transfer. Changes cancel unfinished AI work.')
        ],'Save AI permissions',async data=>{
          if(operationPending)throw new Error('Wait for the current key operation to finish.');
          if(draftState().conflict)throw new Error('Saved permissions changed. Reload them before saving; your choices are kept.');
          setOperation(true);
          try{await save({enabled:data.has('enabled'),allow_guest_reports:data.has('guests'),tools:Object.fromEntries(tools.map(([id])=>[id,data.has(id)]))});await refreshPool();permissionForm(true);notify('AI permissions saved');}
          finally{setOperation(false);setTimeout(()=>permissionEditor?.update(),0);}
        });
        function update(){
          const state=draftState();
          const wanted=Object.values(values().tools).some(Boolean),toolReady=pool.keys.some(k=>k.enabled&&k.model_available&&k.capabilities_model===config.settings.model&&k.capabilities.text_generation&&k.capabilities.tool_calling);
          const missing=(config.readiness?.issues||[]).filter(i=>i!=='answer_tools_unverified');
          if(wanted&&!toolReady)missing.push('answer_tools_unverified');
          const labels={answer_key_missing:'Choose an account API key.',answer_model_missing:'Choose an answer model.',answer_model_unavailable:'Refresh available models.',answer_model_unverified:'Check answer-model access.',answer_tools_unverified:'Check model access for the optional tools you selected.',embedding_model_not_ready:'Install the local model or configure external embeddings in AI runtime.',knowledge_not_ready:'Approve and index at least one knowledge source.'};
          replace(readiness,el('strong',{},missing.length?(config.settings.enabled?'Review AI readiness':'Before you enable AI'):(config.settings.enabled?'AI support is on':'Ready to enable AI')),missing.length?el('ul',{},missing.map(i=>el('li',{},labels[i]||i))):hint('Model access and approved sources are ready.'),hint((missing.length?'Saved AI is '+(config.settings.enabled?'on.':'off.')+' ':'')+'Changes below apply only after saving.'));
          verify.disabled=operationPending;
          notice.textContent=state.conflict?'Saved permissions changed. Your unsaved choices are kept; reload them before saving. Saved AI is currently '+(config.settings.enabled?'on.':'off.'):state.dirty?'Your unsaved permission choices are kept. Save them when you are ready.':'';
          for(const input of fields.querySelectorAll('input'))input.disabled=operationPending;
          fields.querySelector('button[type=submit]').disabled=operationPending||state.conflict;
          reload.hidden=!state.dirty&&!state.conflict;reload.disabled=operationPending;
        }
        permissionEditor={dirty:()=>draftState().dirty,update};fields.addEventListener('change',update);
        replace(permissionsBox,readiness,fields,notice,row(verify,reload),hint('Checking makes short provider requests and may use quota. It never enables AI or a tool.'));update();
      }
      async function destinationForm(){
        const destinations=await api(base+'/feedback-destinations');
        const choices=[['','Choose a board'],...boards.map(b=>[b.id,b.name])];
        const board=(text,name)=>select(text,name,choices,config.settings[name]||'');
        replace(destinationsBox,hint('Boards receive submitted reports. Feedback pages are navigation links on your own website; they do not change where a report is stored.'),form([
          el('div',{class:'ai-columns'},board('Feature request board','feature_board_id'),board('Bug report board','bug_board_id')),
          field('Project feedback page URL','feedback_url','url',config.settings.feedback_url||'',{placeholder:'https://your-site.com/feedback',maxlength:2048}),
          el('div',{class:'ai-columns'},field('Feature page override (optional)','feature_feedback_url','url',config.settings.feature_feedback_url||'',{maxlength:2048}),field('Bug page override (optional)','bug_feedback_url','url',config.settings.bug_feedback_url||'',{maxlength:2048})),
          hint('Link priority: the feature/bug override, then the project feedback page, then its SupaKnock board. Use HTTPS. Saving never fetches your site or adds draft, identity, or secret parameters.')
        ],'Save feedback destinations',async data=>{await save(Object.fromEntries(['feature_board_id','bug_board_id','feedback_url','feature_feedback_url','bug_feedback_url'].map(key=>[key,nil(data.get(key))])));await refreshPool();destinationsDirty=false;await destinationForm();notify('Feedback destinations saved');}),
        el('div',{class:'ai-link-preview'},el('h3',{},'Saved link preview'),...['feature','bug'].map(kind=>el('p',{},el('strong',{},kind==='feature'?'Feature requests: ':'Bug reports: '),destinations[kind]?external(destinations[kind],destinations[kind]):'No destination yet'))));
      }
      function limitsForm(){replace(limitsBox,form([
        el('div',{class:'ai-columns'},field('Requests per day (UTC)','daily_request_limit','number',config.settings.daily_request_limit,{required:true,min:1,max:100000}),field('Token allowance per day (UTC)','daily_token_limit','number',config.settings.daily_token_limit,{required:true,min:1,max:100000000})),
        el('div',{class:'ai-columns'},field('Maximum response tokens','max_output_tokens','number',config.settings.max_output_tokens,{required:true,min:1,max:8192}),field('Maximum tool rounds','max_tool_rounds','number',config.settings.max_tool_rounds,{required:true,min:1,max:8})),
        hint('4,096 response tokens is the recommended starting point for section drafts. This is a maximum per response, not a target. Existing saved limits are preserved.'),
        hint('Requests and conservative token allowances are reserved before generation. Known provider usage replaces its token reservation. Previews and section generation share these limits. Failed or cancelled requests may still consume provider quota; an uncertain request is not refunded automatically.')
      ],'Save usage limits',async data=>{await save(Object.fromEntries(['daily_request_limit','daily_token_limit','max_output_tokens','max_tool_rounds'].map(key=>[key,Number(data.get(key))])));await refreshPool();limitsDirty=false;limitsForm();notify('Usage limits saved');}));}
      async function usage(){try{const [u,jobs]=await Promise.all([api(base+'/usage'),api(base+'/jobs')]);const outcomes={answered:'Answered',insufficient:'Not enough source information',evidence_rejected:'Draft did not pass the source check',check_unavailable:'Answer check unavailable',failed:'Could not finish',action:'Action prepared',greeting:'Greeting',clarify:'Clarification requested',refuse:'Outside support scope',unavailable:'Unavailable'};replace(usageBox,hint('Today • '+u.day+' UTC'),el('dl',{class:'ai-facts'},el('dt',{},'Reserved requests'),el('dd',{},u.reserved_requests),el('dt',{},'Used + pending token allowance'),el('dd',{},u.reserved_tokens),el('dt',{},'Provider-reported input tokens'),el('dd',{},u.reported_input_tokens),el('dt',{},'Provider-reported output tokens'),el('dd',{},u.reported_output_tokens)),hint('Reported tokens can be incomplete after failed or interrupted calls. Monetary cost is not estimated.'),button('Refresh usage',usage),el('h3',{},'Recent AI replies'),...jobs.filter(j=>j.kind==='turn').map(j=>el('div',{class:'ai-job'},el('strong',{},outcomes[j.answer_outcome]||j.state),hint(date(j.created_at)+' • '+j.attempt_count+' attempt'+(j.attempt_count===1?'':'s')+(j.stage?' • '+j.stage:'')+(j.error?' • '+j.error.replaceAll('_',' '):'')))));}catch(error){failure(usageBox,error,usage);}}
      destinationsBox.addEventListener('input',()=>{destinationsDirty=true;});destinationsBox.addEventListener('change',()=>{destinationsDirty=true;});limitsBox.addEventListener('input',()=>{limitsDirty=true;});
      const hasDraft=()=>operationPending||permissionEditor?.dirty()||destinationsDirty||limitsDirty;
      box._refresh=async()=>{if(hasDraft())return;const nextConfig=await api(base),nextPool=await api(base+'/keys');if(hasDraft())return;config=nextConfig;pool=nextPool;showSummary();permissionForm();limitsForm();await destinationForm();};
      permissionForm();limitsForm();await destinationForm();
      const reloadSettings=row(button('Reload saved settings',async()=>{if(operationPending)throw new Error('Wait for the current key operation to finish.');setOperation(true);try{if(!await confirmChange('Reload saved settings?', 'Unsaved edits in this section will be discarded.', 'Reload settings'))return;await reloadConfig();await setup(box,mode);}finally{setOperation(false);}}));
      replace(box,hint('Choose what your assistant can do. Manage its model and sources in the tabs above.'),panel('Permissions',permissionsBox),el('details',{class:'ai-disclosure'},el('summary',{},'Feedback destinations'),destinationsBox),el('details',{class:'ai-disclosure'},el('summary',{},'Usage & limits'),limitsBox,usageBox),reloadSettings);await usage();
    }
    function markdownForm(onImported, source=null, initial='') {
      const name=field('Source name','name','text',source?.name||'',{required:true,maxlength:300});
      const filename=field('Filename','filename','text',source?.latest_document?.filename||'support.md',{required:true,maxlength:255,pattern:'.*\\.(md|markdown)'});
      const content=field('Knowledge content','content','textarea',initial,{required:true,maxlength:65536,rows:9});
      const file=field('Load a Markdown file','file','file','',{accept:'.md,.markdown,text/markdown'});
      const fileError=el('p',{class:'error',role:'alert'});
      file.querySelector('input').addEventListener('change',async event=>{
        fileError.textContent='';try{const f=event.target.files[0];if(!f)return;if(f.size>65536)throw new Error('Markdown files must be at most 64 KiB.');if(!/\.(md|markdown)$/i.test(f.name))throw new Error('Choose a .md or .markdown file.');loadedOriginal=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(await f.arrayBuffer());content.querySelector('textarea').value=loadedOriginal;loadedDisplay=content.querySelector('textarea').value;filename.querySelector('input').value=f.name;if(!name.querySelector('input').value)name.querySelector('input').value=f.name;}catch(error){fileError.textContent=error.message;}});
      let loadedOriginal=null, loadedDisplay=null;
      const identity=requestIdentity();
      return form([name,content,el('details',{class:'ai-disclosure'},el('summary',{},'Upload a Markdown file'),file,fileError,filename),hint('Write an answer or article, or upload Markdown. Save it, then review and approve it for AI answers.')],source?'Save revised article':'Save article',async data=>{
        const submittedContent=loadedOriginal!==null && data.get('content')===loadedDisplay?loadedOriginal:data.get('content');
        if(new TextEncoder().encode(submittedContent).length>65536)throw new Error('Markdown must be at most 64 KiB after UTF-8 encoding.');
        const body={name:data.get('name'),filename:data.get('filename'),content:submittedContent,...(source?{version:source.version}:{})};
        const result=await api(base+'/knowledge/'+(source?'sources/'+source.id+'/markdown':'markdown'),source?'PUT':'POST',{...body,client_import_id:identity(body)});identity.reset();await onImported(result.source);notify('Original saved; indexing is queued');
      });
    }
    function websiteForm(onImported, source=null, settings=null) {
      const c=settings||{seed_url:'',approved_origins:[],include_paths:['/'],exclude_paths:[],max_pages:20,max_depth:3};
      const identity=requestIdentity(),name=field('Source name','name','text',source?.name||'',{required:true,maxlength:300});
      const seed=field('Website starting URL','seed_url','url',c.seed_url,{required:true,maxlength:2048,placeholder:'https://your-site.com/help/'});
      const origins=field('Allowed website origins','approved_origins','textarea',c.approved_origins.join('\n'),{required:true,rows:2});
      const paths=field('Include paths — one per line','include_paths','textarea',c.include_paths.join('\n'),{required:true,rows:3});
      let scopeEdited=Boolean(source);
      origins.addEventListener('input',()=>scopeEdited=true);paths.addEventListener('input',()=>scopeEdited=true);
      seed.querySelector('input').addEventListener('input',()=>{if(scopeEdited)return;try{const u=new URL(seed.querySelector('input').value);origins.querySelector('textarea').value=u.origin;paths.querySelector('textarea').value=u.pathname||'/';if(!name.querySelector('input').value)name.querySelector('input').value=u.hostname;}catch{}});
      return form([seed,name,hint('We start here and follow public links within the path below. Review crawl scope to include more of your website.'),
        field('Maximum pages','max_pages','number',c.max_pages,{required:true,min:1,max:50}),
        el('details',{class:'ai-disclosure'},el('summary',{},'Crawl scope & advanced options'),origins,paths,
          field('Exclude paths — one per line','exclude_paths','textarea',c.exclude_paths.join('\n'),{rows:3}),
          field('Maximum link depth','max_depth','number',c.max_depth,{required:true,min:0,max:5}),
          hint('Public pages only. Robots rules apply. Sign-in pages, private networks and redirects are not followed; use the final website URL.')),
        hint('Login pages, blank pages, redirects and other unusable URLs are skipped. Readable pages are imported. The page limit counts attempts, including skips. If nothing usable is found, previous knowledge is kept. New sources need your approval.')
      ],source?'Refresh website':'Import website',async data=>{
        const body={name:data.get('name'),config:{seed_url:data.get('seed_url'),approved_origins:lines(data.get('approved_origins')),include_paths:lines(data.get('include_paths')),exclude_paths:lines(data.get('exclude_paths')),max_pages:Number(data.get('max_pages')),max_depth:Number(data.get('max_depth'))},...(source?{version:source.version}:{})};
        const result=await api(base+'/knowledge/'+(source?'sources/'+source.id+'/website':'websites'),source?'PUT':'POST',{...body,client_import_id:identity(body)});identity.reset();await onImported(result.source);notify('Website import queued');
      });
    }
    async function sourceManager(box, initialId=null) {
      const list=el('div'),detail=el('div',{class:'ai-source-detail',tabindex:'-1'}),overview=el('div');
      async function load(offset=0){await paged(list,base+'/knowledge/sources',items=>items.map(s=>el('article',{class:'card ai-source-card'},
        el('div',{class:'ai-status-line'},el('h3',{},s.name),sourceState(s)),hint((s.kind==='website'?'Website':'Markdown')+' • '+(s.approved?'Approved':'Needs approval')+' • '+(s.active?'Active':'Inactive')),
        s.location?el('p',{class:'ai-url'},s.location):null,s.index_stale?hint('Latest refresh has not replaced the last complete index.'):null,
        s.job?.error?el('p',{class:'error'},'Indexing: '+s.job.error):null,
        button('Review '+s.name,async()=>{await inspect(s.id);detail.focus();detail.scrollIntoView({block:'start',behavior:'smooth'});})
      )));}
      async function imported(s){await load();await inspect(s.id);detail.focus();}
      async function inspect(id){
        overview.hidden=true;
        const s=await api(base+'/knowledge/sources/'+id), prefix=base+'/knowledge/sources/'+id;
        const docs=el('div'),history=el('div'),progress=el('div',{'aria-live':'polite'});
        let progressConflict=false,drawGeneration=()=>{};
        const generationSettings=await api(base);
        function drawProgress(v){
          const completedIndex=aiCompletedIndex(s,v);
          if(completedIndex){s.version=v.version;s.index_status=v.index_status;s.active_revision_id=v.active_revision_id;s.index_ready=v.index_ready;s.job=v.job;}
          progressConflict=v.version!==s.version;
          const badge=list.querySelector('[data-ai-source-state="'+s.id+'"]');if(badge)badge.replaceWith(sourceState(v));
          replace(progress,progressConflict?el('p',{class:'error',role:'alert'},'This source changed while you were reviewing it. Your edits are still here. Use Refresh source status to review the latest version before saving.'):null,state(v.index_status),hint((v.approved?'Approved':'Not approved')+' • '+(v.active?'Active':'Inactive')+' • Updated '+date(v.updated_at)),v.index_stale?hint('The latest content is not yet indexed. The previous complete index is preserved.'):null,v.job?.error?el('p',{class:'error'},'Indexing failed: '+v.job.error):null);drawGeneration(v);}
        async function syncVersion(){drawProgress(await api(prefix));if(progressConflict)throw new Error('Source changed. Your approval choices have been kept; use Refresh source status to review the latest version before saving.');}
        drawProgress(s);
        const change=form([checkbox('Active — include this source when approved','active',s.active),checkbox('Approved for AI answers','approved',s.approved)],'Save source permissions',async data=>{await syncVersion();await api(prefix,'PATCH',{version:s.version,active:data.has('active'),approved:data.has('approved')});await imported(s);notify('Source permissions saved');});
        replace(detail,row(button('← All sources',async()=>{if(!await leaveKnowledge(detail))return;replace(detail);overview.hidden=false;overview.querySelector('button')?.focus();})),panel(s.name,progress,hint('Only active, approved, fully indexed originals can be used in answers. Deactivation or removal immediately cancels unfinished source work.'),row(button('Refresh source status',async()=>{if(await leaveKnowledge(detail))await inspect(id);}),!s.deleted_at?button('Remove source',async()=>{if(!await confirmChange('Remove this source?', 'The source will stop appearing in answers. Private original history will be retained.', 'Remove source'))return;await syncVersion();await api(prefix+'?version='+s.version,'DELETE');await imported(s);},'danger'):null),!s.deleted_at?change:null),el('details',{class:'ai-disclosure'},el('summary',{},'Original versions'),docs));
        async function loadDocs(offset=0){await paged(docs,prefix+'/documents',items=>items.map(d=>{
          const body=el('div');return el('article',{class:'ai-history'},el('h3',{},d.title),hint('Revision '+d.revision_number+' • '+date(d.created_at)+' • '+bytes(d.byte_count)),button('Read original revision '+d.revision_number,async()=>{const value=await api(prefix+'/documents/'+d.id);replace(body,original(value.filename,value.content));body.querySelector('details').open=true;}),body);
        }));}await loadDocs();
        if(s.kind==='website'){
          const crawlLabel=el('summary',{},'Crawl history');detail.append(el('details',{class:'ai-disclosure'},crawlLabel,history));let latest=null;
          async function crawlHistory(offset=0){await paged(history,prefix+'/crawls',items=>{latest=items[0]||null;if(!offset&&latest)crawlLabel.textContent='Crawl history · '+aiCrawlSummary(latest);return items.map(run=>{
            const result=el('div');
            const header=el('div',{class:'ai-status-line'},state(run.status),el('span',{},date(run.created_at)));
            const overview=hint(aiCrawlSummary(run));
            const finished=hint('Finished: '+date(run.finished_at));
            async function read(){const r=await api(prefix+'/crawls/'+run.id);draw(r);if(busyStates.includes(r.status))periodic(result,()=>api(prefix+'/crawls/'+run.id),draw,v=>busyStates.includes(v.status));}
            function draw(r){replace(header,state(r.status),el('span',{},date(r.created_at)));finished.textContent='Finished: '+date(r.finished_at);overview.textContent=aiCrawlSummary(r);if(latest?.id===r.id)crawlLabel.textContent='Crawl history · '+aiCrawlSummary(r);replace(result,r.status==='succeeded'?hint('Import complete. Usable pages are saved for indexing; skipped URLs do not block this import.'):null,r.error?el('p',{class:'error'},aiCrawlFailure(r)):null,r.stop_reason==='page_limit'?hint('Stopped normally at the configured page limit of '+r.config.max_pages+'. Only fetched pages are included; this is not a complete website crawl.'):null,hint('Attempt '+r.attempt_count+' • Expand a URL below to see its outcome'),busyStates.includes(r.status)?button('Cancel this crawl',async()=>{draw(await api(prefix+'/crawls/'+run.id+'/cancel','POST',{version:r.version}));await load();}):null,
              ...r.pages.map(page=>el('details',{},el('summary',{},(stateNames[page.status]||page.status)+' · '+page.url),hint(page.error ? aiCrawlReason(page.error) : page.title || 'No extracted content'),page.content?el('pre',{tabindex:'0'},page.content):null)));
            }
            if(busyStates.includes(run.status))periodic(result,()=>api(prefix+'/crawls/'+run.id),draw,v=>busyStates.includes(v.status));
            return el('article',{class:'ai-history'},header,overview,finished,button('Inspect crawl',read),result);
          });});}await crawlHistory();
          if(!s.deleted_at)detail.append(el('details',{class:'ai-disclosure'},el('summary',{},'Website settings & refresh'),websiteForm(imported,s,latest?.config)));
        }else if(!s.deleted_at){const original=s.latest_document?await api(prefix+'/documents/'+s.latest_document.id):null;detail.append(el('details',{class:'ai-disclosure'},el('summary',{},'Edit article'),markdownForm(imported,s,original?.content||'')));}
        const generationIdentity=requestIdentity();
        if(!s.deleted_at){
          const requirements=el('div',{'aria-live':'polite'});
          const selection=el('div');
          let pages=[];if(s.kind==='website'){const value=await api(base+'/knowledge/library?source_id='+s.id+'&limit=100');pages=value.items;}
          const scope=select('Draft from','scope',[['all','All indexed content'],...(pages.length?[['selected','Selected pages']]:[])],'all');
          const pageChoices=el('div',{class:'ai-key-choices',hidden:true},pages.map(page=>checkbox(page.title||page.url,'page_ids',false,page.id)));
          scope.querySelector('select').addEventListener('change',()=>pageChoices.hidden=scope.querySelector('select').value!=='selected');
          selection.append(scope,pageChoices);
          const generate=button('Draft articles & FAQs',async()=>{await syncVersion();const latest=await api(base);const selected=[...pageChoices.querySelectorAll('input:checked')].map(i=>i.value);if(scope.querySelector('select').value==='selected'&&!selected.length)throw new Error('Choose at least one page.');
            const body={version:latest.version,source_version:s.version,...(scope.querySelector('select').value==='selected'?{page_ids:selected}:{})};
            if(!await confirmChange('Create review drafts?', 'This processes '+(body.page_ids?body.page_ids.length+' selected pages':'all indexed content in this source')+' in small batches. Each batch uses provider quota and your project allowance. Completed drafts are saved if work stops. This does not enable AI or approve drafts.', 'Create drafts'))return;const run=await api(prefix+'/sections/generate','POST',{...body,client_request_id:generationIdentity(body)});generationIdentity.reset();notify('Section generation queued');await showRun(run,detail);});
          drawGeneration=v=>{const blockers=aiSectionPrerequisites(generationSettings,v,progressConflict);generate.disabled=blockers.length>0;replace(requirements,blockers.length?el('p',{},'Before you generate:'):null,blockers.length?el('ul',{class:'ai-steps'},blockers.map(item=>el('li',{},item.message,item.target?el('span',{},' ',link(item.target==='runtime'?'Open AI runtime →':'Open Models & keys →',item.target==='runtime'?'/admin/ai':'/admin/projects/'+p.id+'#ai-support/providers')):null))):hint('Ready to generate from this approved source. Website AI can remain off.'));};
          drawGeneration(s);
          detail.append(panel('Draft articles from this source',hint('Optional: turn source material into articles and FAQs you can review. Your approved originals are already usable by AI. Generation covers the content you choose and saves progress in small batches.'),selection,requirements,row(generate)));
        }
        if(['pending','indexing'].includes(s.index_status))periodic(progress,()=>api(prefix),drawProgress,v=>['pending','indexing'].includes(v.index_status));
      }
      replace(overview,panel('Connected sources',hint('Review originals before approving them. Generated drafts are separate from the source evidence.'),row(button('Refresh sources',()=>load())),list),
        el('details',{},el('summary',{},'Add Markdown knowledge'),markdownForm(imported)),el('details',{'data-connect':'website'},el('summary',{},'Add a website'),websiteForm(imported)));replace(box,overview,detail);await load();if(initialId)await inspect(initialId);
    }
    async function knowledge(box) {
      const nav=el('div',{class:'ai-segments','aria-label':'Knowledge views'}),content=el('div'),add=el('div');
      let mode=location.hash.endsWith('/drafts')?'drafts':location.hash.endsWith('/sources')?'sources':'library';
      const views=[['library','Library'],['sources','Sources'],['drafts','Drafts']];
      async function open(next,sourceId=null,connect=false){
        if(!await leaveKnowledge(box))return;
        mode=next;add.replaceChildren();
        for(const b of nav.children)b.setAttribute('aria-pressed',String(b.dataset.view===mode));
        window.history.replaceState(null,'','#'+(embedded?'ai-support/':'')+'knowledge/'+mode);
        const view=el('div',{'aria-busy':'true'},loading('Loading '+mode+'…'));replace(content,view);
        try{if(mode==='sources'){await sourceManager(view,sourceId);if(connect){const form=view.querySelector('[data-connect]');form.open=true;form.querySelector('input')?.focus();form.scrollIntoView({block:'start',behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});}}else if(mode==='drafts')await draftLibrary(view);else await library(view);}
        catch(error){failure(view,error,()=>open(mode));}
        finally{view.setAttribute('aria-busy','false');}
      }
      for(const [id,title] of views){const b=button(title,()=>open(id));b.dataset.view=id;b.setAttribute('aria-pressed',String(mode===id));nav.append(b);}
      const addArticle=async()=>{if(!await leaveKnowledge(add))return;replace(add,panel('Write an article',markdownForm(async source=>{replace(add);await open('sources',source.id);notify('Article saved. Review and approve it for AI answers.');}),row(button('Cancel article',async()=>{if(await leaveKnowledge(add))replace(add);}))));add.querySelector('input')?.focus();};
      replace(box,el('div',{class:'ai-workspace-heading'},el('div',{},el('h2',{},'Knowledge'),hint('Give your assistant reliable answers. Keep everything it can use in one place.')),row(button('Write an article',addArticle),button('Connect a source',()=>open('sources',null,true)))),nav,add,content);
      box.addEventListener('input',event=>{if(event.target.type!=='search' && event.target.closest('form'))event.target.closest('form').dataset.edited='true';});
      await open(mode);
      async function library(target){
        const list=el('div'),reader=el('div',{class:'ai-reader',tabindex:'-1'}),search=field('Search knowledge','query','search','',{placeholder:'Find a page, answer or topic…',maxlength:200});
        const overview=el('div',{},searchForm([search],'Search',async()=>load(),button('Refresh library',()=>load())),list);
        async function load(){await paged(list,base+'/knowledge/library?query='+encode(search.querySelector('input').value),items=>items.map(item=>el('article',{class:'ai-library-row'},el('div',{},el('h3',{},item.title||'Untitled page'),el('div',{class:'ai-key-facts'},el('span',{class:item.ready?'ai-ready-text':''},item.status),el('span',{},item.source_name),el('span',{},item.kind==='website'?'Website':'Article')),hint(item.excerpt.replace(/^#{1,6}[^\n]*\n+/, '').replace(/\s+/g,' ').trim())),namedAction('Read','Read '+(item.title||'page'),async()=>{
          const value=await api(base+'/knowledge/library/'+item.id);overview.hidden=true;
          replace(reader,row(button('← Library',()=>{replace(reader);overview.hidden=false;})),panel(value.title||'Page',hint(value.status+' · '+value.source_name),value.url?external('Open original page',value.url):null,el('div',{class:'ai-reading-text'},value.content),row(button('Manage source',()=>open('sources',item.source_id)))));reader.focus();
        }))),0,search.querySelector("input").value?"No matching knowledge. Try another word or clear your search.":"Your knowledge library starts here. Write an article or connect a website, then review and approve it for AI answers.");}
        replace(target,overview,reader);await load();
      }
    }
    async function draftLibrary(box){
      const list=el('div'),editor=el('div',{class:'ai-reader',tabindex:'-1'}),search=field('Search drafts','query','search','',{placeholder:'Find an article or FAQ…',maxlength:200});
      const overview=el('div',{},hint('AI suggestions stay private until you review and approve them. Approving creates a separate article; it never replaces a website page.'),searchForm([search],'Search drafts',async()=>load()),list);
      async function load(){await paged(list,base+'/knowledge/sections?query='+encode(search.querySelector('input').value),items=>items.map(section=>el('article',{class:'ai-library-row'},el('div',{},el('h3',{},section.title),hint(section.published_source_id?'Added to library':section.stale?'Source changed · review evidence':'Ready for review'),hint(section.body.slice(0,170))),namedAction('Review','Review '+section.title,async()=>{
        overview.hidden=true;replace(editor,row(button('← Drafts',async()=>{if(!await leaveKnowledge(editor))return;replace(editor);overview.hidden=false;load();})));
        const run=await api(base+'/authoring-runs/'+section.run_id);renderSection(section,run,editor,async()=>{replace(editor);overview.hidden=false;await load();});editor.focus();
      }))),0,search.querySelector("input").value?"No matching drafts. Try another word or clear your search.":"No drafts to review yet. Open an approved source and choose Draft articles & FAQs. Your approved sources already work in AI answers.");}
      replace(box,overview,editor);await load();
      const historyBox=el('details',{class:'ai-disclosure'},el('summary',{},'Generation activity'));box.append(historyBox);await history(historyBox,'sections');
    }
    function renderSection(section,run,parent,onSaved){
      const card=el('article',{class:'ai-draft-editor'}),available=new Map([...run.citations,...section.citations].map(c=>[c.id,c]));
      const search=field('Find a reference','references','search','',{placeholder:'Search source titles or excerpts'});
      const choices=el('div',{class:'ai-key-choices'},[...available.values()].map(c=>el('div',{class:'ai-reference-option'},checkbox(c.title,'citation_ids',section.citation_ids.includes(c.id),c.id),hint(c.excerpt))));
      search.querySelector('input').addEventListener('input',()=>{const q=search.querySelector('input').value.toLocaleLowerCase();for(const child of choices.children)child.hidden=!child.textContent.toLocaleLowerCase().includes(q);});
      let dirty=false;
      const edit=form([field('Title','title','text',section.title,{required:true,maxlength:200}),field('Category','category','text',section.category,{required:true,maxlength:100}),field('Draft text','body','textarea',section.body,{required:true,maxlength:4000,rows:10}),
        el('div',{class:'ai-evidence'},el('h3',{},'Supporting sources'),hint('References are attached automatically. Open an excerpt to check a claim.'),citations(section.citations)),
        el('details',{class:'ai-disclosure'},el('summary',{},'Change references'),hint('Choose one to twenty supporting passages from this generation.'),search,choices)],'Save draft',async data=>{
          const ids=data.getAll('citation_ids');if(!ids.length||ids.length>20)throw new Error('Choose one to twenty references.');
          await api(base+'/knowledge/sections/'+section.id,'PUT',{version:section.version,title:data.get('title'),category:data.get('category'),body:data.get('body'),citation_ids:ids});dirty=false;notify('Draft saved');await onSaved();
        });
      edit.addEventListener('input',()=>dirty=true);
      const approve=button('Approve for AI answers',async()=>{
        if(dirty)throw new Error('Save your edits before approving this draft.');
        if(!await confirmChange('Add this reviewed answer to the library?', 'You are approving the saved text as knowledge for this project. Local indexing follows. It is a separate article: the original pages remain available, and future website refreshes will not update this article. Check for conflicting or duplicate information. Adding knowledge can stop other in-progress AI work; earlier drafts remain saved.', 'Approve saved answer'))return;
        await api(base+'/knowledge/sections/'+section.id+'/approve','POST',{version:section.version});notify('Approved article added to the library. Indexing is queued.');await onSaved();
      });
      approve.disabled=section.stale||Boolean(section.published_source_id)||busyStates.includes(run.status);
      card.append(...[el('h3',{},section.title),busyStates.includes(run.status)?hint('Generation is still running. You can edit this saved draft now and approve it when generation finishes.'):null,section.stale?el('p',{class:'error'},'Supporting evidence changed or was removed. This draft is preserved; generate a new draft from current sources before approving.'):null,section.published_source_id?hint('A reviewed version is in your library. Edit that article through its source.'):null,edit,row(approve,button('Delete draft',async()=>{if(!await confirmChange('Delete draft?','Original sources and any approved library article remain.','Delete draft'))return;await api(base+'/knowledge/sections/'+section.id+'?version='+section.version,'DELETE');await onSaved();},'danger'))].filter(Boolean));
      if(section.stale)for(const input of edit.querySelectorAll('input,textarea,button'))input.disabled=true;
      parent.append(card);
    }
    async function showRun(initial,parent) {
      const box=el('section',{class:'panel ai-run','aria-label':'Generation result'});parent.append(box);
      const retryIdentity=requestIdentity();
      function draw(run){
        const restoreFocus=retainButtonFocus(box);
        const body=el('div');replace(box,el('div',{class:'ai-status-line'},el('h3',{},run.kind==='preview'?'Preview result':'Generated sections'),state(run.status)),hint('Started '+date(run.created_at)),
          run.stale?hint('Generation settings or knowledge changed. Saved drafts remain available; each draft shows whether its own supporting evidence is current.'):null,
          run.error?el('p',{class:'error',role:'alert'},aiAuthoringError(run)):null,
          run.coverage?hint(run.coverage.scope==='all'?(run.coverage.page_ids?'Selected pages':'Entire indexed source')+' · '+run.coverage.selected_passages+' passages to process.':'Earlier generation: '+run.coverage.selected_passages+' of '+run.coverage.total_passages+' passages selected.'):null,
          run.coverage?.total_batches?hint('Progress: '+run.coverage.completed_batches+' of '+run.coverage.total_batches+' batches complete ('+run.coverage.completed_passages+' passages processed).'):null,
          run.kind==='sections'&&['failed','partial'].includes(run.status)&&run.coverage?.batch_size&&!run.stale?row(button('Retry unfinished batches',async()=>{
            const body={version:run.version};const next=await api(base+'/authoring-runs/'+run.id+'/retry','POST',{...body,client_request_id:retryIdentity(body)});retryIdentity.reset();draw(next);notify('Unfinished batches queued. Each request consumes AI allowance.');periodic(box,()=>api(base+'/authoring-runs/'+run.id),draw,v=>busyStates.includes(v.status));
          }),hint('Retries consume AI allowance. Completed batches are kept and are not regenerated.')):null,
          busyStates.includes(run.status)?row(button('Refresh generation status',async()=>draw(await api(base+'/authoring-runs/'+run.id))),button('Cancel generation',async()=>{draw(await api(base+'/authoring-runs/'+run.id+'/cancel','POST',{version:run.version}));notify('Generation cancelled. Dispatched requests may still consume quota.');})):null,body);
        if(run.kind==='preview'&&run.result){body.append(el('div',{},el('p',{class:'content ai-answer'},run.result.body),run.result.status==='insufficient'?hint('The available sources did not support an answer.'):null,citations(run.citations)));}
        const editor=el('div',{class:'ai-reader',tabindex:'-1'});
        for(const section of run.sections){if(section.deleted_at)continue;
          body.append(el('article',{class:'ai-library-row'},el('div',{},el('h3',{},section.title),hint(section.published_source_id?'Added to library':section.stale?'Source changed':'Draft · '+section.category)),namedAction('Review','Review '+section.title,async()=>{if(!await leaveKnowledge(editor))return;replace(editor);renderSection(section,run,editor,async()=>draw(await api(base+'/authoring-runs/'+run.id)));editor.focus();editor.scrollIntoView({block:'start'});})));
        }
        body.append(editor);
        if(run.section_count>run.sections.length)body.append(hint('Showing the first '+run.sections.length+' drafts. Open Knowledge → Drafts to search and review all '+run.section_count+'.'));
        if(run.kind==='sections'&&run.result)body.append(el('details',{},el('summary',{},'Original generated snapshot and source provenance'),original('Generated result',JSON.stringify(run.result.sections,null,2)),citations(run.citations)));
        restoreFocus();
      }
      draw(initial);if(busyStates.includes(initial.status))periodic(box,()=>api(base+'/authoring-runs/'+initial.id),draw,v=>busyStates.includes(v.status));
      return box;
    }
    async function history(box,kind) {
      const list=el('div');
      async function load(offset=0){await paged(list,base+'/authoring-runs?kind='+kind,items=>items.map(run=>{
        const detail=el('div');return el('article',{class:'ai-history'},el('div',{class:'ai-status-line'},state(run.status),el('span',{},date(run.created_at))),run.query?hint(run.query):null,run.stale?hint('Earlier settings or knowledge version'):null,button('Inspect '+(kind==='sections'?'sections':'preview'),async()=>{replace(detail,);await showRun(await api(base+'/authoring-runs/'+run.id),detail);}),detail);
      }));}
      box.append(panel(kind==='sections'?'Section history':'Previous previews',hint(kind==='sections'?'Runs show generation progress and recoverable failures. Review individual articles in Drafts; only explicitly approved articles enter the library.':'Previews are private administrator tests. They do not create a customer chat.'),button('Refresh history',()=>load()),list));await load();
    }
    async function personality(box) {
      let value=await api(base+'/personality');
      const content=field('Agent personality (soul.md)','content','textarea',value.content,{rows:16,maxlength:40000});
      const file=field('Load soul.md','file','file','',{accept:'.md,text/markdown'}),error=el('p',{class:'error',role:'alert'});
      file.querySelector('input').addEventListener('change',async event=>{error.textContent='';try{const f=event.target.files[0];if(!f)return;if(f.name!=='soul.md'||f.size>81920)throw new Error('Choose soul.md, at most 80 KiB.');const text=new TextDecoder('utf-8',{fatal:true}).decode(await f.arrayBuffer());if([...text].length>20000)throw new Error('Personality must be at most 20,000 characters.');content.querySelector('textarea').value=text;}catch(e){error.textContent=e.message;}});
      replace(box,panel('Personality',hint('Describe tone, manner, and style. Personality cannot override source evidence, permissions, or confirmation requirements. Saving fences unfinished generation.'),form([file,error,content,hint('Up to 20,000 characters / 80 KiB. Loading a file only fills the editor; Save applies it.')],'Save personality',async data=>{const text=data.get('content');if([...text].length>20000 || new TextEncoder().encode(text).length>81920)throw new Error('Personality must be at most 20,000 characters and 80 KiB.');value=await api(base+'/personality','PUT',{version:value.version,filename:'soul.md',content:text});config.version=value.version;config.settings.soul=value.content;showSummary();notify('Personality saved');})));
    }
    async function preview(box) {
      const results=el('div');const identity=requestIdentity();
      replace(box,panel('Try a grounded answer',hint('Uses the saved model, personality, and approved knowledge with every assistant tool disabled. This consumes AI allowance even when customer AI is off. It never enables AI or sends a customer message.'),form([field('Question','question','textarea','',{required:true,maxlength:1000,rows:3})],'Generate preview',async data=>{const latest=await api(base);const body={version:latest.version,question:data.get('question')};const run=await api(base+'/previews','POST',{...body,client_request_id:identity(body)});identity.reset();replace(results,);await showRun(run,results);notify('Preview queued');})),results);await history(box,'preview');
    }
    async function topics(box, mode="policy") {
      let saved=await api(base+'/topics');
      const providers=await api('/ai/decision-providers');
      const provider=()=>providers.find(item=>item.id===saved.policy.provider);
      const policyBox=el('div'),providerStatus=el('div'),keysBox=el('div'),checksBox=el('div'),testResult=el('div',{'aria-live':'polite'});
      let keyDraw=0,addingKey=false;
      let policyDirty=false;
      const requireSaved=()=>{if(policyDirty)throw new Error('Save your topic edits before changing keys or testing the saved scope.');};
      const split=text=>text.split('\n').map(value=>value.trim()).filter(Boolean);
      function drawPolicy() {
        const value=saved.policy;
        const enabled=checkbox('Enable allowed topics','enabled',value.enabled);
        enabled.querySelector('input').disabled=!provider()?.available;
        replace(providerStatus,provider()?.available?hint('Save credentials, refresh models, select a model and verify its decision capability before enabling. A model listing alone does not establish access or topic accuracy.'):
          el('p',{class:'error',role:'status'},'Jev is listed by KodeKey, but its typed decision endpoint is currently unavailable. Choose Cloudflare for the supported Clef integration, or prepare Jev settings with activation blocked.'));
        const preset=select('How strictly to apply the scope','preset',[['balanced','Balanced'],['strict','Strict']],value.preset);
        const models=[...new Set(saved.keys.flatMap(key=>key.catalogue))];
        if(value.model&&!models.includes(value.model))models.push(value.model);
        const providerSelect=select('Decision provider','provider',providers.map(item=>[item.id,item.name]),value.provider);
        const modelSelect=select('Decision model','model',[['','Choose an available model'],...models.map(model=>[model,model])],value.model||'');
        const providerFields=[providerSelect,modelSelect,hint('Changing provider detaches this project’s decision keys and disables the topic gate. Account credentials remain saved. Changing its model also turns the gate off until you verify access and enable it in Allowed topics.')];
        const policyFields=[enabled,hint('Off by default. Applies to customer AI chat and full answer Preview. Human support remains available. Topic decisions cannot enable tools or submit reports.'),
          ...(mode==='providers'?[providerSelect,modelSelect]:[]),
          field('Allowed scope','scope','textarea',value.scope,{rows:5,maxlength:4000}),
          field('Public scope summary','public_summary','text',value.public_summary,{maxlength:240}),
          hint('The public summary is shown in refusals. Keep private instructions in the scope. Personality in soul.md cannot change these boundaries.'),
          el('div',{class:'ai-columns'},field('Allowed examples (one per line)','allowed_examples','textarea',value.allowed_examples.join('\n'),{rows:4,maxlength:3010}),field('Outside-scope examples (one per line)','disallowed_examples','textarea',value.disallowed_examples.join('\n'),{rows:4,maxlength:3010})),
          preset,hint('Balanced accepts reasonably clear matches. Strict needs stronger evidence. Thresholds are provisional; test in-scope, unrelated and mixed questions against your own scope before activation.'),
          field('Refusal messages (one per line)','refusal_messages','textarea',value.refusal_messages.join('\n'),{rows:4,maxlength:2505}),
          hint('Paste up to five plain-text messages, 500 characters each. Optional {scope} inserts the public summary. Leave empty for the default message. No links or HTML.'),
          hint('The decision provider receives the current customer message, the allowed scope and up to four earlier messages (8 KiB). It does not receive your knowledge library or customer identity.')
        ];
        replace(policyBox,form(mode==='providers'?providerFields:policyFields,mode==='providers'?'Save decision provider and model':'Save topic settings',async data=>{
          const policy=mode==='providers'?{...value,provider:data.get('provider'),model:data.get('model')||null,
            enabled:(data.get('provider')===value.provider&&(data.get('model')||null)===value.model)?value.enabled:false}:
            {enabled:data.get('enabled')==='true',provider:value.provider,model:value.model,
            scope:data.get('scope'),public_summary:data.get('public_summary'),preset:data.get('preset'),
            allowed_examples:split(data.get('allowed_examples')),disallowed_examples:split(data.get('disallowed_examples')),refusal_messages:split(data.get('refusal_messages'))};
          if(mode==='providers'&&policy.provider!==value.provider&&saved.keys.length&&!await confirmChange('Change decision provider?', 'This detaches '+saved.keys.length+' selected decision keys and turns the topic gate off. Account credentials and other projects are unchanged.', 'Change decision provider'))return;
          saved=await api(base+'/topics','PUT',{version:saved.version,policy});policyDirty=false;replace(testResult);await reloadConfig();if(!policyDirty)drawPolicy();drawKeys();drawChecks();notify(mode==='providers'?'Decision provider saved':'Topic settings saved');
        }));
        policyBox.querySelector('form').addEventListener('input',()=>{policyDirty=true;replace(testResult);keysBox.querySelectorAll('button').forEach(action=>action.disabled=true);});
        providerSelect.querySelector('select').addEventListener('change',event=>{
          policyDirty=true;
          const same=event.target.value===saved.policy.provider;
          const selectElement=modelSelect.querySelector('select');
          replace(selectElement,...[['','Choose an available model'],...(same?models.map(model=>[model,model]):[])].map(([id,label])=>el('option',{value:id},label)));
          selectElement.value=same?(value.model||''):'';
          enabled.querySelector('input').checked=false;
          enabled.querySelector('input').disabled=!same||!provider()?.available;
        });
      }
      function actionData(key){requireSaved();return {version:saved.version,key_version:key.version};}
      function drawKeys() {
        const draw=++keyDraw,selectedProvider=saved.policy.provider;
        const selection=el('div'),list=el('div');
        replace(list,...saved.keys.map(key=>el('article',{class:'ai-history'},el('h3',{},key.label),hint(key.verified?'Verified for '+saved.policy.model:'Not verified for the selected decision model'),row(
          button('Refresh decision models for '+key.label,async()=>{saved=await api(base+'/topics/keys/'+encode(key.id)+'/models/refresh','POST',actionData(key));await reloadConfig();if(!policyDirty)drawPolicy();drawKeys();drawChecks();}),
          button('Verify decision capability for '+key.label,async()=>{const action=actionData(key);const result=await api(base+'/topics/keys/'+encode(key.id)+'/probe','POST',action);if(policyDirty||saved.version!==action.version)throw new Error('Topic settings changed during this check. Reload and try again.');replace(testResult,hint(result.error?'Decision verification unavailable':'Typed decision response verified'));saved=await api(base+'/topics');if(!policyDirty)drawPolicy();drawKeys();drawChecks();})))));
        replace(keysBox,selection,list);
        keySelection(selection,selectedProvider,'decision',saved.keys.map(k=>k.id),async(ids,libraryVersion)=>{
          requireSaved();if(draw!==keyDraw||selectedProvider!==saved.policy.provider)throw new Error('Decision settings changed. Reload selections.');
          saved=await api(base+'/topics/keys/selection','PUT',{version:saved.version,library_version:libraryVersion,key_ids:ids});addingKey=false;await reloadConfig();drawPolicy();drawKeys();drawChecks();notify('Decision keys selected');
        },()=>addingKey=true).catch(error=>failure(selection,error,drawKeys));
      }
      function drawChecks(){
        if(mode==='providers')return;
        replace(checksBox,form([select('Decision key','key_id',saved.keys.map(key=>[key.id,key.label]),saved.keys[0]?.id||''),field('Synthetic test question','question','textarea','',{required:true,maxlength:4000,rows:3}),field('Previous customer message (optional)','previous','textarea','',{maxlength:4000,rows:2}),field('Previous assistant reply (optional)','assistant','textarea','',{maxlength:4000,rows:2}),hint('Uses the saved policy. Save edits first. This explicit check does not search knowledge, answer the question, run tools or enable the gate.')],'Test saved scope',async data=>{const key=saved.keys.find(key=>key.id===data.get('key_id'));if(!key)throw new Error('Save a decision key first.');const history=[];if(data.get('previous').trim())history.push({sender_role:'customer',body:data.get('previous')});if(data.get('assistant').trim())history.push({sender_role:'ai',body:data.get('assistant')});const action=actionData(key);const result=await api(base+'/topics/keys/'+encode(key.id)+'/test','POST',{...action,question:data.get('question'),history});if(policyDirty||saved.version!==action.version)throw new Error('Topic settings changed during this check. Test the saved settings again.');replace(testResult,el('p',{},'Decision: '+result.outcome),hint('Probability: '+(result.probability??'unavailable')+' • '+result.model+' • '+result.preset),result.reply?el('p',{class:'content'},result.reply):null);}),testResult);
      }
      replace(box,...(mode==='providers'?[panel('Topic-decision provider',hint('Checks whether a question belongs to your allowed topics before the assistant answers.'),el('ol',{class:'ai-steps'},el('li',{},'Choose and save Cloudflare or another supported decision provider.'),el('li',{},'Select account keys below, then refresh their available models.'),el('li',{},'Save a decision model and verify its capability. Enable it separately in Allowed topics.')),providerStatus,policyBox),panel('Account keys used for decisions',keysBox,testResult,row(button('Reload decision settings',async()=>{if((policyDirty||addingKey)&&!await confirmChange('Reload decision settings?','Unsaved model and key selections will be discarded.','Reload settings'))return;saved=await api(base+'/topics');policyDirty=false;addingKey=false;await reloadConfig();drawPolicy();drawKeys();drawChecks();})))]:[panel('Allowed topics',hint('Optional restrictions on what your assistant will answer. Choose its provider, model and account keys in Models & keys.'),button('Open Models & keys',()=>{window.history.pushState(null,'','#ai-support/providers');window.dispatchEvent(new Event('hashchange'));}),providerStatus,policyBox),panel('Test a topic decision',checksBox)]));
      box._refresh=async()=>{if(policyDirty||addingKey)return;saved=await api(base+'/topics');drawPolicy();drawKeys();drawChecks();};
      drawPolicy();drawKeys();drawChecks();if(mode==='providers')return;
      const historyList=el('div');
      async function history(offset=0){await paged(historyList,base+'/topics/decisions',items=>items.map(item=>el('article',{class:'ai-history'},el('p',{},item.outcome+' • '+(item.model||'Local policy reply')),hint(date(item.created_at)+' • probability '+(item.probability??'unavailable')+' • '+item.preset_version),item.error?hint('Decision service: '+item.error):null)),offset);}
      box.append(panel('Recent topic decisions',hint('Private administrator metadata. Message contents and decision keys are excluded.'),button('Refresh decisions',()=>history()),historyList));await history();
    }
    async function answerModels(box){
      let pool=await api(base+'/keys'),library=await api('/ai/keys'),baseline=config,dirty=false,pending=false,needsReload=false;
      const registry=await api('/ai/providers');
      let draft={provider:config.settings.provider,keyIds:pool.keys.map(key=>key.id),model:config.settings.model||''};
      const status=el('div',{class:'ai-setup-status',role:'status','aria-live':'polite'});
      const provider=select('1. Answer provider','provider',registry.map(item=>[item.id,item.name]),draft.provider);
      const keys=el('div',{class:'ai-key-choices'}),model=select('3. Answer model','model',[],draft.model);
      const modelHint=hint(''),changeHint=hint('');
      const checkTools=checkbox('Also verify assistant tools','check_tools',Object.values(config.settings.tools).some(Boolean)||pool.keys.some(key=>key.capabilities.tool_calling)||config.readiness?.issues.includes('answer_tools_unverified'));
      const submit=el('button',{type:'submit'},'Save & verify');
      const setStatus=(message,error=false)=>{replace(status,el('p',{class:error?'error':'hint'},message));status.setAttribute('role',error?'alert':'status');};
      async function acceptResult(result){
        config=result.config;baseline=config;pool=result.pool;library=await api('/ai/keys');dirty=false;
        draft={provider:config.settings.provider,keyIds:pool.keys.map(key=>key.id),model:config.settings.model||''};
        provider.querySelector('select').value=draft.provider;showSummary();drawKeys();drawModels();showSaved();
      }
      const disconnect=button('Disconnect answer keys',async()=>{
        if(pending||dirty||needsReload)return;
        if(!await confirmChange('Disconnect answer keys?','This turns AI off and removes this project’s answer-key selections. Account credentials remain saved.','Disconnect keys',status))return;
        await perform(async()=>{
          await acceptResult(await aiSaveAnswerSetup(api,base,baseline,pool,library,{provider:baseline.settings.provider,keyIds:[],model:null,disconnect:true,tools:false},setStatus));
          setStatus('Answer keys disconnected. AI is off. Your account credentials remain saved.');
        });
      });
      const refresh=button('Load / refresh available models',async()=>{
        if(pending)return;
        const selected=aiAnswerChoices(library,draft.provider,draft.keyIds).selected;
        if(!selected.length){setStatus('Choose at least one account key first.',true);return;}
        await perform(async()=>{
          for(const key of selected){
            setStatus('Loading models for '+key.label+'…');
            library=await api('/ai/keys/'+encode(key.id)+'/models/refresh','POST',{version:library.version,key_version:key.version,role:'generation'});
          }
          // Catalogue refreshes do not advance project configuration versions.
          drawModels();setStatus('Available models refreshed. Your project choices have not been saved.');
        });
      });
      const reload=button('Reload saved setup',async()=>{
        if(pending)return;
        if(dirty&&!needsReload&&!await confirmChange('Reload saved setup?','This discards your unsaved provider, key and model choices.','Reload setup',status))return;
        await perform(async()=>{
          await reloadConfig();baseline=config;pool=await api(base+'/keys');library=await api('/ai/keys');
          draft={provider:config.settings.provider,keyIds:pool.keys.map(key=>key.id),model:config.settings.model||''};
          dirty=false;needsReload=false;provider.querySelector('select').value=draft.provider;drawKeys();drawModels();showSaved();
        });
      });
      function controls(){
        fields.querySelectorAll('button,input,select').forEach(control=>control.disabled=pending);
        submit.disabled=pending||needsReload||!draft.keyIds.length||!aiAnswerChoices(library,draft.provider,draft.keyIds).models.includes(draft.model);
        refresh.disabled=pending||needsReload||!draft.keyIds.length;
        disconnect.disabled=pending||dirty||needsReload||!pool.keys.length;
        model.querySelector('select').disabled=pending||!aiAnswerChoices(library,draft.provider,draft.keyIds).models.length;
        fields.setAttribute('aria-busy',String(pending));
      }
      async function perform(action){
        pending=true;controls();
        try{await action();}catch(error){needsReload=true;try{await reloadConfig();}catch{}setStatus(error.message+' Setup stopped; some changes may already be saved. Reload saved setup before retrying.',true);}
        finally{pending=false;controls();}
      }
      function showSaved(){
        if(pool.keys.length&&pool.keys.every(key=>key.eligible))setStatus('Saved · '+pool.selected_model+' · verified'+(config.settings.enabled?'. AI is enabled.':'. AI is off. Enable it in Settings when you are ready.'));
        else setStatus('Choose your provider, account keys and model, then save and verify together.');
        if(!config.settings.enabled&&pool.keys.length&&pool.keys.every(key=>key.eligible))status.append(link('Open AI settings →','#'+(embedded?'ai-support/':'')+'setup'));
      }
      function edited(){dirty=true;setStatus('Unsaved choices — your current setup stays in place until you save.');drawModels();}
      function drawKeys(){
        const choices=aiAnswerChoices(library,draft.provider,draft.keyIds);
        replace(keys,...choices.keys.map(key=>{
          const choice=checkbox(key.label,'key_ids',draft.keyIds.includes(key.id),key.id);
          choice.querySelector('input').addEventListener('change',event=>{
            draft.keyIds=event.target.checked?[...draft.keyIds,key.id]:draft.keyIds.filter(id=>id!==key.id);edited();
          });
          return el('div',{class:'ai-account-choice'},choice,hint(key.quota_group+' · '+(key.enabled?key.status.replaceAll('_',' '):'Paused')));
        }),!choices.keys.length?hint('No answer keys saved for this provider. Add one in account settings, then reload this setup.'):null);
      }
      function drawModels(){
        const choices=aiAnswerChoices(library,draft.provider,draft.keyIds),selectElement=model.querySelector('select');
        if(!choices.models.includes(draft.model))draft.model='';
        replace(selectElement,...[['','Choose a model'],...choices.models.map(id=>[id,id])].map(([id,label])=>el('option',{value:id},label)));selectElement.value=draft.model;
        modelHint.textContent=!draft.keyIds.length?'Select an account key to see its models.':!choices.models.length?'Load the models for your selected keys. If they have no models in common, select different keys.':'Models available to every selected key. You can refresh this list without saving the project.';
        changeHint.textContent=draft.provider!==baseline.settings.provider?'Saving replaces this project’s answer-key selection and turns AI off. Your account keys remain saved.':'Changing the model or selected keys turns AI off until you enable it again in Settings.';
        controls();
      }
      const fields=el('form',{class:'ai-answer-setup'},provider,el('div',{},el('h4',{},'2. Account keys'),hint('Choose the saved keys this project may use.'),keys,link('Manage account API keys →','/admin/ai/providers')),el('div',{class:'ai-model-choice'},model,refresh),modelHint,checkTools,
        hint('Verification sends short test requests and may use provider quota. It does not enable AI or grant tool permissions.'),changeHint,row(submit,reload),status,
        el('details',{},el('summary',{},'Disconnect this project'),hint('Reload saved setup first if you have unsaved choices.'),disconnect));
      fields.addEventListener('submit',async event=>{
        event.preventDefault();if(pending||needsReload)return;
        await perform(async()=>{
          const result=await aiSaveAnswerSetup(api,base,baseline,pool,library,{...draft,keyIds:[...draft.keyIds],tools:checkTools.querySelector('input').checked},setStatus);
          await acceptResult(result);
        });
      });
      provider.querySelector('select').addEventListener('change',event=>{
        draft={provider:event.target.value,keyIds:[],model:''};drawKeys();edited();
      });
      model.querySelector('select').addEventListener('change',event=>{draft.model=event.target.value;dirty=true;setStatus('Ready to save and verify your choices.');controls();});
      checkTools.querySelector('input').addEventListener('change',()=>{dirty=true;});
      replace(box,panel('Answer setup',hint('Choose everything here, then save and verify in one step.'),fields));drawKeys();drawModels();showSaved();
      box._refresh=async()=>{
        if(dirty||pending||needsReload)return;
        const observed=baseline,nextConfig=await api(base),nextPool=await api(base+'/keys'),nextLibrary=await api('/ai/keys');
        if(dirty||pending||needsReload||baseline!==observed)return;
        config=nextConfig;baseline=config;pool=nextPool;library=nextLibrary;showSummary();
        draft={provider:config.settings.provider,keyIds:pool.keys.map(key=>key.id),model:config.settings.model||''};provider.querySelector('select').value=draft.provider;drawKeys();drawModels();showSaved();
      };
    }
    async function providers(box){
      const answers=el('section',{class:'ai-provider-group','aria-label':'Answer generation',tabindex:'-1'}),decisions=el('section',{class:'ai-provider-group','aria-label':'Topic decisions',tabindex:'-1'});
      const jump=section=>{section.scrollIntoView({block:'start'});section.focus({preventScroll:true});};
      replace(box,el('div',{class:'ai-provider-intro'},hint('Choose how your assistant answers and checks topics. Each project keeps its own selections.'),row(link('Manage account API keys →','/admin/ai/providers','button quiet-button'),button('Topic-decision setup ↓',()=>jump(decisions),'quiet-button'))),answers,decisions);
      await reloadConfig();await answerModels(answers);await topics(decisions,'providers');
      box._refresh=async()=>{await answers._refresh?.();await decisions._refresh?.();};
    }
    showSummary();replace(target,...(embedded?[]:[link('← '+p.name+' settings','/admin/projects/'+encode(p.id),'crumb'),hero('Project AI support',p.name,'Connect your model, review its knowledge, and decide exactly what the assistant may do.')]),
      el('div',{class:'ai-top-links'},summary,link('Embedding settings →','/admin/ai','button quiet-button')),
      ...tabs([['setup','Settings',box=>setup(box)],['providers','Models & keys',providers],['tools','Connected tools',box=>projectTools(box,p.id)],['topics','Allowed topics',topics],['knowledge','Knowledge',knowledge],['personality','Personality',personality],['preview','Preview',preview]],embedded?'ai-support/':''));
  }
  function projectPanel(p){
    const area=el('div',{class:'ai-admin'}),section=panel('AI support',area);
    section.classList.add('ai-project-settings');let loaded=false;
    section._activate=()=>{if(loaded)return;loaded=true;const load=async()=>{area.setAttribute('aria-busy','true');replace(area,loading());try{await project(p.id,area,true);}catch(error){loaded=false;failure(area,error,load);}finally{area.setAttribute('aria-busy','false');}};load();};
    return section;
  }

  async function toolConnections() {
    if(!requireAdmin())return;
    root.classList.add('ai-admin');
    const list=el('div'),editor=el('div');
    async function load(){const library=await api('/ai/tool-connections');replace(list,...library.items.map(connection=>panel(connection.label,
      hint(connection.url),hint(connection.enabled?(connection.tested?'Connected · review tools for each project':'Needs discovery'):'Disabled'),
      hint(connection.projects.length?'Used by '+connection.projects.map(p=>p.name).join(', '):'Not selected by any project'),
      row(button('Edit connection',()=>edit(connection)),button('Discover tools',async()=>{await api('/ai/tool-connections/'+encode(connection.id)+'/discover','POST',{version:connection.version});await load();notify('Tool catalogue refreshed');})),
      ...connection.catalogue.map(tool=>el('details',{class:'ai-original'},el('summary',{},tool.name+(tool.unsupported?' · unavailable':'')),hint(tool.reason||tool.description||'No description supplied'),
        tool.unsupported?null:original('Input schema',JSON.stringify(tool.inputSchema,null,2)),
        tool.unsupported?null:form([field('Test arguments (JSON)','arguments','textarea','{}',{rows:4,required:true}),hint('Runs this read using the saved credential. Use only public, synthetic test data. Results are not added to knowledge.')],'Run read-only test',async data=>{const result=await api('/ai/tool-connections/'+encode(connection.id)+'/test','POST',{version:connection.version,name:tool.name,arguments:JSON.parse(data.get('arguments'))});replace(editor,panel('Test result',original('Returned data',JSON.stringify(result.result,null,2)),button('Close',()=>replace(editor))));editor.scrollIntoView({block:'start'});notify('Read-only test completed');}))))));if(!library.items.length)replace(list,panel('No connected tools yet',hint('Connect a public read-only MCP service, discover its tools, then approve them for a project.')));}
    function edit(connection=null){
      const fields=[field('Connection name','label','text',connection?.label||'',{required:true,maxlength:80}),field('Public HTTPS MCP endpoint','url','url',connection?.url||'',{required:true}),field(connection?.has_token?'Replacement bearer token (leave blank to keep)':'Bearer token (optional)','token','password','',{autocomplete:'new-password'}),
        select('Authentication','auth',[['keep','Keep or replace token'],['none','No authentication']],'keep'),
        field('Allowed source link origins (one per line)','origins','textarea',(connection?.link_origins||[]).join('\n'),{rows:3}),
        select('Connection availability','enabled',[['false','Disabled'],['true','Available for project approval']],String(connection?.enabled||false)),
        hint('Only approve services whose selected tools are safe for anonymous visitors and cannot change data. A service token is not a customer login. Saving changes clears discovery and requires project review again.')];
      replace(editor,panel(connection?'Edit '+connection.label:'New tool connection',form(fields,'Save connection',async data=>{
        const token=data.get('token'),value={version:connection?.version||0,label:data.get('label'),url:data.get('url'),enabled:data.get('enabled')==='true',link_origins:lines(data.get('origins'))};
        if(data.get('auth')==='none')value.token=null;else if(token)value.token=token;else if(!connection)value.token=null;
        await api('/ai/tool-connections'+(connection?'/'+encode(connection.id):''),connection?'PATCH':'POST',value);replace(editor);await load();notify('Connection saved. Discover its tools next.');
      }),row(button('Cancel',()=>replace(editor)),connection?button('Remove connection',async()=>{if(!await confirmChange('Remove this connection?','This stops access in every project using it.','Remove connection'))return;await api('/ai/tool-connections/'+encode(connection.id),'DELETE',{version:connection.version});replace(editor);await load();},'danger'):null)));
      editor.scrollIntoView({block:'start'});
    }
    replace(root,link('← Administration','/admin','crumb'),hero('Account settings','Connected AI tools','Give your assistant approved, read-only access to public information from your apps.'),row(button('Add connection',()=>edit()),button('Refresh connections',load)),editor,list);await load();
  }
  async function projectTools(box,project) {
    const path='/projects/'+encode(project)+'/ai/tool-connections';
    const [saved,library,settings]=await Promise.all([api(path),api('/ai/tool-connections'),api('/projects/'+encode(project)+'/ai')]);
    const choices=[];
    const readiness=hint('Support knowledge: '+(settings.readiness.knowledge_ready?'ready':'not ready')+' · Connected tools: '+(settings.readiness.tools_ready?'ready':'not ready')+' · '+Object.values(settings.settings.tools).filter(Boolean).length+' of 8 tool slots used by built-in tools.');
    const cards=library.items.map(connection=>{
      const binding=saved.bindings.find(b=>b.connection_id===connection.id),current=connection.enabled&&connection.tested;
      const context=select('Resource context','context-'+connection.id,[['','General public data'],['podcast_episode','Episode on the visitor’s page']],binding?.context_kind||'');
      const fields=connection.catalogue.filter(t=>!t.unsupported).map(tool=>{
        const check=el('input',{type:'checkbox',checked:Boolean(binding?.connection_version===connection.version&&binding.tools.some(t=>t.digest===tool.digest)),disabled:!current});
        choices.push({connection,tool,check,context});return el('label',{class:'mcp-tool-choice'},check,el('span',{},el('strong',{},tool.name),hint(tool.description||'Read-only tool')));
      });
      return panel(connection.label,hint(current?'Select only tools that are safe for anonymous visitors.':'Enable and discover this connection in account settings first.'),context,...fields);
    });
    const mode=select('Answer sources','source_mode',[['knowledge','Support knowledge'],['combined','Support knowledge and connected tools'],['tools','Connected tools only']],saved.source_mode);
    replace(box,panel('Connected tools',readiness,hint('Use public app data in this project’s answers. Every selected tool is an explicit permission. Episode context requires get_episode, search_transcript and get_transcript_window together.'),link('Manage account connections →','/admin/ai/tools','button secondary')),
      form([mode,...cards],'Save tool permissions',async data=>{const bindings=[];for(const connection of library.items){const selected=choices.filter(c=>c.connection.id===connection.id&&c.check.checked);if(selected.length)bindings.push({connection_id:connection.id,connection_version:connection.version,context_kind:selected[0].context.querySelector('select').value||null,tools:selected.map(c=>({name:c.tool.name,digest:c.tool.digest}))});}await api(path,'PUT',{version:saved.version,source_mode:data.get('source_mode'),bindings});await projectTools(box,project);notify('Tool permissions saved');}));
  }

  return {runtime,accountKeys,toolConnections,project,projectPanel};
})();
