"""Disposable archive/restore UI fixture. No production data or model calls."""
from pathlib import Path
import runpy
import uvicorn

env=runpy.run_path(str(Path(__file__).with_name('connected-ui-preview.py')))
app,client=env['app'],env['client']
project=client.get('/api/v1/projects/glean').json()
for identifier,name in [('features','Features'),('bugs','Bugs')]:
    board=client.post('/api/v1/projects/'+project['id']+'/boards',json={'identifier':identifier,'name':name}).json()
    request=client.post('/api/v1/boards/'+board['id']+'/requests',json={'title':'Preserved '+name.lower()+' request','description':'Synthetic archive preview; this content must survive archive and restore.'}).json()
    client.post('/api/v1/requests/'+request['id']+'/accept',json={'version':request['version']})
    client.post('/api/v1/boards/'+board['id']+'/requests',json={'title':'Pending '+name.lower()+' request'})
if __name__=='__main__':uvicorn.run(app,host='127.0.0.1',port=8768,access_log=False)
