/* SupaKnock Forms. Inline, credential-free intake. Appearance stays on your website. */
(() => {
  'use strict';
  const script = document.currentScript;
  const assetServer = script?.src ? new URL(script.src).origin : null;
  function autoMount(api) {
    if (!script?.dataset.form || !script.dataset.server) return;
    let options = {};
    const config = script.dataset.config && document.getElementById(script.dataset.config);
    if (config?.tagName === 'SCRIPT' && config.type.trim().toLowerCase() === 'application/json') {
      try {
        const value = JSON.parse(config.textContent);
        if (value && typeof value === 'object' && !Array.isArray(value)) {
          for (const key of ['theme','density','showTitle','submitText','successTitle','successMessage']) options[key] = value[key];
        }
      } catch { /* Invalid appearance uses the defaults; routing stays in data attributes. */ }
    }
    api.mount(script.dataset.target || '#supaknock-form', {...options,server:script.dataset.server,form:script.dataset.form});
  }
  if (window.SupaKnockForms) {autoMount(window.SupaKnockForms);return;}
  const controllers = new WeakMap();
  const palettes = {
    light:{accent:'#147d50',onAccent:'#ffffff',background:'#ffffff',surface:'#f5f7f6',text:'#202823',muted:'#606e65',border:'#dbe3de'},
    dark:{accent:'#3ecf8e',onAccent:'#092c1c',background:'#161616',surface:'#202220',text:'#ededed',muted:'#a0aaa4',border:'#353c37'}
  };
  const modes = ['auto','light','dark'];
  const targetElement = target => typeof target === 'string' ? document.querySelector(target) : target;
  const label = (value, limit, fallback) => typeof value === 'string' && value.trim() ? [...value.trim()].slice(0,limit).join('') : fallback;
  function el(tag,text,attrs={}) {
    const node=document.createElement(tag);
    if (text) node.textContent=text;
    for (const [key,value] of Object.entries(attrs)) node.setAttribute(key,value);
    return node;
  }
  function mount(target, options={}) {
    const host=targetElement(target);
    if (!host) return;
    if (controllers.has(host)) return controllers.get(host);
    let server;
    try {
      const url=new URL(options.server || assetServer);
      if (!['https:','http:'].includes(url.protocol) || url.username || url.password) return;
      server=url.origin;
    } catch {return;}
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(options.form || '')) return;
    if (host.shadowRoot && !host.hasAttribute('data-supaknock-form')) return;
    host.setAttribute('data-supaknock-form','');
    const shadow=host.shadowRoot || host.attachShadow({mode:'open'});
    const css=el('link',null,{rel:'stylesheet',href:server+'/form-widget.css'});
    const content=el('section',null,{'aria-label':'Form',class:'content'});
    shadow.replaceChildren(css,content);
    const appearance=options.theme && typeof options.theme==='object' ? options.theme : {};
    const colors=Object.fromEntries(Object.entries(palettes).map(([name,defaults])=>[name,
      Object.fromEntries(Object.entries(defaults).map(([key,fallback])=>{
        const value=appearance[name]?.[key];
        return [key,typeof value==='string' && /^#[a-f\d]{3}(?:[a-f\d]{3})?$/i.test(value) ? value : fallback];
      }))
    ]));
    const system=matchMedia('(prefers-color-scheme: dark)');
    let themeMode=modes.includes(appearance.mode)?appearance.mode:'auto';
    const radius=typeof appearance.radius==='number' && Number.isFinite(appearance.radius) ? Math.max(0,Math.min(32,appearance.radius)) : 14;
    content.style.setProperty('--sf-radius',radius+'px');
    content.dataset.density=options.density==='compact'?'compact':'comfortable';
    function applyTheme() {
      const resolved=themeMode==='auto'?(system.matches?'dark':'light'):themeMode;
      for (const [key,value] of Object.entries(colors[resolved])) content.style.setProperty('--sf-'+key,value);
      content.style.colorScheme=resolved;
    }
    function themeChanged() {if(themeMode==='auto')applyTheme();}
    system.addEventListener('change',themeChanged);applyTheme();
    const submitText=label(options.submitText,80,'Send request');
    const endpoint=server+'/api/v1/embed/forms/'+encodeURIComponent(options.form);
    let epoch=0, stopped=false, active=null, pending=null;
    async function api(path,body) {
      active=new AbortController();
      const controller=active;
      const timeout=setTimeout(()=>controller.abort(),15000);
      try {
        const r=await fetch(path,{method:body?'POST':'GET',mode:'cors',credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',
          headers:body?{'Content-Type':'application/json'}:{Accept:'application/json'},body:body?JSON.stringify(body):undefined,signal:controller.signal});
        let data;try{data=await r.json();}catch{throw new Error('Could not read the server response. Try again.');}
        if(!r.ok){const error=new Error(data.error?.message || 'Please check your answers and try again.');error.status=r.status;throw error;}
        return data;
      } finally {clearTimeout(timeout);if(active===controller)active=null;}
    }
    async function load(saved={}, focus=false) {
      const generation=++epoch;
      active?.abort();pending=null;
      content.setAttribute('aria-busy','true');
      content.replaceChildren(el('p','Opening form…',{role:'status',class:'muted'}),el('div',null,{class:'skeleton','aria-hidden':'true'}),el('div',null,{class:'skeleton short','aria-hidden':'true'}));
      try {
        const definition=await api(endpoint);
        if(stopped || generation!==epoch)return;
        const form=el('form',null,{autocomplete:'on','data-private':'true',class:'fields'});
        const heading=el('header');
        if(options.showTitle!==false) heading.append(el('p',definition.project_name,{class:'eyebrow'}),el('h2',definition.name),el('p',definition.description,{class:'description'}));
        content.setAttribute('aria-label',definition.name);
        for (const field of definition.fields) {
          const row=el('div',null,{class:'field'});
          const title=el('label',field.label,{for:'answer-'+field.key});
          if(!field.required)title.append(el('span',' (optional)',{class:'muted'}));
          let input;
          if(field.type==='choice') {
            input=el('select');input.append(el('option','Choose an option',{value:''}));
            for(const choice of field.choices) input.append(el('option',choice,{value:choice}));
          } else if(field.type==='textarea') input=el('textarea',null,{rows:'4',maxlength:'4000'});
          else input=el('input',null,{type:field.type==='email'?'email':'text',maxlength:field.type==='email'?'254':'500',autocomplete:field.type==='email'?'email':field.key==='name'?'name':'off'});
          input.name=field.key;input.id='answer-'+field.key;input.required=field.required;
          if(typeof saved[field.key]==='string' && (field.type!=='choice'||field.choices.includes(saved[field.key])))input.value=saved[field.key];
          row.append(title,input);form.append(row);
        }
        const trap=el('div',null,{class:'trap','aria-hidden':'true'});
        const honey=el('input',null,{tabindex:'-1',autocomplete:'off',id:'company-site'});
        trap.append(el('label','Company website',{for:'company-site'}),honey);
        const privacy=el('p','Your answers are sent to '+definition.project_name+' and can only be read by this project’s administrators. No account is created.',{class:'privacy'});
        const submit=el('button',submitText,{type:'submit'});
        const status=el('p',null,{role:'status','aria-live':'polite',class:'status'});
        const reload=el('button','Reload form',{type:'button',class:'secondary'});reload.hidden=true;
        reload.addEventListener('click',()=>load(Object.fromEntries(new FormData(form)),true));
        form.append(trap,privacy,submit,status,reload);
        content.replaceChildren(heading,form,el('a','Powered by SupaKnock',{href:'https://supaknock.com/',target:'_blank',rel:'noopener noreferrer',class:'brand'}));
        if(focus)form.querySelector('input,select,textarea')?.focus();
        form.addEventListener('submit',async event=>{
          event.preventDefault();
          if(stopped||submit.disabled||!form.reportValidity())return;
          const answers=Object.fromEntries(new FormData(form));
          const signature=JSON.stringify(answers);
          if(!pending || pending.signature!==signature)pending={id:crypto.randomUUID(),signature};
          submit.disabled=true;submit.textContent='Sending…';submit.setAttribute('aria-busy','true');status.textContent='';
          const inputs=[...form.querySelectorAll('input,select,textarea')];inputs.forEach(input=>input.disabled=true);
          try {
            const result=await api(endpoint+'/submissions',{answers,ticket:definition.ticket,client_request_id:pending.id,website:honey.value});
            if(stopped||generation!==epoch)return;
            if(result.ok!==true)throw new Error('We couldn’t confirm your request. Try again.');
            form.reset();pending=null;
            const done=el('section',null,{class:'success',tabindex:'-1','aria-label':'Request received'});
            done.append(el('div','✓',{class:'success-mark','aria-hidden':'true'}),el('h2',label(options.successTitle,120,'Request received.')),
              el('p',label(options.successMessage,500,'Thanks for taking the time to fill this out. Your answers have been sent to '+definition.project_name+'.')));
            content.replaceChildren(done);done.focus();
          } catch(error) {
            if(stopped||generation!==epoch)return;
            inputs.forEach(input=>input.disabled=false);
            if(error.status===404){status.textContent='This form is no longer accepting submissions.';submit.textContent='Form closed';}
            else if(error.status===403 || error.status===409) {
              status.textContent=error.message+' Matching answers will be kept when you reload.';submit.textContent=submitText;reload.hidden=false;
            } else {
              status.textContent=['AbortError','TypeError'].includes(error.name)?'We couldn’t confirm your request. Check your connection and try again. Your unchanged answers won’t be saved twice.':error.message;
              submit.disabled=false;submit.textContent='Try again';
            }
          } finally {if(!stopped&&generation===epoch)submit.removeAttribute('aria-busy');}
        });
      } catch(error) {
        if(stopped||generation!==epoch)return;
        content.replaceChildren(el('h2','Couldn’t open this form.'),el('p',error.status===404?'This form is closed or its embed is turned off.':'Check your connection. The site owner may also need to enable this website in the form’s embed settings.',{class:'description'}));
        const retry=el('button','Try again',{type:'button'});retry.addEventListener('click',()=>load(saved,true));content.append(retry);
      } finally {if(!stopped&&generation===epoch)content.removeAttribute('aria-busy');}
    }
    const controller={
      setTheme(mode){if(!stopped&&modes.includes(mode)){themeMode=mode;applyTheme();}},
      destroy(){stopped=true;epoch++;active?.abort();pending=null;system.removeEventListener('change',themeChanged);shadow.replaceChildren();controllers.delete(host);}
    };
    controllers.set(host,controller);load();return controller;
  }
  window.SupaKnockForms={mount,get:target=>controllers.get(targetElement(target))};
  autoMount(window.SupaKnockForms);
})();
