"""Disposable private-board browser fixture. No production data or services."""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from feedback.api import create_app
from feedback.config import Config
import uvicorn

work = tempfile.TemporaryDirectory(prefix='supaknock-private-boards-')
config = Config(Path(work.name)/'feedback.sqlite', Fernet.generate_key().decode(), 'private-board-fixture-setup',
                worker=False, base_url='http://127.0.0.1:8784', rate_limit=10000, auth_rate_limit=10000)
app = create_app(config)
admin = TestClient(app)
r = admin.post('/api/v1/setup', json={'email':'admin@example.test','password':'synthetic-preview-password','setup_token':config.setup_token})
admin.headers.update({'Origin':config.base_url,'X-CSRF-Token':r.json()['csrf_token']})
project = admin.post('/api/v1/projects',json={'identifier':'privacy-preview','name':'Privacy preview'}).json()
board = admin.post('/api/v1/projects/'+project['id']+'/boards',json={'identifier':'private-feedback','name':'Private feedback','description':'Send the team a feature request or bug report.','visibility':'private'}).json()
public = admin.post('/api/v1/projects/'+project['id']+'/boards',json={'identifier':'community','name':'Community ideas'}).json()
user = TestClient(app)
r=user.post('/api/v1/auth/register',json={'email':'reader@example.test','password':'synthetic-preview-password','display_name':'Preview reader'})
user.headers.update({'Origin':config.base_url,'X-CSRF-Token':r.json()['csrf_token']})
for client,title in [(admin,'Someone else’s confidential report'),(user,'My private request')]:
    r=client.post('/api/v1/boards/'+board['id']+'/requests',json={'title':title,'description':'Only the owner and authorized team may read this.'}).json()
    admin.post('/api/v1/requests/'+r['id']+'/accept',json={'version':r['version']})
Path('/tmp/private-board-preview.json').write_text(__import__('json').dumps({'project':project['id'],'board':board['id'],'public_board':public['id']}))
if __name__=='__main__':
    try: uvicorn.run(app,host='127.0.0.1',port=8784,access_log=False)
    finally: work.cleanup()
