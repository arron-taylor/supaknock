"""Disposable Forms browser/native QA at localhost:8776; no external services."""
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import uvicorn
from feedback.api import create_app
from feedback.config import Config

with TemporaryDirectory(prefix='supaknock-forms-preview-') as directory:
    app=create_app(Config(Path(directory)/'feedback.sqlite',Fernet.generate_key().decode(),'forms-preview-setup-token',base_url='http://localhost:8776',worker=False))
    admin=TestClient(app)
    login=admin.post('/api/v1/setup',json={'email':'admin@example.com','password':'local-simulator-password','setup_token':'forms-preview-setup-token','display_name':'Alex'}).json()
    admin.headers.update({'Origin':'http://localhost:8776','X-CSRF-Token':login['csrf_token']})
    p=admin.post('/api/v1/projects',json={'identifier':'supaknock','name':'SupaKnock'}).json()
    admin.post('/api/v1/projects/'+p['id']+'/boards',json={'identifier':'ideas','name':'Feature requests'})
    f=admin.post('/api/v1/admin/projects/'+p['id']+'/forms',json={'name':'iPhone early access','description':'A first look at SupaKnock on your iPhone.','enabled':True,'public_enabled':True,'fields':[{'key':'email','label':'Email','type':'email','required':True},{'key':'name','label':'Name','type':'text'},{'key':'message','label':'What are you building?','type':'textarea'}]}).json()
    key=admin.post('/api/v1/projects/'+p['id']+'/keys',json={'name':'Preview backend','permissions':['forms:submit']}).json()['secret']
    backend=TestClient(app,headers={'Authorization':'Bearer '+key})
    for name,email,message in [('Sam','sam@example.com','A small reading app. I’d love to reply to support messages while I’m away from my desk.'),('Jamie','jamie@example.com','A habit tracker for our local running club.'),('Morgan','morgan@example.com','An open-source notes app for iPhone and iPad.')]:
        backend.post('/api/v1/forms/'+f['id']+'/submissions',json={'client_request_id':str(uuid4()),'answers':{'email':email,'name':name,'message':message}})
    print('Public form: http://localhost:8776/forms/'+f['id'],flush=True)
    print('Disposable Forms QA: http://localhost:8776 — admin@example.com / local-simulator-password',flush=True)
    uvicorn.run(app,host='127.0.0.1',port=8776,log_level='warning')
