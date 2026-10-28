/* SupaKnock — self-hosted, plain-text chat. No project or relay credentials. */
function suggestionBoxAIProgress(conversation, cursor = 0) {
  if (!conversation || conversation.state !== 'open' || conversation.support_mode !== 'ai' ||
      !['queued', 'running'].includes(conversation.ai_status) ||
      !Number.isInteger(conversation.ai_trigger_sequence) ||
      conversation.last_sequence !== conversation.ai_trigger_sequence || cursor > conversation.ai_trigger_sequence) return '';
  if (conversation.ai_status === 'queued') return 'Getting ready…';
  return ({thinking:'Thinking…', reading:'Reading sources…', writing:'Putting the answer together…',
    checking:'Checking the answer…', refining:'Refining the answer…'})[conversation.ai_progress] || 'Thinking…';
}
function suggestionBoxReviewSignature(value, kind) {
  // The displayed content and authority must still match before confirmation.
  return JSON.stringify(kind === 'draft'
    ? [kind, value.id, value.version, value.draft_hash, value.title, value.description,
       value.state, value.can_confirm, value.can_edit, value.feedback_url,
       value.tracking_notice, value.board?.id, value.board?.name, value.expires_at]
    : [kind, value.id, value.version, value.title, value.description, value.accepted_at,
       value.deleted, value.can_edit, value.can_delete, value.moderation, value.status,
       value.feedback_url, value.public_url, value.tracking_notice]);
}
// Reveal once; explicit opening also releases the scroll gate. No session or focus effects.
function suggestionBoxScrollReveal(launcher, threshold, viewport = window) {
  let revealed = false;
  function reveal() {
    if (revealed) return;
    revealed = true;
    viewport.removeEventListener('scroll', check);
    launcher.classList.remove('awaiting-scroll');
    launcher.inert = false;
    launcher.removeAttribute('aria-hidden');
  }
  function check() {
    if (!launcher.isConnected) {viewport.removeEventListener('scroll', check); return;}
    if (viewport.scrollY >= threshold) reveal();
  }
  if (typeof threshold === 'number' && Number.isFinite(threshold) && threshold > 0 && viewport.scrollY < threshold) {
    launcher.classList.add('awaiting-scroll');
    launcher.inert = true;
    launcher.setAttribute('aria-hidden', 'true');
    viewport.addEventListener('scroll', check, {passive:true});
  } else reveal();
  return reveal;
}
function suggestionBoxComposerKeyboard(input, composer, send) {
  let composing = false;
  input.addEventListener('compositionstart', () => {composing = true;});
  input.addEventListener('compositionend', () => {composing = false;});
  input.addEventListener('keydown', event => {
    // Enter used to confirm an IME candidate must never send the message.
    if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey ||
        event.isComposing || composing || event.keyCode === 229 || event.defaultPrevented) return;
    event.preventDefault();
    if (event.repeat || input.disabled || send.disabled || !input.value.trim()) return;
    composer.requestSubmit(send);
  });
}
(() => {
  'use strict';
  const controllers = new WeakMap();
  const palettes = {
    light: {accent:'#147d50',onAccent:'#ffffff',background:'#ffffff',surface:'#f5f7f6',text:'#202823',muted:'#606e65',border:'#dbe3de'},
    dark: {accent:'#3ecf8e',onAccent:'#092c1c',background:'#161616',surface:'#202220',text:'#ededed',muted:'#a0aaa4',border:'#353c37'}
  };
  const colorProperties = {accent:'accent',onAccent:'on-accent',background:'background',surface:'surface',text:'text',muted:'muted',border:'border'};
  const modes = ['auto','light','dark'];
  const label = (value, limit) => typeof value === 'string' && value.trim() ? [...value.trim()].slice(0, limit).join('') : null;
  const targetElement = target => typeof target === 'string' ? document.querySelector(target) : target;
  function mount(target, options = {}) {
    const host = targetElement(target);
    if (!host) return;
    if (controllers.has(host)) return controllers.get(host);
    if (host.shadowRoot) return;
    const server = new URL(options.server).origin;
    const project = options.project;
    let currentContext=options.context||null,contextProvider=options.contextProvider||null;
    function contextSnapshot(){
      const raw=contextProvider?contextProvider():currentContext;
      if(raw==null)return null;
      const value=JSON.parse(JSON.stringify(raw));
      if(value.version!==1||value.resource?.kind!=='podcast_episode'||typeof value.resource.id!=='string'||!value.resource.id||value.resource.id.length>200||Object.keys(value).some(k=>!['version','resource','position_seconds'].includes(k))||Object.keys(value.resource).some(k=>!['kind','id'].includes(k))||value.position_seconds!==undefined&&(!Number.isFinite(value.position_seconds)||value.position_seconds<0||value.position_seconds>86400))throw new Error('Invalid episode context.');
      return value;
    }

    const presentation = ['bubble', 'bar', 'inline'].includes(options.presentation) ? options.presentation : 'bubble';
    host.setAttribute('data-sb-presentation', presentation);
    const key = `suggestion-box-chat:${server}:${project}:${location.origin}`;
    const shadow = host.attachShadow({mode: 'open'});
    shadow.innerHTML = `<style>
:host{all:initial;font:13px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--sb-text);color-scheme:light dark}
*{box-sizing:border-box}button,textarea{font:inherit}button{cursor:pointer;border:1px solid transparent;border-radius:8px;padding:10px 14px;background:var(--sb-accent);color:var(--sb-on-accent);font-weight:550;line-height:1.2;transition:filter .16s ease,transform .16s ease}button:disabled{opacity:.45;cursor:not-allowed}button:focus-visible,textarea:focus-visible{outline:2px solid var(--sb-accent);outline-offset:3px}.icon{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}
.launcher{position:fixed;right:22px;bottom:22px;z-index:2147483000;display:flex;align-items:center;gap:9px;max-width:calc(100vw - 44px);padding:14px 18px;box-shadow:0 5px 22px #0003,0 1px 2px #0002;border:1px solid color-mix(in srgb,var(--sb-accent) 80%,var(--sb-on-accent));border-radius:14px;font-size:12px;letter-spacing:.05px;text-align:left;transition:filter .18s ease,transform .22s cubic-bezier(.2,.8,.2,1),box-shadow .22s ease}.launcher-label{overflow-wrap:anywhere}.launcher:hover{filter:brightness(1.06);transform:translateY(-2px);box-shadow:0 8px 28px #0003,0 2px 5px #0002}.launcher:active{transform:translateY(0) scale(.97)}
.panel{position:fixed;right:22px;bottom:calc(var(--sb-launcher-height,46px) + 38px);width:min(366px,calc(100vw - 28px));height:min(570px,calc(100dvh - var(--sb-launcher-height,46px) - 64px));display:flex;flex-direction:column;z-index:2147483001;background:var(--sb-background);border:1px solid var(--sb-border);border-radius:18px;box-shadow:0 20px 70px #0004,0 2px 8px #0002;overflow:hidden;transform-origin:bottom right;opacity:0;transform:translateY(12px) scale(.96);transition:opacity .18s ease,transform .26s cubic-bezier(.2,.8,.2,1);pointer-events:none}.panel.is-open{opacity:1;transform:translateY(0) scale(1);pointer-events:auto}.panel[hidden]{display:none}
header{padding:18px;border-bottom:1px solid var(--sb-border);display:flex;gap:11px;align-items:center;background:var(--sb-surface)}.brand-icon{display:grid;place-items:center;flex-shrink:0;width:34px;height:34px;border-radius:9px;background:color-mix(in srgb,var(--sb-accent) 12%,var(--sb-surface));border:1px solid color-mix(in srgb,var(--sb-accent) 25%,var(--sb-border));color:var(--sb-accent)}.brand-copy{min-width:0;flex:1}h2{font-size:13px;line-height:1.4;font-weight:600;letter-spacing:-.15px;margin:0;color:var(--sb-text);overflow-wrap:anywhere}small{display:block;color:var(--sb-muted);font-size:10px;line-height:1.5}.response{margin-top:3px}.close{padding:4px;width:30px;height:30px;display:grid;place-items:center;background:transparent;color:var(--sb-muted);border-radius:8px;font-size:22px;font-weight:400;flex-shrink:0;align-self:flex-start}.close:hover{color:var(--sb-text);background:color-mix(in srgb,var(--sb-text) 7%,var(--sb-surface))}
.log{padding:20px 16px;overflow:auto;flex:1;overscroll-behavior:contain;scrollbar-width:thin;scrollbar-color:var(--sb-border) transparent;min-height:0}.empty{display:flex;min-height:230px;height:100%;align-items:center;justify-content:center;flex-direction:column;text-align:center;padding:20px}.empty-icon{display:grid;place-items:center;width:46px;height:46px;border:1px solid var(--sb-border);border-radius:13px;background:var(--sb-surface);color:var(--sb-accent);margin-bottom:17px}.empty strong{font-size:13px;font-weight:500;color:var(--sb-text);letter-spacing:-.1px}.empty p{font-size:11px;line-height:1.7;color:var(--sb-muted);max-width:220px;margin:8px 0 0}.bubble{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--sb-surface);color:var(--sb-text);border:1px solid var(--sb-border);border-radius:12px 12px 12px 3px;padding:10px 12px;margin:0 34px 16px 0;font-size:12px;line-height:1.7}.visitor{background:color-mix(in srgb,var(--sb-accent) 12%,var(--sb-background));border-color:color-mix(in srgb,var(--sb-accent) 30%,var(--sb-border));border-radius:12px 12px 3px 12px;margin:0 0 16px 34px}.bubble small{font-size:9px;color:var(--sb-muted);margin-top:6px}.status{padding:0 17px 10px;font-size:10px;line-height:1.5;color:var(--sb-muted);min-height:24px}.status:empty{display:none}
form{margin:0 12px 10px;border:1px solid var(--sb-border);border-radius:12px;padding:8px;display:flex;gap:7px;align-items:end;background:var(--sb-surface);box-shadow:0 1px 3px #0001;transition:border-color .16s ease,box-shadow .16s ease}form:focus-within{border-color:var(--sb-accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--sb-accent) 12%,transparent)}textarea{width:100%;min-width:0;min-height:46px;max-height:120px;resize:vertical;border:0;border-radius:3px;padding:5px 4px;background:transparent;color:var(--sb-text);line-height:1.6;font-size:12px;box-shadow:none}textarea::placeholder{color:var(--sb-muted)}textarea:focus{outline:none}form button{font-size:11px;padding:9px 11px;min-height:33px}form button:hover:not(:disabled){filter:brightness(1.06)}form button:active:not(:disabled){transform:scale(.96)}.reset{border:0;border-top:1px solid var(--sb-border);border-radius:0;background:var(--sb-surface);color:var(--sb-muted);font-size:9px;font-weight:400;line-height:1.4;padding:11px 8px}.reset:hover{color:var(--sb-text)}.sr{position:absolute;width:1px;height:1px;clip:rect(0,0,0,0);overflow:hidden}
@media(max-width:720px){textarea{font-size:16px}}@media(max-width:440px){.launcher{right:16px;bottom:16px;max-width:calc(100vw - 32px)}.panel{right:14px;bottom:calc(var(--sb-launcher-height,46px) + 30px);width:calc(100vw - 28px);height:min(570px,calc(100dvh - var(--sb-launcher-height,46px) - 50px))}}@media(prefers-reduced-motion:reduce){button,.launcher,.panel,form{transition:none}.launcher:hover,.launcher:active,.panel,.panel.is-open,form button:active:not(:disabled){transform:none}}
[hidden]{display:none!important}input{font:inherit;color:var(--sb-text);background:var(--sb-background);border:1px solid var(--sb-border);border-radius:6px;padding:8px;min-width:0;width:100%}input:focus-visible,a:focus-visible,summary:focus-visible,[tabindex]:focus-visible{outline:2px solid var(--sb-accent);outline-offset:3px}a{color:var(--sb-accent);overflow-wrap:anywhere}h3{font-size:12px;margin:10px 0;line-height:1.45}h4{font-size:12px;margin:14px 0 8px;overflow-wrap:anywhere}.help{color:var(--sb-muted);font-size:10px;line-height:1.65;margin:7px 0}.mode{padding:12px 16px;border-bottom:1px solid var(--sb-border);background:var(--sb-surface);flex-shrink:0;font-size:11px}.mode .actions{margin-top:8px}.actions{display:flex;gap:7px;flex-wrap:wrap;margin:10px 0}.actions button{font-size:10px;padding:8px 10px;min-height:34px}.secondary{background:var(--sb-surface);color:var(--sb-text);border-color:var(--sb-border)}.activity-card{padding:12px 0;border-bottom:1px solid var(--sb-border);font-size:11px;overflow-wrap:anywhere}.activity-card .actions{margin-bottom:0}.review{border:1px solid var(--sb-border);border-radius:9px;padding:12px;margin:12px 0 20px;font-size:11px;overflow-wrap:anywhere;scroll-margin-top:12px}.review>.secondary{font-size:10px;padding:7px}.plain{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7}.tracking{padding:10px;border:1px solid var(--sb-border);border-left:3px solid var(--sb-accent);border-radius:5px;font-size:10px;line-height:1.7}.error{color:var(--sb-text);border-left:3px solid #d68b43;padding:8px;font-size:11px;background:var(--sb-surface)}.error:empty{display:none}.report-editor{display:block;margin:14px 0 0;padding:10px}.report-editor label{display:block;font-size:11px;margin:12px 0}.report-editor input,.report-editor textarea{display:block;margin-top:6px;width:100%;border:1px solid var(--sb-border);padding:8px;background:var(--sb-background);border-radius:5px}.report-editor textarea{min-height:120px;max-height:260px}.report-editor .actions{margin-bottom:0}.feedback-destination{margin:10px 0;font-size:11px}.copy-area{min-height:160px;border:1px solid var(--sb-border);padding:8px;margin-top:10px}.sources{white-space:normal;margin-top:10px;border-top:1px solid var(--sb-border);padding-top:7px;font-size:10px}.sources summary{cursor:pointer;color:var(--sb-accent);font-weight:550}.source{margin:12px 0 4px;overflow-wrap:anywhere}.source p{white-space:pre-wrap;margin:6px 0}.ai{border-left:3px solid var(--sb-accent)}.inline-confirm{border:1px solid var(--sb-accent);border-radius:8px;padding:12px;margin:8px 12px;max-height:230px;overflow:auto;flex-shrink:0;background:var(--sb-surface);font-size:11px}.inline-confirm h3{margin-top:0}.inline-confirm p{margin-bottom:8px}.inline-confirm:focus{outline:2px solid var(--sb-accent);outline-offset:2px}@media(max-width:440px){.mode{padding:10px 13px}.review{padding:10px}.report-editor input,.hours-card input{font-size:16px}.panel{height:min(660px,calc(100dvh - var(--sb-launcher-height,46px) - 50px))}}

.launcher[hidden]{display:none}.panel{bottom:22px;width:min(400px,calc(100vw - 28px));height:min(640px,calc(100dvh - 44px))}header{padding:12px 14px;gap:9px;flex-shrink:0}h2{font-size:14px}.response{font-size:11px;margin-top:1px}.brand-icon{width:30px;height:30px}.close,.options-toggle{width:36px;height:36px;padding:4px;align-self:center;background:transparent;color:var(--sb-muted);font-size:24px;flex-shrink:0}.chat-options{padding:12px 16px;border-bottom:1px solid var(--sb-border);background:var(--sb-surface);max-height:40%;overflow:auto;flex-shrink:0}.chat-options .reset{padding:9px 12px;font-size:12px;border:1px solid var(--sb-border);border-radius:8px}.mode{padding:8px 14px;background:var(--sb-background);font-size:12px}.mode.compact{display:flex;align-items:center;justify-content:space-between;gap:8px}.mode.compact .actions{margin:0}.mode.compact .help{margin:0}.mode .actions button{padding:7px 10px;min-height:32px;font-size:11px}.log{padding:14px;scroll-padding:12px}.empty{min-height:130px;padding:12px}.bubble{font-size:14px;line-height:1.6;margin-bottom:12px;padding:10px 12px;margin-right:22px}.visitor{margin-right:0;margin-left:22px}.bubble small{font-size:10px;margin-top:4px}.status{font-size:11px;padding:0 14px 7px;min-height:0}.composer{margin:0 10px 10px;padding:6px;gap:6px;flex-shrink:0}.composer textarea{font-size:14px;min-height:34px;max-height:110px;resize:none;padding:5px 6px;line-height:1.5}.composer button{font-size:12px;min-height:36px}.help{font-size:11px;line-height:1.55}.sources{margin-top:7px;padding-top:5px;font-size:11px}.activity-card{margin:8px 0 14px;padding:12px;border:1px solid var(--sb-border);border-radius:12px;background:var(--sb-surface);font-size:12px;scroll-margin:12px}.activity-card h3{margin:0 0 5px;font-size:13px}.activity-card .report-preview{margin:6px 0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.activity-card .review{border:0;margin:0;padding:0}.activity-card .review h4{font-size:14px}.activity-card .review .plain{font-size:13px}.activity-card .review .tracking{font-size:11px}.activity-card .actions button{font-size:12px;min-height:36px}.activity-card .review-warning{margin-top:8px}.report-history:empty{display:none}.report-history .actions{margin-top:0}.inline-confirm{flex-shrink:1}.report-editor textarea{font-size:14px}.report-editor label{font-size:12px}
@media(max-width:440px){.panel{right:8px;bottom:8px;width:calc(100vw - 16px);height:calc(100dvh - 16px);border-radius:16px}.composer textarea,.report-editor input,.report-editor textarea{font-size:16px}.log{padding:12px}.mode{padding:7px 12px}}

.sources{margin-top:10px;padding-top:8px;border-top:1px solid var(--sb-border);font-size:12px;white-space:normal}.sources>summary{display:flex;align-items:center;gap:7px;list-style:none;width:fit-content;max-width:100%;min-height:32px;padding:4px 8px 4px 0;color:var(--sb-muted);font-weight:550;border-radius:5px;cursor:pointer}.sources>summary::-webkit-details-marker{display:none}.sources>summary::after{content:'';width:6px;height:6px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;transform:rotate(45deg);margin:0 3px 3px 4px}.sources[open]>summary::after{transform:rotate(225deg);margin-bottom:0}.sources>summary:hover{color:var(--sb-accent)}.sources-count{display:inline-grid;place-items:center;min-width:19px;height:19px;padding:0 5px;border-radius:6px;background:color-mix(in srgb,var(--sb-accent) 9%,var(--sb-background));color:var(--sb-accent);font-size:10px;font-weight:650}.source-cards{display:grid;gap:9px;margin:7px 0 2px}.source{margin:0;padding:11px;border:1px solid var(--sb-border);border-radius:10px;background:var(--sb-background);overflow-wrap:anywhere}.source-heading{display:flex;align-items:flex-start;gap:9px}.source-number{display:grid;place-items:center;width:24px;height:26px;flex-shrink:0;border-radius:6px;background:var(--sb-surface);color:var(--sb-muted);font-size:11px;font-weight:600}.source-identity{min-width:0;flex:1}.source-title{display:block;font-size:12px;line-height:1.45;font-weight:600;color:var(--sb-text);text-decoration:none}.source-title[href]:hover{color:var(--sb-accent);text-decoration:underline;text-underline-offset:3px}.source-origin{display:block;color:var(--sb-muted);font-size:10px;line-height:1.5;margin-top:2px;overflow-wrap:anywhere}.source .source-excerpt{margin:10px 0 0;padding:0;font-size:12px;line-height:1.6;color:var(--sb-muted);white-space:pre-wrap}.source-expand{background:transparent;color:var(--sb-accent);padding:6px 0 0;margin-top:2px;font-size:11px;min-height:30px;border:0;font-weight:550}.source-expand:hover{text-decoration:underline;text-underline-offset:3px}

/* Layout belongs to the embedding website; all variants share one conversation. */
.launcher{opacity:1;visibility:visible;transition:opacity .24s ease,visibility .24s,filter .18s ease,transform .28s cubic-bezier(.2,.8,.2,1),box-shadow .22s ease}
.launcher-arrow{display:none;margin-left:auto;font-size:20px;line-height:1;font-weight:400}
:host([data-sb-presentation="bar"]) .launcher,:host([data-sb-presentation="inline"]) .launcher{width:440px;max-width:calc(100vw - 32px);padding:15px 18px;gap:12px;background:var(--sb-background);color:var(--sb-text);border:1px solid var(--sb-border);border-radius:18px;font-size:14px;min-height:58px}
:host([data-sb-presentation="bar"]) .launcher,:host([data-sb-presentation="bar"]) .panel{left:0;right:0;margin-inline:auto;bottom:max(16px,env(safe-area-inset-bottom));transform-origin:bottom center}
:host([data-sb-presentation="bar"]) .panel{height:min(820px,calc(100dvh - max(32px,calc(env(safe-area-inset-bottom) + 16px))))}
:host([data-sb-presentation="bar"]) .launcher .icon,:host([data-sb-presentation="inline"]) .launcher .icon{color:var(--sb-accent);width:22px;height:22px}
:host([data-sb-presentation="bar"]) .launcher-arrow,:host([data-sb-presentation="inline"]) .launcher-arrow{display:block;color:var(--sb-accent)}
/* A field-like launcher: one accessible button, not a second composer. */
.launcher-send{display:none}
:host([data-sb-presentation="bar"]) .launcher{width:480px;min-height:64px;padding:9px 10px 9px 20px;gap:14px;border-radius:22px;background:var(--sb-background);box-shadow:0 12px 36px #00000018,0 2px 6px #0000000d;letter-spacing:0;font-weight:400}
:host([data-sb-presentation="bar"]) .launcher>.icon,:host([data-sb-presentation="bar"]) .launcher-arrow{display:none}
:host([data-sb-presentation="bar"]) .launcher-label{flex:1;min-width:0;color:var(--sb-muted);font-size:15px;line-height:1.5;padding-block:7px}
:host([data-sb-presentation="bar"]) .launcher-send{display:grid;place-items:center;width:44px;height:44px;flex-shrink:0;border-radius:14px;background:var(--sb-accent);color:var(--sb-on-accent);box-shadow:0 2px 4px #0000000d}
:host([data-sb-presentation="bar"]) .launcher-send .icon{width:20px;height:20px;color:inherit;stroke-width:2}
:host([data-sb-presentation="bar"]) .launcher:hover{filter:none;border-color:color-mix(in srgb,var(--sb-accent) 45%,var(--sb-border));box-shadow:0 14px 40px #00000020,0 3px 8px #0000000d}
:host([data-sb-presentation="bar"]) .launcher:active{transform:translateY(0)}
:host([data-sb-presentation="inline"]){display:block;width:100%;min-width:0}
:host([data-sb-presentation="inline"]) .launcher{position:relative;inset:auto;margin:0;width:100%;max-width:100%;z-index:auto;box-shadow:0 4px 18px #0000000d;transform-origin:bottom center}
:host([data-sb-presentation="inline"]) .panel{margin:0;padding:0;left:auto;top:auto}
:host([data-sb-presentation="inline"]) .panel::backdrop{background:transparent;pointer-events:none}
.launcher.awaiting-scroll{opacity:0;visibility:hidden;pointer-events:none;transition:none;transform:translateY(calc(100% + 32px))}
@media(prefers-reduced-motion:reduce){.launcher,.panel{transition:none}.launcher.awaiting-scroll,.launcher:hover,.launcher:active,.panel,.panel.is-open{transform:none}}
/* Small, readable controls; every color still belongs to the embedding website. */
header{padding:12px 14px}.close,.options-toggle{width:40px;height:40px;border-radius:10px}.options-toggle:hover{color:var(--sb-text);background:color-mix(in srgb,var(--sb-text) 7%,var(--sb-surface))}.empty strong{font-size:15px;font-weight:600}.empty p{font-size:13px;line-height:1.65;max-width:250px}.empty-icon{width:44px;height:44px;margin-bottom:15px}.help{font-size:12px;line-height:1.6}.bubble small{font-size:11px}.status{font-size:12px;line-height:1.55;padding:0 14px 10px}.mode .actions button{min-height:38px;font-size:12px}.composer{padding:7px;border-radius:14px}.composer button{min-height:40px;padding-inline:13px}.chat-options .reset,.source-expand{min-height:36px}.source-origin{font-size:11px}.sources>summary,.hours-card summary{min-height:36px}.hours-card summary{cursor:pointer;line-height:1.5;padding-block:6px}.hours-card label{display:block;margin-top:14px}.hours-card input{min-height:40px}.inline-confirm{font-size:12px;line-height:1.6}.inline-confirm h3{font-size:14px}.inline-confirm .actions button{min-height:40px;font-size:12px}.activity-card .tracking{font-size:12px}
button[aria-busy="true"]::after{content:'';display:inline-block;width:11px;height:11px;margin-left:6px;border:1.5px solid currentColor;border-right-color:transparent;border-radius:50%;vertical-align:-1px;animation:sb-progress .7s linear infinite}.history-loading{padding:12px 0}.history-loading p{margin:0 0 16px;font-size:12px;color:var(--sb-muted)}.history-skeleton{height:64px;width:78%;background:var(--sb-surface);border:1px solid var(--sb-border);border-radius:12px 12px 12px 3px;margin:12px 0;animation:sb-breathe 1.6s ease-in-out infinite alternate}.history-skeleton:last-child{margin-left:auto;width:62%;height:44px;border-radius:12px 12px 3px 12px}.bubble.is-new{animation:sb-message-in .2s ease-out}
.ai-progress{display:flex;align-items:center;gap:12px;padding:14px 16px;margin:12px 0 4px;max-width:90%;border:1px solid var(--sb-border);background:var(--sb-surface);border-radius:14px 14px 14px 4px;color:var(--sb-text)}.ai-progress small{display:block;color:var(--sb-muted);font-size:11px;margin-bottom:4px}.ai-progress-label{font-size:13px;line-height:1.5}.ai-progress-dots{display:flex;gap:4px;flex-shrink:0;color:var(--sb-accent)}.ai-progress-dots i{height:4px;width:4px;border-radius:50%;background:currentColor;animation:sb-breathe .85s ease-in-out infinite alternate}.ai-progress-dots i:nth-child(2){animation-delay:.2s}.ai-progress-dots i:nth-child(3){animation-delay:.4s}
@keyframes sb-progress{to{transform:rotate(360deg)}}@keyframes sb-breathe{to{opacity:.4}}@keyframes sb-message-in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
@media(max-width:440px){.close,.options-toggle{width:40px;height:44px}.panel{bottom:max(8px,env(safe-area-inset-bottom));height:calc(100dvh - max(16px,calc(env(safe-area-inset-bottom) + 8px)))}.composer{margin-bottom:10px}.chat-options .reset,.activity-card .actions button{min-height:40px}}
@media(prefers-reduced-motion:reduce){button[aria-busy="true"]::after,.history-skeleton,.bubble.is-new,.ai-progress-dots i{animation:none}}

/* Quiet motion and stable controls: no layout shift while sending. */
.panel{box-shadow:0 16px 56px #0002,0 2px 8px #0001;transition:opacity .18s ease,transform .24s cubic-bezier(.22,1,.36,1)}
.bubble{border-radius:14px 14px 14px 4px}.visitor{border-radius:14px 14px 4px 14px}.ai{border-left-width:2px}
.bubble.is-new{animation:sb-message-in .24s cubic-bezier(.22,1,.36,1)}
.ai-progress{animation:sb-message-in .22s ease-out}.ai-progress-label{display:block}
.chat-options:not([hidden]),.sources[open]>.source-cards{animation:sb-reveal .18s ease-out}
.sources>summary::after{transition:transform .18s ease}.close,.options-toggle,.secondary{transition:background .16s ease,color .16s ease,border-color .16s ease,transform .16s ease}
.close:active,.options-toggle:active{transform:scale(.94)}
.timeline{position:relative;display:flex;flex-direction:column;flex:1;min-height:0;overflow:hidden}.timeline .log{flex:1;overflow-anchor:none;padding-bottom:calc(var(--sb-dock-height,110px) + 16px);scroll-padding-bottom:calc(var(--sb-dock-height,110px) + 16px)}
.latest{position:absolute;z-index:3;bottom:calc(var(--sb-dock-height,110px) + 8px);left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:7px;white-space:nowrap;max-width:calc(100% - 32px);min-height:36px;padding:8px 13px;border-radius:20px;background:var(--sb-background);border:1px solid var(--sb-border);color:var(--sb-text);box-shadow:0 3px 14px #0002;font-size:12px;animation:sb-reveal .18s ease-out}.latest:hover{border-color:var(--sb-accent);color:var(--sb-accent)}.latest.has-reply{background:var(--sb-accent);color:var(--sb-on-accent);border-color:transparent}.latest .icon{width:14px;height:14px}
.composer{background:var(--sb-surface);box-shadow:0 6px 24px #0000000d,0 2px 6px #00000008}.composer textarea{padding:9px 7px;min-height:40px;scrollbar-width:thin}.composer button{width:40px;height:40px;min-width:40px;min-height:40px;padding:0;display:grid;place-items:center;position:relative;border-radius:11px;transition:background .16s ease,color .16s ease,transform .16s ease,opacity .16s ease}.composer button:disabled:not([aria-busy="true"]){opacity:1;background:var(--sb-surface);color:var(--sb-muted)}.composer button .icon{width:19px;height:19px}.composer button[aria-busy="true"]{opacity:.65}.composer button[aria-busy="true"] .icon{visibility:hidden}.composer button[aria-busy="true"]::after{position:absolute;width:15px;height:15px;margin:0}
.composer-dock{position:absolute;inset:auto 0 0;z-index:2;display:flex;flex-direction:column;align-items:stretch;gap:9px;padding:28px 12px 12px;max-height:75%;overflow-y:auto;pointer-events:none;background:linear-gradient(to bottom,transparent,var(--sb-background) 30px);scrollbar-width:thin}
.composer-dock>*{pointer-events:auto;flex-shrink:0}.composer-dock .composer{margin:0;border-radius:18px;padding:7px}.composer-dock .composer:focus-within{box-shadow:0 6px 24px #0000000d,0 0 0 2px color-mix(in srgb,var(--sb-accent) 12%,transparent)}.composer-dock .status{margin:0;padding:9px 12px;border:1px solid var(--sb-border);border-radius:12px;background:var(--sb-surface);font-size:11px;line-height:1.5}.composer-dock .status:empty{display:none}
.episode-context{display:inline-flex;align-items:center;align-self:flex-start;gap:6px;max-width:100%;margin:0 3px;padding:5px 10px;border:1px solid var(--sb-border);border-radius:999px;background:var(--sb-surface);box-shadow:0 2px 8px #00000008;font-size:11px;line-height:1.5;font-weight:500;color:var(--sb-muted)}.episode-context::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--sb-accent);flex-shrink:0}
@media(max-width:440px){.composer button{width:44px;height:44px;min-width:44px;min-height:44px}}
@keyframes sb-reveal{from{opacity:0}to{opacity:1}}
@media(prefers-reduced-motion:reduce){.panel,.close,.options-toggle,.secondary,.composer button,.sources>summary::after{transition:none}.bubble.is-new,.ai-progress,.ai-progress-label.is-changing,.chat-options:not([hidden]),.sources[open]>.source-cards,.latest{animation:none}.close:active,.options-toggle:active{transform:none}}

/* Metadata belongs to the conversation, not inside the message itself. */
.message{margin:0 22px 20px 0;min-width:0}.message.outgoing{margin:0 0 20px 22px}.message .bubble{margin:0}.message-meta{display:flex;align-items:baseline;gap:6px;padding:0 2px;margin-bottom:6px;color:var(--sb-muted);font-size:11px;line-height:1.5}.message-sender{font-weight:550}.message-meta time{font-size:10px;font-variant-numeric:tabular-nums}.message-separator{opacity:.6}.outgoing .message-meta{justify-content:flex-end}.message-context{font-size:10px;line-height:1.5;color:var(--sb-muted);margin:-2px 2px 6px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}.outgoing .message-context{text-align:right}.message.is-new{animation:sb-message-in .24s cubic-bezier(.22,1,.36,1)}
.ai-progress-row{margin:0 22px 18px 0;animation:sb-message-in .22s ease-out}.ai-progress-row .ai-progress{margin:0;max-width:none;padding:13px 14px;animation:none}.ai-progress-label{display:grid;min-width:0;flex:1;overflow:hidden;isolation:isolate}.ai-progress-label>span{grid-area:1/1;min-width:0;align-self:center}.ai-progress-size{visibility:hidden;pointer-events:none}.stage-current{position:relative}.stage-entering{animation:sb-stage-in .32s cubic-bezier(.22,1,.36,1) both}.stage-leaving{pointer-events:none;animation:sb-stage-out .22s ease-in both}
@keyframes sb-stage-in{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:translateY(0)}}@keyframes sb-stage-out{from{opacity:1;transform:translateY(0)}to{opacity:0;transform:translateY(-5px)}}
@media(prefers-reduced-motion:reduce){.message.is-new,.ai-progress-row,.stage-entering,.stage-leaving{animation:none}.stage-leaving{display:none}}

/* Embedded support: generous type, a calm welcome and a floating writing surface. */
.panel{width:min(440px,calc(100vw - 32px));height:min(820px,calc(100dvh - 44px));border-radius:26px;box-shadow:0 20px 64px #0000001a,0 3px 12px #0000000a}
header{padding:18px 18px 12px;background:var(--sb-background);border-bottom:0;gap:8px}
.brand-icon{display:none}.brand-copy h2{font-size:15px;line-height:1.45;font-weight:600;letter-spacing:-.2px}.response{font-size:12px;line-height:1.55;margin-top:3px}
.close,.options-toggle{width:40px;height:40px;border-radius:50%;align-self:flex-start}.close .icon{width:22px;height:22px;stroke-width:1.8}.options-toggle .icon{width:22px;height:22px}
.mode{padding:8px 20px 12px;border:0;background:var(--sb-background)}.mode.compact{gap:12px}.mode.compact .help{font-size:12px}.mode .actions button{border-radius:999px;min-height:40px;padding:9px 13px}.mode .actions{gap:8px}
.timeline .log{padding:20px 20px calc(var(--sb-dock-height,174px) + 20px);scroll-padding:20px 20px calc(var(--sb-dock-height,174px) + 20px)}
.log:has(>.empty){display:flex;flex-direction:column}.empty{flex:1;height:auto;min-height:180px;padding:24px 8px 36px;gap:0}.empty .welcome-eyebrow{margin:0 0 8px;font-size:15px;line-height:1.5;color:var(--sb-muted)}.empty .welcome-title{margin:0;max-width:100%;font-size:32px;line-height:1.15;font-weight:650;letter-spacing:-1px;text-wrap:balance;overflow-wrap:anywhere;color:var(--sb-text)}.empty .welcome-description{font-size:14px;line-height:1.65;max-width:290px;margin:16px 0 0;text-wrap:pretty}
.empty:has(.mode:not([hidden])){min-height:290px}.empty:has(.mode:not([hidden])) .welcome-description{display:none}.empty .mode{padding:0;margin:18px 0 0;width:100%;max-width:310px}.empty .mode>.help{font-size:13px;margin:0}.empty .mode .actions{justify-content:center;margin:20px 0 0;gap:10px}.empty .mode .actions button{font-size:13px;padding:12px 16px;min-height:44px}.empty .mode>.mode-disclosure{font-size:11px;line-height:1.6;margin:16px 0 0;max-width:none}.empty .mode .actions button:first-child{background:var(--sb-accent);border-color:transparent;color:var(--sb-on-accent)}
.message{margin:0 14px 24px 0}.message.outgoing{margin:0 0 24px 28px}.message-meta{font-size:12px;margin-bottom:8px;padding:0 2px}.message-meta time{font-size:11px}.message-context{font-size:11px;margin-bottom:8px}
.message .bubble{font-size:15px;line-height:1.7;border-radius:18px;padding:14px 16px;border-color:color-mix(in srgb,var(--sb-border) 65%,transparent);background:var(--sb-background)}.message .visitor{background:var(--sb-surface);border-color:transparent;border-radius:20px 20px 6px 20px}.message .ai{border-left-width:1px}.sources{margin-top:14px;padding-top:10px}.source{background:var(--sb-surface);border-color:transparent;border-radius:12px}.source-number{background:var(--sb-background)}
.ai-progress-row{margin-right:14px}.ai-progress-row .ai-progress{border:0;background:var(--sb-surface);padding:16px;border-radius:18px}.ai-progress-label{font-size:14px}
.composer-dock{gap:10px;padding:30px 16px 16px;background:linear-gradient(to bottom,transparent,var(--sb-background) 30px)}
.composer-dock .composer{display:flex;flex-wrap:wrap;gap:8px;padding:14px;border-radius:26px;background:var(--sb-surface);background:light-dark(#fff,var(--sb-surface));border-color:var(--sb-border);box-shadow:0 5px 22px #0000000a,0 1px 4px #00000008}
.composer-dock .composer:focus-within{border-color:color-mix(in srgb,var(--sb-accent) 55%,var(--sb-border));box-shadow:0 5px 22px #0000000a,0 0 0 2px color-mix(in srgb,var(--sb-accent) 9%,transparent)}
.composer textarea{flex:1 0 100%;width:100%;font-size:16px;line-height:1.55;padding:3px 4px;min-height:52px;max-height:144px}.composer button{margin-left:auto;width:42px;height:42px;min-width:42px;min-height:42px;border-radius:50%}.composer button .icon{width:22px;height:22px;stroke-width:2}.composer button:disabled:not([aria-busy="true"]){background:var(--sb-surface);color:var(--sb-muted);opacity:.65}
.composer-dock .status{padding:0 6px;border:0;border-radius:0;background:transparent;font-size:12px;line-height:1.55}.episode-context{margin-inline:4px;padding:6px 11px;background:var(--sb-background);font-size:12px;box-shadow:none}
.chat-options{padding:16px 20px;background:var(--sb-surface);border-bottom:0}.chat-options>strong{font-size:14px}.chat-options .help{font-size:12px}.chat-options .reset{border-radius:999px}.inline-confirm{margin:8px 16px;border-radius:16px;padding:16px}
@media(max-width:480px){:host([data-sb-presentation="bar"]) .panel{left:8px}.panel,:host([data-sb-presentation="bar"]) .panel{right:8px;bottom:max(8px,env(safe-area-inset-bottom));width:calc(100vw - 16px);height:calc(100dvh - max(16px,calc(env(safe-area-inset-bottom) + 8px)));border-radius:24px}header{padding:14px 14px 10px}.mode{padding-inline:16px}.timeline .log{padding-inline:16px}.empty .welcome-title{font-size:29px}.composer-dock{padding:26px 12px 12px}.composer-dock .composer{padding:12px;border-radius:24px}.composer button{width:44px;height:44px;min-width:44px;min-height:44px}}
@media(max-height:540px){header{padding-top:10px;padding-bottom:6px}.response{font-size:11px}.empty{min-height:120px;padding-block:12px}.empty:has(.mode:not([hidden])){min-height:230px}.empty .welcome-title{font-size:25px}.empty .mode{margin-top:12px}.empty .mode .actions{margin-top:12px}.composer textarea{min-height:28px;max-height:80px}.composer-dock .composer{padding:9px;border-radius:20px}.composer-dock{padding-top:20px;gap:6px}.composer button{width:36px;height:36px;min-width:36px;min-height:36px}}
</style><button class="launcher" aria-expanded="false" aria-controls="chat-panel"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5A8.5 8.5 0 0 1 10.5 3h2A8.5 8.5 0 0 1 21 11.5ZM7 10h9M7 14h5"/></svg><span class="launcher-label">Chat with us</span><span class="launcher-arrow" aria-hidden="true">↗</span><span class="launcher-send" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><path d="M12 19V5m-6 6 6-6 6 6"/></svg></span></button><section class="panel" id="chat-panel" aria-label="Support chat" aria-hidden="true" inert hidden><header><div class="brand-icon" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><path d="M5 4h14v13H9l-4 4V4ZM9 8h6M9 12h4"/></svg></div><div class="brand-copy"><h2>Chat with us</h2><small class="response"></small></div><button class="options-toggle" aria-label="Chat options" aria-expanded="false" aria-controls="chat-options"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg></button><button class="close" aria-label="Close chat"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></header><section id="chat-options" class="chat-options" hidden><strong>About this chat</strong><p class="help">AI answers can be mistaken. In AI mode, messages are processed by the configured AI provider. Relevant question details may also be sent to connected services to retrieve answers. Check the sources when you need to verify an answer.</p><button class="reset secondary">End this chat</button><p class="help">Clears access from this browser. Submitted reports are kept.</p></section><div class="mode" aria-live="polite"></div><div id="conversation-view" class="log" role="log" aria-live="polite" aria-label="Conversation messages"><div class="empty"><p class="welcome-eyebrow">Get help from</p><h3 class="welcome-title">Support</h3><p class="welcome-description">Ask a question or share what’s on your mind. We’ll reply right here.</p></div></div><div class="status" role="status"></div><form class="composer"><label class="sr" for="message">Your message</label><textarea id="message" rows="1" maxlength="4000" placeholder="How can we help?" required></textarea><button type="submit" aria-label="Send message" title="Send message"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg></button></form></section>`;
    const $ = s => shadow.querySelector(s);
    const launcher=$('.launcher'), panel=$('.panel'), log=$('.log'), status=$('.status'), input=$('textarea'), send=$('button[type=submit]');
    // The top layer keeps an inline launcher's overlay clear of clipped/transformed ancestors.
    const topLayer = presentation === 'inline' && typeof panel.showPopover === 'function';
    if(topLayer)panel.setAttribute('popover','manual');
    const launcherText = label(options.launcherText, 80), title = label(options.title, 120);
    $('.launcher-label').textContent = launcherText || (presentation === 'bubble' ? 'Chat with us' : 'Ask a question…');
    const revealLauncher = suggestionBoxScrollReveal(launcher, presentation === 'inline' ? 0 : options.revealAfterScroll);
    if (title) {$('h2').textContent = title; panel.setAttribute('aria-label', title);}
    const appearance = options.theme && typeof options.theme === 'object' ? options.theme : {};
    const colors = Object.fromEntries(Object.entries(palettes).map(([name, defaults]) => [name,
      Object.fromEntries(Object.entries(defaults).map(([token, fallback]) => {
        const value = appearance[name]?.[token];
        return [token, typeof value === 'string' && /^#[a-f\d]{3}(?:[a-f\d]{3})?$/i.test(value) ? value : fallback];
      }))
    ]));
    const systemTheme = matchMedia('(prefers-color-scheme: dark)');
    const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
    let themeMode = modes.includes(appearance.mode) ? appearance.mode : 'auto';
    function applyTheme() {
      const resolved = themeMode === 'auto' ? (systemTheme.matches ? 'dark' : 'light') : themeMode;
      for (const [token, value] of Object.entries(colors[resolved])) host.style.setProperty(`--sb-${colorProperties[token]}`, value);
      host.style.colorScheme = resolved;
    }
    function setTheme(mode) {if (modes.includes(mode)) {themeMode = mode; applyTheme();}}
    systemTheme.addEventListener('change', () => {if (themeMode === 'auto') applyTheme();});
    applyTheme();
    // Keep a wrapped custom launcher clear of the panel on narrow screens.
    const launcherSize = new ResizeObserver(() => host.style.setProperty('--sb-launcher-height', `${launcher.getBoundingClientRect().height}px`));
    launcherSize.observe(launcher);
    let hideTimer=null, session=null, socket=null, cursor=0, retry=1000, timer=null, pollTimer=null;
    let open=false, stopped=false, pending=null, busy=false, closed=false, connecting=false, lastTyping=0, epoch=0;
    let modeSignature='', hoursSignature='';
    let hoursCard=null, aiProgressCard=null;
    let contactOptions=null;
    const contactInput=document.createElement('input');contactInput.type='email';contactInput.maxLength=254;contactInput.autocomplete='email';contactInput.setAttribute('aria-label','Optional email for follow-up');contactInput.placeholder='you@example.com';
    let config=null, conversation=null, chosen=false, lastQuestion='', lastConfig=0, refreshing=false;
    let activitySignature='';
    let draftOffset=0, reportOffset=0, activity={drafts:{items:[],has_more:false},reports:{items:[],has_more:false}};
    let detail=null, detailKind=null, detailValue=null, detailEditing=false, detailStale=false, confirmationClose=null;
    const confirmationIds=new Map();
    const make=(tag,attrs={},...children)=>{const n=document.createElement(tag);for(const [k,v]of Object.entries(attrs)){if(k==='class')n.className=v;else if(k==='value')n.value=v;else if(k==='onclick')n.onclick=v;else if(v!==null&&v!==false)n.setAttribute(k,v===true?'':String(v));}for(const c of children.flat())if(c!==null&&c!==undefined)n.append(c instanceof Node?c:document.createTextNode(String(c)));return n;};
    const replace=(target,...children)=>target.replaceChildren(...children.flat().filter(c=>c!==null&&c!==undefined));
    const help=text=>make('p',{class:'help'},text);
    const actions=(...children)=>make('div',{class:'actions'},children);
    const modeBox=$('.mode'), activityList=make('div',{class:'report-history'}), reportCards=new Map();
    let detailBox=null;log.prepend(activityList);
    const composer=$('.composer'), reset=$('.reset');
    const timeline=make('div',{class:'timeline'});
    log.before(timeline);timeline.append(log);
    const latest=make('button',{type:'button',class:'latest',hidden:true},'Latest messages ↓');
    timeline.append(latest);
    let unreadReply=false;
    const nearLatest=()=>log.scrollHeight-log.scrollTop-log.clientHeight<100;
    function updateLatest(){
      const near=nearLatest();
      if(near)unreadReply=false;
      latest.hidden=!cursor||near;
      latest.classList.toggle('has-reply',unreadReply);
      latest.textContent=unreadReply?'New reply ↓':'Latest messages ↓';
    }
    function scrollToLatest(smooth=false){
      log.scrollTo({top:log.scrollHeight,behavior:smooth&&!reducedMotion.matches?'smooth':'instant'});
      if(!smooth||reducedMotion.matches)updateLatest();
    }
    log.addEventListener('scroll',updateLatest,{passive:true});
    latest.onclick=()=>{scrollToLatest(true);if(!input.disabled)input.focus({preventScroll:true});else{log.tabIndex=-1;log.focus({preventScroll:true});}};
    new ResizeObserver(updateLatest).observe(log);
    function drawAIProgress(){
      const label=stopped||closed?'':suggestionBoxAIProgress(conversation,cursor);
      if(!label){aiProgressCard?.remove();aiProgressCard=null;return;}
      if(!aiProgressCard){
        const nearBottom=nearLatest();
        aiProgressCard=make('div',{class:'ai-progress-row','aria-atomic':'true'},
          make('div',{class:'message-meta'},make('span',{class:'message-sender'},'AI assistant')),
          make('div',{class:'ai-progress'},
            make('span',{class:'ai-progress-dots','aria-hidden':'true'},make('i'),make('i'),make('i')),
            make('div',{class:'ai-progress-label'},make('span',{class:'ai-progress-size','aria-hidden':'true'},'Putting the answer together…'))));
        log.querySelector('.empty')?.remove();log.append(aiProgressCard);
        if(nearBottom)scrollToLatest();
      }
      const text=aiProgressCard.querySelector('.ai-progress-label');
      if(text.dataset.label!==label){
        const previous=text.querySelector('.stage-current');
        text.querySelector('.stage-leaving')?.remove();
        text.dataset.label=label;
        if(previous&&!reducedMotion.matches){
          previous.className='stage-leaving';previous.setAttribute('aria-hidden','true');
          previous.addEventListener('animationend',()=>previous.remove(),{once:true});
        }else previous?.remove();
        text.append(make('span',{class:'stage-current'+(previous&&!reducedMotion.matches?' stage-entering':'')},label));
      }
    }
    const contextChip=make('div',{class:'episode-context',hidden:true});
    const composerDock=make('div',{class:'composer-dock'},status,contextChip,composer);
    timeline.append(composerDock);
    new ResizeObserver(()=>{
      const height=Math.ceil(composerDock.getBoundingClientRect().height);
      if(!height||timeline.style.getPropertyValue('--sb-dock-height')===height+'px')return;
      const following=nearLatest();
      timeline.style.setProperty('--sb-dock-height',height+'px');
      if(following)scrollToLatest();else updateLatest();
    }).observe(composerDock);
    function drawContext(){contextChip.hidden=!currentContext&&!contextProvider;contextChip.textContent=contextChip.hidden?'':'About this episode';}
    drawContext();
    function link(text,url){try{const value=new URL(url);if(!['https:','http:'].includes(value.protocol)||value.username||value.password)return null;return make('a',{href:value.href,target:'_blank',rel:'noopener noreferrer',referrerpolicy:'no-referrer'},text+' ↗');}catch{return null;}}
    function action(text,fn,kind='secondary') {const b=make('button',{type:'button',class:kind},text);b.onclick=async()=>{if(b.disabled)return;b.disabled=true;b.setAttribute('aria-busy','true');try{await fn();}catch(error){if(!error.stale)showError(error);}finally{b.removeAttribute('aria-busy');if(b.isConnected)b.disabled=false;updateControls();}};return b;}
    function showError(error){if(error.stale)return;status.textContent=error.message||'Unable to connect. Please try again.';if([401,403,404].includes(error.status)&&session){stopped=true;status.textContent+=' Open Chat options to start again.';}updateControls();}
    function path(suffix=''){return `/chat/conversations/${session.conversation.id}${suffix}`;}
    try{session=JSON.parse(localStorage.getItem(key));}catch{}
    if(session&&(!session.token||typeof session.token!=='string'||!session.conversation?.id||!Number.isFinite(Date.parse(session.expires_at))||Date.parse(session.expires_at)<=Date.now()))session=null;
    chosen=!!session;
    function save(){try{if(session)localStorage.setItem(key,JSON.stringify({token:session.token,expires_at:session.expires_at,conversation:{id:session.conversation.id}}));else localStorage.removeItem(key);}catch{}}
    async function api(pathname,method='GET',body,authenticated=true){
      const generation=epoch, token=session?.token, headers={Accept:'application/json'};
      if(body!==undefined)headers['Content-Type']='application/json';
      if(authenticated&&token)headers['X-Chat-Token']=token;
      const response=await fetch(`${server}/api/v1${pathname}`,{method,headers,body:body===undefined?undefined:JSON.stringify(body),credentials:'omit',signal:AbortSignal.timeout(15000)});
      if(authenticated&&(generation!==epoch||token!==session?.token)){const error=new Error('This chat has changed.');error.stale=true;throw error;}
      if(response.status===204)return null;
      const data=await response.json();
      if(authenticated&&(generation!==epoch||token!==session?.token)){const error=new Error('This chat has changed.');error.stale=true;throw error;}
      if(!response.ok){const error=new Error(data.error?.message||'Unable to connect. Please try again.');error.status=response.status;error.code=data.error?.code;throw error;}
      return data;
    }
    function supportHours(){return config?.business_hours;}
    function nextOpening(){
      const hours=supportHours();
      return hours?.next_open_at?new Date(hours.next_open_at).toLocaleString([],{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZone:hours.timezone,timeZoneName:'short'}):null;
    }
    function availabilityText(){
      const hours=supportHours();
      if(hours?.enabled)return hours.is_open?'Within support hours · Replies may take time':nextOpening()?'Human support opens '+nextOpening():'No live support hours scheduled';
      return config?.response_time?.label||'Support team';
    }
    function drawHours(){
      if(!contactOptions){contactOptions=make('div',{});$('.chat-options').append(contactOptions);}
      const savedContact=conversation?.follow_up_email;
      const contactSig=JSON.stringify([savedContact,conversation?.contact_version,stopped]);
      if(contactOptions.dataset.signature!==contactSig){
        contactOptions.dataset.signature=contactSig;
        replace(contactOptions,...(savedContact&&!stopped?[help('Follow-up email: '+savedContact),action('Remove follow-up email',async()=>applyState(await api(path('/contact/remove'),'POST',{version:conversation.contact_version})))]:[]));
      }
      const hours=supportHours(), human=conversation?.support_mode!=='ai' && (!config?.ai_available||chosen);
      const signature=JSON.stringify([hours?.enabled,hours?.is_open,hours?.next_open_at,hours?.timezone,hours?.days,hours?.collect_email,human,closed,stopped,conversation?.follow_up_email,conversation?.contact_version]);
      if(signature===hoursSignature)return;hoursSignature=signature;
      if(!hours?.enabled||!human||closed||stopped){hoursCard?.remove();hoursCard=null;return;}
      const focus=shadow.activeElement===contactInput;
      if(!hoursCard){log.querySelector('.empty')?.remove();hoursCard=make('article',{class:'activity-card hours-card'});insertTimeline(hoursCard,cursor+0.25);log.scrollTop=log.scrollHeight;}
      const schedule=make('details',{},make('summary',{},'View business hours · '+hours.timezone),
        ...hours.days.map((day,i)=>help(['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][i]+' · '+(day.map(w=>w.start+'–'+w.end).join(', ')||'Closed'))));
      replace(hoursCard,make('h3',{},hours.is_open?'You’re within our support hours':'Our team is outside support hours'),
        help(hours.is_open?'Leave a message below. A team member will reply here when available.':(nextOpening()?'Our next support hours start '+nextOpening()+'. ':'')+'You can leave a message below for the team to review when available.'),schedule);
      if(conversation?.follow_up_email){
        hoursCard.append(help('Follow-up email saved: '+conversation.follow_up_email),action('Remove saved email',async()=>{applyState(await api(path('/contact/remove'),'POST',{version:conversation.contact_version}));}));
      }
      if(hours.collect_email&&!hours.is_open&&!conversation?.follow_up_email){
        hoursCard.append(make('label',{},help('Optional email for follow-up'),contactInput),help('Sent with your next message. Shared privately with the team for manual follow-up; no automated email. Without an email, return to this browser to check replies.'));
      }
      if(focus&&contactInput.isConnected)contactInput.focus({preventScroll:true});
    }
    function connectedStatus(){
      if(stopped)return 'This chat is no longer available. Open Chat options to start again.';
      if(closed)return 'This conversation is closed. You can still review its reports. Open Chat options to start again.';
      if(config?.ai_available&&!chosen)return '';
      if(conversation?.support_mode==='waiting_human'&&supportHours()?.enabled&&!supportHours().is_open)return 'Your conversation is waiting for the team’s next support hours.';
      if(conversation?.support_mode==='waiting_human')return conversation.ai_status==='failed'?'AI could not reply, so this conversation is waiting for a person. The team can see it; a reply is not guaranteed immediately.':'Waiting for a person. The team can see this conversation; a reply is not guaranteed immediately.';
      if(conversation?.support_mode==='ai'){
        if(!config?.ai_available)return 'AI is unavailable. Choose a person to continue.';
        if(suggestionBoxAIProgress(conversation,cursor))return '';
        if(conversation.ai_status==='failed')return 'AI could not reply. Try your question again or choose a person.';
        if(conversation.ai_status==='cancelled')return 'The AI reply was cancelled. You can send another question or choose a person.';
        return '';
      }
      return '';
    }
    function syncWelcome(){
      const empty=log.querySelector('.empty');
      const nested=empty&&!chosen&&config?.ai_available&&!closed&&!stopped;
      const target=nested?empty:panel;
      if(modeBox.parentNode!==target){if(nested)empty.append(modeBox);else panel.insertBefore(modeBox,timeline);}
      if(empty){
        const heading=empty.querySelector('.welcome-title');
        if(heading)heading.textContent=title||config?.name||'Support';
      }
    }
    function updateControls(){
      drawAIProgress();
      syncWelcome();
      const needsChoice=config?.ai_available&&!chosen;
      composer.hidden=!!needsChoice;
      const aiUnavailable=conversation?.support_mode==='ai'&&!config?.ai_available;
      send.disabled=busy||closed||stopped||!config||needsChoice||aiUnavailable||!input.value.trim();
      input.disabled=closed||stopped||needsChoice||aiUnavailable||!config;
      reset.disabled=busy;contactInput.disabled=busy||closed||stopped;
      for(const b of modeBox.querySelectorAll('button'))b.disabled=busy||stopped||closed;
      if(detail){for(const b of detail.querySelectorAll('[data-write]'))b.disabled=stopped||busy||detailStale;}
    }
    function drawMode(){
      drawHours();
      const signature=JSON.stringify([config?.ai_available,conversation?.support_mode,chosen,closed,stopped,availabilityText(),conversation?.ai_status==='failed']);
      $('.response').textContent=conversation?.support_mode==='ai'?'AI assistant':availabilityText();
      if(signature===modeSignature){updateControls();return;}modeSignature=signature;
      const focus=shadow.activeElement?.closest('.mode')?shadow.activeElement.textContent:null;
      if(!config){replace(modeBox,help('Support is unavailable right now.'),action('Try connecting again',refreshConfig));return;}
      const mode=conversation?.support_mode;
      if(!session&&!config.ai_available){replace(modeBox);modeBox.hidden=true;updateControls();return;}
      modeBox.hidden=false;
      modeBox.classList.toggle('compact',chosen);
      replace(modeBox,
        !chosen&&config.ai_available?help('Choose AI for answers with sources, or leave a message for the team.'):null,
        chosen?help(mode==='ai'?'Answers with sources':mode==='waiting_human'?'Waiting for the team':'Human support'):null,
        actions(config.ai_available&&mode!=='ai'?action('Talk to AI',()=>chooseMode('ai')):null,
          mode!=='waiting_human'&&(!chosen||mode==='ai')?action('Talk to a person',()=>chooseMode('waiting_human')):null,
          mode==='ai'&&conversation?.ai_status==='failed'?action('Try last question',()=>{if(input.value.trim()){status.textContent='Your unsent message is still in the editor.';return;}input.value=lastQuestion;resizeInput();input.focus();status.textContent='Review your question, then choose Send.';}):null),
        !chosen&&config.ai_available?make('p',{class:'help mode-disclosure'},'AI messages are processed by our AI provider. Relevant details may be sent to connected services.'):null);
      updateControls();if(focus)[...modeBox.querySelectorAll('button')].find(b=>b.textContent===focus)?.focus({preventScroll:true});
    }
    async function refreshConfig(){
      const data=await api(`/chat/projects/${encodeURIComponent(project)}/config`,'GET',undefined,false);
      config=data;lastConfig=Date.now();
      if(!title){$('h2').textContent=data.name;panel.setAttribute('aria-label',data.name);}
      $('.response').textContent=availabilityText();drawMode();
      return data;
    }
    async function ensureSession(){
      if(session)return;
      const generation=epoch;
      const value=await api(`/chat/projects/${encodeURIComponent(project)}/sessions`,'POST',undefined,false);
      if(generation!==epoch){const error=new Error('This chat has changed.');error.stale=true;throw error;}
      session=value;conversation=value.conversation;save();stopped=false;closed=conversation.state==='closed';
    }
    async function chooseMode(mode){
      if(busy||closed||stopped)return;busy=true;updateControls();
      try{await ensureSession();const current=await api(path());const result=await api(path('/support-mode'),'PUT',{mode,mode_version:current.mode_version});chosen=true;applyState(result);await refreshActivity();if(!socket)connect();}
      finally{busy=false;updateControls();if(!input.disabled)input.focus();}
    }
    function sourceReferences(citations){
      const sources=make('details',{class:'sources'},make('summary',{},make('span',{class:'sources-label'},'Sources'),make('span',{class:'sources-count'},citations.length)));
      const cards=make('div',{class:'source-cards'});
      for(const [index,citation] of citations.entries()){
        const title=citation.title?.trim()||'Support documentation';
        const destination=citation.url?link(title,citation.url):null;
        if(destination){destination.className='source-title';destination.setAttribute('aria-label',title+' (opens in a new tab)');}
        const origin=destination?new URL(destination.href).hostname.replace(/^www\./,''):'Support library';
        const card=make('article',{class:'source'},make('div',{class:'source-heading'},make('span',{class:'source-number','aria-hidden':'true'},index+1),make('div',{class:'source-identity'},destination||make('strong',{class:'source-title'},title),make('span',{class:'source-origin'},origin))));
        let excerpt=(citation.excerpt||'').trim();
        // Avoid repeating the document heading; source text always remains inert.
        const first=excerpt.split('\n')[0];
        if(first.replace(/^#{1,6}\s+/,'').trim()===title)excerpt=excerpt.slice(first.length).trim();
        if(excerpt){
          const preview=excerpt.replace(/\s+/g,' '),long=preview.length>180;
          const id='source-excerpt-'+crypto.randomUUID();
          const quote=make('p',{class:'source-excerpt',id},long?preview.slice(0,180).replace(/\s+\S*$/,'')+'…':excerpt);
          card.append(quote);
          if(long){const toggle=action('Read more',()=>{const expanded=toggle.getAttribute('aria-expanded')!=='true';toggle.setAttribute('aria-expanded',String(expanded));toggle.setAttribute('aria-label',(expanded?'Collapse excerpt from ':'Read full excerpt from ')+title);toggle.textContent=expanded?'Show less':'Read more';quote.textContent=expanded?excerpt:preview.slice(0,180).replace(/\s+\S*$/,'')+'…';},'source-expand');toggle.setAttribute('aria-expanded','false');toggle.setAttribute('aria-controls',id);toggle.setAttribute('aria-label','Read full excerpt from '+title);card.append(toggle);}
        }
        if(citation.resource_kind==='podcast_episode'&&Number.isFinite(citation.start_seconds)&&citation.timing_kind!=='unavailable'){
          const time=Math.floor(citation.start_seconds),label=(citation.timing_kind==='approximate'?'About ':'')+Math.floor(time/60)+':'+String(time%60).padStart(2,'0');
          const seek=action('Listen at '+label,()=>{
            const detail={episodeId:citation.resource_id,seconds:citation.start_seconds,url:destination?.href||null};
            const event=new CustomEvent('supaknock:seek',{detail,bubbles:true,composed:true,cancelable:true});
            let handled=false;
            try{handled=options.onSourceActivate?.({kind:'podcast_episode',episodeId:citation.resource_id,seconds:citation.start_seconds,url:destination?.href||null})===true;}catch(_){}
            if(!handled)host.dispatchEvent(event);
            if(!handled&&!event.defaultPrevented&&destination)window.open(destination.href,'_blank','noopener,noreferrer');
          },'source-expand');
          card.append(seek);
        }
        cards.append(card);
      }
      sources.append(cards);return sources;
    }
    function message(item,animate=false){
      if(log.querySelector(`[data-sequence="${item.sequence}"]`))return;
      log.querySelector('.empty')?.remove();
      const sender=item.sender==='visitor'?'You':item.sender==='ai'?'AI assistant':'Team';
      const date=new Date(item.created_at);
      const bubble=make('div',{class:'bubble '+(item.sender==='visitor'?'visitor':item.sender==='ai'?'ai':'team')},
        make('span',{class:'message-copy'},item.body));
      const node=make('div',{class:'message '+(item.sender==='visitor'?'outgoing':'incoming')+(animate?' is-new':''),'data-sequence':item.sequence,'data-order':item.sequence},
        make('div',{class:'message-meta'},make('span',{class:'message-sender'},sender),
          make('span',{'aria-hidden':'true',class:'message-separator'},'·'),
          make('time',{datetime:item.created_at,title:date.toLocaleString()},date.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}))));
      if(item.sender==='visitor')lastQuestion=item.body;
      if(item.context?.resource?.kind==='podcast_episode'){
        const context='Episode · '+((item.tool_sources||[]).find(s=>s.resource_kind==='podcast_episode')?.title||'Question about an episode');
        node.append(make('div',{class:'message-context',title:context},context));
      }
      node.append(bubble);
      const sources=[...(item.citations||[]),...(item.tool_sources||[])];
      if(item.sender==='ai'&&sources.length)bubble.append(sourceReferences(sources));
      const nearBottom=nearLatest();
      insertTimeline(node,item.sequence);
      if(nearBottom||(animate&&item.sender==='visitor'))scrollToLatest();
      else if(animate&&item.sender!=='visitor')unreadReply=true;
      while(log.querySelector(`[data-sequence="${cursor+1}"]`))cursor++;
      drawAIProgress();updateLatest();
    }
    async function markRead(){if(open&&session&&cursor)await api(path('/read'),'POST',{sequence:cursor});}
    async function history(){const initial=cursor===0;let more=true;while(more){const page=await api(path(`/messages?after=${cursor}&limit=100`));page.items.forEach(item=>message(item,!initial));more=page.has_more;}if(initial)scrollToLatest();await markRead();}
    function applyState(value){
      const activityChanged=!conversation||conversation.version!==value.version||conversation.mode_version!==value.mode_version||conversation.last_sequence!==value.last_sequence;
      conversation=value;closed=value.state==='closed';drawMode();status.textContent=connectedStatus();
      if(open&&activityChanged)refreshActivity().catch(error=>{if(!error.stale)showError(error);});
    }
    const reviewSignature=suggestionBoxReviewSignature;
    async function refreshActivity(){
      if(!session||stopped||refreshing)return;refreshing=true;
      try{
        const [drafts,reports]=await Promise.all([api(path(`/proposals?limit=25&offset=${draftOffset}`)),api(path(`/reports?limit=25&offset=${reportOffset}`))]);
        activity={drafts,reports};renderActivityList();
        if(detailValue){
          const observed=detailValue, observedKind=detailKind, observedNode=detail;
          const latest=await api(path('/'+(observedKind==='draft'?'proposals':'reports')+'/'+observed.id));
          if(detailValue===observed && detail===observedNode && reviewSignature(latest,observedKind)!==reviewSignature(observed,observedKind)){
            detailStale=true;
            if(observedKind==='report'&&!detailEditing&&!busy){renderReview('report',latest);status.textContent='Report status updated.';}
            else {const warning=detail.querySelector('.review-warning');warning.textContent=detailEditing?'This item changed. Your edits are kept below. Refresh this review before saving.':'This draft changed. Refresh this review before submitting.';warning.hidden=false;}
            const destination=detail.querySelector('.feedback-destination');replace(destination,link('Open feedback page',latest.feedback_url));
          }
        }
        updateControls();
      }finally{refreshing=false;}
    }
    function reportLabel(r){if(r.deleted)return 'Deleted';if(r.moderation==='pending')return 'Awaiting moderation · private';if(r.moderation==='rejected')return 'Not approved for publication';return 'Accepted · '+({under_consideration:'Under consideration',planned:'Planned',in_progress:'In progress',shipped:'Shipped'}[r.status]||'Published');}
    function insertTimeline(node,order){
      node.dataset.order=order;
      const next=[...log.children].find(n=>n!==node&&Number(n.dataset.order)>order);
      if(node.parentNode!==log||node.nextElementSibling!==(next||null))log.insertBefore(node,next||null);
    }
    function upsertReport(value,kind){
      let card=reportCards.get(value.id);
      if(!card){card=make('article',{class:'activity-card','data-report':value.id},make('div',{class:'report-summary'}),make('div',{class:'report-detail'}));reportCards.set(value.id,card);}
      card.dataset.kind=kind;
      const summary=card.querySelector('.report-summary');
      if(card.dataset.signature!==reviewSignature(value,kind)){
        card.dataset.signature=reviewSignature(value,kind);
        replace(summary,make('h3',{},value.kind==='bug'?'Bug report':'Feature request'),make('strong',{},value.title),help(kind==='draft'?(value.state==='pending'?'Draft · not submitted':value.state==='confirmed'?'Submitted':value.state==='cancelled'?'Cancelled':value.state):reportLabel(value)),make('p',{class:'report-preview'},value.description),action(kind==='draft'&&value.state!=='confirmed'?'Review draft':'View report',()=>openReview(value.state==='confirmed'?'report':kind,value.id)));
      }
      insertTimeline(card,(value.message_sequence||cursor)+0.5);
      return card;
    }
    function renderActivityList(){
      const signature=JSON.stringify(activity);if(signature===activitySignature)return;activitySignature=signature;
      const nearBottom=nearLatest();
      for(const d of activity.drafts.items)if(!activity.reports.items.some(r=>r.id===d.id)&&reportCards.get(d.id)?.dataset.kind!=='report')upsertReport(d,'draft');
      for(const r of activity.reports.items)upsertReport(r,'report');
      const pageControls=(kind,page)=>actions(page.offset?action('Newer '+kind,async()=>{if(kind==='drafts')draftOffset=Math.max(0,draftOffset-25);else reportOffset=Math.max(0,reportOffset-25);await refreshActivity();}):null,page.has_more?action('Earlier '+kind,async()=>{if(kind==='drafts')draftOffset+=25;else reportOffset+=25;await refreshActivity();}):null);
      replace(activityList,pageControls('drafts',activity.drafts),pageControls('reports',activity.reports));
      if(!activityList.textContent)replace(activityList);
      if(nearBottom&&!detailEditing)log.scrollTop=log.scrollHeight;
    }
    function feedback(value){return make('div',{class:'feedback-destination'},link('Open feedback page',value.feedback_url));}
    async function copyText(text){try{await navigator.clipboard.writeText(text);status.textContent='Draft copied. Nothing was submitted or transferred to another site.';}catch{const area=make('textarea',{class:'copy-area',readonly:true,'aria-label':'Select and copy this text',value:text});detail.append(area);area.focus();area.select();status.textContent='Select and copy the text shown below.';}}
    function reviewField(caption,name,value,maximum){return make('label',{},caption,make(name==='title'?'input':'textarea',{name,value,required:name==='title',maxlength:maximum,rows:name==='title'?null:7}));}
    async function openReview(kind,id){
      if(detailEditing&&!await inlineConfirm('Leave unsaved edits?','Your changes have not been saved.','Leave edits'))return;
      const value=await api(path('/'+(kind==='draft'?'proposals':'reports')+'/'+id));renderReview(kind,value);detail.scrollIntoView({block:'start'});detail.focus({preventScroll:true});
    }
    function renderReview(kind,value){
      if(detailBox){replace(detailBox);detailBox.parentElement.querySelector('.report-summary').hidden=false;}
      const card=upsertReport(value,kind);card.querySelector('.report-summary').hidden=true;detailBox=card.querySelector('.report-detail');
      detailKind=kind;detailValue=value;detailStale=false;detailEditing=false;
      detail=make('section',{class:'review',tabindex:'-1','aria-label':kind==='draft'?'Review draft':'Report status'});
      const warning=make('p',{class:'review-warning error',role:'alert',hidden:true});
      const reload=action('Refresh this review',async()=>{if(detailEditing&&!await inlineConfirm('Discard unsaved edits?','Reloading will replace your unsaved edits with the current saved text.','Refresh review'))return;await openReview(kind,value.id);});
      detail.append(make('div',{},actions(action('← Back to messages',async()=>{if(detailEditing&&!await inlineConfirm('Leave unsaved edits?','Your changes have not been saved.','Leave edits'))return;replace(detailBox);card.querySelector('.report-summary').hidden=false;detail=detailValue=detailBox=null;detailEditing=false;card.querySelector('button')?.focus();})),make('h3',{},kind==='draft'?'Review before submitting':'Report submitted'),warning,
        make('h4',{},value.title),make('p',{class:'plain'},value.description||'No description'),
        kind==='draft'?help('Destination: '+value.board.name+' · '+(value.kind==='bug'?'Bug report':'Feature request')):help(reportLabel(value)),
        kind==='draft'?help('Submission is private while awaiting moderation. The team decides whether to publish it.'):null,
        value.tracking_notice?make('p',{class:'tracking'},value.tracking_notice):null,feedback(value)));
      const buttons=actions();buttons.classList.add("review-actions");
      if(kind==='draft'){
        if(value.state==='confirmed'&&value.report_id)buttons.append(action('View submitted report',()=>openReview('report',value.report_id)));
        buttons.append(action('Copy draft',()=>copyText(value.copy_text||value.title+'\n\n'+value.description)));
        if(value.state==='pending'&&value.can_edit)buttons.append(writeButton('Edit draft',()=>editReview(kind,value)));
        if(value.state==='pending'&&value.can_confirm)buttons.append(writeButton(value.kind==='bug'?'Submit bug report':'Submit feature request',()=>submitDraft(value),'primary'));
        else if(value.state==='pending')detail.append(help('This draft cannot be submitted from this chat. Copy it and use the feedback page to submit it yourself. Signing in here does not link this guest chat to an account.'));
        if(value.state==='pending'&&value.can_edit)buttons.append(writeButton('Cancel draft',async()=>{const current=await api(path('/proposals/'+value.id+'/cancel'),'POST',{version:value.version});renderReview(kind,current);await refreshActivity();status.textContent='Draft cancelled. Nothing was submitted.';}));
        if(value.expires_at)detail.append(help('Draft expires '+new Date(value.expires_at).toLocaleString()+'. Sending another message or changing support mode may invalidate it.'));
      }else{
        if(value.public_url)detail.append(link('Open published request',value.public_url));
        if(value.accepted_at)detail.append(help('This report was accepted. Its creator can no longer edit or delete it, even if its status changes later.'));
        if(!value.deleted&&!value.accepted_at&&value.can_edit)buttons.append(writeButton('Edit report',()=>editReview(kind,value)));
        if(!value.deleted&&!value.accepted_at&&value.can_delete)buttons.append(writeButton('Delete report',async()=>{
          if(!await inlineConfirm('Delete this report?','Your report will be removed. Its private receipt remains in this chat.','Delete report'))return;
          const current=await api(path('/reports/'+value.id+'?version='+value.version),'DELETE');renderReview(kind,current);await refreshActivity();status.textContent='Report deleted.';
        }));
      }
      detail.append(buttons,make('details',{},make('summary',{},'More options'),reload));replace(detailBox,detail);updateControls();
    }
    function writeButton(text,fn,kind='secondary'){const b=action(text,async()=>{if(busy)return;if(detailStale)throw new Error('Refresh this review before continuing.');busy=true;updateControls();try{await fn();}catch(error){if(error.status===409){detailStale=true;const warning=detail.querySelector('.review-warning');warning.hidden=false;warning.textContent=detailEditing?'This item changed. Your edits have been kept. Refresh this review to continue.':'This item changed. Refresh this review to see its current state.';}throw error;}finally{busy=false;updateControls();if(detail&&!shadow.activeElement)detail.focus({preventScroll:true});}},kind);b.dataset.write='true';return b;}
    function editReview(kind,value){
      detailEditing=true;
      const editor=make('form',{class:'report-editor'},reviewField('Title','title',value.title,200),reviewField('Description','description',value.description,20000),help(kind==='draft'?'Save your changes first. A draft still needs a separate submission step.':'Saving updates this submitted report and does not create another submission. Creator edits are permanently locked after acceptance.'),make('p',{class:'error',role:'alert'}),actions(make('button',{type:'submit','data-write':'true'},'Save changes'),action('Discard edits',()=>renderReview(kind,value))));
      detail.querySelector('.review-actions')?.remove();detail.append(editor);
      editor.onsubmit=async event=>{event.preventDefault();if(busy||detailStale||stopped)return;const values=new FormData(editor);busy=true;updateControls();try{const updated=await api(path('/'+(kind==='draft'?'proposals':'reports')+'/'+value.id),'PUT',{version:value.version,title:values.get('title'),description:values.get('description')});renderReview(kind,updated);await refreshActivity();status.textContent=kind==='draft'?'Draft saved. Review it again before submitting.':'Report updated.';}catch(error){editor.querySelector('.error').textContent=error.status===409?'This item changed. Your edits are still here; refresh the review to continue.':error.message;if(error.status===409)detailStale=true;}finally{busy=false;updateControls();if(detail&&!shadow.activeElement)detail.focus({preventScroll:true});}};
      editor.querySelector('input').focus();updateControls();
    }
    async function submitDraft(value){
      const current=await api(path('/proposals/'+value.id));
      if(current.state==='confirmed'&&current.report_id){const receipt=await api(path('/reports/'+current.report_id));renderReview('report',receipt);await refreshActivity();status.textContent='This report was already submitted. Its saved receipt is shown.';return;}
      if(reviewSignature(current,'draft')!==reviewSignature(value,'draft')||!current.can_confirm){renderReview('draft',current);throw new Error('The draft or its permissions changed. Review the current version before submitting.');}
      const signature=value.id+':'+value.version+':'+value.draft_hash;
      if(!confirmationIds.has(signature))confirmationIds.set(signature,crypto.randomUUID());
      const receipt=await api(path('/proposals/'+value.id+'/confirm'),'POST',{version:value.version,draft_hash:value.draft_hash,client_request_id:confirmationIds.get(signature)});
      renderReview('report',receipt);await refreshActivity();status.textContent='Report submitted and awaiting moderation. Its private status is shown here.';
    }
    function inlineConfirm(title,text,caption){
      confirmationClose?.(false);
      return new Promise(resolve=>{
        const previous=shadow.activeElement, area=make('section',{class:'inline-confirm',role:'alertdialog',tabindex:'-1','aria-label':title},make('h3',{},title),make('p',{},text));
        function finish(result){area.remove();if(confirmationClose===finish)confirmationClose=null;resolve(result);setTimeout(()=>{if(!confirmationClose){const target=previous?.isConnected&&!previous.disabled&&!previous.closest('[hidden]')?previous:$('.options-toggle');target.focus({preventScroll:true});}},0);}
        confirmationClose=finish;area.append(actions(action('Cancel',()=>finish(false)),action(caption,()=>finish(true),'primary')));
        area.addEventListener('keydown',event=>{if(event.key==='Escape'){event.stopPropagation();event.preventDefault();finish(false);}});
        $('.chat-options').hidden=true;$('.options-toggle').setAttribute('aria-expanded','false');
        panel.insertBefore(area,timeline);area.focus({preventScroll:true});
      });
    }
    $('.options-toggle').onclick=()=>{const box=$('.chat-options');box.hidden=!box.hidden;$('.options-toggle').setAttribute('aria-expanded',String(!box.hidden));};
    function resizeInput(){input.style.height='auto';const maximum=parseFloat(getComputedStyle(input).maxHeight)||144;input.style.height=Math.min(maximum,input.scrollHeight)+'px';input.style.overflowY=input.scrollHeight>maximum?'auto':'hidden';updateControls();}
    async function poll(){
      clearTimeout(pollTimer);pollTimer=null;if(!open||stopped)return;
      if(!document.hidden){try{if(Date.now()-lastConfig>15000)await refreshConfig();if(session){if(!socket)await history();applyState(await api(path()));await refreshActivity();}}catch(error){if(!error.stale)showError(error);}}
      if(open&&!stopped)pollTimer=setTimeout(poll,5000);
    }
    async function connect(){
      if(stopped||!open||!session||connecting||socket)return;
      connecting=true;clearTimeout(timer);const generation=epoch;
      const placeholder=cursor===0?make('div',{class:'history-loading'},make('p',{},'Loading conversation…'),make('div',{class:'history-skeleton','aria-hidden':'true'}),make('div',{class:'history-skeleton','aria-hidden':'true'})):null;
      const emptyState=log.querySelector('.empty');
      if(placeholder){if(emptyState)emptyState.hidden=true;log.append(placeholder);log.setAttribute('aria-busy','true');}
      try{
        await history();applyState(await api(path()));await refreshActivity();
        const ticket=await api('/chat/tickets','POST',{conversation_id:session.conversation.id});
        if(!open||stopped||generation!==epoch)return;
        const ws=new WebSocket(`${server.replace(/^http/,'ws')}/api/v1/chat/ws`);socket=ws;
        const current=()=>socket===ws&&generation===epoch;
        ws.onopen=()=>{if(current()){ws.send(JSON.stringify({ticket:ticket.ticket,after:cursor}));retry=1000;}};
        ws.onmessage=event=>{if(!current())return;let frame;try{frame=JSON.parse(event.data);}catch{return;}
          if(frame.type==='message'){message(frame.message,true);status.textContent=connectedStatus();markRead().catch(()=>{});refreshActivity().catch(showError);}
          else if(frame.type==='state')applyState(frame.conversation);
          else if(frame.type==='ready')refreshActivity().catch(showError);
          else if(frame.type==='ping')ws.send(JSON.stringify({type:'pong'}));
          else if(frame.type==='typing'&&frame.sender==='admin'){status.textContent='Someone on the team is typing…';setTimeout(()=>{if(current())status.textContent=connectedStatus();},3000);}
          else if(frame.type==='error')status.textContent=frame.error.message;
        };
        ws.onclose=()=>{if(!current())return;socket=null;if(open&&!stopped){status.textContent='Reconnecting… Your messages and reports are saved.';timer=setTimeout(connect,retry);retry=Math.min(retry*2,30000);}};
        ws.onerror=()=>{};
      }catch(error){if(error.stale)return;showError(error);if(open&&!stopped){timer=setTimeout(connect,retry);retry=Math.min(retry*2,30000);}}
      finally{placeholder?.remove();if(emptyState?.isConnected)emptyState.hidden=false;if(generation===epoch){connecting=false;log.setAttribute('aria-busy','false');}if(open&&session&&!pollTimer)pollTimer=setTimeout(poll,5000);}
    }
    function finishHide(){if(!open){if(topLayer&&panel.matches(':popover-open'))panel.hidePopover();panel.hidden=true;clearTimeout(hideTimer);hideTimer=null;}}
    async function show(){
      if(open)return;revealLauncher();open=true;clearTimeout(hideTimer);panel.hidden=false;panel.inert=false;panel.removeAttribute('aria-hidden');if(topLayer)panel.showPopover();void panel.offsetWidth;
      panel.classList.add('is-open');launcher.hidden=presentation!=='inline';launcher.setAttribute('aria-expanded','true');
      try{await refreshConfig();if(!open)return;if(session)connect();if(!pollTimer)pollTimer=setTimeout(poll,5000);status.textContent=connectedStatus();}catch(error){showError(error);}
      if(open)(input.disabled?modeBox.querySelector('button')||$('.close'):input).focus({preventScroll:true});
    }
    function hide(){if(!open)return;confirmationClose?.(false);open=false;launcher.hidden=false;launcher.setAttribute('aria-expanded','false');launcher.focus({preventScroll:true});panel.inert=true;panel.setAttribute('aria-hidden','true');panel.classList.remove('is-open');clearTimeout(timer);clearTimeout(pollTimer);pollTimer=null;if(socket)socket.close();if(reducedMotion.matches)finishHide();else hideTimer=setTimeout(finishHide,280);}
    panel.addEventListener('transitionend',event=>{if(event.target===panel&&event.propertyName==='transform')finishHide();});
    launcher.onclick=()=>open?hide():show();$('.close').onclick=hide;
    shadow.addEventListener('keydown',event=>{if(event.key==='Escape'&&open){event.preventDefault();event.stopPropagation();const box=$('.chat-options');if(!box.hidden){box.hidden=true;$('.options-toggle').setAttribute('aria-expanded','false');$('.options-toggle').focus({preventScroll:true});}else hide();}});
    input.addEventListener('input',()=>{resizeInput();if(socket?.readyState===WebSocket.OPEN&&Date.now()-lastTyping>=2000){socket.send(JSON.stringify({type:'typing'}));lastTyping=Date.now();}});
    input.title='Enter to send. Shift+Enter for a new line.';
    input.setAttribute('aria-description',input.title);
    suggestionBoxComposerKeyboard(input,composer,send);
    composer.onsubmit=async event=>{
      event.preventDefault();if(send.disabled||!input.value.trim())return;
      const email=contactInput.isConnected&&supportHours()?.collect_email&&conversation?.support_mode!=='ai'&&contactInput.value.trim()?contactInput.value.trim():null;
      if(email&&!contactInput.reportValidity())return;
      busy=true;updateControls();send.setAttribute('aria-label','Sending message');send.setAttribute('aria-busy','true');const body=input.value;
      try{
      if(!pending||pending.body!==body||pending.follow_up_email!==email)pending={client_message_id:crypto.randomUUID(),body,follow_up_email:email,context:contextSnapshot()};status.textContent='Sending…';
      await ensureSession();if(!chosen&&!config.ai_available)chosen=true;
        const item=await api(path('/messages'),'POST',pending);message(item,true);if(input.value===body)input.value='';resizeInput();pending=null;contactInput.value='';try{applyState(await api(path()));}catch(_){}status.textContent=suggestionBoxAIProgress(conversation,cursor)?'':'Message sent. Replies will appear here.';await markRead();await refreshActivity();if(!socket)connect();
      }catch(error){if(!error.stale){showError(error);status.textContent+=' Your message is still here; try Send again.';}}
      finally{busy=false;send.setAttribute('aria-label','Send message');send.removeAttribute('aria-busy');updateControls();if(open&&!input.disabled)input.focus();}
    };
    reset.onclick=async()=>{
      if(busy)return;
      if(!await inlineConfirm('End this chat on this browser?','You will lose access to this conversation and its guest report status here. Reports already submitted are not deleted.','End chat'))return;
      busy=true;updateControls();
      try{if(session)await api(path('/session'),'DELETE');}catch(error){if(![401,403,404].includes(error.status)){showError(error);busy=false;updateControls();return;}}
      epoch++;contactInput.value='';aiProgressCard?.remove();aiProgressCard=null;hoursCard=null;hoursSignature='';stopped=true;clearTimeout(timer);clearTimeout(pollTimer);pollTimer=null;const old=socket;socket=null;if(old)old.close();session=null;conversation=null;save();cursor=0;unreadReply=false;latest.hidden=true;replace(log,make('div',{class:'empty'},make('p',{class:'welcome-eyebrow'},'Get help from'),make('h3',{class:'welcome-title'},title||config?.name||'Support'),make('p',{class:'welcome-description'},'Ask a question or share what’s on your mind. We’ll reply right here.')));input.value='';lastQuestion='';pending=null;closed=false;chosen=false;busy=false;connecting=false;stopped=false;activity={drafts:{items:[],has_more:false},reports:{items:[],has_more:false}};activitySignature='';draftOffset=reportOffset=0;detail=null;detailValue=null;detailBox=null;reportCards.clear();replace(activityList);log.prepend(activityList);confirmationIds.clear();$('.chat-options').hidden=true;$('.options-toggle').setAttribute('aria-expanded','false');resizeInput();modeSignature='';drawMode();status.textContent='This browser’s chat access was cleared. Submitted reports still exist.';updateControls();
    };
    refreshConfig().catch(error=>{status.textContent='Chat is unavailable right now. Open chat to try again.';drawMode();});
    updateControls();
    const controller = {open(options={}){if(options.intent==='episode')input.placeholder='Ask about this episode…';else if(options.intent==='support')input.placeholder='How can we help?';return show();},close:hide,setTheme,setContext(value){currentContext=value;contextProvider=null;drawContext();},setContextProvider(provider){if(provider!==null&&typeof provider!=='function')throw new Error('Expected a context callback.');contextProvider=provider;drawContext();}};
    controllers.set(host, controller);
    host.dispatchEvent(new CustomEvent('supaknock:ready',{detail:{controller},bubbles:true}));
    return controller;
  }
  window.SuggestionBoxChat={mount,get:target=>controllers.get(targetElement(target))};
  // New embeds may use the product name; existing integrations keep their API.
  window.SupaKnockChat=window.SuggestionBoxChat;
  const script=document.currentScript;
  if(script?.dataset.project&&script?.dataset.server){
    let appearance = {};
    const config = script.dataset.config && document.getElementById(script.dataset.config);
    if(config?.tagName==='SCRIPT' && config.type.trim().toLowerCase()==='application/json'){
      try {
        const parsed = JSON.parse(config.textContent);
        if(parsed && typeof parsed==='object' && !Array.isArray(parsed)) appearance={launcherText:parsed.launcherText,title:parsed.title,theme:parsed.theme,presentation:parsed.presentation,revealAfterScroll:parsed.revealAfterScroll};
      } catch (_) { /* Invalid appearance configuration uses the standard widget. */ }
    }
    mount(script.dataset.target||'#suggestion-box-chat',{...appearance,server:script.dataset.server,project:script.dataset.project});
  }
})();
