"""Disposable MCP/web/native QA. Public-data fixtures only; no external services.
Open localhost:8767/mcp-preview, or sign in with admin@example.com / local-simulator-password.
"""
import sys,tempfile,json,time,asyncio,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from feedback.api import create_app
from feedback.config import Config
from feedback.security import Vault,digest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from fastapi.responses import HTMLResponse
from test_ai_mcp_generation import PodcastClient,catalog
import uvicorn
folder=tempfile.TemporaryDirectory(prefix='mcp-preview-')
app=create_app(Config(Path(folder.name)/'preview.sqlite',Fernet.generate_key().decode(),'mcp-preview-setup-token-for-tests',base_url='http://localhost:8767',worker=False))
@app.middleware('http')
async def preview_policy(request,call_next):
    response=await call_next(request)
    if request.url.path=='/mcp-preview':
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
    return response
client=TestClient(app,base_url='http://localhost:8767')
login=client.post('/api/v1/setup',json={'email':'admin@example.com','password':'local-simulator-password','setup_token':'mcp-preview-setup-token-for-tests','display_name':'Alex'}).json()
client.headers.update({'Origin':'http://localhost:8767','X-CSRF-Token':login['csrf_token']})
p=client.post('/api/v1/projects',json={'identifier':'podcasts','name':'Audiology'}).json()
client.post('/api/v1/projects/'+p['id']+'/boards',json={'identifier':'ideas','name':'Feature requests'})
app.state.ai_mcp_admin.client_factory=PodcastClient
saved=client.post('/api/v1/ai/tool-connections',json={'version':0,'label':'Audiology public transcripts','url':'https://podcasts.example/mcp','enabled':True,'link_origins':['https://podcasts.example']}).json()
client.post('/api/v1/ai/tool-connections/'+saved['id']+'/discover',json={'version':1})
client.put('/api/v1/admin/chat/projects/'+p['id'],json={'enabled':True,'origins':['http://localhost:8767'],'version':0})
# Synthetic providers only; no external inference, credentials or MCP requests.
from feedback.ai_domain import default_ai_settings
from feedback.ai_providers import GenerationResult,ToolCall,Usage
store=app.state.storage
account=store.session(digest(client.cookies['feedback_session']))
principal={'kind':'admin','user_id':account['id'],'session_id':account['session_id']}
settings=default_ai_settings();settings['model']='fixture-model';settings['daily_token_limit']=1000000
cfg=store.ai_configure(p['id'],principal,0,settings,protected_key=Vault(app.state.config.secret).protect('fixture-only'))
store.ai_store_provider_evidence(p['id'],principal,cfg['version'],cfg['credential_version'],'fixture-model',catalogue=['fixture-model'],capabilities={'text_generation':True,'tool_calling':True,'structured_output':False,'streaming':False})
store.mcp_bind(principal,p['id'],0,'tools',[{'connection_id':saved['id'],'connection_version':1,'context_kind':'podcast_episode','tools':[{'name':t['name'],'digest':t['digest']} for t in catalog()]}])
settings['enabled']=True
store.ai_configure(p['id'],principal,cfg['version'],settings)
class PreviewProvider:
    async def generate(self,key,model,messages,**kwargs):
        await asyncio.sleep(float(os.environ.get('SUPAKNOCK_PREVIEW_DELAY','0')))
        data=json.loads(messages[-1].content)
        if data.get('operation')=='evidence_check':return GenerationResult(json.dumps({'assessment':'The fixture excerpt supports this answer.','supported':True,'issues':[]}),(),Usage(10,10,20),'stop')
        if messages[-1].role=='tool':return GenerationResult(json.dumps({'status':'answered','body':'The host recommends taking a walk every morning.','citation_ids':[data['passages'][0]['id']]}),(),Usage(10,10,20),'stop')
        tool=next(t for t in kwargs['tools'] if 'query' in t.parameters['properties'])
        return GenerationResult('',(ToolCall('lookup',tool.name,{'query':'morning walk'}),),Usage(10,10,20),'tool_calls')
app.state.ai_generation.provider_factory=lambda _:PreviewProvider()
class PreviewPodcastClient(PodcastClient):
    async def call(self,tool,args):
        await asyncio.sleep(float(os.environ.get('SUPAKNOCK_PREVIEW_DELAY','0')))
        return await super().call(tool,args)
app.state.ai_mcp_tools.client_factory=PreviewPodcastClient
app.state.ai_chat_worker.start()
@app.get('/mcp-preview',response_class=HTMLResponse)
def preview(mobile:bool=False):
    if mobile:return '<!doctype html><html><body style="background:#dededb;display:grid;place-items:center;margin:20px"><iframe title="390px episode preview" src="/mcp-preview" style="width:390px;height:844px;border:0;border-radius:18px"></iframe></body></html>'
    return '''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="font:18px system-ui;background:#f5f3ef;padding:8vw"><h1>Audiology</h1><h2>Small habits, lasting change</h2><p>An episode about making room for a daily walk.</p><p id="player">Player paused at 0:00</p><button onclick="chat.open({intent:'episode'})">Ask about this episode</button><div id="chat"></div><script src="/widget.js"></script><script>const chat=SupaKnockChat.mount('#chat',{server:location.origin,project:'PROJECT_ID',context:{version:1,resource:{kind:'podcast_episode',id:'alias'}},presentation:'bar',onSourceActivate(source){if(source.episodeId!=='canonical'||source.seconds!==42)return false;document.getElementById('player').textContent='Player paused at 0:42';return true;}});</script></body></html>'''.replace('PROJECT_ID',p['id'])
print('Project '+p['id'],flush=True)
uvicorn.run(app,host='127.0.0.1',port=8767,log_level='warning')
