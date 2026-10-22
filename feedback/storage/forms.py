"""Shared serialized relational Forms operations; sensitive state is rechecked here."""
import hashlib
import json
import time
from feedback.domain import new_id, now
from feedback.errors import Error, missing, conflict
from feedback.forms import FormDefinition, FormSubmissionInput, validate_answers


class FormsStorage:
    def _forms_admin(self, c, p):
        self._admin_session(c, p['user_id'], p['session_id'])

    @staticmethod
    def _form(c, fid):
        row = c.execute('SELECT f.*,p.name project_name,p.archived_at project_archived_at FROM forms f JOIN projects p ON p.id=f.project_id WHERE f.id=?', (fid,)).fetchone()
        if not row: missing()
        return dict(row)

    @staticmethod
    def _form_view(row):
        return {**row, 'fields': json.loads(row['fields']), 'enabled': bool(row['enabled']), 'public_enabled': bool(row['public_enabled']), 'embed_enabled': bool(row['embed_enabled']), 'embed_origins': json.loads(row['embed_origins'])}

    def forms_list(self, principal, project, limit, offset):
        with self.transaction() as c:
            self._forms_admin(c, principal)
            rows = c.execute('SELECT f.*,p.name project_name,p.archived_at project_archived_at FROM forms f JOIN projects p ON p.id=f.project_id WHERE (? IS NULL OR f.project_id=?) ORDER BY f.created_at DESC,f.id DESC LIMIT ? OFFSET ?', (project, project, limit+1, offset)).fetchall()
            return {'items':[self._form_view(dict(r)) for r in rows[:limit]], 'limit':limit, 'offset':offset, 'has_more':len(rows)>limit}

    def forms_read(self, principal, fid):
        with self.transaction() as c:
            self._forms_admin(c, principal)
            return self._form_view(self._form(c, fid))

    def _form_event(self, c, pid, fid, sid, kind, actor):
        eid, ts = new_id(), now()
        payload = {'id':eid, 'type':kind, 'schema_version':1, 'occurred_at':ts, 'project_id':pid, 'form_id':fid, 'submission_id':sid, 'actor':actor}
        self._persist_event(c, pid, None, None, payload)
        if kind == 'form.submission_created': self._queue_push(c, eid, kind, sid)

    def forms_save(self, principal, project, fid, data):
        values = FormDefinition.model_validate({k:v for k,v in data.items() if k!='version'}).model_dump()
        with self.transaction(True) as c:
            self._forms_admin(c, principal)
            if fid:
                old = self._form(c, fid)
                pid = old['project_id']
                if old['version'] != data['version']: conflict()
            else:
                row = c.execute('SELECT id FROM projects WHERE id=? OR identifier=?', (project, project)).fetchone()
                if not row: missing()
                pid = row['id']
                if c.execute('SELECT count(*) FROM forms WHERE project_id=?', (pid,)).fetchone()[0] >= 100:
                    raise Error(409, 'form_limit', 'This project already has 100 forms')
            self._active_project(c, pid)
            ts = now()
            fields = json.dumps(values['fields'], separators=(',', ':'), ensure_ascii=False)
            origins = json.dumps(values['embed_origins'])
            if fid:
                if all(old[k]==values[k] for k in ('name','description','enabled','public_enabled','embed_enabled')) and json.loads(old['fields'])==values['fields'] and json.loads(old['embed_origins'])==values['embed_origins']:
                    return self._form_view(old)
                c.execute('UPDATE forms SET name=?,description=?,enabled=?,public_enabled=?,embed_enabled=?,embed_origins=?,fields=?,version=version+1,updated_at=? WHERE id=?', (values['name'], values['description'], values['enabled'], values['public_enabled'], values['embed_enabled'], origins, fields, ts, fid))
            else:
                fid = new_id()
                c.execute('INSERT INTO forms(id,project_id,name,description,enabled,public_enabled,embed_enabled,embed_origins,fields,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (fid,pid,values['name'],values['description'],values['enabled'],values['public_enabled'],values['embed_enabled'],origins,fields,ts,ts))
            self._form_event(c, pid, fid, None, 'form.updated' if project is None else 'form.created', {'kind':'native','id':principal['user_id']})
            return self._form_view(self._form(c, fid))

    def forms_submit(self, actor, fid, data):
        data = FormSubmissionInput.model_validate(data).model_dump()
        encoded = json.dumps(data['answers'], sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with self.transaction(True) as c:
            key = c.execute('SELECT * FROM api_keys WHERE id=? AND revoked_at IS NULL', (actor.key_id,)).fetchone()
            if not key: raise Error(401, 'unauthorized', 'Project token expired or revoked')
            if 'forms:submit' not in json.loads(key['permissions']): raise Error(403, 'forbidden', 'Form submission permission required')
            form = self._form(c, fid)
            if form['project_id'] != key['project_id'] or form['project_id'] != actor.project_id: missing()
            old = c.execute('SELECT id,answer_hash,created_at FROM form_submissions WHERE form_id=? AND client_request_id=?', (fid,data['client_request_id'])).fetchone()
            if old:
                if old['answer_hash'] != fingerprint: conflict('This request ID was already used with different answers')
                # A receipt survives closing, editing and deletion without reviving data.
                return {'id':old['id'], 'created_at':old['created_at']}
            self._active_project(c, form['project_id'])
            if not form['enabled']: raise Error(409, 'form_closed', 'This form is not accepting submissions')
            validate_answers(json.loads(form['fields']), data['answers'])
            sid, ts = new_id(), now()
            c.execute('INSERT INTO form_submissions(id,form_id,client_request_id,answers,answer_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (sid,fid,data['client_request_id'],encoded,fingerprint,ts,ts))
            self._form_event(c, form['project_id'], fid, sid, 'form.submission_created', {'kind':'integration','id':key['id']})
            return {'id':sid, 'created_at':ts}

    @staticmethod
    def _public_form(form, embed_origin=None):
        if not form['enabled'] or form['project_archived_at']:
            missing()
        if embed_origin is None:
            if not form['public_enabled']: missing()
        else:
            if not form['embed_enabled']: missing()
            if not embed_origin or embed_origin not in json.loads(form['embed_origins']):
                raise Error(403, 'origin_denied', 'This website is not enabled for this form')

    def forms_embed_origin_allowed(self, fid, origin):
        try:
            self.forms_public_read(fid, origin or '')
            return True
        except Error:
            return False

    def forms_public_read(self, fid, embed_origin=None):
        with self.transaction() as c:
            form = self._form(c, fid)
            self._public_form(form, embed_origin)
            return {k:v for k,v in self._form_view(form).items() if k in ('id','name','description','fields','project_name','version')}

    def forms_public_submit(self, fid, data, version, client_bucket, embed_origin=None):
        data = FormSubmissionInput.model_validate(data).model_dump()
        encoded = json.dumps(data['answers'], sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with self.transaction(True) as c:
            form = self._form(c, fid)
            self._public_form(form, embed_origin)
            if form['version'] != version:
                raise Error(409, 'form_changed', 'This form changed. Reload it before sending your answers.')
            old = c.execute('SELECT answer_hash FROM form_submissions WHERE form_id=? AND client_request_id=?', (fid,data['client_request_id'])).fetchone()
            if old:
                if old['answer_hash'] != fingerprint: conflict('This request was already sent with different answers')
                return {'ok':True}
            validate_answers(json.loads(form['fields']), data['answers'])
            window_start = int(time.time() // 3600)
            c.execute('DELETE FROM form_public_limits WHERE window_start<?', (window_start-1,))
            buckets = [('client:'+client_bucket,10),('form:'+fid,200)]
            new_buckets = 0
            for bucket,limit in buckets:
                row = c.execute('SELECT count FROM form_public_limits WHERE bucket=? AND window_start=?',(bucket,window_start)).fetchone()
                new_buckets += int(row is None)
                if row and row['count']>=limit:
                    raise Error(429, 'rate_limited', 'Too many submissions. Please try again in an hour.')
            if c.execute('SELECT count(*) FROM form_public_limits').fetchone()[0]+new_buckets>10000:
                raise Error(429, 'rate_limited', 'Too many submissions. Please try again in an hour.')
            for bucket,_ in buckets:
                c.execute('INSERT INTO form_public_limits(bucket,window_start,count) VALUES(?,?,1) ON CONFLICT(bucket,window_start) DO UPDATE SET count=form_public_limits.count+1',(bucket,window_start))
            sid,ts = new_id(),now()
            c.execute('INSERT INTO form_submissions(id,form_id,client_request_id,answers,answer_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(sid,fid,data['client_request_id'],encoded,fingerprint,ts,ts))
            self._form_event(c,form['project_id'],fid,sid,'form.submission_created',{'kind':'anonymous'})
            return {'ok':True}

    def _form_submission(self, c, sid):
        row = c.execute('SELECT s.*,f.name form_name,f.project_id,p.name project_name,f.fields FROM form_submissions s JOIN forms f ON f.id=s.form_id JOIN projects p ON p.id=f.project_id WHERE s.id=? AND s.deleted_at IS NULL', (sid,)).fetchone()
        if not row: missing()
        result = dict(row)
        result['answers'] = json.loads(result['answers'])
        result['fields'] = json.loads(result['fields'])
        return result

    def form_submissions(self, principal, fid, state, limit, offset):
        with self.transaction() as c:
            self._forms_admin(c, principal)
            self._form(c, fid)
            rows = c.execute('SELECT id FROM form_submissions WHERE form_id=? AND deleted_at IS NULL AND (? IS NULL OR state=?) ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (fid,state,state,limit+1,offset)).fetchall()
            return {'items':[self._form_submission(c,r['id']) for r in rows[:limit]], 'limit':limit, 'offset':offset, 'has_more':len(rows)>limit}

    def form_submission(self, principal, sid):
        with self.transaction() as c:
            self._forms_admin(c, principal)
            return self._form_submission(c, sid)

    def form_submission_review(self, principal, sid, version, state):
        if state not in ('new','reviewed','archived'): raise Error(400,'invalid_state','Choose a review state')
        with self.transaction(True) as c:
            self._forms_admin(c, principal)
            row = self._form_submission(c, sid)
            if row['version'] != version: conflict()
            if row['state'] != state:
                c.execute('UPDATE form_submissions SET state=?,version=version+1,updated_at=? WHERE id=?', (state,now(),sid))
                self._form_event(c,row['project_id'],row['form_id'],sid,'form.submission_reviewed',{'kind':'native','id':principal['user_id']})
            return self._form_submission(c, sid)

    def form_submission_delete(self, principal, sid, version):
        with self.transaction(True) as c:
            self._forms_admin(c, principal)
            row = self._form_submission(c, sid)
            if row['version'] != version: conflict()
            ts = now()
            c.execute("UPDATE form_submissions SET answers='{}',deleted_at=?,updated_at=?,version=version+1 WHERE id=?", (ts,ts,sid))
            self._form_event(c,row['project_id'],row['form_id'],sid,'form.submission_deleted',{'kind':'native','id':principal['user_id']})
            c.execute("UPDATE push_deliveries SET cancel_requested=1,next_attempt_at=0,failure_count=0,state=CASE WHEN state='failed' THEN 'queued' ELSE state END WHERE kind='form.submission_created' AND resource_id=? AND (state IN ('queued','awaiting','sending') OR (state='failed' AND submitted=1))", (sid,))
