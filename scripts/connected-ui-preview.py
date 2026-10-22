"""Launch isolated synthetic fixture UI; no production files or credentials."""
import sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import uvicorn
from feedback.api import create_app
from feedback.config import Config
from feedback.security import digest
from feedback.errors import Error
from connected_fixture import FixtureApp

work=Path(tempfile.mkdtemp(prefix='sb-connected-preview-'))
cfg=Config(work/'feedback.sqlite',Fernet.generate_key().decode(),'fixture-setup-token'*3,worker=False,base_url='http://127.0.0.1:8768',rate_limit=10000,auth_rate_limit=10000)
app=create_app(cfg);client=TestClient(app)
r=client.post('/api/v1/setup',json=dict(email='admin@example.test',password='synthetic-preview-password',display_name='Preview owner',setup_token=cfg.setup_token));client.headers.update({'Origin':cfg.base_url,'X-CSRF-Token':r.json()['csrf_token']})
fixtures={}
for name,n in [('Glean',130),('JoyPass',9),('Quiet app',0),('Offline demo',3)]:
    project=client.post('/api/v1/projects',json=dict(identifier=name.lower().replace(' ','-'),name=name)).json()
    url='https://'+project['identifier']+'.example';fixtures[url]=FixtureApp(project['id'],name,n)
    root='/api/v1/projects/'+project['id']+'/app-data'
    client.put(root+'/connection',json={'version':0,'base_url':url,'token':'synthetic-only'})
    principal=app.state.storage.session(digest(client.cookies['feedback_session']))
    app.state.storage.connected_change(project['id'],dict(kind='admin',user_id=principal['id'],session_id=principal['session_id']),1,'enable',dict(app_instance_id=project['id'],link_origins=[],support_linked=False))
async def transport(url,token,op,q,deadline):
    if url=='https://offline-demo.example' and op=='activity':raise Error(502,'app_unavailable','The synthetic app is offline.')
    return fixtures[url].query(op,q)
app.state.connected_apps.transport=transport
if __name__=='__main__':uvicorn.run(app,host='127.0.0.1',port=8768,access_log=False)
