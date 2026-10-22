/* Shared account schedule and explicit project overrides. */
const BusinessHours = (() => {
  const names = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  function editor(project = null) {
    const host = el('section', {class:'business-hours', id:project?'project-business-hours':'business-hours'});
    const endpoint = project ? '/admin/chat/projects/'+encodeURIComponent(project)+'/business-hours' : '/admin/chat/business-hours';
    async function load(restoreFocus=false) {
      host.setAttribute('aria-busy','true');
      host.replaceChildren(el('p',{class:'loading',role:'status'},'Loading business hours…'));
      try {
        const [settings,account]=await Promise.all([api(endpoint),project?api('/admin/chat/business-hours'):Promise.resolve(null)]);
        const inheritedSchedule=account?.effective||settings.effective;
        const heading=el('h3',{},project?'Human support hours':'Account business hours');
        const intro=el('p',{class:'hint'},project?'Choose when this project expects to answer human support. Messages can always be sent.':'Shared by all projects unless a project overrides them. These hours describe your schedule, not whether someone is online.');
        const mode=el('select',{'aria-label':'Business hours source'},...[
          ['inherit','Use account hours'],['override','Use project hours'],['off','Do not show hours for this project']
        ].map(([value,label])=>el('option',{value,selected:settings.mode===value},label)));
        const working=structuredClone(settings.schedule);
        let dirty=false;
        const notice=el('p',{class:'hint',role:'status'});
        function markEdited(){if(!dirty){dirty=true;notice.textContent='Unsaved changes';}}
        if(!working.enabled && settings.version===0)working.timezone=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';
        const enabled=checkbox('Show business hours in chat','enabled',working.enabled);
        const email=checkbox('Offer an optional email for manual follow-up','collect_email',working.collect_email);
        const zoneId='support-timezones-'+crypto.randomUUID();
        const zone=field('Schedule timezone','timezone','text',working.timezone,{required:true,list:zoneId,maxlength:100});
        const zones=el('datalist',{id:zoneId},...['UTC',...(Intl.supportedValuesOf?.('timeZone')||[])].map(value=>el('option',{value})));
        const days=el('div',{class:'hours-days'});
        names.forEach((name,index)=>{
          const slots=working.days[index];
          const rows=el('div',{class:'hours-ranges'});
          const add=button('Add hours',()=>{if(slots.length<3){slots.push({start:'09:00',end:'17:00'});markEdited();draw(slots.length-1);}});
          add.setAttribute('aria-label','Add hours for '+name);
          function draw(focusIndex=null){
            rows.replaceChildren();
            if(!slots.length)rows.append(el('span',{class:'hint'},'Closed'));
            slots.forEach((slot,i)=>{
              const start=el('input',{type:'time',value:slot.start,required:true,'aria-label':name+' start '+(i+1)});
              // Text permits 24:00 for midnight at the end of a day.
              const end=el('input',{type:'text',value:slot.end,required:true,placeholder:'17:00',pattern:'([01][0-9]|2[0-3]):[0-5][0-9]|24:00','aria-label':name+' end '+(i+1)});
              start.oninput=()=>slot.start=start.value;end.oninput=()=>slot.end=end.value;
              const remove=button('Remove',()=>{slots.splice(i,1);markEdited();draw(Math.min(i,slots.length-1));});remove.setAttribute('aria-label','Remove '+name+' range '+(i+1));
              rows.append(el('div',{class:'hours-range'},start,el('span',{},'to'),end,remove));
            });add.disabled=slots.length>=3;
            if(focusIndex!==null)(rows.querySelectorAll('input')[focusIndex*2]||add).focus({preventScroll:true});
          }
          draw();days.append(el('div',{class:'hours-day'},el('strong',{},name),rows,add));
        });
        const inherited=el('div',{class:'hours-inherited'},el('p',{},inheritedSchedule.enabled?'Account schedule · '+inheritedSchedule.timezone:'Account business hours are not configured.'),
          ...(inheritedSchedule.enabled?names.map((name,i)=>el('p',{class:'hint'},name+' · '+(inheritedSchedule.days[i].map(w=>w.start+'–'+w.end).join(', ')||'Closed'))):[]),
          link('Manage account business hours →','/account#business-hours'));
        const controls=el('fieldset',{class:'hours-controls'},enabled,zone,zones,days,email,
          el('p',{class:'hint'},'Use 24-hour times; 24:00 means the end of the day. Split overnight hours across two days. Daylight-saving changes follow your timezone.'),
          el('p',{class:'hint'},'Email is optional, unverified and shown privately to your team. Reply manually using your email app; SupaKnock does not send email.'));
        const save=form([], 'Save business hours', async()=>{
          const result=await api(endpoint,'PUT',{version:settings.version,mode:project?mode.value:'override',schedule:{...working,enabled:enabled.querySelector('input').checked,collect_email:email.querySelector('input').checked,timezone:zone.querySelector('input').value}});
          settings.version=result.version;dirty=false;notice.textContent='All changes saved';notify('Business hours saved');
        });
        const refresh=button('Reload saved hours',()=>{
          if(!dirty)return load(true);
          if(host.querySelector('.hours-confirm'))return;
          const confirmation=el('section',{class:'panel hours-confirm',role:'alertdialog','aria-label':'Discard unsaved business hours?',tabindex:'-1'},
            el('h3',{},'Discard unsaved hours?'),el('p',{class:'hint'},'Reloading restores the saved schedule. Your unsaved changes will be lost.'));
          const cancel=()=>{confirmation.remove();refresh.focus({preventScroll:true});};
          confirmation.append(el('div',{class:'actions'},button('Keep editing',cancel),button('Discard and reload',()=>load(true),'danger')));
          confirmation.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();cancel();}});
          host.append(confirmation);confirmation.focus({preventScroll:true});confirmation.scrollIntoView({block:'nearest'});
        });refresh.classList.add('secondary');
        save.prepend(...(project?[el('label',{},'Schedule source',mode),inherited]:[]),controls);
        save.addEventListener('input',markEdited);save.addEventListener('change',markEdited);
        function selection(){const own=!project||mode.value==='override';controls.hidden=!own;controls.disabled=!own;inherited.hidden=!project||mode.value!=='inherit';}
        mode.onchange=selection;selection();
        host.replaceChildren(heading,intro,save,notice,refresh);
        if(restoreFocus)refresh.focus({preventScroll:true});
      }catch(error){const message=el('p',{class:'error',role:'alert',tabindex:'-1'},error.message);host.replaceChildren(message,button('Retry loading hours',()=>load(true)));if(restoreFocus)message.focus({preventScroll:true});}
      finally{host.setAttribute('aria-busy','false');}
    }
    load();return host;
  }
  return {editor};
})();
