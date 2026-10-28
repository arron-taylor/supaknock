"""MCP relational state; sensitive reads/writes recheck current authority atomically."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import time
from feedback.ai_limits import MAX_SOURCES, MAX_TOOL_SOURCE_BYTES, MAX_TOOL_EVIDENCE_BYTES
from feedback.domain import new_id,now
from feedback.errors import Error,conflict,missing
from feedback.ai_mcp_protocol import encode,definition


class MCPStorage:
    @staticmethod
    def _mcp_needs_tools(c,pid):
        return bool(c.execute("SELECT 1 FROM ai_mcp_projects p JOIN ai_mcp_bindings b ON b.project_id=p.project_id WHERE p.project_id=? AND p.source_mode!='knowledge' LIMIT 1",(pid,)).fetchone())

    @staticmethod
    def _mcp_connection(c,cid):
        row=c.execute('SELECT * FROM ai_mcp_connections WHERE id=? AND deleted_at IS NULL',(cid,)).fetchone()
        if not row:missing()
        return dict(row)

    @staticmethod
    def _mcp_view(c,row):
        return {k:row[k] for k in ('id','label','url','version','tested_at','error','created_at','updated_at')} | {
            'enabled':bool(row['enabled']),'has_token':bool(row['protected_token']),
            'tested':row['tested_version']==row['version'], 'catalogue':json.loads(row['catalogue']),
            'link_origins':json.loads(row['link_origins']),
            'projects':[dict(r) for r in c.execute('SELECT p.id,p.name FROM projects p JOIN ai_mcp_bindings b ON b.project_id=p.id WHERE b.connection_id=?',(row['id'],))]}

    def mcp_connections(self,p):
        with self.transaction() as c:
            self._ai_admin(c,p)
            return {'items':[self._mcp_view(c,dict(r)) for r in c.execute('SELECT * FROM ai_mcp_connections WHERE deleted_at IS NULL ORDER BY created_at,id')]}

    def _mcp_fence(self,c,cid):
        for r in c.execute('SELECT project_id FROM ai_mcp_bindings WHERE connection_id=?',(cid,)).fetchall():
            self._ai_cancel_generation(c,r[0])
            c.execute('UPDATE ai_mcp_projects SET version=version+1 WHERE project_id=?',(r[0],))

    def mcp_save(self,p,cid,version,*,label,url,enabled,protected_token,link_origins):
        with self.transaction(True) as c:
            self._ai_admin(c,p);ts=now()
            if cid:
                old=self._mcp_connection(c,cid)
                if old['version']!=version:conflict()
                self._mcp_fence(c,cid)
                c.execute("UPDATE ai_mcp_connections SET label=?,url=?,enabled=?,protected_token=?,link_origins=?,version=version+1,tested_version=NULL,catalogue='[]',error=NULL,updated_at=? WHERE id=?",(label,url,int(enabled),protected_token,encode(link_origins).decode(),ts,cid))
            else:
                if version!=0:conflict()
                if c.execute('SELECT count(*) FROM ai_mcp_connections WHERE deleted_at IS NULL').fetchone()[0]>=50:raise Error(409,'mcp_limit','At most 50 tool connections are supported')
                cid=new_id()
                c.execute('INSERT INTO ai_mcp_connections(id,label,url,enabled,protected_token,link_origins,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(cid,label,url,int(enabled),protected_token,encode(link_origins).decode(),ts,ts))
            return self._mcp_view(c,self._mcp_connection(c,cid))

    def mcp_snapshot(self,p,cid,version):
        with self.transaction() as c:
            self._ai_admin(c,p);row=self._mcp_connection(c,cid)
            if row['version']!=version:conflict()
            return row

    def mcp_remove(self,p,cid,version):
        with self.transaction(True) as c:
            self._ai_admin(c,p);row=self._mcp_connection(c,cid)
            if row['version']!=version:conflict()
            self._mcp_fence(c,cid)
            c.execute('DELETE FROM ai_mcp_bindings WHERE connection_id=?',(cid,))
            c.execute("UPDATE ai_mcp_connections SET protected_token=NULL,enabled=0,version=version+1,tested_version=NULL,catalogue='[]',deleted_at=? WHERE id=?",(now(),cid))
            return {'removed':True}

    def mcp_discovered(self,p,cid,version,catalogue,error=None):
        value=encode(catalogue,262144).decode()
        with self.transaction(True) as c:
            self._ai_admin(c,p);row=self._mcp_connection(c,cid)
            if row['version']!=version:conflict()
            if row['catalogue']!=value or error:self._mcp_fence(c,cid)
            c.execute('UPDATE ai_mcp_connections SET catalogue=?,tested_version=?,error=?,tested_at=? WHERE id=?',(value,version if not error else None,error,now(),cid))
            return self._mcp_view(c,self._mcp_connection(c,cid))

    @staticmethod
    def _mcp_project(c,pid):
        row=c.execute('SELECT * FROM ai_mcp_projects WHERE project_id=?',(pid,)).fetchone()
        result=dict(row) if row else {'project_id':pid,'version':0,'source_mode':'knowledge'}
        result['bindings']=[dict(r)|{'tools':json.loads(r['tools'])} for r in c.execute('SELECT * FROM ai_mcp_bindings WHERE project_id=? ORDER BY connection_id',(pid,))]
        return result

    def mcp_project(self,p,project):
        with self.transaction() as c:
            self._ai_admin(c,p);return self._mcp_project(c,self._ai_project(c,project))

    def _mcp_tools(self,c,pid):
        result=[]
        for b in self._mcp_project(c,pid)['bindings']:
            try:row=self._mcp_connection(c,b['connection_id'])
            except Error:continue
            if not row['enabled'] or row['version']!=b['connection_version'] or row['tested_version']!=row['version']:continue
            catalogue={t['name']:t for t in json.loads(row['catalogue'])}
            for selection in b['tools']:
                t=catalogue.get(selection['name'])
                if not t or t.get('unsupported') or t.get('digest')!=selection['digest']:continue
                alias='mcp_'+hashlib.sha256((row['id']+'\0'+t['name']).encode()).hexdigest()[:32]
                result.append({'alias':alias,'tool':t,'connection_id':row['id'],'connection_version':row['version'],
                    'label':row['label'],'context_kind':b['context_kind']})
        return result

    def mcp_bind(self,p,project,version,source_mode,bindings):
        if source_mode not in ('knowledge','tools','combined') or not isinstance(bindings,list) or len(bindings)>4:raise Error(400,'invalid_input','Choose a source mode and at most four tool connections')
        with self.transaction(True) as c:
            self._ai_admin(c,p);pid=self._ai_project(c,project);old=self._mcp_project(c,pid)
            if old['version']!=version:conflict()
            selected=[];ids=set();count=0;podcasts=0
            for b in bindings:
                if b['connection_id'] in ids:raise Error(400,'invalid_input','Select a connection only once')
                ids.add(b['connection_id']);r=self._mcp_connection(c,b['connection_id'])
                if r['version']!=b['connection_version'] or not r['enabled'] or r['tested_version']!=r['version']:conflict('Save and discover this enabled connection first')
                catalog={t['name']:t for t in json.loads(r['catalogue'])};approved=[]
                for item in b['tools']:
                    t=catalog.get(item['name'])
                    if not t or t.get('unsupported') or t['digest']!=item['digest']:conflict('Discover and review the current tool definition')
                    definition(t) # Independently check portable schema/read-only declaration.
                    if item['name'] in {x['name'] for x in approved}:raise Error(400,'invalid_input','Select each tool once')
                    approved.append({'name':item['name'],'digest':item['digest']})
                context=b.get('context_kind')
                if context not in (None,'podcast_episode'):raise Error(400,'invalid_input','Unsupported resource context')
                if context:
                    podcasts+=1
                    if {x['name'] for x in approved} not in ({'get_episode','search_transcript','get_transcript_window'}, {'get_episode','search_transcript','get_transcript_window','get_transcript'}):raise Error(400,'invalid_input','Episode context requires metadata, search and window tools; complete transcript reading is optional')
                    for t in [catalog[x['name']] for x in approved]:
                        expected={'episode_id':'string'}
                        if t['name']!='get_episode':expected['transcript_revision']='string'
                        if t['name']=='search_transcript':expected['query']='string'
                        if t['name']=='get_transcript_window':expected.update(start_seconds='number',end_seconds='number')
                        schema=t['inputSchema']
                        for field,kind in expected.items():
                            actual=schema.get('properties',{}).get(field,{}).get('type')
                            if actual!=kind and not (kind=='number' and actual=='integer') or field not in schema.get('required',[]):
                                raise Error(400,'invalid_input','Podcast tools must require the documented episode, revision and query/window fields')
                count+=len(approved);selected.append((r['id'],r['version'],encode(approved).decode(),context))
            if podcasts>1:raise Error(400,'invalid_input','Choose one episode-context connection per project')
            cfg=self._ai_config(c,pid)
            if count+sum(cfg['settings']['tools'].values())>8:raise Error(400,'mcp_limit','Select at most eight built-in and connected tools altogether')
            if source_mode=='tools' and not count:raise Error(400,'invalid_input','Tools-only answers require selected tools')
            c.execute('INSERT INTO ai_mcp_projects(project_id,version,source_mode) VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET version=excluded.version,source_mode=excluded.source_mode',(pid,version+1,source_mode))
            c.execute('DELETE FROM ai_mcp_bindings WHERE project_id=?',(pid,))
            for cid,cv,items,kind in selected:c.execute('INSERT INTO ai_mcp_bindings VALUES(?,?,?,?,?)',(pid,cid,cv,items,kind))
            self._ai_cancel_generation(c,pid)
            return self._mcp_project(c,pid)

    def mcp_job_snapshot(self,job_id,token):
        with self.transaction(True) as c:
            job=self._ai_job_lease(c,job_id,token,time.time())
            if not job or job['kind']!='turn':conflict('This assistant turn is no longer authorized')
            self._ai_topic_require_allow(c,job)
            return {'project':self._mcp_project(c,job['project_id']),'tools':self._mcp_tools(c,job['project_id']),
                    'visitor':c.execute('SELECT conversation_id FROM chat_messages WHERE id=?',(job['resource_id'],)).fetchone()[0],
                    'context':json.loads(c.execute('SELECT context FROM chat_messages WHERE id=?',(job['resource_id'],)).fetchone()[0] or 'null')}

    def mcp_job_connection(self,job_id,token,cid,version):
        with self.transaction(True) as c:
            job=self._ai_job_lease(c,job_id,token,time.time())
            if not job or job['kind']!='turn':conflict('This assistant turn is no longer authorized')
            self._ai_topic_require_allow(c,job)
            row=self._mcp_connection(c,cid)
            if row['version']!=version or row['tested_version']!=version or not row['enabled'] or not c.execute('SELECT 1 FROM ai_mcp_bindings WHERE project_id=? AND connection_id=? AND connection_version=?',(job['project_id'],cid,version)).fetchone():conflict('This tool approval changed')
            return row

    def mcp_reserve(self,job_id,token,tool,call_id):
        with self.transaction(True) as c:
            job=self._ai_job_lease(c,job_id,token,time.time())
            if not job or job['kind']!='turn':conflict()
            self._ai_topic_require_allow(c,job)
            allowed={t['alias']:t for t in self._mcp_tools(c,job['project_id'])}
            if tool!=allowed.get(tool['alias']):conflict('Tool approval changed')
            old=c.execute('SELECT id FROM ai_mcp_calls WHERE job_id=? AND lease_token=? AND call_id=?',(job_id,token,call_id)).fetchone()
            if old:conflict('This remote read has already been dispatched; it cannot be replayed')
            counts=c.execute('SELECT count(*),COALESCE(SUM(elapsed),0) FROM ai_mcp_calls WHERE job_id=?',(job_id,)).fetchone()
            attempt=c.execute('SELECT count(*),COALESCE(SUM(elapsed),0) FROM ai_mcp_calls WHERE job_id=? AND lease_token=?',(job_id,token)).fetchone()
            if counts[0]>=16 or attempt[0]>=8 or attempt[1]>=25:raise Error(429,'mcp_limit','This answer has reached its connected-tool limit')
            cutoff=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat().replace('+00:00','Z')
            c.execute("DELETE FROM ai_mcp_calls WHERE created_at<? AND job_id IN (SELECT id FROM ai_jobs WHERE state IN ('succeeded','failed','cancelled'))",(cutoff,))
            self._mcp_memory={k:v for k,v in getattr(self,'_mcp_memory',{}).items() if v['expires']>time.time()}
            rid=new_id();c.execute('INSERT INTO ai_mcp_calls(id,job_id,lease_token,connection_id,connection_version,tool_name,call_id,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(rid,job_id,token,tool['connection_id'],tool['connection_version'],tool['tool']['name'],call_id,'dispatched',now()))
            return rid

    def mcp_finish(self,job_id,token,rid,evidence,elapsed,error=None):
        encode(evidence,MAX_TOOL_EVIDENCE_BYTES)
        if len(evidence)>MAX_SOURCES:raise Error(400,'mcp_limit','Too many tool sources')
        with self.transaction(True) as c:
            job=self._ai_job_lease(c,job_id,token,time.time())
            call=c.execute('SELECT * FROM ai_mcp_calls WHERE id=? AND job_id=? AND lease_token=?',(rid,job_id,token)).fetchone()
            if not job or not call or call['state']!='dispatched':conflict()
            self._ai_topic_require_allow(c,job)
            if not any(t['connection_id']==call['connection_id'] and t['connection_version']==call['connection_version'] and t['tool']['name']==call['tool_name'] for t in self._mcp_tools(c,job['project_id'])):conflict()
            c.execute('UPDATE ai_mcp_calls SET state=?,elapsed=? WHERE id=?',('failed' if error else 'succeeded',elapsed,rid))
            out=[]
            if not error:
                for source in evidence:
                    fingerprint=hashlib.sha256(encode(source,MAX_TOOL_SOURCE_BYTES)).hexdigest()
                    scope=(job_id,token,call['connection_id'],call['connection_version'],fingerprint)
                    previous=next((v for v in getattr(self,'_mcp_memory',{}).values() if v.get('scope')==scope and v['expires']>time.time()),None)
                    if previous:
                        previous['expires']=job['lease_until']
                        if previous['content'] not in out:out.append(previous['content'])
                        continue
                    eid='mcp:'+new_id();value=source|{'id':eid,'observed_at':now()}
                    encode(value,MAX_TOOL_SOURCE_BYTES)
                    if not hasattr(self,'_mcp_memory'):self._mcp_memory={}
                    self._mcp_memory[eid]={'job':job_id,'token':token,'call':rid,'expires':job['lease_until'],'content':value,'scope':scope}
                    out.append(value)
            return out

    def _mcp_evidence_rows(self,c,job_id,token,ids):
        if len(ids)>MAX_SOURCES or len(set(ids))!=len(ids):raise Error(400,'mcp_limit','Use at most twenty distinct tool sources')
        out=[]
        for eid in ids:
            item=getattr(self,'_mcp_memory',{}).get(eid)
            if not item or item['job']!=job_id or item['token']!=token or item['expires']<=time.time():conflict('This tool source does not belong to the current answer')
            row=c.execute("SELECT connection_id,connection_version FROM ai_mcp_calls WHERE id=? AND job_id=? AND lease_token=? AND state='succeeded'",(item['call'],job_id,token)).fetchone()
            if not row:conflict('This tool source does not belong to the current answer')
            connection=self._mcp_connection(c,row['connection_id'])
            if not connection['enabled'] or connection['version']!=row['connection_version'] or connection['tested_version']!=connection['version']:conflict('Tool evidence is no longer authorized')
            out.append(item['content'])
        encode(out,MAX_TOOL_EVIDENCE_BYTES)
        return out

    def mcp_clear_attempt(self,job_id,token):
        with self.transaction(True):
            self._mcp_memory={k:v for k,v in getattr(self,'_mcp_memory',{}).items() if (v['job'],v['token'])!=(job_id,token) and v['expires']>time.time()}
