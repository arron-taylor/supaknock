import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const context=vm.createContext({window:{},URL});
vm.runInContext(readFileSync(new URL('../feedback/web/chat-presentation.js',import.meta.url),'utf8'),context);
const view=context.window.ChatPresentation;
test('AI is never labeled as a customer or human and legacy modes remain readable',()=>{
  assert.equal(view.senderLabel('ai','Customer'),'AI assistant');
  assert.equal(view.senderLabel('visitor','Customer'),'Customer');
  assert.equal(view.senderLabel('admin','Customer'),'You and your team');
  assert.equal(view.senderLabel('future','Customer'),'Unknown sender');
  assert.equal(view.modeLabel(undefined),'Human support');
  assert.equal(view.modeLabel('waiting_human'),'Waiting for a human');
});
test('only safe verified HTTP or HTTPS source links become navigation',()=>{
  assert.equal(view.sourceURL('https://support.example/docs?q=a#section'),'https://support.example/docs?q=a#section');
  assert.equal(view.sourceURL('http://support.example/docs'),'http://support.example/docs');
  for(const url of ['javascript:alert(1)','data:text/html,x','//example.com','ftp://example.com','https://user:secret@example.com','https://example.com/\nunsafe',null,'https://example.com/'+ 'x'.repeat(2048)])assert.equal(view.sourceURL(url),null);
});
