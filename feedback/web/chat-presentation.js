'use strict';
window.ChatPresentation = Object.freeze({
  modeLabel(value) {
    return {ai:'AI support',waiting_human:'Waiting for a human',human:'Human support'}[value] || 'Human support';
  },
  senderLabel(sender, customer) {
    return sender === 'ai' ? 'AI assistant' : sender === 'admin' ? 'You and your team' : sender === 'visitor' ? customer : 'Unknown sender';
  },
  sourceURL(value) {
    if(typeof value!=='string'||value.length>2048||/[\u0000-\u0020\u007f]/.test(value))return null;
    try {const url=new URL(value);return ['https:','http:'].includes(url.protocol)&&url.hostname&&!url.username&&!url.password?url.href:null;}catch(_){return null;}
  },
});
