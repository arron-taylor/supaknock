"""Synthetic, in-memory app. Test/demo only; never imported by the runtime."""
import base64,hashlib,hmac,json,secrets,threading,time
from datetime import datetime,timezone,timedelta


def stamp(value=None):return (value or datetime.now(timezone.utc)).isoformat(timespec='microseconds').replace('+00:00','Z')

class FixtureApp:
    def __init__(self,instance='fixture',name='Demo',count=130):
        self.instance=instance;self.name=name;self.secret=secrets.token_bytes(32);self.lock=threading.Lock();self.sequence=0;self.events=[]
        now=datetime.now(timezone.utc)-timedelta(minutes=1)
        self.users=[dict(id=f'person-{i:04}',display_name=f'{name} member {i+1}',email=f'member{i+1}@example.test',joined_at=stamp(now-timedelta(hours=i)),status='active',plan='Free',last_active_at=None) for i in range(count)]
        if self.users:self.users[0]['phone_number']='+1 (312) 555-0142 ext. 9'
        for i,u in enumerate(self.users):self.commit(dict(id=f'event-{i:04}',occurred_at=u['joined_at'],type='user.created',title='Account created',description=f'{u["display_name"]} joined {name}.',user_id=u['id'],url=None))
    def commit(self,event):
        # Allocate the visible watermark inside the same lock as publication.
        with self.lock:
            self.sequence+=1;self.events.append((self.sequence,event))
    def cursor(self,data):
        raw=json.dumps(data,sort_keys=True,separators=(',',':')).encode();return base64.urlsafe_b64encode(raw).decode()+'.'+hmac.new(self.secret,raw,hashlib.sha256).hexdigest()
    def unc(self,value):
        raw,sig=value.split('.');raw=base64.urlsafe_b64decode(raw)
        if not hmac.compare_digest(sig,hmac.new(self.secret,raw,hashlib.sha256).hexdigest()):raise ValueError('invalid cursor')
        data=json.loads(raw)
        if data['expiry']<=time.time():raise ValueError('expired cursor')
        return data
    def query(self,op,q):
        envelope=dict(protocol={'major':1,'minor':0},app_instance_id=self.instance,observed_at=stamp())
        if op=='manifest':return envelope|dict(capabilities=dict(summary=True,users=True,profiles=True,activity=True,user_activity=True),user_filters=['query','joined_range'],link_origins=[],activity_history=dict(available_from=None,complete=True),activity_definition='Retained synthetic signups')
        if op=='summary':
            n=sum(q['from']<=u['joined_at']<q['until'] for u in self.users)
            return envelope|dict(interval=q,total_accounts={'count':len(self.users)},new_accounts=dict(count=n,basis='current_accounts',complete=True))
        if op=='profile':
            u=next((x for x in self.users if x['id']==q['user_id']),None)
            if not u:raise LookupError()
            return envelope|dict(user=u,details=[dict(key='projects',label='Projects created',type='number',value=3)],url=None)
        binding={k:v for k,v in q.items() if k!='cursor'}
        with self.lock:
            state=self.unc(q['cursor']) if q.get('cursor') else dict(expiry=time.time()+1800,binding=binding,watermark=self.sequence,scan=stamp(),last=None)
            if state['binding']!=binding:raise ValueError('changed query')
            items=[dict(x) for seq,x in self.events if seq<=state['watermark']] if op=='activity' else list(self.users)
        field='occurred_at' if op=='activity' else 'joined_at'
        if op=='activity':items=[x for x in items if q['from']<=x[field]<q['until'] and (q.get('user_id') is None or x['user_id']==q['user_id'])]
        else:
            items=[x for x in items if x[field]<=state['scan'] and (q.get('query','').casefold() in ((x['display_name'] or '')+' '+(x['email'] or '')).casefold()) and (not q.get('joined') or q['joined']['from']<=x[field]<q['joined']['until'])]
        items.sort(key=lambda x:(x[field],x['id'].encode()),reverse=True)
        if state['last']:items=[x for x in items if (x[field],x['id'].encode())<(state['last'][0],state['last'][1].encode())]
        page=items[:q.get('limit',50)]
        nxt=None
        if len(items)>len(page):state['last']=[page[-1][field],page[-1]['id']];nxt=self.cursor(state)
        result=envelope|dict(items=page,next_cursor=nxt,expires_at=stamp(datetime.fromtimestamp(state['expiry'],timezone.utc)))
        if op=='activity':result.update(snapshot=str(state['watermark']),coverage=dict(available_from=None,complete=True))
        else:result['scan_until']=state['scan']
        return result
