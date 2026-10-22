import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const context=vm.createContext({window:{},document:{currentScript:null}});
vm.runInContext(readFileSync(new URL('../feedback/web/widget.js',import.meta.url),'utf8'),context);
const signature=context.suggestionBoxReviewSignature;
test('AI progress is explicit, current-turn only, and contains no draft text',()=>{
  const progress=context.suggestionBoxAIProgress;
  const current={state:'open',support_mode:'ai',ai_status:'running',ai_trigger_sequence:3,last_sequence:3};
  for(const [stage,label] of Object.entries({thinking:'Thinking…',reading:'Reading sources…',writing:'Putting the answer together…',checking:'Checking the answer…',refining:'Refining the answer…'}))
    assert.equal(progress({...current,ai_progress:stage},3),label);
  assert.equal(progress({...current,ai_status:'queued'}),'Getting ready…');
  assert.equal(progress({...current,ai_progress:'private draft'}),'Thinking…');
  assert.equal(progress(current,4),'');
  for(const change of [{state:'closed'},{support_mode:'human'},{support_mode:'waiting_human'},
    {ai_status:'succeeded'},{ai_status:'failed'},{ai_status:'cancelled'},{ai_trigger_sequence:null},{last_sequence:4}])
    assert.equal(progress({...current,...change}), '');
});
function composerFixture() {
  const listeners = new Map(), submissions = [];
  const input = {value:'Hello there', disabled:false, addEventListener:(name, fn)=>listeners.set(name, fn)};
  const send = {disabled:false};
  context.suggestionBoxComposerKeyboard(input, {requestSubmit:button=>submissions.push(button)}, send);
  return {input, send, submissions, fire(name, overrides={}) {
    const event = {key:'Enter', preventDefault(){this.defaultPrevented=true;}, ...overrides};
    listeners.get(name)(event);
    return event;
  }};
}
test('Enter requests normal form submission with the Send button; Shift+Enter keeps a newline',()=>{
  const f=composerFixture();
  assert.equal(f.fire('keydown').defaultPrevented,true);
  assert.deepEqual(f.submissions,[f.send]);
  assert.equal(f.fire('keydown',{shiftKey:true}).defaultPrevented,undefined);
  assert.equal(f.submissions.length,1);
});
test('composition confirmation and modified keys never submit chat',()=>{
  const f=composerFixture();
  for(const event of [{isComposing:true},{keyCode:229},{ctrlKey:true},{altKey:true},{metaKey:true},{key:'a'},{defaultPrevented:true}]) {
    f.fire('keydown',event);
  }
  f.fire('compositionstart');
  assert.equal(f.fire('keydown').defaultPrevented,undefined);
  f.fire('compositionend');
  assert.equal(f.submissions.length,0);
  f.fire('keydown');
  assert.equal(f.submissions.length,1);
});
test('empty, disabled and held Enter cannot send or add stray newlines',()=>{
  const f=composerFixture();
  for(const value of ['', '  \n ']) {
    f.input.value=value;
    assert.equal(f.fire('keydown').defaultPrevented,true);
  }
  f.input.value='Hello';
  f.send.disabled=true;f.fire('keydown');
  f.send.disabled=false;f.input.disabled=true;f.fire('keydown');
  f.input.disabled=false;f.fire('keydown',{repeat:true});
  assert.equal(f.submissions.length,0);
});
const draft={id:'draft-1',version:1,draft_hash:'a'.repeat(64),title:'Keep my exact title',description:'Private requested content',state:'pending',can_confirm:true,can_edit:true,feedback_url:'https://customer.test/ideas',tracking_notice:'Guest tracking notice',board:{id:'board-1',name:'Ideas'},expires_at:'2026-10-03T12:00:00Z'};

test('draft review binds every displayed content, destination, notice and permission change',()=>{
  const original=signature(draft,'draft');
  const changes=[{id:'draft-2'},{version:2},{draft_hash:'b'.repeat(64)},{title:'Changed'},{description:'Changed'},{state:'confirmed'},{can_confirm:false},{can_edit:false},{feedback_url:'https://customer.test/bugs'},{tracking_notice:'New notice'},{board:{id:'other',name:'Ideas'}},{board:{id:'board-1',name:'Renamed'}},{expires_at:'2026-10-03T11:59:00Z'}];
  for(const change of changes)assert.notEqual(signature({...draft,...change},'draft'),original,JSON.stringify(change));
  assert.equal(signature(structuredClone(draft),'draft'),original);
});

test('receipt updates observe permanent acceptance locks, moderation, tombstones and current links',()=>{
  const report={id:'receipt-1',version:1,title:'Report',description:'Body',accepted_at:null,deleted:false,can_edit:true,can_delete:true,moderation:'pending',status:'under_consideration',feedback_url:'https://customer.test/ideas',public_url:null,tracking_notice:'Guest only'};
  const original=signature(report,'report');
  for(const change of [{accepted_at:'2026-10-03T11:00:00Z'},{deleted:true},{can_edit:false},{can_delete:false},{moderation:'accepted'},{status:'shipped'},{feedback_url:'https://customer.test/new'},{public_url:'https://server.test/requests/public'},{tracking_notice:'Updated notice'},{version:2},{title:'Edited'},{description:'Edited'}])assert.notEqual(signature({...report,...change},'report'),original,JSON.stringify(change));
  assert.notEqual(signature(report,'draft'),original);
});

function revealFixture(scrollY=0) {
  const classes=new Set(), attributes=new Map(), listeners=new Map();
  const viewport={scrollY,addEventListener(type,callback,options){assert.equal(options.passive,true);listeners.set(type,callback);},removeEventListener(type,callback){if(listeners.get(type)===callback)listeners.delete(type);}};
  const launcher={isConnected:true,inert:false,classList:{add:name=>classes.add(name),remove:name=>classes.delete(name)},setAttribute:(name,value)=>attributes.set(name,value),removeAttribute:name=>attributes.delete(name)};
  return {launcher,viewport,classes,attributes,listeners,scroll(y){viewport.scrollY=y;listeners.get('scroll')?.();}};
}
test('scroll reveal stays inaccessible until threshold, reveals once, and removes listener',()=>{
  const f=revealFixture();context.suggestionBoxScrollReveal(f.launcher,600,f.viewport);
  assert.equal(f.launcher.inert,true);assert.equal(f.attributes.get('aria-hidden'),'true');assert.equal(f.classes.has('awaiting-scroll'),true);
  f.scroll(599);assert.equal(f.launcher.inert,true);
  f.scroll(600);assert.equal(f.launcher.inert,false);assert.equal(f.attributes.size,0);assert.equal(f.classes.size,0);assert.equal(f.listeners.size,0);
  f.scroll(0);assert.equal(f.launcher.inert,false);
});
test('explicit open bypasses delay and restored scroll position is respected',()=>{
  const f=revealFixture();const reveal=context.suggestionBoxScrollReveal(f.launcher,600,f.viewport);
  reveal();reveal();assert.equal(f.launcher.inert,false);assert.equal(f.listeners.size,0);
  const restored=revealFixture(800);context.suggestionBoxScrollReveal(restored.launcher,600,restored.viewport);
  assert.equal(restored.launcher.inert,false);assert.equal(restored.listeners.size,0);
});
test('invalid scroll settings fail visible; disconnected launchers release listener',()=>{
  for(const threshold of [undefined,null,0,-1,NaN,Infinity,'600',{},true]){
    const f=revealFixture();context.suggestionBoxScrollReveal(f.launcher,threshold,f.viewport);
    assert.equal(f.launcher.inert,false);assert.equal(f.listeners.size,0);
  }
  const f=revealFixture();context.suggestionBoxScrollReveal(f.launcher,600,f.viewport);f.launcher.isConnected=false;f.scroll(20);assert.equal(f.listeners.size,0);
});
