"use strict";
(() => {
  const paths = {
    box: ['M4 8l8-4 8 4v10l-8 4-8-4V8z','M4 8l8 4 8-4M12 12v10M8 6l8 4'],
    key: ['M8 15a5 5 0 1 1 4-8M12 11l9 9M17 16l3-3M19 18l3-3'],
    grid: ['M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z'],
    inbox: ['M4 4h16v16H4z','M4 14h5l2 3h2l2-3h5','M8 8h8'],
    chat: ['M21 11.5a8.5 8.5 0 01-8.5 8.5H4l-2 2V11.5A8.5 8.5 0 0110.5 3h2a8.5 8.5 0 018.5 8.5z','M7 10h9M7 14h6'],
    bell: ['M18 8a6 6 0 00-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4'],
    user: ['M16 7a4 4 0 11-8 0 4 4 0 018 0zM4 21v-2a8 8 0 0116 0v2'],
    arrow: ['M5 12h14M13 6l6 6-6 6'],
    chevron: ['M7 14l5-5 5 5'],
    code: ['M8 6l-6 6 6 6M16 6l6 6-6 6M14 3l-4 18'],
    logout: ['M9 4H4v16h5M10 12h11M17 8l4 4-4 4'],
    sun: ['M12 8a4 4 0 110 8 4 4 0 010-8M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5'],
    menu: ['M4 6h16M4 12h16M4 18h16'],
    close: ['M6 6l12 12M6 18L18 6'],
    plus: ['M12 5v14M5 12h14'],
    check: ['M5 12l4 4L19 6'],
    clock: ['M12 3a9 9 0 110 18 9 9 0 010-18M12 7v5l3 2'],
    globe: ['M12 3a9 9 0 110 18 9 9 0 010-18M3 12h18M12 3c-5 6-5 12 0 18 5-6 5-12 0-18'],
  };
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [k,v] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.5','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',class:'icon'})) svg.setAttribute(k,v);
    for (const d of paths[name] || paths.box) { const path = document.createElementNS(svg.namespaceURI,'path'); path.setAttribute('d',d); svg.append(path); }
    return svg;
  }
  const node = (tag, cls, text) => { const n=document.createElement(tag); n.className=cls || ''; if(text) n.textContent=text; return n; };
  function skeleton(kind='rows', label='Loading…') {
    const state=node('div','skeleton-set skeleton-'+kind);state.setAttribute('role','status');
    state.append(node('span','visually-hidden',label));
    for(let i=0;i<(kind==='page'?3:4);i++) {
      const row=node('div','skeleton-item');row.setAttribute('aria-hidden','true');
      row.append(node('span','skeleton-block skeleton-symbol'));
      const copy=node('div','skeleton-copy');
      copy.append(node('span','skeleton-block skeleton-line'),node('span','skeleton-block skeleton-line is-short'));
      row.append(copy);state.append(row);
    }
    return state;
  }
  let disposeNotifications=()=>{};
  let disposeNavigation=()=>{};
  function notificationCenter() {
    const center=node('div','notification-center');
    const trigger=node('button','icon-button notification-bell');trigger.type='button';
    trigger.setAttribute('aria-label','Notifications');trigger.setAttribute('aria-haspopup','dialog');
    trigger.setAttribute('aria-controls','notification-inbox');trigger.setAttribute('aria-expanded','false');
    trigger.append(icon('bell'));
    const badge=node('span','notification-badge');badge.hidden=true;badge.setAttribute('aria-hidden','true');trigger.append(badge);
    const popover=node('section','notification-popover');popover.id='notification-inbox';popover.hidden=true;
    popover.setAttribute('role','dialog');popover.setAttribute('aria-labelledby','notification-heading');popover.setAttribute('aria-describedby','notification-shared');popover.tabIndex=-1;
    const heading=node('div','notification-heading');
    const headingCopy=node('div');const title=node('h2','','Notifications');title.id='notification-heading';
    const shared=node('p','','Shared with all admins');shared.id='notification-shared';headingCopy.append(title,shared);
    const close=node('button','icon-button notification-close');close.type='button';close.setAttribute('aria-label','Close notifications');close.append(icon('close'));heading.append(headingCopy,close);
    const toolbar=node('div','notification-toolbar');
    const filters=node('div','notification-filters');filters.setAttribute('role','group');filters.setAttribute('aria-label','Filter notifications');
    const all=node('button','','All');all.type='button';all.setAttribute('aria-pressed','true');
    const unread=node('button','','Unread');unread.type='button';unread.setAttribute('aria-pressed','false');filters.append(all,unread);
    const markAll=node('button','quiet-button notification-mark-all','Mark all read');markAll.type='button';markAll.disabled=true;
    toolbar.append(filters,markAll);
    const message=node('p','notification-message');message.hidden=true;message.setAttribute('role','status');
    const list=node('ul','notification-list');list.setAttribute('aria-label','Notifications');
    const pager=node('div','notification-pager');pager.hidden=true;
    const range=node('span');const previous=node('button','quiet-button','Newer');previous.type='button';
    const next=node('button','quiet-button','Older');next.type='button';pager.append(range,previous,next);
    const live=node('span','visually-hidden');live.setAttribute('role','status');live.setAttribute('aria-live','polite');
    popover.append(heading,toolbar,message,list,pager);center.append(trigger,popover,live);

    let open=false,disposed=false,busy=false,loading=false,unreadOnly=false,offset=0;
    let count=null,throughSequence=0,generation=0,summaryGeneration=0,fingerprint='',items=[],hasMore=false;
    let listController=null,summaryController=null;
    const limit=25;
    async function request(path,method='GET',body,signal) {
      const headers={};
      if(body!==undefined)headers['Content-Type']='application/json';
      if(method!=='GET') {
        const token=document.cookie.split('; ').find(value=>value.startsWith('feedback_csrf='))?.split('=').slice(1).join('=')||'';
        headers['X-CSRF-Token']=decodeURIComponent(token);
      }
      const response=await fetch('/api/v1/admin/notifications'+path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),signal,cache:'no-store'});
      const data=await response.json().catch(()=>null);
      if(!response.ok)throw new Error(data?.error?.message||'Notifications could not be updated. Please try again.');
      return data;
    }
    function updateCount(value) {
      const changed=count!==null&&count!==value;count=value;
      badge.hidden=!value;badge.textContent=value>99?'99+':String(value);
      trigger.setAttribute('aria-label',value?`Notifications, ${value} unread`:'Notifications, all read');
      trigger.removeAttribute('title');
      if(changed)live.textContent=value?`${value} unread notifications shared with all admins.`:'All notifications are read.';
      updateControls();
    }
    function updateControls() {
      all.disabled=unread.disabled=busy;
      markAll.disabled=busy||loading||!count||!throughSequence;
      previous.disabled=busy||offset===0;next.disabled=busy||!hasMore;
      for(const control of list.querySelectorAll('button'))control.disabled=busy;
      list.setAttribute('aria-busy',String(loading||busy));
    }
    function showMessage(text,error=false) {
      message.textContent=text;message.hidden=!text;message.classList.toggle('is-error',error);
    }
    function showEmpty(text,kind='bell') {
      const empty=node('li','notification-empty');empty.append(icon(kind),node('strong','',text));
      empty.append(node('span','',unreadOnly?'You’re up to date. New requests and messages will appear here.':'New requests and customer messages will appear here.'));
      list.replaceChildren(empty);
    }
    function safeURL(item) {
      if(!item.resource_available||typeof item.url!=='string'||!item.url.startsWith('/')||item.url.startsWith('//'))return null;
      const url=new URL(item.url,location.origin);
      return url.origin===location.origin?url.pathname+url.search+url.hash:null;
    }
    function renderItems() {
      const signature=JSON.stringify([offset,unreadOnly,items]);
      if(signature===fingerprint)return;
      fingerprint=signature;
      const focused=document.activeElement;
      const focusId=list.contains(focused)?focused.dataset.notification:null;
      const focusAction=focusId?focused.dataset.action:null;
      const scroll=list.scrollTop;
      const rows=items.map(item=>{
        const row=node('li','notification-item'+(item.read_at?'':' is-unread'));
        const symbol=node('span','notification-symbol');symbol.append(icon(item.resource_kind==='conversation'?'chat':'inbox'));
        const copy=node('div','notification-copy');const url=safeURL(item);
        const title=node(url?'a':'span','notification-title',item.title);title.dataset.notification=item.id;title.dataset.action='open';
        if(url) {
          title.href=url;
          title.addEventListener('click',async event=>{
            if(event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
            event.preventDefault();
            if(busy)return;
            if(item.read_at){location.assign(url);return;}
            await mutate(()=>request('/'+encodeURIComponent(item.id)+'/read','PUT',{read:true}),()=>location.assign(url));
          });
        }
        const metadata=node('span','notification-meta');
        const label=item.type==='request.resubmitted'?'Request resubmitted':item.resource_kind==='conversation'?'Customer message':'New request';
        metadata.append(node('span','',item.project_name+' · '+label));
        const time=node('time','',new Date(item.created_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}));
        time.dateTime=item.created_at;metadata.append(time);
        copy.append(title,metadata);
        if(!url)copy.append(node('span','notification-unavailable','This '+(item.resource_kind==='conversation'?'conversation':'request')+' is no longer available.'));
        const read=node('button','notification-read');read.type='button';
        const labelRead=item.read_at?'Mark unread':'Mark read';read.title=labelRead;read.setAttribute('aria-label',labelRead+': '+item.title);
        read.dataset.notification=item.id;read.dataset.action='read';
        if(item.read_at)read.append(icon('check'));else read.append(node('span','notification-unread-dot'));
        read.addEventListener('click',()=>mutate(()=>request('/'+encodeURIComponent(item.id)+'/read','PUT',{read:!item.read_at})));
        row.append(symbol,copy,read);return row;
      });
      if(rows.length)list.replaceChildren(...rows);else showEmpty(unreadOnly?'All caught up':'Nothing here yet');
      list.scrollTop=scroll;
      if(focusId) {
        const replacement=[...list.querySelectorAll('[data-notification]')].find(n=>n.dataset.notification===focusId&&n.dataset.action===focusAction);
        (replacement||popover).focus({preventScroll:true});
      }
    }
    async function loadList(quiet=false) {
      if(disposed||!open||busy)return;
      summaryGeneration++;summaryController?.abort();
      listController?.abort();listController=new AbortController();
      const current=++generation;
      const restoreMarkAll=document.activeElement===markAll;
      if(restoreMarkAll)popover.focus({preventScroll:true});
      loading=true;updateControls();
      if(!quiet){showMessage('');if(!items.length){fingerprint='';const state=node('li','notification-loading');state.append(skeleton('rows','Loading notifications…'));list.replaceChildren(state);}}
      try {
        const result=await request(`?unread_only=${unreadOnly}&limit=${limit}&offset=${offset}`,'GET',undefined,listController.signal);
        if(disposed||current!==generation||!open)return;
        if(!result.items.length&&offset>0){offset=Math.max(0,offset-limit);loading=false;await loadList(quiet);return;}
        items=result.items;hasMore=result.has_more;
        // An older page must not acknowledge notifications that arrived above it.
        if(offset===0)throughSequence=result.through_sequence;
        updateCount(result.unread_count);showMessage('');renderItems();
        pager.hidden=offset===0&&!hasMore;range.textContent=items.length?`${offset+1}–${offset+items.length}`:'';
      } catch(error) {
        if(error.name==='AbortError'||disposed||current!==generation||!open)return;
        showMessage(error.message,true);
        if(!items.length){const failed=node('li','notification-empty');failed.append(node('strong','','Couldn’t load notifications'));const retry=node('button','secondary','Try again');retry.type='button';retry.addEventListener('click',()=>loadList());failed.append(retry);list.replaceChildren(failed);}
      } finally {if(current===generation){loading=false;updateControls();if(open&&restoreMarkAll&&document.activeElement===popover&&!markAll.disabled)markAll.focus({preventScroll:true});}}
    }
    async function summary() {
      if(disposed||busy||document.hidden)return;
      summaryController?.abort();summaryController=new AbortController();const current=++summaryGeneration;
      try {
        const result=await request('/summary','GET',undefined,summaryController.signal);
        if(!disposed&&current===summaryGeneration)updateCount(result.unread_count);
      } catch(error) {
        if(error.name!=='AbortError'&&!disposed&&current===summaryGeneration)trigger.title='Notifications could not be refreshed. Open to try again.';
      }
    }
    async function mutate(operation,onSuccess) {
      if(busy||disposed)return;
      const focused=document.activeElement;
      const restoreFocus=popover.contains(focused);
      const focusId=focused.dataset?.notification,focusAction=focused.dataset?.action;
      if(restoreFocus)popover.focus({preventScroll:true});
      let succeeded=false;
      busy=true;generation++;summaryGeneration++;listController?.abort();summaryController?.abort();loading=false;updateControls();showMessage('');
      try {
        await operation();if(disposed)return;
        if(onSuccess){onSuccess();return;}
        fingerprint='';succeeded=true;
      } catch(error) {if(!disposed)showMessage(error.message,true);}
      finally {busy=false;if(!disposed)updateControls();}
      if(succeeded){if(open)await loadList(true);else await summary();}
      if(open&&restoreFocus&&document.activeElement===popover) {
        const target=focusId?[...list.querySelectorAll('[data-notification]')].find(n=>n.dataset.notification===focusId&&n.dataset.action===focusAction):focused;
        (target?.isConnected&&!target.disabled?target:popover).focus({preventScroll:true});
      }
    }
    function setOpen(value,restoreFocus=true) {
      open=value;popover.hidden=!value;trigger.setAttribute('aria-expanded',String(value));
      if(value){close.focus();loadList();}
      else {generation++;listController?.abort();loading=false;if(restoreFocus)trigger.focus();}
    }
    function setFilter(value) {
      if(busy||unreadOnly===value)return;
      unreadOnly=value;offset=0;items=[];fingerprint='';
      all.setAttribute('aria-pressed',String(!value));unread.setAttribute('aria-pressed',String(value));loadList();
    }
    trigger.addEventListener('click',()=>setOpen(!open));close.addEventListener('click',()=>setOpen(false));
    all.addEventListener('click',()=>setFilter(false));unread.addEventListener('click',()=>setFilter(true));
    markAll.addEventListener('click',()=>{if(loading||busy)return;const watermark=throughSequence;mutate(()=>request('/read-all','POST',{through_sequence:watermark}));});
    previous.addEventListener('click',()=>{if(loading||busy)return;offset=Math.max(0,offset-limit);loadList();});next.addEventListener('click',()=>{if(loading||busy)return;offset+=limit;loadList();});
    const outside=event=>{if(open&&!center.contains(event.target))setOpen(false,false);};
    const escape=event=>{if(open&&event.key==='Escape'){event.preventDefault();setOpen(false);}};
    const visibility=()=>{if(!document.hidden){if(open)loadList(true);else summary();}};
    center.addEventListener('focusout',event=>{if(open&&event.relatedTarget&&!center.contains(event.relatedTarget))setOpen(false,false);});
    document.addEventListener('pointerdown',outside);document.addEventListener('keydown',escape);document.addEventListener('visibilitychange',visibility);
    const timer=setInterval(()=>{if(document.hidden||busy||loading)return;if(open)loadList(true);else summary();},20000);
    summary();
    disposeNotifications=()=>{disposed=true;clearInterval(timer);listController?.abort();summaryController?.abort();document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',escape);document.removeEventListener('visibilitychange',visibility);};
    return center;
  }
  let selectedTheme='dark';
  try { selectedTheme=localStorage.getItem('suggestion-box-theme') || 'dark'; } catch {}
  document.documentElement.dataset.theme=selectedTheme;
  function mount(me, logout) {
    const host=document.getElementById('shell');
    if(!host) return;
    disposeNotifications();
    disposeNavigation();
    const current=location.pathname;
    const returnPath=['/login','/register','/setup'].includes(current)?new URLSearchParams(location.search).get('return'):current+location.search+location.hash;
    const authSuffix=returnPath?'?return='+encodeURIComponent(returnPath):'';
    const sidebar=node('aside','sidebar'); sidebar.id='sidebar';
    const brand=node('a','brand'); brand.href='https://supaknock.com/'; brand.setAttribute('aria-label','SupaKnock home');
    const mark=node('span','brandmark');const logo=document.createElement('img');logo.src='/assets/brand.svg';logo.width=28;logo.height=32;logo.alt='';mark.append(logo);
    brand.append(mark,node('span','','SupaKnock'));sidebar.append(brand);
    const closeNav=node('button','icon-button mobile-nav-close');closeNav.type='button';closeNav.setAttribute('aria-label','Close navigation');closeNav.append(icon('close'));sidebar.append(closeNav);
    const workspace=node('div','workspace-label');
    workspace.append(node('span','workspace-monogram','S'),node('div','','Your workspace'),node('span','workspace-tag','Self-hosted'));
    sidebar.append(workspace);
    const nav=node('nav','side-nav');nav.id='navigation';nav.setAttribute('aria-label','Main navigation');
    const add=(text,href,symbol,active) => {const a=node('a','nav-item'+(active?' active':''));a.href=href;a.append(icon(symbol),node('span','',text));if(active)a.setAttribute('aria-current','page');nav.append(a);};
    nav.append(node('p','nav-label','Workspace'));
    if(me?.role==='admin') add('Overview','/admin','grid',current==='/admin');
    if(me?.role==='admin') add('Projects','/workspace','box',current==='/workspace'||/^\/(projects|boards|requests)\//.test(current));
    if(me) add('My requests','/my-requests','inbox',current==='/my-requests');
    if(me?.role==='admin') {
      nav.append(node('p','nav-label','Manage'));
      add('Forms','/admin/forms','inbox',current.startsWith('/admin/forms')||current.startsWith('/admin/form-submissions'));
      add('Live chat','/admin/chat','chat',current==='/admin/chat');
      add('Push notifications','/admin/push','bell',current==='/admin/push');
      add('AI providers','/admin/ai/providers','key',current==='/admin/ai/providers');
      add('Connected AI tools','/admin/ai/tools','code',current==='/admin/ai/tools');
      add('AI runtime','/admin/ai','box',current==='/admin/ai');
      add('App activity','/admin/app-activity','clock',current==='/admin/app-activity');
      add('Account activity','/admin/account-activity','clock',current==='/admin/account-activity');
      add('Project settings','/admin#projects','code',current.startsWith('/admin/projects/'));
    }
    sidebar.append(nav);
    const bottom=node('div','sidebar-bottom');
    const theme=node('button','nav-item theme-toggle');theme.type='button';theme.append(icon('sun'),node('span','','Appearance'),node('span','theme-value',selectedTheme==='dark'?'Dark':'Light'));
    theme.setAttribute('aria-label','Toggle light or dark appearance');
    theme.addEventListener('click',()=>{selectedTheme=selectedTheme==='dark'?'light':'dark';document.documentElement.dataset.theme=selectedTheme;theme.querySelector('.theme-value').textContent=selectedTheme==='dark'?'Dark':'Light';try{localStorage.setItem('suggestion-box-theme',selectedTheme);}catch{}});bottom.append(theme);
    const account=node('a','account-link');account.href=me?'/account':'/login';
    const name=me?.display_name || (me ? 'Your account' : 'Welcome, visitor');
    account.append(node('span','avatar',name.slice(0,1).toUpperCase()),node('span','account-copy',name),icon('user'));bottom.append(account);sidebar.append(bottom);
    const topbar=node('header','topbar');
    const mobile=node('button','icon-button mobile-menu');mobile.type='button';mobile.append(icon('menu'));mobile.setAttribute('aria-label','Open navigation');mobile.setAttribute('aria-controls','sidebar');mobile.setAttribute('aria-expanded','false');
    const shade=node('button','sidebar-shade');shade.type='button';shade.setAttribute('aria-label','Close navigation');shade.hidden=true;
    const setOpen=(open,restoreFocus=true)=>{
      document.body.classList.toggle('nav-open',open);mobile.setAttribute('aria-expanded',String(open));shade.hidden=!open;
      if(open){sidebar.setAttribute('role','dialog');sidebar.setAttribute('aria-modal','true');sidebar.setAttribute('aria-label','Workspace navigation');}
      else{sidebar.removeAttribute('role');sidebar.removeAttribute('aria-modal');sidebar.removeAttribute('aria-label');}
      const page=document.querySelector('.workspace');if(page)page.inert=open;topbar.inert=open;
      if(open) (nav.querySelector('[aria-current]')||nav.querySelector('a')||closeNav).focus();
      else if(restoreFocus) mobile.focus();
    };
    mobile.addEventListener('click',()=>setOpen(true));shade.addEventListener('click',()=>setOpen(false));closeNav.addEventListener('click',()=>setOpen(false));
    const navigationKeys=e=>{
      if(!document.body.classList.contains('nav-open'))return;
      if(e.key==='Escape'){e.preventDefault();setOpen(false);}
      if(e.key==='Tab'){
        const targets=[...sidebar.querySelectorAll('a[href],button:not(:disabled)')].filter(n=>n.getClientRects().length);
        const first=targets[0],last=targets[targets.length-1];
        if(!sidebar.contains(document.activeElement)){e.preventDefault();first.focus();}
        else if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}
        else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
      }
    };
    document.addEventListener('keydown',navigationKeys);
    const navigationWidth=matchMedia('(min-width:801px)');
    const resizeNavigation=e=>{if(e.matches&&document.body.classList.contains('nav-open'))setOpen(false,false);};
    navigationWidth.addEventListener('change',resizeNavigation);
    disposeNavigation=()=>{setOpen(false,false);document.removeEventListener('keydown',navigationKeys);navigationWidth.removeEventListener('change',resizeNavigation);};
    const sectionName=current.startsWith('/admin/forms')||current.startsWith('/admin/form-submissions')?'Forms':current==='/admin/chat'?'Conversations':current==='/admin/ai/providers'?'AI providers':current==='/admin/ai'?'AI runtime':current==='/admin/push'?'Push notifications':current==='/admin/app-activity'?'App activity':current==='/admin/account-activity'?'Account activity':current.startsWith('/admin/projects/')?'Project settings':current==='/admin'?'Overview':current==='/api-docs'?'API documentation':current==='/account'?'Account':current==='/my-requests'?'My requests':current==='/login'?'Sign in':current==='/register'?'Create account':current==='/setup'?'Set up':'Feedback';
    const crumb=node('div','topbar-crumb');crumb.append(node('span','','Workspace'),node('span','slash','/'),node('strong','',sectionName));
    const right=node('div','topbar-actions');
    if(me?.role==='admin')right.append(notificationCenter());
    if(me){const role=me.role==='admin'?node('span','role-tag','Administrator'):node('a','quiet-button','Account');if(me.role!=='admin')role.href='/account';right.append(role);const out=node('button','quiet-button','Sign out');out.type='button';out.addEventListener('click',async()=>{out.disabled=true;try{await logout();}catch{out.disabled=false;out.textContent='Try signing out again';}});right.append(out);}
    else{const login=node('a','quiet-button','Sign in');login.href='/login'+authSuffix;const signup=node('a','button','Create account');signup.href='/register'+authSuffix;right.append(login,signup);}
    const administrative=me?.role==='admin';
    document.body.classList.toggle('participant-surface',!administrative);
    if(!administrative){
      crumb.replaceChildren(brand);
      if(me){const requests=node('a','quiet-button','My requests');requests.href='/my-requests';right.prepend(requests);}
    }
    topbar.append(...(administrative?[mobile]:[]),crumb,right);
    host.replaceChildren(...(administrative?[sidebar,shade]:[]),topbar);
  }
  window.SuggestionShell={mount,icon,skeleton};
})();
