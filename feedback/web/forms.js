"use strict";
const SupaForms = (() => {
  const base = '/admin/forms';
  const states = [['','All submissions'],['new','New'],['reviewed','Reviewed'],['archived','Archived']];
  const template = [
    {key:'email',label:'Email',type:'email',required:true,choices:[]},
    {key:'name',label:'Name',type:'text',required:false,choices:[]},
    {key:'message',label:'What would you like to try?',type:'textarea',required:false,choices:[]}
  ];
  const stamp = value => new Date(value).toLocaleString();
  async function list() {
    if(!requireAdmin()) return;
    const project = new URLSearchParams(location.search).get('project_id');
    const entries=el('div');
    root.replaceChildren(hero('Private inbox','Forms','Collect early access requests and signups. Read and review them here.'),
      el('div',{class:'actions'},link('Create form',base+'/new'+(project?'?project_id='+encodeURIComponent(project):''),'button')),
      panel('Your forms',entries));
    await paged(entries,base+(project?'?project_id='+encodeURIComponent(project):''),items=>items.map(f=>el('article',{class:'row form-entry'},
      el('div',{},link(f.name,base+'/'+f.id,'title'),el('p',{class:'hint'},f.project_name)),badge(f.project_archived_at?'Project archived':f.enabled?'Open':'Closed'),link('View submissions →',base+'/'+f.id))),0,'No forms yet. Create one, then share its public page or connect your backend.');
  }
  async function editor(id) {
    if(!requireAdmin()) return;
    const current=id?await api(base+'/'+id):null;
    const projectSelect=select('Project','project',[],new URLSearchParams(location.search).get('project_id'));
    if(!current){
      const target=projectSelect.querySelector('select'); let offset=0,page;
      do {page=await api('/projects?limit=100&offset='+offset); for(const p of page.items) target.append(el('option',{value:p.id,selected:p.id===new URLSearchParams(location.search).get('project_id')},p.name));offset+=page.items.length;} while(page.has_more);
    }
    const fields=el('div',{class:'form-fields'}); let serial=0;
    function add(value={key:'',label:'',type:'text',required:false,choices:[]}) {
      if(fields.children.length>=20) return notify('A form can have up to 20 questions.','error');
      const n=++serial;
      const type=select('Answer type','type-'+n,[['text','Short text'],['textarea','Long text'],['email','Email'],['choice','Choose one']],value.type);
      const choices=field('Choices (one per line)','choices-'+n,'textarea',value.choices.join('\n'),{maxlength:3030});
      const row=el('fieldset',{class:'form-question'},el('legend',{},'Question'),
        el('div',{class:'form-question-grid'},field('Label','label-'+n,'text',value.label,{required:true,maxlength:100}),field('Field key','key-'+n,'text',value.key,{required:true,pattern:'[a-z][a-z0-9_]{0,39}',maxlength:40})),
        type,choices,el('div',{class:'actions'},checkbox('Required','required-'+n,value.required),button('Remove question',()=>row.remove(),'secondary')));
      row.dataset.serial=n; choices.hidden=value.type!=='choice'; type.addEventListener('change',()=>choices.hidden=type.querySelector('select').value!=='choice'); fields.append(row);
    }
    (current?.fields||template).forEach(add);
    const f=form([!current?projectSelect:null,field('Form name','name','text',current?.name||'Early access',{required:true,maxlength:100}),
      field('Description','description','textarea',current?.description||'',{maxlength:2000}),
      checkbox('Accept submissions','enabled',current?.enabled||false),
      checkbox('Enable public form page','public_enabled',current?.public_enabled||false),
      el('p',{class:'hint'},'Anyone with the public link can read these questions and submit without signing in. Answers stay private. Forms are not listed in a public directory.'),
      checkbox('Enable website embed','embed_enabled',current?.embed_enabled||false),
      field('Allowed embed websites (one origin per line)','embed_origins','textarea',(current?.embed_origins||[]).join('\n'),{maxlength:5000,placeholder:'https://example.com'}),
      el('p',{class:'hint'},'Use exact website origins, with no path or trailing slash. Embeds work independently of the public page. Keep colors and other appearance options in your website’s snippet.'),
      el('p',{class:'hint'},'Saving updates the form immediately. Existing answers keep their field keys and values. If you change the questions, update your website’s backend too.'),fields,
      button('Add question',()=>add()),el('p',{class:'hint'},'Use a stable field key for each question, such as email or message. Share the optional public page, or send submissions through your own backend.')],current?'Save changes':'Create form',async data=>{
        const body={name:data.get('name'),description:data.get('description'),enabled:data.get('enabled')==='true',public_enabled:data.get('public_enabled')==='true',embed_enabled:data.get('embed_enabled')==='true',embed_origins:String(data.get('embed_origins')).split('\n').map(x=>x.trim()).filter(Boolean),fields:[...fields.children].map(row=>{const n=row.dataset.serial,type=data.get('type-'+n);return {key:data.get('key-'+n),label:data.get('label-'+n),type,required:data.get('required-'+n)==='true',choices:type==='choice'?String(data.get('choices-'+n)).split('\n').map(x=>x.trim()).filter(Boolean):[]};})};
        if(!body.fields.length) throw new Error('Add at least one question.');
        if(current) body.version=current.version;
        const saved=await api(current?base+'/'+id:'/admin/projects/'+data.get('project')+'/forms',current?'PUT':'POST',body);
        location.href=base+'/'+saved.id;
      });
    root.replaceChildren(link('← '+(current?'Submissions':'Forms'),current?base+'/'+id:base,'crumb'),hero(current?.project_name||'New form',current?'Edit '+current.name:'Create a form','Only administrators can see the answers.'),panel('Form details',f));
    if(current?.project_archived_at){f.querySelectorAll('input,select,textarea,button').forEach(x=>x.disabled=true);f.prepend(el('p',{class:'hint'},'Restore this project before editing its forms.'));}
  }
  async function inbox(id) {
    if(!requireAdmin()) return;
    const f=await api(base+'/'+id), entries=el('div'), filter=select('Show','state',states,'');
    const reload=()=>paged(entries,base+'/'+id+'/submissions'+(filter.querySelector('select').value?'?state='+filter.querySelector('select').value:''),items=>items.map(s=>el('article',{class:'row form-entry'},el('div',{},link(s.answers.email||'Submission','/admin/form-submissions/'+s.id,'title'),el('p',{class:'hint'},stamp(s.created_at))),badge(s.state.charAt(0).toUpperCase()+s.state.slice(1)))),0,'No submissions here yet.');
    filter.addEventListener('change',()=>reload().catch(e=>notify(e.message,'error')));
    const example={client_request_id:'GENERATE_A_UUID_ON_YOUR_BACKEND',answers:Object.fromEntries(f.fields.map(x=>[x.key,x.type==='email'?'person@example.com':x.type==='choice'?x.choices[0]:'Your answer']))};
    const integration=el('details',{class:'panel'},el('summary',{},'Connect your website'),el('p',{},'Send a POST from your backend with a project token that has forms:submit permission. Keep the token on your server. Reuse the same request ID and answers when retrying.'),
      el('pre',{},el('code',{},'POST '+location.origin+'/api/v1/forms/'+f.id+'/submissions\nAuthorization: Bearer YOUR_PROJECT_TOKEN\nContent-Type: application/json\n\n'+JSON.stringify(example,null,2))),
      link('Manage this project’s tokens','/admin/projects/'+f.project_id),el('p',{class:'hint'},'Your backend handles spam protection and your website’s visitor permissions. Email addresses are unverified. SupaKnock does not send follow-up emails.'));
    const publicLink=f.public_enabled?panel('Public form page',el('p',{class:'hint'},f.enabled&&!f.project_archived_at?'Anyone with this link can fill out the form. Answers are only visible to administrators.':'The link is unavailable while submissions are closed or the project is archived.'),link(location.origin+'/forms/'+f.id,'/forms/'+f.id),button('Copy link',async()=>{await navigator.clipboard.writeText(location.origin+'/forms/'+f.id);notify('Link copied.');},'secondary')):null;
    const target='supaknock-form-'+f.id;
    const snippet='<div id="'+target+'"></div>\n<script type="application/json" id="'+target+'-style">\n'+JSON.stringify({submitText:'Send request',theme:{mode:'auto',radius:14,light:{accent:'#147d50',onAccent:'#ffffff'},dark:{accent:'#3ecf8e',onAccent:'#092c1c'}}},null,2)+'\n</script>\n<script defer src="'+location.origin+'/form-widget.js"\n  data-server="'+location.origin+'"\n  data-form="'+f.id+'"\n  data-target="#'+target+'"\n  data-config="'+target+'-style"></script>';
    const embed=el('details',{class:'panel'},el('summary',{},'Embed on your website'),
      el('p',{},f.embed_enabled?'Paste this div, appearance JSON, and script where you want the form. No API key or backend is needed.':'Enable website embed and add allowed websites in Edit form, then paste this snippet into your site.'),
      el('p',{class:'hint'},f.embed_enabled&&f.enabled&&!f.project_archived_at&&f.embed_origins.length?'Allowed websites: '+f.embed_origins.join(', '):'The embed will stay unavailable until submissions and embedding are enabled, the project is active, and your website origin is allowed.'),
      el('pre',{},el('code',{},snippet)),button('Copy embed code',async()=>{await navigator.clipboard.writeText(snippet);notify('Embed code copied.');},'secondary'),
      el('p',{class:'hint'},'Match chat’s theme.mode and light/dark color palettes. Colors, corner radius, button copy, and density belong in your site’s code. Answers stay in this private inbox. Your own website’s scripts and analytics can access fields on your page; exclude form answers from recording.'));
    root.replaceChildren(link('← Forms',base,'crumb'),hero(f.project_name,f.name,f.description||'A private inbox for your form submissions.'),el('div',{class:'actions'},badge(f.project_archived_at?'Project archived':f.enabled?'Open':'Closed'),link('Edit form',base+'/'+id+'?edit=1','button secondary')),...(publicLink?[publicLink]:[]),embed,integration,panel('Submissions',filter,entries));await reload();
  }
  async function detail(id) {
    if(!requireAdmin()) return;
    const s=await api('/admin/form-submissions/'+id), answers=el('dl',{class:'form-answers'});
    const keys=[...s.fields.map(f=>f.key).filter(k=>k in s.answers),...Object.keys(s.answers).filter(k=>!s.fields.some(f=>f.key===k)).sort()];
    for(const key of keys){const value=s.answers[key];const f=s.fields.find(x=>x.key===key);answers.append(el('dt',{},f?.label||key),el('dd',{},value||'—'));}
    const state=select('Review status','state',states.slice(1),s.state);
    const review=form([state],'Save status',async d=>{await api('/admin/form-submissions/'+id+'/state','PUT',{version:s.version,state:d.get('state')});await detail(id);notify('Status saved.');});
    root.replaceChildren(link('← '+s.form_name,base+'/'+s.form_id,'crumb'),hero(s.project_name,'Submission',stamp(s.created_at)),panel('Answers',answers),panel('Review',review),
      el('details',{class:'panel'},el('summary',{},'Delete submission'),el('p',{},'This removes the answers permanently. The receipt is kept so a retry cannot restore them.'),button('Delete answers',async()=>{if(!confirm('Permanently delete these answers?'))return;await api('/admin/form-submissions/'+id,'DELETE',{version:s.version});location.href=base+'/'+s.form_id;},'danger')));
  }
  return {list,editor,inbox,detail};
})();
