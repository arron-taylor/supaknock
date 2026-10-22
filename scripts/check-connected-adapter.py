"""Bounded explicit conformance probe. Use a synthetic/staging app, not live user harvesting."""
import argparse,asyncio,json,os,sys,time
from pathlib import Path
from datetime import datetime,timezone,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from feedback.connected_apps_protocol import validated_response,UserQuery
from feedback.connected_apps_transport import public_request

async def check(url,token,loopback=False):
    async def fetch(op,q):
        if loopback:
            import httpx
            from feedback.connected_apps_protocol import PATHS
            method,path=PATHS[op]
            async with httpx.AsyncClient(timeout=10,follow_redirects=False,trust_env=False) as client:
                r=await client.request(method,url+path,headers={'Authorization':'Bearer '+token},json=q if method=='POST' else None)
                if r.status_code!=200 or len(r.content)>1024*1024:raise ValueError('Reference request failed')
                return r.json()
        return await public_request(url,token,op,q,time.monotonic()+10)
    manifest=validated_response('manifest',await fetch('manifest',{}),{})
    instance=manifest['app_instance_id'];origins=manifest['link_origins'];caps=manifest['capabilities']
    interval={'from':(datetime.now(timezone.utc)-timedelta(days=7)).isoformat().replace('+00:00','Z'),'until':datetime.now(timezone.utc).isoformat().replace('+00:00','Z')}
    checks=['manifest'];user=None
    for op in ('summary','users','activity'):
        if not caps[op]:continue
        q=interval if op=='summary' else UserQuery(limit=2).model_dump() if op=='users' else interval|{'limit':2,'user_id':None,'cursor':None}
        page=validated_response(op,await fetch(op,q),q,instance,origins);checks.append(op)
        if op=='users' and page['items']:user=page['items'][0]['id']
        if page.get('next_cursor'):
            q=q|{'cursor':page['next_cursor']};second=validated_response(op,await fetch(op,q),q,instance,origins)
            field='joined_at' if op=='users' else 'occurred_at'
            from feedback.connected_apps_protocol import instant
            if second['items'] and (instant(second['items'][0][field]),second['items'][0]['id'].encode()) >= (instant(page['items'][-1][field]),page['items'][-1]['id'].encode()):raise ValueError('Non-progressing page')
            boundary='scan_until' if op=='users' else 'snapshot'
            if second[boundary]!=page[boundary] or second['expires_at']!=page['expires_at']:raise ValueError('Changed continuation scope')
            checks.append(op+' continuation')
    if user and caps['profiles']:
        q={'user_id':user};validated_response('profile',await fetch('profile',q),q,instance,origins);checks.append('profile')
    return {'passed':checks,'note':'Bounded shape/authenticated read probe; full adapter mutation/retention tests are still required.'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('url');p.add_argument('--token-env',default='CONNECTED_REFERENCE_TOKEN');p.add_argument('--synthetic-loopback',action='store_true');args=p.parse_args()
    if args.synthetic_loopback and args.url!='http://127.0.0.1:8769':p.error('Synthetic mode only supports the fixed reference address')
    token=os.environ.get(args.token_env)
    if not token:p.error('Set the named token environment variable')
    try:print(json.dumps(asyncio.run(check(args.url,token,args.synthetic_loopback)),indent=2))
    except Exception:raise SystemExit('Conformance check failed. Check the adapter contract and staging configuration; private responses are not printed.') from None
