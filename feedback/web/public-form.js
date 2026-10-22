(() => {
  'use strict';
  const content=document.getElementById('content');
  const id=location.pathname.split('/')[2];
  const endpoint='/api/v1/public/forms/'+encodeURIComponent(id);
  function el(tag,text,attrs={}) {const e=document.createElement(tag); if(text)e.textContent=text; for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);return e;}
  async function api(path,body) {
    const r=await fetch(path,{method:body?'POST':'GET',credentials:'omit',cache:'no-store',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(15000)});
    const data=await r.json(); if(!r.ok){const e=new Error(data.error?.message||'Please check your answers and try again.');e.status=r.status;throw e;}return data;
  }
  async function load(){
    content.setAttribute('aria-busy','true');
    try {
      const f=await api(endpoint);document.title=f.name+' · '+f.project_name;
      const form=el('form',null,{class:'card',autocomplete:'on'});
      for(const field of f.fields){
        const row=el('div');const label=el('label',field.label,{for:'answer-'+field.key});
        if(!field.required)label.append(el('span',' (optional)'));
        let input;
        if(field.type==='choice') {input=el('select');input.append(el('option','Choose an option',{value:''}));for(const choice of field.choices)input.append(el('option',choice,{value:choice}));}
        else if(field.type==='textarea')input=el('textarea',null,{maxlength:'4000',rows:'4'});
        else input=el('input',null,{type:field.type==='email'?'email':'text',maxlength:field.type==='email'?'254':'500',autocomplete:field.type==='email'?'email':field.key==='name'?'name':'off'});
        input.name=field.key;input.id='answer-'+field.key;input.required=field.required;row.append(label,input);form.append(row);
      }
      const trap=el('div',null,{class:'trap','aria-hidden':'true'});const honey=el('input',null,{tabindex:'-1',autocomplete:'off',id:'company-site'});trap.append(el('label','Company website',{for:'company-site'}),honey);
      const privacy=el('p','Your answers are private and visible only to this project’s administrators. Submitting doesn’t create an account. Contact details are not verified.',{class:'privacy'});
      const submit=el('button','Send request',{type:'submit'});const status=el('p',null,{role:'status','aria-live':'polite',class:'error'});
      form.append(trap,privacy,submit,status);
      content.replaceChildren(el('p',f.project_name,{class:'eyebrow'}),el('h1',f.name),el('p',f.description,{class:'description'}),form);
      let pending=null;
      form.addEventListener('submit',async event=>{
        event.preventDefault();if(submit.disabled||!form.reportValidity())return;
        const answers=Object.fromEntries(new FormData(form));const signature=JSON.stringify(answers);
        if(!pending||pending.signature!==signature)pending={id:crypto.randomUUID(),signature};
        submit.disabled=true;submit.textContent='Sending…';status.textContent='';form.setAttribute('aria-busy','true');
        try{
          await api(endpoint+'/submissions',{answers,client_request_id:pending.id,ticket:f.ticket,website:honey.value});
          const done=el('section',null,{class:'card',tabindex:'-1','aria-label':'Request received'});
          done.append(el('div','✓',{class:'success-mark','aria-hidden':'true'}),el('h2','Request received.'),el('p','Thanks for taking the time to fill this out. Your answers have been sent to '+f.project_name+'.'));
          form.reset();form.replaceWith(done);done.focus();pending=null;
        }catch(e){
          if(e.status===404){status.textContent='This form is no longer accepting submissions.';submit.textContent='Form closed';}
          else if(e.status===409||e.status===403){status.textContent=e.message+' Your answers are still here if you need to copy them.';submit.textContent='Reload form';submit.disabled=false;submit.type='button';submit.addEventListener('click',()=>load(),{once:true});}
          else {status.textContent=e.name==='TimeoutError'||e.name==='TypeError'?'We couldn’t confirm your request. Check your connection and try again; the same request won’t be saved twice.':e.message;submit.disabled=false;submit.textContent='Try again';}
        }finally{form.removeAttribute('aria-busy');}
      });
    }catch(e){
      const card=el('section',null,{class:'card'});card.append(el('h1',e.status===404?'This form isn’t available.':'Couldn’t open this form.'),el('p',e.status===404?'It may be closed or its public link may have been turned off.':'Check your connection and try again.',{class:'description'}));
      if(e.status!==404){const retry=el('button','Try again',{type:'button'});retry.addEventListener('click',load);card.append(retry);}content.replaceChildren(card);
    }finally{content.removeAttribute('aria-busy');}
  }
  load();
})();
