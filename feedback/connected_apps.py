"""On-demand connected-app service. Only configuration is durable."""
import asyncio
import hashlib
import json
import secrets
import time
from datetime import datetime,timezone
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from feedback.connected_apps_protocol import REQUESTS,validated_response,instant,safe_origin
from feedback.connected_apps_transport import Admission,public_request,base_address
from feedback.errors import Error


def invalid():return Error(502,'app_invalid_response','The app returned data that does not match the connection protocol.')


class ConnectedApps:
    def __init__(self,storage,vault,transport=public_request):
        self.storage=storage;self.vault=vault;self.transport=transport
        self.epoch=secrets.token_urlsafe(32);self.admission=Admission()

    def seal(self,body):
        token=self.vault.protect(json.dumps(body,ensure_ascii=False,separators=(',',':')))
        if len(token)>16384:raise invalid()
        return token

    def open(self,token,scope):
        try:
            body=json.loads(self.vault.reveal(token))
            if any(body.get(k)!=v for k,v in scope.items()) or body['expires']<=time.time():raise ValueError()
            return body
        except Exception:raise Error(409,'app_browse_expired','This view has expired or changed. Refresh to start again.') from None

    def scope(self,snapshot,principal,kind):
        return dict(epoch=self.epoch,session=principal['session_id'],project=snapshot['project_id'],connection=snapshot['id'],version=snapshot['version'],kind=kind)

    def check(self,snapshot,principal,enabled=True):
        return self.storage.connected_snapshot(snapshot['project_id'],principal,snapshot['version'],enabled=enabled,connection_id=snapshot['id'])

    def save(self,project,principal,version,url,token):
        values={'base_url':base_address(url)}
        if token:
            if any(ord(x)<33 or ord(x)>126 for x in token):raise Error(400,'invalid_token','Use the app-issued bearer token without spaces')
            values['protected_token']=self.vault.protect(token)
        return self.storage.connected_change(project,principal,version,'save',values)

    async def fetch(self,snapshot,principal,operation,query,enabled=True):
        self.admission.budget(principal)
        async def call(deadline):
            token=self.vault.reveal(snapshot['protected_token'])
            raw=await self.transport(snapshot['base_url'],token,operation,query,deadline)
            self.check(snapshot,principal,enabled)
            try:return validated_response(operation,raw,query,snapshot['app_instance_id'],json.loads(snapshot['link_origins']))
            except (ValueError,TypeError,KeyError):raise invalid() from None
        return await self.admission.run(snapshot['id'],call,lambda:self.check(snapshot,principal,enabled))

    async def test(self,project,principal,version):
        snapshot=self.storage.connected_snapshot(project,principal,version,enabled=False)
        manifest=await self.fetch(snapshot,principal,'manifest',{},False)
        return {'manifest':manifest,'receipt':self.seal(self.scope(snapshot,principal,'test')|{'expires':time.time()+300,'manifest':manifest})}

    def enable(self,project,principal,version,receipt,origins,support_linked):
        snapshot=self.storage.connected_snapshot(project,principal,version,enabled=False)
        body=self.open(receipt,self.scope(snapshot,principal,'test'))
        try:origins=[safe_origin(x) for x in origins]
        except ValueError:raise Error(400,'invalid_input','Invalid approved link origin') from None
        if len(set(origins))!=len(origins) or any(x not in body['manifest']['link_origins'] for x in origins):raise Error(400,'invalid_input','Approve only origins shown in this connection test')
        return self.storage.connected_change(project,principal,version,'enable',dict(app_instance_id=body['manifest']['app_instance_id'],link_origins=origins,support_linked=support_linked))

    def preferences(self,principal,version=None,zone=None):
        if zone is not None:
            try:ZoneInfo(zone)
            except (ValueError,ZoneInfoNotFoundError):raise Error(400,'invalid_timezone','Choose an IANA timezone, such as America/Chicago') from None
        return self.storage.connected_preferences(principal,version,zone)

    async def read(self,project,principal,operation,version,query,continuation=None):
        snapshot=self.storage.connected_snapshot(project,principal,version)
        query=REQUESTS[operation].model_validate(query).model_dump(by_alias=True)
        if query.get('cursor') is not None:raise Error(400,'invalid_input','Use the wrapped continuation')
        fingerprint=hashlib.sha256(json.dumps(query,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        scope=self.scope(snapshot,principal,operation)|{'query':fingerprint,'instance':snapshot['app_instance_id']}
        previous=self.open(continuation,scope) if continuation else None
        if previous:query['cursor']=previous['cursor']
        # Each demand read verifies the live identity/capability; no saved catalogue.
        manifest=await self.fetch(snapshot,principal,'manifest',{})
        capability={'profile':'profiles'}.get(operation,operation)
        if not manifest['capabilities'][capability]:raise Error(409,'app_unsupported','This app does not provide this view.')
        if operation=='users':
            if query['query'] and 'query' not in manifest['user_filters']:raise Error(409,'app_unsupported','This app does not support user search.')
            if query['joined'] and 'joined_range' not in manifest['user_filters']:raise Error(409,'app_unsupported','This app does not support signup date filtering.')
        if operation=='activity' and query['user_id'] is not None and not manifest['capabilities']['user_activity']:raise Error(409,'app_unsupported','This app does not support user activity.')
        data=await self.fetch(snapshot,principal,operation,query)
        result={'data':data,'version':snapshot['version'],'project_id':snapshot['project_id'],'capabilities':manifest['capabilities'],'user_filters':manifest['user_filters'],'continuation':None}
        if operation in ('activity','users'):
            boundary='snapshot' if operation=='activity' else 'scan_until'
            expires=min(time.time()+1800,instant(data['expires_at']).timestamp())
            if expires<=time.time():raise Error(409,'app_browse_expired','The app page has expired. Refresh to start again.')
            field='occurred_at' if operation=='activity' else 'joined_at'
            if previous:
                expires=min(expires,previous['expires'])
                if data[boundary]!=previous['boundary'] or data['expires_at']!=previous['remote_expiry']:raise invalid()
                if data['items'] and (instant(data['items'][0][field]),data['items'][0]['id'].encode()) >= (instant(previous['last'][0]),previous['last'][1].encode()):raise invalid()
            cursor=data['next_cursor'];data['next_cursor']=None
            if cursor:
                if len(cursor.encode())>8192:raise invalid()
                last=data['items'][-1]
                result['continuation']=self.seal(scope|{'expires':expires,'remote_expiry':data['expires_at'],'cursor':cursor,'boundary':data[boundary],'last':[last[field],last['id']]})
            result['expires_at']=datetime.fromtimestamp(expires,timezone.utc).isoformat().replace('+00:00','Z')
        self.check(snapshot,principal)
        return result
