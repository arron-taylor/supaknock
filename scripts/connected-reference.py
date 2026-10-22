"""Loopback-only synthetic protocol reference. Token is read from an environment variable."""
import os,secrets,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
import uvicorn
from connected_fixture import FixtureApp

app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
fixture=FixtureApp('synthetic-reference-v1','Reference')
token=os.environ.get('CONNECTED_REFERENCE_TOKEN')
if not token:raise SystemExit('Set CONNECTED_REFERENCE_TOKEN to a disposable reference token.')

@app.middleware('http')
async def authorize(request,call_next):
    if not secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+token):return JSONResponse({'error':{'code':'unauthorized'}},status_code=401)
    response=await call_next(request);response.headers['Cache-Control']='no-store';return response

@app.get('/v1/manifest')
def manifest():return fixture.query('manifest',{})

@app.post('/v1/{resource}/{action}')
async def query(resource:str,action:str,request:Request):
    operations={('summary','query'):'summary',('users','query'):'users',('users','lookup'):'profile',('activity','query'):'activity'}
    operation=operations.get((resource,action))
    if not operation:return JSONResponse({'error':{'code':'not_found'}},status_code=404)
    raw=await request.body()
    if len(raw)>65536:return JSONResponse({'error':{'code':'invalid_query'}},status_code=400)
    try:return fixture.query(operation,await request.json())
    except LookupError:return JSONResponse({'error':{'code':'not_found'}},status_code=404)
    except (KeyError,ValueError,TypeError):return JSONResponse({'error':{'code':'invalid_cursor'}},status_code=409)

if __name__=='__main__':uvicorn.run(app,host='127.0.0.1',port=8769,access_log=False)
