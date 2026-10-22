'use strict';
const chatRoot = document.getElementById('chat-admin');
const chatIcons = {
  message: 'M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5A8.5 8.5 0 0 1 10.5 3h2A8.5 8.5 0 0 1 21 11.5ZM7 10h9M7 14h5',
  user: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0M18 8v6M15 11h6',
  code: 'm8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 16',
  refresh: 'M20 7v5h-5M4 17v-5h5M6.1 7a7 7 0 0 1 11.6-2L20 8M4 16l2.3 3A7 7 0 0 0 17.9 17',
  chevron: 'm9 5 7 7-7 7',
  check: 'm5 12 4 4L19 6',
  back: 'm14 6-6 6 6 6',
  send: 'm22 2-7 20-4-9-9-4 20-7ZM22 2 11 13',
  clock: 'M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  close: 'm6 6 12 12M6 18 18 6',
  lock: 'M7 11V7a5 5 0 0 1 10 0v4M5 11h14v10H5ZM12 15v2',
  copy: 'M9 9h12v12H9ZM15 5V3H3v12h2',
};
function chatIcon(name, extra='') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox','0 0 24 24'); svg.setAttribute('class',`chat-icon ${extra}`);
  svg.setAttribute('aria-hidden','true');
  const path = document.createElementNS('http://www.w3.org/2000/svg','path');
  path.setAttribute('d',chatIcons[name] || chatIcons.message); svg.append(path); return svg;
}
function node(tag,attrs={},...children) {
  const n=document.createElement(tag);
  for(const [key,value] of Object.entries(attrs)) {
    if(key.startsWith('on'))n.addEventListener(key.slice(2),value);
    else if(key==='value')n.value=value;
    else if(key==='checked')n.checked=value;
    else n.setAttribute(key,value);
  }
  for(const child of children.flat()) if(child!==null&&child!==undefined)n.append(child?.nodeType?child:document.createTextNode(child));
  return n;
}
async function api(path,method='GET',body) {
  const headers={'Content-Type':'application/json'};
  if(method!=='GET')headers['X-CSRF-Token']=document.cookie.split('; ').find(x=>x.startsWith('feedback_csrf='))?.split('=').slice(1).join('=')||'';
  const response=await fetch('/api/v1'+path,{method,headers,body:body===undefined?undefined:JSON.stringify(body)});
  if(response.status===204)return null;
  const data=await response.json();
  if(!response.ok)throw new Error(data.error?.message||'Unable to complete this action');
  return data;
}
let active=null,cursor=0,historyRoot=null,polling=false,listState='open',offset=0;
let listGeneration=0,detailGeneration=0;
let refreshingInbox=false,inboxLoading=false;
let composerInput=null,sendButton=null,stateButton=null,stateBadge=null,closedNote=null,sending=false;
let supportPanel=null,reportPanel=null,modeBusy=false;
const presentation=window.ChatPresentation;
const drafts=new Map();
const notice=node('div',{class:'chat-notice',role:'status','aria-live':'polite'});
const inbox=node('div',{class:'chat-inbox-list','aria-label':'Conversations'});
const detail=node('section',{class:'chat-detail','aria-label':'Conversation detail'});
const frame=node('div',{class:'chat-frame'});
const setup=node('section',{class:'chat-setup','aria-label':'Website chat setup'});
let noticeTimer;
function showNotice(text,error=false) {
  clearTimeout(noticeTimer);notice.classList.toggle('is-error',error);
  notice.replaceChildren(chatIcon(error?'close':'check'),node('span',{},text),node('button',{type:'button',class:'chat-notice-close','aria-label':'Dismiss notification',onclick:()=>{clearTimeout(noticeTimer);notice.replaceChildren();}},chatIcon('close')));
  if(!error)noticeTimer=setTimeout(()=>notice.replaceChildren(),5000);
}
function report(error) {showNotice(error.message||String(error),true);}
function inform(text) {showNotice(text);}
function empty(title,description,small=false) {
  return node('div',{class:'chat-empty'},node('div',{class:'chat-empty-symbol'},chatIcon('message')),node('h2',{},title),node('p',{},description));
}
function shortDate(value) {
  const date=new Date(value);
  return date.toDateString()===new Date().toDateString()?date.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}):date.toLocaleDateString([],{month:'short',day:'numeric'});
}
function customerAuthenticated(conversation) {
  return ['external','native'].includes(conversation?.customer?.kind);
}
function customerName(conversation) {
  const name=conversation?.customer?.display_name;
  return typeof name==='string'&&name.trim()?name:customerAuthenticated(conversation)?'Customer':'Website visitor';
}
function customerSubtitle(conversation) {
  return `${presentation.modeLabel(conversation.support_mode)} · ${conversation.project_name}`;
}
function chatSettingsURL(projectId) {
  return '/admin/projects/'+encodeURIComponent(projectId)+'#website-chat';
}
function conversationProject(conversation) {
  return node('p',{},`${customerAuthenticated(conversation)?'Authenticated':'Anonymous'} · `,
    node('a',{href:chatSettingsURL(conversation.project_id),title:'Website chat settings for '+conversation.project_name},conversation.project_name));
}
function conversationInfo() {
  const fields=[['identity','Identity'],['follow_up_email','Follow-up email (unverified)'],['started','Started'],['updated','Updated'],['initial_ip_address','Initial IP address'],['last_customer_ip_address','Latest customer IP']];
  return node('details',{class:'chat-conversation-info'},node('summary',{},'Conversation info'),
    node('dl',{class:'chat-info-fields'},fields.map(([key,label])=>node('div',{},node('dt',{},label),node('dd',{'data-info':key},'')))),
    node('p',{class:'chat-info-hint'},'Connection details are visible only to administrators.'));
}
function updateConversationInfo(conversation) {
  const values={
    identity:customerAuthenticated(conversation)?'Authenticated':'Anonymous',
    follow_up_email:conversation.follow_up_email||'Not provided',
    started:new Date(conversation.created_at).toLocaleString(),
    updated:new Date(conversation.updated_at).toLocaleString(),
    initial_ip_address:conversation.initial_ip_address||'Not recorded',
    last_customer_ip_address:conversation.last_customer_ip_address||'Not recorded',
  };
  for(const field of detail.querySelectorAll('[data-info]')) {
    if(field.textContent!==values[field.dataset.info])field.textContent=values[field.dataset.info];
  }
}
function refreshSelection() {
  for(const row of inbox.querySelectorAll('[data-conversation]')) {
    const selected=row.dataset.conversation===active?.id;
    row.classList.toggle('is-active',selected);row.setAttribute('aria-pressed',String(selected));
  }
}
function updateInboxRow(row,item) {
  row.querySelector('.chat-row-time').textContent=shortDate(item.updated_at);
  const title=row.querySelector('.chat-row-title');
  title.textContent=customerName(item);title.title=customerName(item);
  row.querySelector('.chat-row-project').textContent=customerSubtitle(item);
  row.querySelector('.chat-avatar').classList.toggle('is-green',Boolean(item.unread_count));
  const old=row.querySelector('.chat-unread');
  if(item.unread_count) {
    const badge=old||node('span',{class:'chat-unread'});
    badge.textContent=item.unread_count;badge.setAttribute('aria-label',`${item.unread_count} unread messages`);
    if(!old)row.querySelector('.chat-row-bottom').append(badge);
  } else old?.remove();
}
function inboxRow(item) {
  return node('button',{class:'chat-row','data-conversation':item.id,'aria-pressed':'false',onclick:()=>openChat(item.id)},
    node('span',{class:`chat-avatar${item.unread_count?' is-green':''}`},chatIcon('message')),
    node('span',{class:'chat-row-main'},
      node('span',{class:'chat-row-top'},node('span',{class:'chat-row-title',title:customerName(item)},customerName(item)),node('span',{class:'chat-row-time'},shortDate(item.updated_at))),
      node('span',{class:'chat-row-bottom'},node('span',{class:'chat-row-project'},customerSubtitle(item)),item.unread_count?node('span',{class:'chat-unread','aria-label':`${item.unread_count} unread messages`},item.unread_count):null)));
}
function moreConversations() {
  const more=node('button',{class:'chat-button chat-button-ghost','data-more':'true',onclick:()=>{more.remove();loadInbox(true);}},'Load more conversations');
  return more;
}
async function refreshInbox() {
  if(document.hidden||refreshingInbox||inboxLoading||offset>100)return;
  const generation=listGeneration;
  const loaded=Math.max(25,offset);
  refreshingInbox=true;
  try {
    const page=await api(`/admin/chat/conversations?state=${listState}&limit=${loaded}&offset=0`);
    if(generation!==listGeneration)return;
    const focused=inbox.contains(document.activeElement)?document.activeElement:null;
    const previousScroll=inbox.scrollTop;
    const rows=new Map([...inbox.querySelectorAll('[data-conversation]')].map(row=>[row.dataset.conversation,row]));
    const wanted=new Set(page.items.map(item=>item.id));
    if(page.items.length) {
      for(const child of [...inbox.children])if(!child.dataset.conversation&&!child.dataset.more)child.remove();
      let position=0;
      for(const item of page.items) {
        const row=rows.get(item.id)||inboxRow(item);
        updateInboxRow(row,item);
        if(inbox.children[position]!==row)inbox.insertBefore(row,inbox.children[position]||null);
        position++;
      }
    }
    for(const [id,row] of rows)if(!wanted.has(id))row.remove();
    let more=inbox.querySelector('[data-more]');
    if(page.has_more) {if(!more)inbox.append(moreConversations());}
    else more?.remove();
    if(!page.items.length&&!inbox.querySelector('.chat-empty'))inbox.replaceChildren(empty(listState==='open'?'Your inbox is clear':'No closed conversations',listState==='open'?'New conversations appear here when a customer sends their first message.':'Conversations you close will be kept here.'));
    offset=page.items.length;refreshSelection();
    if(focused?.isConnected&&document.activeElement!==focused)focused.focus({preventScroll:true});
    else if(focused&&!focused.isConnected)(inbox.querySelector('[data-conversation]')||chatRoot.querySelector('.chat-tab[aria-selected=true]'))?.focus({preventScroll:true});
    inbox.scrollTop=previousScroll;
  } catch(_) {
    // A background refresh should not interrupt composing or erase the inbox.
  } finally {refreshingInbox=false;}
}
async function loadInbox(append=false,quiet=false) {
  const generation=++listGeneration;
  inboxLoading=true;inbox.setAttribute('aria-busy','true');
  const previousScroll=inbox.scrollTop;
  if(!append)offset=0;
  if(!append&&!quiet)inbox.replaceChildren(SuggestionShell.skeleton('rows','Loading conversations…'));
  try {
    const page=await api(`/admin/chat/conversations?state=${listState}&limit=25&offset=${offset}`);
    if(generation!==listGeneration)return;
    if(!append)inbox.replaceChildren();
    for(const item of page.items) {
      inbox.append(inboxRow(item));
    }
    if(!page.items.length&&!append)inbox.append(empty(listState==='open'?'Your inbox is clear':'No closed conversations',listState==='open'?'New conversations appear here when a customer sends their first message.':'Conversations you close will be kept here.'));
    offset+=page.items.length;
    if(page.has_more) {
      inbox.append(moreConversations());
    }
    refreshSelection();
    if(quiet)inbox.scrollTop=previousScroll;
  } catch(error) {
    if(generation===listGeneration) {
      report(error);
      if(!quiet) {
        const retry=node('button',{class:'chat-button',onclick:()=>{if(append)state.remove();loadInbox(append);}},'Try again');
        const state=empty('Couldn’t load conversations','Your conversations are still here. Try loading them again.');state.append(retry);
        if(append)inbox.append(state);else inbox.replaceChildren(state);
      }
    }
  }
  finally {if(generation===listGeneration){inboxLoading=false;inbox.setAttribute('aria-busy','false');}}

}
function applyState(value) {
  if(active?.id!==value.id)return;
  active=value;
  updateConversationInfo(value);
  const heading=detail.querySelector('.chat-conversation-heading');
  if(heading) {heading.querySelector('h2').textContent=customerName(value);heading.querySelector('h2').title=customerName(value);
    const projectLink=heading.querySelector('p a');
    if(!projectLink||projectLink.getAttribute('href')!==chatSettingsURL(value.project_id)||projectLink.textContent!==value.project_name)heading.querySelector('p').replaceWith(conversationProject(value));
  }
  for(const label of detail.querySelectorAll('[data-customer-label]'))label.textContent=customerName(value);
  if(stateBadge) {stateBadge.textContent=value.project_archived_at?'Archived':value.state==='open'?'Open':'Closed';stateBadge.classList.toggle('is-open',value.state==='open');}
  if(stateButton) {
    stateButton.replaceChildren(chatIcon(value.state==='open'?'check':'refresh'),document.createTextNode(value.state==='open'?'Close':'Reopen'));
    stateButton.disabled=Boolean(value.project_archived_at);
    stateButton.setAttribute('aria-label',value.state==='open'?'Close conversation':'Reopen conversation');
  }
  if(composerInput) {composerInput.disabled=sending||Boolean(value.project_archived_at)||value.state!=='open';composerInput.placeholder=value.project_archived_at?'Project archived — history is read-only':value.state==='open'?'Write a reply…':'This conversation is closed';}
  if(sendButton)sendButton.disabled=sending||Boolean(value.project_archived_at)||value.state!=='open'||!composerInput?.value.trim();
  if(closedNote)closedNote.hidden=value.state==='open';
  renderSupport(value);
  renderReports(value);
}
function renderSupport(value) {
  if(!supportPanel||!value)return;
  const signature=JSON.stringify([value.project_archived_at,value.support_mode,value.state,value.ai_available,Number.isInteger(value.mode_version),modeBusy,value.follow_up_email]);
  if(supportPanel.dataset.signature===signature)return;
  supportPanel.dataset.signature=signature;
  const mode=value.support_mode||'human';
  const text=value.project_archived_at?'This project is archived. Conversation history is preserved.':mode==='waiting_human'?'This customer is waiting for your team.':mode==='ai'?'AI is assisting. Sending a reply takes over this conversation.':'Your team is handling this conversation.';
  const controls=[];
  if(!value.project_archived_at&&value.state==='open'&&Number.isInteger(value.mode_version)) {
    if(mode!=='human')controls.push(node('button',{class:'chat-button',onclick:()=>changeMode('human')},'Take over'));
    if(mode!=='ai'&&value.ai_available===true)controls.push(node('button',{class:'chat-button chat-button-ghost',onclick:()=>changeMode('ai')},'Return to AI'));
  }
  for(const control of controls)control.disabled=modeBusy;
  supportPanel.replaceChildren(node('div',{},node('strong',{},presentation.modeLabel(mode)),node('p',{},text),value.follow_up_email?node('p',{},'Manual follow-up: '+value.follow_up_email+' (unverified)'):null),...controls);
}
async function changeMode(mode) {
  const id=active?.id,version=active?.mode_version,generation=detailGeneration;
  if(!id||modeBusy)return;
  modeBusy=true;renderSupport(active);
  try {
    const updated=await api(`/chat/conversations/${id}/support-mode`,'PUT',{mode,mode_version:version});
    if(generation!==detailGeneration||active?.id!==id)return;
    applyState(updated);inform(mode==='human'?'Your team has taken over.':'AI is ready for the customer’s next message.');await loadInbox(false,true);
  } catch(error) {
    if(generation!==detailGeneration||active?.id!==id)return;
    try {applyState(await api('/admin/chat/conversations/'+id));}catch(_){}
    report(error);
  } finally {if(generation===detailGeneration){modeBusy=false;renderSupport(active);}}
}
function renderReports(value) {
  if(!reportPanel)return;
  const reports=value.reports||[];
  const signature=JSON.stringify([value.report_count,reports]);
  if(reportPanel.dataset.signature===signature)return;
  reportPanel.dataset.signature=signature;
  reportPanel.hidden=!reports.length;
  reportPanel.replaceChildren(node('summary',{},`Submitted feedback (${value.report_count||reports.length})`),
    ...reports.map(report=>node('div',{class:'chat-report'},
      report.deleted?node('span',{},report.title):node('a',{href:'/requests/'+encodeURIComponent(report.request_id)},report.title),
      node('small',{},`${report.guest?'Guest report':'Customer report'} · ${report.deleted?'Deleted':report.moderation==='pending'?'Awaiting review':report.moderation==='rejected'?'Rejected':(report.status||'Accepted').replaceAll('_',' ')}`))),
    node('p',{class:'chat-info-hint'},'Guest reports remain separate from accounts. This view does not grant access to the customer’s chat credential.'));
}
function appendMessage(item,target) {
  const isAdmin=item.sender==='admin',isAI=item.sender==='ai';
  const timestamp=isAI?node('time',{datetime:item.created_at},shortDate(item.created_at)):node('details',{class:'chat-message-details'},
    node('summary',{title:'Show message connection details','aria-label':'Message details, '+new Date(item.created_at).toLocaleString()},node('time',{datetime:item.created_at},shortDate(item.created_at))),
    node('p',{},'IP address · ',node('span',{class:'ip-address'},item.ip_address||'Not recorded')));
  const sources=[...(item.citations||[]),...(item.tool_sources||[])];
  const citations=isAI&&sources.length?node('details',{class:'chat-citations'},node('summary',{},'Sources'),...sources.map(citation=>{
    const url=presentation.sourceURL(citation.url);
    return node('div',{},url?node('a',{href:url,target:'_blank',rel:'noopener noreferrer'},citation.title):node('strong',{},citation.title),node('p',{},citation.excerpt),citation.start_seconds!=null?node('small',{},(citation.timing_kind==='approximate'?'Approximate time · ':'Time · ')+Math.floor(citation.start_seconds/60)+':'+String(Math.floor(citation.start_seconds%60)).padStart(2,'0')):null);
  })):null;
  target.append(node('article',{class:`chat-message${isAdmin?' is-admin':isAI?' is-ai':''}`},node('p',{class:'chat-bubble'},item.body),citations,
    node('div',{class:'chat-message-meta'},node('span',item.sender==='visitor'?{'data-customer-label':''}:{},presentation.senderLabel(item.sender,customerName(active))),node('span',{'aria-hidden':'true'},'·'),timestamp,isAdmin?chatIcon('check'):null)));
}
async function loadMessageHistory() {
  if(!active||polling||!historyRoot)return;
  const conversationId=active.id,logTarget=historyRoot;
  polling=true;
  try {
    let more=true,added=false;
    const atBottom=logTarget.scrollHeight-logTarget.scrollTop-logTarget.clientHeight<80;
    while(more) {
      const page=await api(`/chat/conversations/${conversationId}/messages?after=${cursor}&limit=100`);
      if(active?.id!==conversationId||historyRoot!==logTarget)return;
      for(const item of page.items) {appendMessage(item,logTarget);cursor=item.sequence;added=true;}
      more=page.has_more;
    }
    if(added&&atBottom)logTarget.scrollTop=logTarget.scrollHeight;
    if(cursor&&!document.hidden&&logTarget.offsetParent!==null)await api(`/chat/conversations/${conversationId}/read`,'POST',{sequence:cursor});
    const latest=await api('/admin/chat/conversations/'+conversationId);
    if(active?.id===conversationId)applyState(latest);
  } catch(error) {report(error);} finally {polling=false;}
}
async function openChat(id) {
  const generation=++detailGeneration;
  if(active&&composerInput)drafts.set(active.id,{...(drafts.get(active.id)||{}),body:composerInput.value});
  frame.classList.add('is-detail');
  detail.replaceChildren(SuggestionShell.skeleton('page','Opening conversation…'));
  detail.setAttribute('aria-busy','true');
  try {
    const conversation=await api('/admin/chat/conversations/'+encodeURIComponent(id));
    if(generation!==detailGeneration)return;
    const conversationURL=new URL(location.href);
    conversationURL.searchParams.set('conversation',conversation.id);
    window.history.replaceState(window.history.state,'',conversationURL);
    active=conversation;cursor=0;historyRoot=null;composerInput=null;sendButton=null;sending=false;modeBusy=false;
    const back=node('button',{class:'chat-button chat-button-ghost chat-back','aria-label':'Back to conversations',onclick:()=>{
      frame.classList.remove('is-detail');
      const inboxURL=new URL(location.href);inboxURL.searchParams.delete('conversation');
      window.history.replaceState(window.history.state,'',inboxURL);
      ([...inbox.querySelectorAll('[data-conversation]')].find(row=>row.dataset.conversation===id)||chatRoot.querySelector('.chat-tab[aria-selected=true]'))?.focus({preventScroll:true});
    }},chatIcon('back'));
    stateBadge=node('span',{class:'chat-state-badge is-open'},'Open');
    stateButton=node('button',{class:'chat-button chat-button-ghost','aria-label':'Change conversation state',onclick:async()=>{
      stateButton.disabled=true;
      try {const next=await api(`/admin/chat/conversations/${id}`,'PATCH',{state:active.state==='open'?'closed':'open',version:active.version});applyState(next);inform(next.state==='closed'?'Conversation closed.':'Conversation reopened.');await loadInbox(false,true);}
      catch(error){report(error);}finally{stateButton.disabled=false;}
    }},chatIcon('check'),'Close');
    const heading=node('header',{class:'chat-conversation-header'},back,node('span',{class:'chat-avatar'},chatIcon('message')),
      node('div',{class:'chat-conversation-heading'},node('h2',{title:customerName(conversation)},customerName(conversation)),conversationProject(conversation)),stateBadge,stateButton);
    historyRoot=node('div',{class:'chat-history',role:'log','aria-live':'polite','aria-label':'Conversation messages'},node('p',{class:'chat-history-date'},new Date(conversation.created_at).toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'})));
    composerInput=node('textarea',{id:'chat-reply',maxlength:'4000',required:'',rows:'2',placeholder:'Write a reply…',value:drafts.get(id)?.body||''});
    sendButton=node('button',{class:'chat-button chat-button-primary',type:'submit'},'Send reply',chatIcon('send'));
    const replyForm=node('form',{class:'chat-composer',onsubmit:async event=>{
      event.preventDefault();if(sending||active?.id!==id||!composerInput.value.trim())return;
      const input=composerInput;const submit=sendButton;sending=true;input.disabled=true;submit.disabled=true;submit.setAttribute('aria-busy','true');submit.replaceChildren(document.createTextNode('Sending…'));
      let pending=drafts.get(id);
      if(!pending?.client_message_id||pending.body!==input.value)pending={client_message_id:crypto.randomUUID(),body:input.value};
      drafts.set(id,pending);
      try {
        await api(`/chat/conversations/${id}/messages`,'POST',{client_message_id:pending.client_message_id,body:pending.body});
        drafts.delete(id);input.value='';await loadMessageHistory();inform('Reply sent.');await loadInbox(false,true);
      } catch(error) {report(error);} finally {sending=false;submit.removeAttribute('aria-busy');submit.replaceChildren(document.createTextNode('Send reply'),chatIcon('send'));if(active?.id===id){input.disabled=Boolean(active.project_archived_at)||active.state!=='open';submit.disabled=input.disabled||!input.value.trim();if(!input.disabled)input.focus();}}
    }},node('label',{class:'chat-sr-only',for:'chat-reply'},'Reply'),composerInput,
      node('div',{class:'chat-compose-footer'},node('span',{class:'chat-compose-hint'},'Enter for a new line · ⌘ / Ctrl + Enter to send'),sendButton));
    composerInput.addEventListener('input',()=>{if(active?.id===id)sendButton.disabled=sending||Boolean(active.project_archived_at)||active.state!=='open'||!composerInput.value.trim();});
    composerInput.addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.metaKey||event.ctrlKey)){event.preventDefault();replyForm.requestSubmit();}});
    closedNote=node('div',{class:'chat-closed-note'},chatIcon('check'),'This conversation is closed. Reopen it to reply.');
    supportPanel=node('div',{class:'chat-support','aria-label':'Support mode'});
    reportPanel=node('details',{class:'chat-conversation-info chat-reports'});
    detail.replaceChildren(heading,supportPanel,conversationInfo(),reportPanel,historyRoot,closedNote,replyForm);
    applyState(conversation);refreshSelection();await loadMessageHistory();
    historyRoot.scrollTop=historyRoot.scrollHeight;
    if(matchMedia('(max-width:720px)').matches)back.focus({preventScroll:true});
  } catch(error) {report(error);detail.replaceChildren(empty('Unable to open conversation','Try selecting the conversation again.'));}
  finally {if(generation===detailGeneration)detail.setAttribute('aria-busy','false');}
}
(async()=>{
  try {
    const account=await api('/auth/me');
    if(account.role!=='admin')throw new Error('Sign in as an administrator to use chat.');
    window.SuggestionShell?.mount(account,async()=>{await api('/auth/logout','POST');location.href='/login';});
    const projects=[];
    let moreProjects=true;
    while(moreProjects) {
      const page=await api('/projects?limit=100&offset='+projects.length);
      projects.push(...page.items);moreProjects=page.has_more;
    }
    const setupButton=node('a',{class:'chat-button',href:'/admin#projects','aria-label':'Widget setup'},chatIcon('code'),node('span',{class:'chat-button-label'},'Widget setup'));
    const settingsLink=node('a',{class:'chat-setup-link',href:'/admin#projects'},'Configure & install',chatIcon('chevron'));
    const project=node('select',{class:'chat-field',id:'chat-project'},projects.map(p=>node('option',{value:p.id},p.name)));
    function selectProject() {
      const selected=projects.find(p=>p.id===project.value);
      if(!selected)return;
      setupButton.href=settingsLink.href=chatSettingsURL(selected.id);
      setupButton.setAttribute('aria-label','Widget setup for '+selected.name);
      settingsLink.setAttribute('aria-label','Configure & install website chat for '+selected.name);
      const url=new URL(location.href);url.searchParams.set('project',selected.id);history.replaceState(history.state,'',url);
    }
    project.addEventListener('change',selectProject);
    const requestedProject=new URLSearchParams(location.search).get('project');
    if(projects.some(p=>p.id===requestedProject))project.value=requestedProject;
    if(projects.length) {
      selectProject();
      setup.append(node('div',{class:'chat-setup-heading'},chatIcon('code'),node('div',{},node('strong',{},'Website chat'),node('p',{},'Settings and installation for each project.'))),
        node('div',{class:'chat-project-control'},node('label',{class:'chat-label',for:'chat-project'},'Project'),project),settingsLink);
    } else {
      setup.append(node('p',{},'Create a project to configure website chat.'),node('a',{class:'chat-button',href:'/admin#projects'},'Create a project'));
    }
    const tabs=node('div',{class:'chat-inbox-tabs',role:'tablist','aria-label':'Conversation state'});
    for(const state of ['open','closed'])tabs.append(node('button',{class:'chat-tab',role:'tab',tabindex:state===listState?'0':'-1','aria-selected':String(state===listState),onclick:event=>{listState=state;for(const tab of tabs.children){tab.setAttribute('aria-selected',String(tab===event.currentTarget));tab.tabIndex=tab===event.currentTarget?0:-1;}loadInbox();}},state==='open'?'Open':'Closed'));
    tabs.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const current=[...tabs.children].indexOf(document.activeElement);const next=event.key==='Home'?0:event.key==='End'?tabs.children.length-1:(current+(event.key==='ArrowRight'?1:-1)+tabs.children.length)%tabs.children.length;tabs.children[next].focus();tabs.children[next].click();}});
    const refresh=node('button',{class:'chat-button chat-button-ghost chat-refresh','aria-label':'Refresh inbox',title:'Refresh inbox',onclick:async event=>{const control=event.currentTarget;if(inboxLoading)return;control.disabled=true;control.setAttribute('aria-busy','true');try{await loadInbox(false,true);}finally{control.disabled=false;control.removeAttribute('aria-busy');}}},chatIcon('refresh'));
    const inboxPanel=node('section',{class:'chat-inbox-panel','aria-label':'Conversation inbox'},node('div',{class:'chat-inbox-tools'},tabs,refresh),inbox,node('div',{class:'chat-inbox-footer'},chatIcon('lock'),'Conversations stay on your server'));
    detail.append(empty('A conversation starts here','Choose a conversation from your inbox to read messages and send a reply.'));
    frame.append(inboxPanel,detail);
    chatRoot.replaceChildren(node('div',{class:'chat-header'},node('div',{},node('h1',{},'Conversations'),node('p',{},'A direct line between your team and your users.')),node('div',{class:'chat-header-actions'},node('span',{class:'chat-status'},'Your server, your conversations'),setupButton)),notice,setup,frame);
    chatRoot.setAttribute('aria-busy','false');
    await loadInbox();
    const referencedConversation=new URLSearchParams(location.search).get('conversation');
    if(referencedConversation) {
      if(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(referencedConversation)) {
        await openChat(referencedConversation.toLowerCase());
        if(active?.id===referencedConversation.toLowerCase()&&active.state!==listState) {
          listState=active.state;
          for(const tab of tabs.children) {const selected=tab.textContent.toLowerCase()===listState;tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;}
          await loadInbox();
        }
      } else report(new Error('This conversation link is invalid.'));
    }
    setInterval(()=>{if(!document.hidden)loadMessageHistory();},2000);
    setInterval(refreshInbox,10000);
  } catch(error) {
    window.SuggestionShell?.mount(null,()=>{});
    chatRoot.setAttribute('aria-busy','false');
    chatRoot.replaceChildren(node('div',{class:'chat-empty'},node('div',{class:'chat-empty-symbol'},chatIcon('lock')),node('h1',{},'Your conversations await'),node('p',{},error.message),node('a',{class:'chat-button chat-button-primary',href:'/login'},'Sign in')));
  }
})();
