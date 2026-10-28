"""Shared relational operations for installation database providers.

Every adapter supplies consistent read transactions and serialized writes. Sensitive
state is rechecked inside each operation, and changes, immutable events and queued
deliveries commit together. Provider SQL and search primitives stay in adapters.
"""
import json
import secrets
import time
import hashlib
import math
import struct
from datetime import datetime, timezone
from feedback.ai_limits import MAX_SOURCES, MAX_TOOL_EVIDENCE_BYTES, AI_PROGRESS_STAGES
from feedback.ai_domain import AI_JOB_KINDS, EMBEDDING_PROFILE, GuestActor, default_ai_settings, validate_ai_settings
from feedback.domain import Actor, new_id, now, utc_timestamp
from feedback.errors import Error, missing, conflict
from feedback.ai_topics import (default_topic_policy, validate_topic_policy, POLICY_VERSION,
                                probability_outcome, policy_reply, is_greeting, topic_context)

_AI_UNSET = object()


from feedback.storage.forms import FormsStorage


from feedback.storage.mcp import MCPStorage


class RelationalStorage(FormsStorage, MCPStorage):
    # These SQL fragments are constant adapter definitions, never user input.
    _ai_chunk_order_sql: str
    _ai_crawl_order_sql: str

    def transaction(self, write=False):
        raise NotImplementedError

    @staticmethod
    def _encode_external_id(value):
        """Preserve the exact opaque ID; adapters may use a lossless DB type."""
        return value

    @staticmethod
    def _decode_external_id(value):
        return value

    @staticmethod
    def _text_contains_sql(column):
        """A literal, case-insensitive substring predicate with one parameter."""
        raise NotImplementedError

    @staticmethod
    def _text_bytes_sql(column):
        """Byte length of UTF-8 text, including multibyte characters."""
        raise NotImplementedError

    @staticmethod
    def _has_ai_turns(c):
        raise NotImplementedError

    def _ai_knowledge_chunk_rows(self, c, project_id, terms, limit):
        """Read authorized admin passages; terms=None means no lexical filter."""
        raise NotImplementedError

    def _ai_search_candidates(self, c, project_id, query, values, take):
        """Return semantic and lexical rows from the authorized corpus only.

        Semantic rows are sorted by cosine distance then id, capped at take.
        Lexical candidates are capped at 10,001; shared code rejects overflow
        before computing project-local term frequencies and the final ranking.
        """
        raise NotImplementedError

    def signing_secrets(self):
        with self.transaction() as c:
            return [x[0] for x in c.execute("SELECT webhook_secret FROM projects WHERE webhook_secret IS NOT NULL UNION SELECT signing_secret FROM webhook_deliveries UNION SELECT token FROM devices UNION SELECT enrollment_proof FROM push_settings UNION SELECT credential FROM push_settings WHERE credential IS NOT NULL UNION SELECT token FROM push_deliveries WHERE token!='' UNION SELECT protected_key FROM ai_settings WHERE protected_key IS NOT NULL UNION SELECT protected_key FROM ai_provider_keys WHERE protected_key IS NOT NULL UNION SELECT protected_token FROM ai_runtime WHERE protected_token IS NOT NULL UNION SELECT protected_token FROM connected_apps WHERE protected_token IS NOT NULL UNION SELECT protected_key FROM ai_embedding_settings WHERE protected_key IS NOT NULL UNION SELECT protected_token FROM ai_mcp_connections WHERE protected_token IS NOT NULL")]

    def ready(self):
        with self.transaction() as c:
            return c.execute("SELECT 1").fetchone()[0] == 1

    def setup_available(self):
        with self.transaction() as c:
            return not c.execute("SELECT 1 FROM installation").fetchone()

    def create_native_account(self, email, password_hash, name, role="participant", setup=False, ip_address=None, activity_type='account_created'):
        with self.transaction(True) as c:
            if setup:
                if c.execute("SELECT 1 FROM installation").fetchone():
                    conflict("Setup has already completed")
                c.execute("INSERT INTO installation VALUES(1,?)", (now(),))
            uid, ts = new_id(), now()
            c.execute("INSERT INTO users VALUES(?,?,?,?,?)", (uid, name, role, ts, ts))
            c.execute("INSERT INTO local_credentials VALUES(?,?,?,?)", (uid, email, password_hash, ts))
            self._account_activity(c, uid, activity_type, None, ip_address, ts)
            return dict(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def credentials(self, email):
        with self.transaction() as c:
            r = c.execute("SELECT u.*,l.password_hash FROM users u JOIN local_credentials l ON l.user_id=u.id WHERE l.email=?", (email,)).fetchone()
            return dict(r) if r else None

    def installation_id(self):
        with self.transaction(True) as c:
            c.execute('INSERT INTO instance_identity VALUES(1,?) ON CONFLICT DO NOTHING', (new_id(),))
            return c.execute("SELECT id FROM instance_identity WHERE singleton=1").fetchone()[0]

    def create_session(self, user_id, token_hash, csrf_hash, expires, transport='cookie', ip_address=None):
        with self.transaction(True) as c:
            c.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            ts = now()
            c.execute("INSERT INTO sessions(id,user_id,token_hash,csrf_hash,created_at,expires_at,transport) VALUES(?,?,?,?,?,?,?)", (new_id(), user_id, token_hash, csrf_hash, ts, expires, transport))
            self._account_activity(c, user_id, 'login', transport, ip_address, ts)

    @staticmethod
    def _account_activity(c, user_id, event_type, transport, ip_address, occurred_at):
        c.execute("INSERT INTO account_activity(id,user_id,event_type,transport,ip_address,occurred_at) VALUES(?,?,?,?,?,?)", (new_id(), user_id, event_type, transport, ip_address, occurred_at))

    def account_activity(self, admin_user_id, session_id, limit, offset, user_id=None):
        with self.transaction() as c:
            self._admin_session(c, admin_user_id, session_id)
            return [dict(row) for row in c.execute("""SELECT a.id,a.user_id,a.event_type,a.transport,a.ip_address,a.occurred_at,
                u.display_name,l.email,u.role FROM account_activity a JOIN users u ON u.id=a.user_id
                JOIN local_credentials l ON l.user_id=a.user_id
                WHERE (? IS NULL OR a.user_id=?) ORDER BY a.occurred_at DESC,a.id DESC LIMIT ? OFFSET ?""", (user_id,user_id,limit,offset))]

    def session(self, token_hash):
        with self.transaction() as c:
            r = c.execute("SELECT u.*,s.csrf_hash,s.id session_id,s.transport,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND s.revoked_at IS NULL", (token_hash, time.time())).fetchone()
            return dict(r) if r else None

    def revoke_session(self, session_id):
        with self.transaction(True) as c:
            c.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (now(), session_id))
            c.execute("UPDATE devices SET enabled=0,generation=generation+1 WHERE session_id=?", (session_id,))

    def change_password(self, user_id, password_hash, expected_hash):
        with self.transaction(True) as c:
            r = c.execute("UPDATE local_credentials SET password_hash=?,updated_at=? WHERE user_id=? AND password_hash=?", (password_hash, now(), user_id, expected_hash))
            if not r.rowcount:
                conflict()
            c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=?", (now(), user_id))
            c.execute("UPDATE devices SET enabled=0,generation=generation+1 WHERE user_id=?", (user_id,))

    def native_credential_for_user(self, user_id):
        with self.transaction() as c:
            return dict(c.execute("SELECT * FROM local_credentials WHERE user_id=?", (user_id,)).fetchone())

    def recover_account(self, email, password_hash):
        with self.transaction(True) as c:
            r = c.execute("SELECT user_id FROM local_credentials WHERE email=?", (email,)).fetchone()
            if not r:
                missing()
            c.execute("UPDATE local_credentials SET password_hash=?,updated_at=? WHERE user_id=?", (password_hash, now(), r[0]))
            c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=?", (now(), r[0]))
            c.execute("UPDATE devices SET enabled=0,generation=generation+1 WHERE user_id=?", (r[0],))

    def _admin_session(self, c, user_id, session_id):
        if not c.execute("SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND u.id=? AND u.role='admin' AND s.revoked_at IS NULL AND s.expires_at>?", (session_id,user_id,time.time())).fetchone():
            raise Error(401, 'unauthorized', 'Administrator session expired')

    @staticmethod
    def _device_view(row):
        return {k: bool(row[k]) if k in ('enabled','request_notifications','chat_notifications','form_notifications') else row[k] for k in ('id','name','environment','enabled','request_notifications','chat_notifications','form_notifications','generation','updated_at')}

    def devices(self, user_id):
        with self.transaction() as c:
            return [self._device_view(x) for x in c.execute("SELECT * FROM devices WHERE user_id=? ORDER BY updated_at DESC,id LIMIT 100", (user_id,))]

    def register_device(self, user_id, session_id, device_id, protected_token, token_hash, data):
        data = {'form_notifications': False, **data}
        with self.transaction(True) as c:
            self._admin_session(c,user_id,session_id)
            old = c.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if old and old['user_id'] != user_id and old['token_hash'] != token_hash:
                missing()
            fields = ('name','environment','enabled','request_notifications','chat_notifications','form_notifications')
            if old and old['user_id']==user_id and old['session_id']==session_id and old['token_hash']==token_hash and all(old[k]==data[k] for k in fields):
                return self._device_view(old)
            if not old and c.execute("SELECT count(*) FROM devices WHERE user_id=?",(user_id,)).fetchone()[0]>=20:
                raise Error(409,'device_limit','Remove an old device before registering another')
            # A physical APNs token has one active account/session owner per installation.
            c.execute("DELETE FROM devices WHERE token_hash=? AND environment=? AND id!=?", (token_hash,data['environment'],device_id))
            c.execute("""INSERT INTO devices(id,user_id,session_id,token,token_hash,environment,name,enabled,request_notifications,chat_notifications,form_notifications,updated_at,generation)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id,session_id=excluded.session_id,token=excluded.token,token_hash=excluded.token_hash,environment=excluded.environment,name=excluded.name,enabled=excluded.enabled,request_notifications=excluded.request_notifications,chat_notifications=excluded.chat_notifications,form_notifications=excluded.form_notifications,generation=devices.generation+1,updated_at=excluded.updated_at""",
                (device_id,user_id,session_id,protected_token,token_hash,data['environment'],data['name'],data['enabled'],data['request_notifications'],data['chat_notifications'],data['form_notifications'],now(),secrets.randbits(62)+1))
            return self._device_view(c.execute("SELECT * FROM devices WHERE id=?",(device_id,)).fetchone())

    def remove_device(self, user_id, session_id, device_id):
        with self.transaction(True) as c:
            self._admin_session(c,user_id,session_id)
            c.execute("DELETE FROM devices WHERE id=? AND user_id=?", (device_id,user_id))

    def resolve_external_identity(self, project_id, external_id):
        with self.transaction(True) as c:
            return self._resolve_external_identity(c, project_id, external_id)

    def _resolve_external_identity(self, c, project_id, external_id):
        self._active_project(c, project_id)
        stored_id = self._encode_external_id(external_id)
        r = c.execute("SELECT user_id FROM external_identities WHERE project_id=? AND external_user_id=?", (project_id, stored_id)).fetchone()
        if r:
            return r[0]
        uid, ts = new_id(), now()
        c.execute("INSERT INTO users VALUES(?,NULL,'participant',?,?)", (uid, ts, ts))
        c.execute("INSERT INTO external_identities VALUES(?,?,?,?,?)", (new_id(), project_id, stored_id, uid, ts))
        return uid

    @staticmethod
    def _active_project(c, pid):
        row = c.execute('SELECT archived_at FROM projects WHERE id=?', (pid,)).fetchone()
        if not row: missing()
        if row['archived_at']:
            raise Error(409, 'resource_archived', 'This project is archived. Restore it before making changes.')

    @staticmethod
    def _active_board(c, bid):
        row = c.execute('SELECT b.archived_at,p.archived_at project_archived_at FROM boards b JOIN projects p ON p.id=b.project_id WHERE b.id=?', (bid,)).fetchone()
        if not row: missing()
        if row['archived_at'] or row['project_archived_at']:
            raise Error(409, 'resource_archived', 'This board or its project is archived. Restore it before making changes.')

    def resource_archive(self, kind, ref, principal, archived, version):
        if kind not in ('project', 'board') or type(archived) is not bool:
            raise Error(400, 'invalid_input', 'Invalid archive operation')
        table = 'projects' if kind == 'project' else 'boards'
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            row = c.execute(f'SELECT * FROM {table} WHERE id=?', (ref,)).fetchone()
            if not row: missing()
            if row['version'] != version: conflict()
            if bool(row['archived_at']) == archived:
                result = dict(row)
                if kind == 'board':
                    result['project_archived_at'] = c.execute('SELECT archived_at FROM projects WHERE id=?', (row['project_id'],)).fetchone()[0]
                return result
            pid = ref if kind == 'project' else row['project_id']
            if kind == 'board' and not archived: self._active_project(c, pid)
            ts = now()
            c.execute(f'UPDATE {table} SET archived_at=?,updated_at=?,version=version+1 WHERE id=?', (ts if archived else None, ts, ref))
            c.execute('INSERT INTO resource_archive_events VALUES(?,?,?,?,?,?,?,?)', (new_id(),kind,ref,pid,principal['user_id'],archived,version+1,ts))
            if archived:
                # Fence generation/proposals immediately; restoring never revives old work.
                if kind == 'board':
                    c.execute("UPDATE ai_proposals SET state='cancelled',version=version+1 WHERE board_id=? AND state='pending'", (ref,))
                if kind == 'project':
                    self._ai_cancel_generation(c, pid)
                    c.execute("UPDATE ai_jobs SET state='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? WHERE project_id=? AND state IN ('queued','running')", (ts,pid))
                    c.execute('UPDATE chat_sessions SET revoked_at=COALESCE(revoked_at,?) WHERE conversation_id IN (SELECT id FROM chat_conversations WHERE project_id=?)', (ts,pid))
                    c.execute('UPDATE chat_customer_sessions SET revoked_at=COALESCE(revoked_at,?) WHERE customer_id IN (SELECT id FROM chat_customers WHERE project_id=?)', (ts,pid))
            if kind == 'project':
                # Version changes also fence reads that straddle archive + restore.
                c.execute('UPDATE connected_apps SET version=version+1,updated_at=? WHERE project_id=?', (ts,pid))
                c.execute('UPDATE ai_settings SET version=version+1,updated_at=? WHERE project_id=?', (ts,pid))
        return self.project(ref) if kind == 'project' else self.board(ref)

    def projects(self, limit, offset, archive="active"):
        with self.transaction() as c:
            return [dict(x) for x in c.execute("SELECT * FROM projects WHERE (?='all' OR (?='archived')=(archived_at IS NOT NULL)) ORDER BY created_at,id LIMIT ? OFFSET ?", (archive, archive, limit, offset))]

    def project(self, ref):
        with self.transaction() as c:
            r = c.execute("SELECT * FROM projects WHERE id=? OR identifier=?", (ref, ref)).fetchone()
            if not r:
                missing()
            return dict(r)

    def create_project(self, data, user_id):
        pid, ts = new_id(), now()
        with self.transaction(True) as c:
            c.execute("INSERT INTO projects(id,identifier,name,description,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (pid, data['identifier'], data['name'], data['description'], user_id, ts, ts))
        return self.project(pid)

    def update_project(self, pid, data):
        with self.transaction(True) as c:
            r = c.execute("UPDATE projects SET name=?,description=?,updated_at=?,version=version+1 WHERE id=? AND version=?", (data['name'], data['description'], now(), pid, data['version']))
            if not r.rowcount:
                conflict()
        return self.project(pid)

    def boards(self, pid, limit=101, offset=0, archive="active", *, include_private=False):
        with self.transaction() as c:
            return [dict(x) for x in c.execute("SELECT b.*,p.archived_at project_archived_at FROM boards b JOIN projects p ON p.id=b.project_id WHERE b.project_id=? AND (?=1 OR b.visibility='public') AND (?='all' OR (?='archived')=(b.archived_at IS NOT NULL)) ORDER BY b.created_at,b.id LIMIT ? OFFSET ?", (pid, include_private, archive, archive, limit, offset))]

    def board(self, bid):
        with self.transaction() as c:
            r = c.execute("SELECT b.*,p.archived_at project_archived_at FROM boards b JOIN projects p ON p.id=b.project_id WHERE b.id=?", (bid,)).fetchone()
            if not r:
                missing()
            return dict(r)

    def _board_manager(self, c, pid, actor, session_id):
        if actor.key_id:
            key = c.execute('SELECT * FROM api_keys WHERE id=? AND project_id=? AND revoked_at IS NULL', (actor.key_id, pid)).fetchone()
            if not key or 'manage' not in json.loads(key['permissions']):
                raise Error(403, 'forbidden', 'Management permission required')
        else:
            self._admin_session(c, actor.user_id, session_id)

    def create_board(self, pid, data, *, actor=None, session_id=None):
        bid, ts = new_id(), now()
        with self.transaction(True) as c:
            if actor is not None: self._board_manager(c, pid, actor, session_id)
            self._active_project(c, pid)
            c.execute("INSERT INTO boards(id,project_id,identifier,name,description,created_at,updated_at,visibility) VALUES(?,?,?,?,?,?,?,?)", (bid, pid, data['identifier'], data['name'], data['description'], ts, ts, data.get('visibility', 'public')))
            return dict(c.execute("SELECT b.*,p.archived_at project_archived_at FROM boards b JOIN projects p ON p.id=b.project_id WHERE b.id=?", (bid,)).fetchone())

    def update_board(self, bid, data, *, actor=None, session_id=None):
        with self.transaction(True) as c:
            board = c.execute('SELECT * FROM boards WHERE id=?', (bid,)).fetchone()
            if not board: missing()
            if actor is not None: self._board_manager(c, board['project_id'], actor, session_id)
            if not c.execute("UPDATE boards SET name=?,description=?,visibility=COALESCE(?,visibility),updated_at=?,version=version+1 WHERE id=? AND version=?", (data['name'], data.get('description', ''), data.get('visibility'), now(), bid, data['version'])).rowcount:
                conflict()
            return dict(c.execute("SELECT b.*,p.archived_at project_archived_at FROM boards b JOIN projects p ON p.id=b.project_id WHERE b.id=?", (bid,)).fetchone())

    def create_api_key(self, pid, name, secret_hash, prefix, permissions, user_id):
        kid = new_id()
        with self.transaction(True) as c:
            c.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?,NULL)", (kid, pid, name, secret_hash, prefix, json.dumps(permissions), user_id, now()))
        return kid

    def api_key(self, secret_hash):
        with self.transaction() as c:
            r = c.execute("SELECT * FROM api_keys WHERE secret_hash=? AND revoked_at IS NULL", (secret_hash,)).fetchone()
            return dict(r) if r else None

    def api_keys(self, pid, limit=101, offset=0):
        with self.transaction() as c:
            return [dict(x) for x in c.execute("SELECT id,name,prefix,permissions,created_at,revoked_at FROM api_keys WHERE project_id=? ORDER BY created_at,id LIMIT ? OFFSET ?", (pid, limit, offset))]

    def revoke_api_key(self, pid, kid):
        with self.transaction(True) as c:
            if not c.execute("UPDATE api_keys SET revoked_at=COALESCE(revoked_at,?) WHERE id=? AND project_id=?", (now(), kid, pid)).rowcount:
                missing()

    def _request(self, c, rid, user_id=None):
        r = c.execute("""SELECT r.*,b.project_id,b.identifier board_identifier,b.name board_name,
            b.visibility board_visibility,p.identifier project_identifier,p.name project_name,p.archived_at project_archived_at,b.archived_at board_archived_at,u.display_name creator_name,
            (SELECT count(*) FROM votes WHERE request_id=r.id) vote_count,
            EXISTS(SELECT 1 FROM votes WHERE request_id=r.id AND user_id=?) voted
            FROM requests r JOIN boards b ON b.id=r.board_id JOIN projects p ON p.id=b.project_id
            JOIN users u ON u.id=r.creator_id WHERE r.id=?""", (user_id, rid)).fetchone()
        if not r:
            missing()
        return dict(r)

    def request(self, rid, user_id=None):
        with self.transaction() as c:
            return self._request(c, rid, user_id)

    def list_requests(self, *, limit, offset, board_id=None, project_id=None, creator_id=None, moderation="accepted", status=None, order="newest", user_id=None, include_archived=False, include_private=False):
        filters, args = ([] if include_archived else ["b.archived_at IS NULL", "NOT EXISTS(SELECT 1 FROM projects p WHERE p.id=b.project_id AND p.archived_at IS NOT NULL)"]), []
        if not include_private:
            filters.append("(b.visibility='public' OR r.creator_id=?)")
            args.append(user_id)
        for field, value in (("r.board_id", board_id), ("b.project_id", project_id), ("r.creator_id", creator_id), ("r.moderation", moderation), ("r.status", status)):
            if value is not None:
                filters.append(f"{field}=?")
                args.append(value)
        sort = "(SELECT count(*) FROM votes WHERE request_id=r.id) DESC," if order == "popular" else ""
        with self.transaction() as c:
            ids = c.execute(f"SELECT r.id FROM requests r JOIN boards b ON b.id=r.board_id WHERE {' AND '.join(filters) or '1=1'} ORDER BY {sort}r.created_at DESC,r.id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            return [self._request(c, x[0], user_id) for x in ids]

    def _identity(self, c, pid, uid):
        r = c.execute("SELECT external_user_id FROM external_identities WHERE project_id=? AND user_id=?", (pid, uid)).fetchone()
        return {"user_id": uid, "external_user_id": self._decode_external_id(r[0]) if r else None}

    def _event(self, c, kind, r, actor, previous=None):
        eid, ts = new_id(), now()
        pid = r['project_id']
        snapshot = {k: r[k] for k in ('id', 'title', 'description', 'moderation', 'status', 'accepted_at', 'rejection_reason', 'version')}
        payload = {"id": eid, "type": kind, "schema_version": 1, "occurred_at": ts,
                   "project": {"id": pid, "identifier": r['project_identifier']},
                   "board": {"id": r['board_id'], "identifier": r['board_identifier']},
                   "request_id": r['id'], "url": f"{self.base_url}/requests/{r['id']}",
                   "request": snapshot, "previous": previous,
                   "actor": actor.record(), "creator": self._identity(c, pid, r['creator_id'])}
        self._persist_event(c, pid, r['board_id'], r['id'], payload, ip_address=actor.ip_address)
        return eid

    def _persist_event(self, c, pid, bid, rid, payload, force=False, *, ip_address=None, notify=True):
        body = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
        c.execute("INSERT INTO request_events(id,type,schema_version,project_id,board_id,request_id,actor,occurred_at,payload,ip_address) VALUES(?,?,?,?,?,?,?,?,?,?)", (payload['id'], payload['type'], payload['schema_version'], pid, bid, rid, json.dumps(payload['actor']), payload['occurred_at'], body, ip_address))
        if notify:
            self._queue_notification(c, pid, rid, payload)
        if payload['type']=='request.created':
            self._queue_push(c,payload['id'],'request.created',rid)
        p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if (force or p['webhook_enabled']) and p['webhook_url'] and p['webhook_secret'] and (force or payload['type'] in json.loads(p['webhook_events'])):
            c.execute("INSERT INTO webhook_deliveries(id,event_id,destination,signing_secret,state,next_attempt_at) VALUES(?,?,?,?,'queued',?)", (new_id(), payload['id'], p['webhook_url'], p['webhook_secret'], time.time()))

    @staticmethod
    def _queue_notification(c, project_id, request_id, payload):
        kind = payload['type']
        if kind in ('request.created', 'request.resubmitted'):
            title, resource_kind, resource_id = payload['request']['title'], 'request', request_id
        elif kind == 'chat.message_created' and payload['message']['sender_role'] == 'customer':
            title, resource_kind, resource_id = 'New customer message', 'conversation', payload['conversation']['id']
        elif kind == 'chat.support_mode_changed' and payload['conversation']['support_mode'] == 'waiting_human':
            title, resource_kind, resource_id = 'Human support requested', 'conversation', payload['conversation']['id']
        elif kind == 'form.submission_created':
            title, resource_kind, resource_id = 'New form submission', 'form_submission', payload['submission_id']
        else:
            return
        c.execute("""INSERT INTO admin_notifications(id,event_id,type,project_id,title,resource_kind,resource_id,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (payload['id'], payload['id'], kind, project_id, title, resource_kind, resource_id, payload['occurred_at']))

    @staticmethod
    def _notification_summary(c):
        return dict(c.execute("""SELECT
            (SELECT count(*) FROM admin_notifications WHERE read_at IS NULL) unread_count,
            (SELECT coalesce(max(sequence),0) FROM admin_notifications) through_sequence""").fetchone())

    @staticmethod
    def _notification_view(row):
        result = {k: row[k] for k in ('id', 'sequence', 'type', 'project_id', 'project_name', 'title', 'created_at', 'read_at', 'resource_kind', 'resource_id')}
        result['url'] = (f"/requests/{row['resource_id']}" if row['resource_kind'] == 'request'
                         else f"/admin/form-submissions/{row['resource_id']}" if row['resource_kind']=='form_submission'
                         else f"/admin/chat?conversation={row['resource_id']}")
        result['resource_available'] = bool(row['resource_available'])
        return result

    @staticmethod
    def _notification_query():
        return """SELECT n.*,p.name project_name,
                  CASE n.resource_kind
                    WHEN 'request' THEN EXISTS(SELECT 1 FROM requests r JOIN boards b ON b.id=r.board_id WHERE r.id=n.resource_id AND b.project_id=n.project_id)
                    WHEN 'form_submission' THEN EXISTS(SELECT 1 FROM form_submissions s JOIN forms f ON f.id=s.form_id WHERE s.id=n.resource_id AND f.project_id=n.project_id AND s.deleted_at IS NULL)
                    ELSE EXISTS(SELECT 1 FROM chat_conversations cv WHERE cv.id=n.resource_id AND cv.project_id=n.project_id)
                  END resource_available
                  FROM admin_notifications n JOIN projects p ON p.id=n.project_id"""

    def notifications(self, user_id, session_id, unread_only, limit, offset):
        with self.transaction() as c:
            self._admin_session(c, user_id, session_id)
            # Rows and watermark share a snapshot. Mark-all can acknowledge only
            # that snapshot even if another customer submits while it is open.
            unread_filter = " WHERE n.read_at IS NULL" if unread_only else ""
            rows = c.execute(self._notification_query() + unread_filter + " ORDER BY n.sequence DESC LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
            return {'items': [self._notification_view(row) for row in rows[:limit]],
                    'limit': limit, 'offset': offset, 'has_more': len(rows) > limit,
                    **self._notification_summary(c)}

    def notification_summary(self, user_id, session_id):
        with self.transaction() as c:
            self._admin_session(c, user_id, session_id)
            return self._notification_summary(c)

    def set_notification_read(self, user_id, session_id, notification_id, read):
        with self.transaction(True) as c:
            self._admin_session(c, user_id, session_id)
            changed = c.execute("UPDATE admin_notifications SET read_at=CASE WHEN ?=1 THEN coalesce(read_at,?) ELSE NULL END WHERE id=?", (read, now(), notification_id))
            if not changed.rowcount:
                missing()
            return self._notification_view(c.execute(self._notification_query() + " WHERE n.id=?", (notification_id,)).fetchone())

    def read_all_notifications(self, user_id, session_id, through_sequence):
        with self.transaction(True) as c:
            self._admin_session(c, user_id, session_id)
            c.execute("UPDATE admin_notifications SET read_at=? WHERE read_at IS NULL AND sequence<=?", (now(), through_sequence))
            return self._notification_summary(c)

    def create_request(self, board_id, title, description, actor):
        rid, ts = new_id(), now()
        with self.transaction(True) as c:
            self._active_board(c, board_id)
            c.execute("INSERT INTO requests(id,board_id,creator_id,title,description,moderation,created_at,updated_at,submitted_ip_address) VALUES(?,?,?,?,?,'pending',?,?,?)", (rid, board_id, actor.user_id, title, description, ts, ts, actor.ip_address))
            r = self._request(c, rid, actor.user_id)
            self._event(c, "request.created", r, actor)
            return r

    def update_creator_request(self, rid, title, description, version, actor, delete=False):
        with self.transaction(True) as c:
            r = self._request(c, rid, actor.user_id)
            if r['creator_id'] != actor.user_id or (actor.key_id and r['project_id'] != actor.project_id):
                missing()
            if r['accepted_at'] is not None:
                conflict("Accepted requests cannot be edited or deleted by their creator")
            if r['version'] != version:
                conflict()
            self._active_board(c, r['board_id'])
            previous = {k: r[k] for k in ('title', 'description', 'moderation', 'rejection_reason')}
            if delete:
                self._event(c, "request.deleted", r, actor, previous)
                c.execute("DELETE FROM requests WHERE id=?", (rid,))
                return None
            resubmit = r['moderation'] == 'rejected'
            if not resubmit and r['title'] == title and r['description'] == description:
                return r
            c.execute("UPDATE requests SET title=?,description=?,moderation='pending',rejection_reason=NULL,updated_at=?,version=version+1 WHERE id=?", (title, description, now(), rid))
            r = self._request(c, rid, actor.user_id)
            self._event(c, "request.resubmitted" if resubmit else "request.updated", r, actor, previous)
            return r

    def moderate_request(self, rid, action, value, version, actor):
        with self.transaction(True) as c:
            r = self._request(c, rid, actor.user_id)
            if not actor.manages(r['project_id']):
                raise Error(403, "forbidden", "Management permission required")
            if r['version'] != version:
                conflict()
            self._active_board(c, r['board_id'])
            previous = {k: r[k] for k in ('moderation', 'status', 'rejection_reason')}
            if action in ('accept', 'reject'):
                if r['moderation'] != 'pending':
                    conflict("Only pending requests can be moderated")
                if action == 'accept':
                    c.execute("UPDATE requests SET moderation='accepted',status='under_consideration',accepted_at=?,accepted_by=?,rejection_reason=NULL,updated_at=?,version=version+1 WHERE id=?", (now(), json.dumps(actor.record()), now(), rid))
                else:
                    c.execute("UPDATE requests SET moderation='rejected',rejection_reason=?,updated_at=?,version=version+1 WHERE id=?", (value, now(), rid))
                kind = 'request.accepted' if action == 'accept' else 'request.rejected'
            elif action == 'status':
                if r['moderation'] != 'accepted':
                    conflict("Lifecycle status requires an accepted request")
                if r['status'] == value:
                    return r
                c.execute("UPDATE requests SET status=?,updated_at=?,version=version+1 WHERE id=?", (value, now(), rid))
                kind = 'request.status_changed'
            else:
                title, description = value
                if (r['title'], r['description']) == (title, description):
                    return r
                previous = {k: r[k] for k in ('title', 'description')}
                c.execute("UPDATE requests SET title=?,description=?,updated_at=?,version=version+1 WHERE id=?", (title, description, now(), rid))
                kind = 'request.updated'
            r = self._request(c, rid, actor.user_id)
            self._event(c, kind, r, actor, previous)
            return r

    def set_vote(self, rid, present, actor):
        with self.transaction(True) as c:
            r = self._request(c, rid, actor.user_id)
            if actor.key_id and r['project_id'] != actor.project_id:
                missing()
            if r['moderation'] != 'accepted':
                missing()
            if r['board_visibility'] == 'private' and r['creator_id'] != actor.user_id and not (actor.manages(r['project_id']) or (actor.key_id and actor.can(r['project_id'], 'read'))):
                missing()
            self._active_board(c, r['board_id'])
            if present:
                c.execute('INSERT INTO votes VALUES(?,?,?) ON CONFLICT DO NOTHING', (rid, actor.user_id, now()))
            else:
                c.execute("DELETE FROM votes WHERE request_id=? AND user_id=?", (rid, actor.user_id))
            r = self._request(c, rid, actor.user_id)
            return {"vote_count": r['vote_count'], "voted": bool(r['voted'])}

    def history(self, rid, limit, offset, public=False):
        with self.transaction() as c:
            rows = c.execute("SELECT payload,ip_address FROM request_events WHERE request_id=? AND (?=0 OR type IN ('request.accepted','request.status_changed')) ORDER BY occurred_at,id LIMIT ? OFFSET ?", (rid, public, limit, offset))
            return [dict(json.loads(x['payload']), **({} if public else {'ip_address': x['ip_address']})) for x in rows]

    def audience(self, rid, limit, offset):
        with self.transaction() as c:
            r = self._request(c, rid)
            ids = c.execute("SELECT user_id FROM votes WHERE request_id=? UNION SELECT creator_id FROM requests WHERE id=? ORDER BY 1 LIMIT ? OFFSET ?", (rid, rid, limit, offset)).fetchall()
            return [dict(self._identity(c, r['project_id'], x[0]), creator=x[0] == r['creator_id']) for x in ids]

    def webhook_settings(self, pid):
        p = self.project(pid)
        return {"url": p['webhook_url'], "enabled": bool(p['webhook_enabled']), "events": json.loads(p['webhook_events']), "has_secret": bool(p['webhook_secret']), "version": p['version']}

    def update_webhook(self, pid, url, enabled, events, secret, version):
        with self.transaction(True) as c:
            if not c.execute("UPDATE projects SET webhook_url=?,webhook_enabled=?,webhook_events=?,webhook_secret=COALESCE(?,webhook_secret),version=version+1,updated_at=? WHERE id=? AND version=?", (url, enabled, json.dumps(events), secret, now(), pid, version)).rowcount:
                conflict()
        return self.webhook_settings(pid)

    def test_webhook(self, pid, actor):
        with self.transaction(True) as c:
            p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            if not p['webhook_url'] or not p['webhook_secret']:
                conflict("Configure a destination and signing secret first")
            eid = new_id()
            payload = {"id": eid, "type": "webhook.test", "schema_version": 1, "occurred_at": now(), "project": {"id": pid, "identifier": p['identifier']}, "actor": actor.record(), "test": True}
            self._persist_event(c, pid, None, None, payload, force=True, ip_address=actor.ip_address)
            return {"event_id": eid}

    def deliveries(self, pid, limit, offset, failed=False):
        with self.transaction() as c:
            return [dict(x) for x in c.execute("SELECT d.id,d.event_id,e.type,d.destination,d.state,d.attempt_count,d.next_attempt_at,d.last_attempt_at,d.last_http_status,d.error,d.delivered_at FROM webhook_deliveries d JOIN request_events e ON e.id=d.event_id WHERE e.project_id=? AND (?=0 OR d.state='failed') ORDER BY e.occurred_at DESC,d.id LIMIT ? OFFSET ?", (pid, failed, limit, offset))]

    def delivery(self, pid, did):
        with self.transaction() as c:
            r = c.execute("SELECT d.id,d.event_id,d.destination,d.state,d.attempt_count,d.next_attempt_at,d.last_attempt_at,d.last_http_status,d.error,d.delivered_at,e.payload FROM webhook_deliveries d JOIN request_events e ON e.id=d.event_id WHERE d.id=? AND e.project_id=?", (did, pid)).fetchone()
            if not r:
                missing()
            result = dict(r)
            result['payload'] = json.loads(result['payload'])
            return result

    def retry_delivery(self, pid, did):
        with self.transaction(True) as c:
            r = c.execute("SELECT d.state FROM webhook_deliveries d JOIN request_events e ON e.id=d.event_id WHERE d.id=? AND e.project_id=?", (did, pid)).fetchone()
            if not r:
                missing()
            if r[0] != 'failed':
                conflict("Only failed deliveries may be retried")
            c.execute("UPDATE webhook_deliveries SET state='queued',attempt_count=0,next_attempt_at=?,lease_until=NULL,lease_token=NULL,error=NULL WHERE id=?", (time.time(), did))

    def claim_delivery(self, current_time, lease_seconds=60):
        with self.transaction(True) as c:
            r = c.execute("SELECT d.*,e.payload FROM webhook_deliveries d JOIN request_events e ON e.id=d.event_id WHERE (d.state='queued' AND d.next_attempt_at<=?) OR (d.state='delivering' AND d.lease_until<=?) ORDER BY d.next_attempt_at,d.id LIMIT 1", (current_time, current_time)).fetchone()
            if not r:
                return None
            result = dict(r)
            token = new_id()
            c.execute("UPDATE webhook_deliveries SET state='delivering',attempt_count=attempt_count+1,lease_until=?,lease_token=?,last_attempt_at=? WHERE id=?", (current_time + lease_seconds, token, now(), r['id']))
            result['lease_token'] = token
            result['attempt_count'] += 1
            return result

    def finish_delivery(self, did, token, state, status, error, next_attempt):
        with self.transaction(True) as c:
            c.execute("UPDATE webhook_deliveries SET state=?,last_http_status=?,error=?,next_attempt_at=?,lease_until=NULL,lease_token=NULL,delivered_at=? WHERE id=? AND state='delivering' AND lease_token=?", (state, status, error, next_attempt, now() if state == 'succeeded' else None, did, token))

    def dashboard(self):
        with self.transaction() as c:
            return {"pending_requests": c.execute("SELECT count(*) FROM requests r JOIN boards b ON b.id=r.board_id JOIN projects p ON p.id=b.project_id WHERE r.moderation='pending' AND b.archived_at IS NULL AND p.archived_at IS NULL").fetchone()[0], "failed_deliveries": c.execute("SELECT count(*) FROM webhook_deliveries WHERE state='failed'").fetchone()[0]}

    def _hours_scope(self, c, project):
        if project is None: return 'account', None
        row=c.execute('SELECT id FROM projects WHERE id=? OR identifier=?',(project,project)).fetchone()
        if not row: missing()
        return 'project:'+row[0], row[0]

    def _hours_read(self, c, project):
        from feedback.business_hours import WeeklyHours
        scope, pid=self._hours_scope(c,project)
        row=c.execute('SELECT * FROM chat_business_hours WHERE scope=?',(scope,)).fetchone()
        account=c.execute("SELECT * FROM chat_business_hours WHERE scope='account'").fetchone()
        own=json.loads(row['schedule']) if row else WeeklyHours().model_dump()
        mode=row['mode'] if row else ('inherit' if pid else 'override')
        effective=(json.loads(account['schedule']) if account else WeeklyHours().model_dump()) if mode=='inherit' else dict(own)
        if mode=='off': effective['enabled']=False
        return {'version':row['version'] if row else 0,'mode':mode,'schedule':own,
                'effective':effective,'source':'account' if not pid or mode=='inherit' else 'project'}

    def chat_business_hours(self, project=None, principal=None):
        with self.transaction() as c:
            if principal: self._admin_session(c,principal['user_id'],principal['session_id'])
            return self._hours_read(c,project)

    def chat_business_hours_save(self, project, principal, values):
        from feedback.business_hours import HoursEdit
        data=HoursEdit.model_validate(values)
        if project is None and data.mode!='override':
            raise Error(400,'invalid_input','Account hours do not inherit another schedule')
        with self.transaction(True) as c:
            self._admin_session(c,principal['user_id'],principal['session_id'])
            old=self._hours_read(c,project)
            if old['version']!=data.version: conflict('Business hours changed. Reload before saving.')
            scope,pid=self._hours_scope(c,project)
            c.execute('INSERT INTO chat_business_hours(scope,project_id,mode,schedule,version,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET mode=excluded.mode,schedule=excluded.schedule,version=excluded.version,updated_at=excluded.updated_at',
                      (scope,pid,data.mode,json.dumps(data.schedule.model_dump()),data.version+1,now()))
            c.execute('INSERT INTO chat_business_hours_audit VALUES(?,?,?,?,?)',(new_id(),scope,principal['user_id'],data.version+1,now()))
            return self._hours_read(c,project)

    def chat_contact_clear(self, conversation_id, principal, version):
        with self.transaction(True) as c:
            row=self.chat_authorize(c,conversation_id,principal)
            if row['contact_version']!=version: conflict('Contact details changed. Reload before removing them.')
            if row['follow_up_email'] is not None:
                c.execute('UPDATE chat_conversations SET follow_up_email=NULL,contact_version=contact_version+1 WHERE id=?',(conversation_id,))
            return self.chat_view(c,conversation_id,principal)

    # Chat uses its own durable domain; each operation rechecks scoped authority.
    def chat_settings(self, project):
        with self.transaction() as c:
            row = c.execute("SELECT p.id project_id,p.identifier project_identifier,p.name project_name,COALESCE(s.enabled,0) enabled,COALESCE(s.origins,'[]') origins,COALESCE(s.version,0) version FROM projects p LEFT JOIN chat_settings s ON s.project_id=p.id WHERE p.id=? OR p.identifier=?", (project,project)).fetchone()
            if not row:
                missing()
            result = dict(row)
            result['origins'], result['enabled'] = json.loads(result['origins']), bool(result['enabled'])
            return result

    def chat_configure(self, project, principal, enabled, origins, version):
        with self.transaction(True) as c:
            self._admin_session(c, principal['user_id'], principal['session_id'])
            p = c.execute("SELECT id FROM projects WHERE id=? OR identifier=?", (project,project)).fetchone()
            if not p:
                missing()
            old = c.execute("SELECT version FROM chat_settings WHERE project_id=?", (p[0],)).fetchone()
            if (old[0] if old else 0) != version:
                conflict()
            c.execute("INSERT INTO chat_settings(project_id,enabled,origins,version) VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET enabled=excluded.enabled,origins=excluded.origins,version=excluded.version", (p[0],enabled,json.dumps(origins),version+1))
        return self.chat_settings(project)

    def _chat_customer_view(self, c, customer_id):
        row = c.execute("SELECT cc.*,u.display_name native_name FROM chat_customers cc LEFT JOIN users u ON u.id=cc.user_id WHERE cc.id=?", (customer_id,)).fetchone()
        result = {k:row[k] for k in ('id','kind','display_name','user_id')}
        if row['kind']=='native':
            result['display_name'] = row['native_name']
        if not result['display_name'] or not result['display_name'].strip():
            result['display_name'] = 'Website visitor' if row['kind']=='anonymous' else 'Signed-in customer'
        identity = c.execute("SELECT external_user_id FROM external_identities WHERE project_id=? AND user_id=?", (row['project_id'],row['user_id'])).fetchone() if row['kind']=='external' else None
        result['external_user_id'] = self._decode_external_id(identity[0]) if identity else None
        return result

    def _chat_customer_origin(self, c, project_id, origin, kind):
        self._active_project(c, project_id)
        settings = c.execute("SELECT * FROM chat_settings WHERE project_id=?", (project_id,)).fetchone()
        if not settings or not settings['enabled']:
            raise Error(403,'chat_disabled','Chat is not enabled for this project')
        # No Origin denotes a native customer client using its scoped bearer.
        # Browser credentials always bind an explicitly permitted website origin.
        own_native = kind=='native' and origin==self.base_url
        if origin is not None and not own_native and origin not in json.loads(settings['origins']):
            raise Error(403,'origin_denied','This website is not enabled for chat')

    def _chat_customer_issuer(self, c, project_id, actor, external_id, parent_session_id, create):
        if actor.key_id:
            key = c.execute("SELECT * FROM api_keys WHERE id=? AND revoked_at IS NULL", (actor.key_id,)).fetchone()
            if not key:
                raise Error(401,'unauthorized','Integration credential expired or revoked')
            if key['project_id']!=project_id or actor.project_id!=project_id:
                missing()
            if 'participate' not in json.loads(key['permissions']):
                raise Error(403,'forbidden','Participation permission required')
            if not isinstance(external_id,str) or not external_id or len(external_id)>512:
                raise Error(400,'invalid_input','Supply an external user ID of 1–512 characters')
            if parent_session_id is not None:
                raise Error(400,'invalid_input','Integration sessions cannot have a native parent')
            if create:
                uid = self._resolve_external_identity(c,project_id,external_id)
            else:
                identity = c.execute("SELECT user_id FROM external_identities WHERE project_id=? AND external_user_id=?", (project_id,self._encode_external_id(external_id))).fetchone()
                uid = identity[0] if identity else None
            return uid,'external'
        if external_id is not None:
            raise Error(400,'invalid_input','Native accounts cannot supply an external identity')
        session = c.execute("SELECT 1 FROM sessions s JOIN local_credentials l ON l.user_id=s.user_id WHERE s.id=? AND s.user_id=? AND s.revoked_at IS NULL AND s.expires_at>?", (parent_session_id,actor.user_id,time.time())).fetchone()
        if not session:
            raise Error(401,'unauthorized','Account session expired or revoked')
        return actor.user_id,'native'

    def chat_create_customer_session(self, project_id, actor, external_id, token_hash, expires, origin, parent_session_id=None, display_name=None):
        with self.transaction(True) as c:
            uid,kind = self._chat_customer_issuer(c,project_id,actor,external_id,parent_session_id,True)
            self._chat_customer_origin(c,project_id,origin,kind)
            if display_name is not None and (not isinstance(display_name,str) or len(display_name)>100):
                raise Error(400,'invalid_input','Display names must contain at most 100 characters')
            customer = c.execute("SELECT * FROM chat_customers WHERE project_id=? AND user_id=?", (project_id,uid)).fetchone()
            ts, sid = now(),new_id()
            if not customer:
                customer_id = new_id()
                c.execute("INSERT INTO chat_customers VALUES(?,?,?,?,?,?,?)", (customer_id,project_id,uid,kind,display_name if kind=='external' else None,ts,ts))
            else:
                customer_id = customer['id']
                if customer['kind']!=kind:
                    conflict('Customer identities cannot be linked implicitly')
                if kind=='external' and display_name is not None:
                    c.execute("UPDATE chat_customers SET display_name=?,updated_at=? WHERE id=?", (display_name,ts,customer_id))
            c.execute("DELETE FROM chat_customer_sessions WHERE expires_at<=?", (time.time(),))
            if c.execute("SELECT count(*) FROM chat_customer_sessions WHERE customer_id=? AND revoked_at IS NULL", (customer_id,)).fetchone()[0]>=100:
                raise Error(429,'rate_limited','Too many active chat sessions; revoke an old session')
            c.execute("INSERT INTO chat_customer_sessions(id,customer_id,token_hash,origin,issuing_key_id,parent_session_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)", (sid,customer_id,token_hash,origin,actor.key_id,parent_session_id,ts,min(expires,time.time()+3600)))
            return {'customer':self._chat_customer_view(c,customer_id),'session_id':sid}

    def _chat_customer_authorize(self, c, principal):
        if principal['kind']=='visitor':
            session = c.execute("SELECT s.*,cv.customer_id FROM chat_sessions s JOIN chat_conversations cv ON cv.id=s.conversation_id WHERE s.id=? AND s.revoked_at IS NULL AND s.expires_at>?", (principal['session_id'],time.time())).fetchone()
        elif principal['kind']=='customer':
            session = c.execute("SELECT * FROM chat_customer_sessions WHERE id=? AND revoked_at IS NULL AND expires_at>?", (principal['session_id'],time.time())).fetchone()
        else:
            raise Error(403,'forbidden','Customer session required')
        if not session:
            if principal['kind']=='visitor':
                missing()
            raise Error(401,'unauthorized','Chat session expired or revoked')
        if session['origin'] != principal.get('origin'):
            raise Error(403,'origin_denied','This chat credential belongs to another origin')
        customer = c.execute("SELECT * FROM chat_customers WHERE id=?", (session['customer_id'],)).fetchone()
        if principal['kind']=='customer':
            if session['issuing_key_id']:
                key = c.execute("SELECT * FROM api_keys WHERE id=? AND project_id=? AND revoked_at IS NULL", (session['issuing_key_id'],customer['project_id'])).fetchone()
                if not key or 'participate' not in json.loads(key['permissions']) or customer['kind']!='external':
                    raise Error(401,'unauthorized','Parent integration credential expired or revoked')
            elif customer['kind']!='native' or not c.execute("SELECT 1 FROM sessions WHERE id=? AND user_id=? AND revoked_at IS NULL AND expires_at>?", (session['parent_session_id'],customer['user_id'],time.time())).fetchone():
                raise Error(401,'unauthorized','Parent account session expired or revoked')
        self._chat_customer_origin(c,customer['project_id'],session['origin'],customer['kind'])
        return customer

    def chat_customer_session(self, token_hash, origin):
        with self.transaction() as c:
            session = c.execute("SELECT id FROM chat_customer_sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if not session:
                raise Error(401,'unauthorized','Chat session expired or revoked')
            principal = {'kind':'customer','session_id':session['id'],'origin':origin}
            customer = self._chat_customer_authorize(c,principal)
            return {**principal,'user_id':customer['user_id'],'customer_id':customer['id'],'project_id':customer['project_id']}

    def chat_customer_conversations(self, principal, state, limit, offset):
        with self.transaction() as c:
            customer = self._chat_customer_authorize(c,principal)
            rows = c.execute("SELECT id FROM chat_conversations WHERE customer_id=? AND (? IS NULL OR state=?) ORDER BY updated_at DESC,id LIMIT ? OFFSET ?", (customer['id'],state,state,limit,offset)).fetchall()
            return [self.chat_view(c,row[0],principal) for row in rows]

    def chat_create_conversation(self, principal, client_conversation_id):
        with self.transaction(True) as c:
            customer = self._chat_customer_authorize(c,principal)
            old = c.execute("SELECT id FROM chat_conversations WHERE customer_id=? AND client_conversation_id=?", (customer['id'],client_conversation_id)).fetchone()
            if old:
                return self.chat_view(c,old[0],principal)
            # Keep anonymous credentials scoped to the conversation originally
            # created by the legacy endpoint; they can read it through this API.
            if principal['kind']=='visitor':
                raise Error(403,'forbidden','Start a new anonymous session for a new conversation')
            if c.execute("SELECT count(*) FROM chat_conversations WHERE project_id=? AND created_at>=?", (customer['project_id'],now()[:10])).fetchone()[0]>=1000:
                raise Error(429,'rate_limited','Chat is busy; please try again later')
            if c.execute("SELECT count(*) FROM chat_conversations WHERE customer_id=? AND created_at>=?", (customer['id'],now()[:10])).fetchone()[0]>=20:
                raise Error(429,'rate_limited','Please wait before opening another conversation')
            cid,ts = new_id(),now()
            c.execute("INSERT INTO chat_conversations(id,project_id,customer_id,client_conversation_id,created_at,updated_at,initial_ip_address,last_customer_ip_address) VALUES(?,?,?,?,?,?,?,?)", (cid,customer['project_id'],customer['id'],client_conversation_id,ts,ts,principal.get('ip_address'),principal.get('ip_address')))
            return self.chat_view(c,cid,principal)

    def chat_revoke_customer_session(self, principal):
        with self.transaction(True) as c:
            self._chat_customer_authorize(c,principal)
            table = 'chat_sessions' if principal['kind']=='visitor' else 'chat_customer_sessions'
            c.execute("UPDATE "+table+" SET revoked_at=? WHERE id=?", (now(),principal['session_id']))
            c.execute("DELETE FROM chat_tickets WHERE kind=? AND session_id=?", (principal['kind'],principal['session_id']))

    def chat_revoke_customer_sessions(self, project_id, actor, external_id, parent_session_id=None):
        with self.transaction(True) as c:
            uid,_ = self._chat_customer_issuer(c,project_id,actor,external_id,parent_session_id,False)
            customer = c.execute("SELECT id FROM chat_customers WHERE project_id=? AND user_id=?", (project_id,uid)).fetchone()
            if customer:
                c.execute("DELETE FROM chat_tickets WHERE kind='customer' AND session_id IN (SELECT id FROM chat_customer_sessions WHERE customer_id=?)", (customer['id'],))
                c.execute("UPDATE chat_customer_sessions SET revoked_at=COALESCE(revoked_at,?) WHERE customer_id=?", (now(),customer['id']))

    def chat_create_session(self, project_id, origin, token_hash, expires, ip_address=None):
        with self.transaction(True) as c:
            self._active_project(c, project_id)
            setting = c.execute("SELECT * FROM chat_settings WHERE project_id=?", (project_id,)).fetchone()
            if not setting or not setting['enabled'] or origin not in json.loads(setting['origins']):
                raise Error(403, 'origin_denied', 'This website is not enabled for chat')
            # An additional installation-wide project cap bounds distributed signup abuse.
            if c.execute("SELECT count(*) FROM chat_conversations WHERE project_id=? AND created_at>=?", (project_id,now()[:10])).fetchone()[0] >= 1000:
                raise Error(429, 'rate_limited', 'Chat is busy; please try again later')
            cid, sid, customer_id, ts = new_id(), new_id(), new_id(), now()
            c.execute("INSERT INTO chat_customers(id,project_id,kind,created_at,updated_at) VALUES(?,?,'anonymous',?,?)", (customer_id,project_id,ts,ts))
            c.execute("INSERT INTO chat_conversations(id,project_id,customer_id,created_at,updated_at,initial_ip_address,last_customer_ip_address) VALUES(?,?,?,?,?,?,?)", (cid,project_id,customer_id,ts,ts,ip_address,ip_address))
            c.execute("INSERT INTO chat_sessions(id,conversation_id,token_hash,origin,created_at,expires_at) VALUES(?,?,?,?,?,?)", (sid,cid,token_hash,origin,ts,expires))
            return self.chat_view(c,cid,{'kind':'visitor','session_id':sid})

    def chat_visitor(self, token_hash, origin):
        with self.transaction() as c:
            session = c.execute("SELECT * FROM chat_sessions WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?", (token_hash,time.time())).fetchone()
            if not session:
                raise Error(401,'unauthorized','Chat session expired; start a new conversation')
            principal = {'kind':'visitor','session_id':session['id'],'origin':origin,'user_id':None}
            self.chat_authorize(c, session['conversation_id'], principal)
            return principal

    def chat_authorize(self, c, conversation_id, principal):
        row = c.execute("SELECT * FROM chat_conversations WHERE id=?", (conversation_id,)).fetchone()
        if principal['kind'] == 'admin':
            self._admin_session(c,principal['user_id'],principal['session_id'])
            if not row:
                missing()
        else:
            customer = self._chat_customer_authorize(c, principal)
            if not row or row['customer_id'] != customer['id']:
                missing()
        return row

    def chat_view(self, c, conversation_id, principal):
        row = c.execute("SELECT cv.*,p.identifier project_identifier,p.name project_name,p.archived_at project_archived_at FROM chat_conversations cv JOIN projects p ON p.id=cv.project_id WHERE cv.id=?", (conversation_id,)).fetchone()
        result = {k: row[k] for k in ('id','project_id','project_identifier','project_name','state','version','created_at','updated_at','last_sequence','project_archived_at')}
        if 'support_mode' in row.keys():
            result.update(support_mode=row['support_mode'], mode_version=row['mode_version'])
            job = c.execute('SELECT j.*,m.sequence trigger_sequence FROM ai_turns t JOIN ai_jobs j ON j.id=t.job_id JOIN chat_messages m ON m.id=t.trigger_message_id WHERE t.conversation_id=? ORDER BY m.sequence DESC LIMIT 1', (conversation_id,)).fetchone() if self._has_ai_turns(c) else None
            result['ai_status'] = job['state'] if job else None
            result['ai_trigger_sequence'] = job['trigger_sequence'] if job else None
            result['ai_progress'] = None
            result['ai_retrying'] = bool(job and job['state'] == 'queued' and job['error'])
            result['ai_failure_reason'] = (job['error'] if job and job['state'] == 'failed' else None)
            result['ai_answer_outcome'] = json.loads(job['payload']).get('answer_outcome') if job else None
            if (job and job['state'] == 'running' and job['lease_until'] and job['lease_until'] > time.time()
                    and row['support_mode'] == 'ai' and row['state'] == 'open'
                    and self._ai_job_authorized(c, job) and self._ai_bound_key(c, job) is not False):
                progress = json.loads(job['payload']).get('ai_progress', {})
                if progress.get('attempt') == job['attempt_count'] and progress.get('stage') in AI_PROGRESS_STAGES:
                    result['ai_progress'] = progress['stage']
        result['customer'] = self._chat_customer_view(c, row['customer_id'])
        result.update(follow_up_email=row['follow_up_email'] if 'follow_up_email' in row.keys() else None,
                      contact_version=row['contact_version'] if 'contact_version' in row.keys() else 0)
        if principal['kind']=='admin':
            result.update(initial_ip_address=row['initial_ip_address'], last_customer_ip_address=row['last_customer_ip_address'])
            if self._has_ai_turns(c):
                result['ai_available'] = self._ai_ready(c, self._ai_config(c, row['project_id']), require_enabled=True)
                result['report_count'] = c.execute('SELECT count(*) FROM ai_reports WHERE conversation_id=? AND project_id=?', (conversation_id, row['project_id'])).fetchone()[0]
                reports = c.execute('''SELECT a.request_id,coalesce(r.title,d.title,p.title) title,p.kind,r.moderation,r.status,
                    r.id IS NULL deleted,a.guest_session_id IS NOT NULL guest
                    FROM ai_reports a JOIN ai_proposals p ON p.id=a.proposal_id
                    LEFT JOIN requests r ON r.id=a.request_id AND r.creator_id=a.creator_id AND r.board_id=p.board_id
                    LEFT JOIN ai_report_deletions d ON d.proposal_id=a.proposal_id
                    WHERE a.conversation_id=? AND a.project_id=? ORDER BY a.created_at DESC,a.proposal_id DESC LIMIT 20''', (conversation_id, row['project_id'])).fetchall()
                result['reports'] = [dict(report) | {'deleted': bool(report['deleted']), 'guest': bool(report['guest'])} for report in reports]
        if principal['kind']!='admin':
            read = c.execute("SELECT read_sequence FROM chat_customer_reads WHERE customer_id=? AND conversation_id=?",(row['customer_id'],conversation_id)).fetchone()
            senders = ('admin', 'ai')
        else:
            read = c.execute("SELECT read_sequence FROM chat_reads WHERE conversation_id=? AND user_id=?",(conversation_id,principal['user_id'])).fetchone()
            senders = ('visitor',)
        result['unread_count'] = c.execute("SELECT count(*) FROM chat_messages WHERE conversation_id=? AND sequence>? AND sender IN (" + ','.join('?' for _ in senders) + ')', (conversation_id,read[0] if read else 0,*senders)).fetchone()[0]
        return result

    def chat_conversation(self, conversation_id, principal):
        with self.transaction() as c:
            self.chat_authorize(c,conversation_id,principal)
            return self.chat_view(c,conversation_id,principal)

    def chat_conversations(self, principal, state, project, limit, offset):
        with self.transaction() as c:
            self._admin_session(c,principal['user_id'],principal['session_id'])
            rows = c.execute("SELECT cv.id FROM chat_conversations cv JOIN projects p ON p.id=cv.project_id WHERE cv.first_message_at IS NOT NULL AND (? IS NULL OR cv.state=?) AND (? IS NULL OR p.id=? OR p.identifier=?) ORDER BY cv.updated_at DESC,cv.id LIMIT ? OFFSET ?", (state,state,project,project,project,limit,offset)).fetchall()
            return [self.chat_view(c,row[0],principal) for row in rows]

    @staticmethod
    def chat_message_view(row, principal=None):
        result = {**{k:row[k] for k in ('id','conversation_id','sequence','client_message_id','sender','body','created_at')},
                  'sender_role':'customer' if row['sender']=='visitor' else row['sender']}
        if 'citations' in row.keys():
            result['citations'] = json.loads(row['citations'])
        if 'context' in row.keys():
            result['context'] = json.loads(row['context'] or 'null')
            result['tool_sources'] = json.loads(row['tool_sources'])
        if principal and principal['kind']=='admin':
            result['ip_address'] = row['ip_address']
        return result

    def chat_messages(self, conversation_id, principal, after, limit):
        with self.transaction() as c:
            self.chat_authorize(c,conversation_id,principal)
            rows = c.execute("SELECT * FROM chat_messages WHERE conversation_id=? AND sequence>? ORDER BY sequence LIMIT ?", (conversation_id,after,limit)).fetchall()
            return [self.chat_message_view(x, principal) for x in rows]

    def chat_send(self, conversation_id, principal, client_message_id, body, follow_up_email=None, context=None):
        from feedback.ai_mcp_context import chat_context
        context_json = self._ai_json(chat_context(context), 2048) if context is not None else None
        from feedback.business_hours import contact_email
        follow_up_email=contact_email(follow_up_email)
        email_hash=hashlib.sha256(follow_up_email.encode()).hexdigest() if follow_up_email else None
        with self.transaction(True) as c:
            row = self.chat_authorize(c,conversation_id,principal)
            sender = 'admin' if principal['kind']=='admin' else 'visitor'
            sender_id = principal['user_id'] if sender=='admin' else row['customer_id']
            old = c.execute("SELECT * FROM chat_messages WHERE conversation_id=? AND sender_id=? AND client_message_id=?", (conversation_id,sender_id,client_message_id)).fetchone()
            if old:
                if (old['context'] if 'context' in old.keys() else None) != context_json or old['body'] != body or (old['follow_up_email_hash'] if 'follow_up_email_hash' in old.keys() else None) != email_hash:
                    conflict('This message ID was already used for different content')
                return self.chat_message_view(old, principal)
            self._active_project(c, row['project_id'])
            if row['state'] != 'open':
                conflict('This conversation is closed')
            if follow_up_email is not None:
                hours=self._hours_read(c,row['project_id'])['effective']
                if principal['kind']=='admin' or row['support_mode']=='ai' or not hours['enabled'] or not hours['collect_email']:
                    raise Error(409,'contact_unavailable','Email follow-up is unavailable. Remove the email to send your message.')
                c.execute('UPDATE chat_conversations SET follow_up_email=?,contact_version=contact_version+1 WHERE id=?',(follow_up_email,conversation_id))
            was_ai = 'support_mode' in row.keys() and row['support_mode'] == 'ai'
            if sender == 'admin' and 'support_mode' in row.keys() and row['support_mode'] != 'human' and self._has_ai_turns(c):
                self._chat_set_mode(c, row, 'human', {'kind': 'admin', 'user_id': principal['user_id']}, 'admin_reply')
                row = self.chat_authorize(c, conversation_id, principal)
            if not body.strip() or len(body)>4000:
                raise Error(400,'invalid_input','Write a message of 1–4,000 characters')
            # Durable per-conversation rate cap includes WebSockets and survives restart.
            cutoff = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(time.time()-60))
            if c.execute("SELECT count(*) FROM chat_messages WHERE conversation_id=? AND sender_id=? AND created_at>=?",(conversation_id,sender_id,cutoff)).fetchone()[0]>=30:
                raise Error(429,'rate_limited','Please wait before sending more messages')
            mid, ts, seq = new_id(), now(), row['last_sequence']+1
            c.execute("INSERT INTO chat_messages(id,conversation_id,sequence,client_message_id,sender,sender_id,body,created_at,ip_address) VALUES(?,?,?,?,?,?,?,?,?)",(mid,conversation_id,seq,client_message_id,sender,sender_id,body,ts,principal.get('ip_address')))
            if context_json is not None:
                c.execute('UPDATE chat_messages SET context=? WHERE id=?',(context_json,mid))
            if email_hash is not None:
                c.execute('UPDATE chat_messages SET follow_up_email_hash=? WHERE id=?',(email_hash,mid))
            first_message = ts if sender=='visitor' and row['first_message_at'] is None else row['first_message_at']
            first_reply = ts if sender=='admin' and first_message and row['first_reply_at'] is None else row['first_reply_at']
            c.execute("UPDATE chat_conversations SET last_sequence=?,updated_at=?,first_message_at=?,first_reply_at=? WHERE id=?",(seq,ts,first_message,first_reply,conversation_id))
            if sender=='visitor':
                c.execute("UPDATE chat_conversations SET last_customer_ip_address=? WHERE id=?",(principal.get('ip_address'),conversation_id))
            project = c.execute("SELECT identifier FROM projects WHERE id=?", (row['project_id'],)).fetchone()
            customer = self._chat_customer_view(c, row['customer_id'])
            # Message UUID is the stable event UUID too. Bodies and credentials
            # never enter notification payloads; consumers fetch authorized history.
            payload = {'id':mid,'type':'chat.message_created','schema_version':1,'occurred_at':ts,
                       'project':{'id':row['project_id'],'identifier':project['identifier']},
                       'conversation':{'id':conversation_id},
                       'message':{'id':mid,'sequence':seq,'sender_role':'admin' if sender=='admin' else 'customer'},
                       'customer':customer,
                       'actor':{'kind':'admin' if sender=='admin' else 'customer',
                                'user_id':principal['user_id'] if sender=='admin' else customer['user_id']}}
            self._persist_event(c,row['project_id'],None,None,payload,ip_address=principal.get('ip_address'),notify=not was_ai)
            if sender=='visitor' and not was_ai and (row['last_alert_at'] is None or time.time()-row['last_alert_at']>=300):
                # Snapshot eligible devices inside this same transaction; disabled push is a no-op.
                self._queue_push(c,mid,'chat.message',conversation_id)
                c.execute("UPDATE chat_conversations SET last_alert_at=? WHERE id=?",(time.time(),conversation_id))
            if sender == 'visitor' and was_ai and self._has_ai_turns(c):
                self._chat_enqueue_turn(c, conversation_id, principal, mid, seq)
            return self.chat_message_view(c.execute("SELECT * FROM chat_messages WHERE id=?",(mid,)).fetchone(), principal)

    def chat_read(self, conversation_id, principal, sequence):
        with self.transaction(True) as c:
            row = self.chat_authorize(c,conversation_id,principal)
            sequence = max(0,min(sequence,row['last_sequence']))
            if principal['kind']!='admin':
                c.execute("INSERT INTO chat_customer_reads VALUES(?,?,?) ON CONFLICT(customer_id,conversation_id) DO UPDATE SET read_sequence=CASE WHEN chat_customer_reads.read_sequence>excluded.read_sequence THEN chat_customer_reads.read_sequence ELSE excluded.read_sequence END",(row['customer_id'],conversation_id,sequence))
                actual = c.execute("SELECT read_sequence FROM chat_customer_reads WHERE customer_id=? AND conversation_id=?",(row['customer_id'],conversation_id)).fetchone()[0]
                if principal['kind']=='visitor':
                    c.execute("UPDATE chat_sessions SET read_sequence=CASE WHEN read_sequence>? THEN read_sequence ELSE ? END WHERE id=?",(sequence,sequence,principal['session_id']))
            else:
                c.execute("INSERT INTO chat_reads VALUES(?,?,?) ON CONFLICT(conversation_id,user_id) DO UPDATE SET read_sequence=CASE WHEN chat_reads.read_sequence>excluded.read_sequence THEN chat_reads.read_sequence ELSE excluded.read_sequence END",(conversation_id,principal['user_id'],sequence))
                actual = c.execute("SELECT read_sequence FROM chat_reads WHERE conversation_id=? AND user_id=?",(conversation_id,principal['user_id'])).fetchone()[0]
            return {'read_sequence':actual}

    def chat_change_state(self, conversation_id, principal, state, version):
        with self.transaction(True) as c:
            self._admin_session(c,principal['user_id'],principal['session_id'])
            row = self.chat_authorize(c,conversation_id,principal)
            self._active_project(c, row['project_id'])
            if row['version']!=version:
                conflict()
            if row['state']!=state:
                c.execute("UPDATE chat_conversations SET state=?,version=version+1,updated_at=? WHERE id=?",(state,now(),conversation_id))
                if self._has_ai_turns(c):
                    self._chat_cancel_ai(c, conversation_id)
            return self.chat_view(c,conversation_id,principal)

    def chat_revoke(self, conversation_id, principal):
        with self.transaction(True) as c:
            self.chat_authorize(c,conversation_id,principal)
            c.execute("UPDATE chat_sessions SET revoked_at=? WHERE conversation_id=?",(now(),conversation_id))
            c.execute("DELETE FROM chat_tickets WHERE conversation_id=? AND kind='visitor'",(conversation_id,))

    def chat_ticket(self, conversation_id, principal, token_hash, expires):
        with self.transaction(True) as c:
            self.chat_authorize(c,conversation_id,principal)
            c.execute("DELETE FROM chat_tickets WHERE expires_at<=?",(time.time(),))
            if c.execute("SELECT count(*) FROM chat_tickets WHERE session_id=?",(principal['session_id'],)).fetchone()[0]>=8:
                raise Error(429,'rate_limited','Too many pending connections')
            c.execute("INSERT INTO chat_tickets VALUES(?,?,?,?,?,?,?)",(token_hash,conversation_id,principal['kind'],principal['session_id'],principal.get('user_id'),principal.get('origin'),expires))

    def chat_consume_ticket(self, token_hash, origin):
        with self.transaction(True) as c:
            row = c.execute("SELECT * FROM chat_tickets WHERE token_hash=? AND expires_at>?",(token_hash,time.time())).fetchone()
            if not row or row['origin']!=origin:
                raise Error(401,'unauthorized','Connection ticket is invalid or expired')
            principal = {k:row[k] for k in ('kind','session_id','user_id','origin')}
            self.chat_authorize(c,row['conversation_id'],principal)
            if principal['kind']=='customer':
                customer = self._chat_customer_authorize(c,principal)
                principal.update(user_id=customer['user_id'],customer_id=customer['id'],project_id=customer['project_id'])
            c.execute("DELETE FROM chat_tickets WHERE token_hash=?",(token_hash,))
            return row['conversation_id'],principal

    def chat_response_samples(self, project_id, since):
        with self.transaction() as c:
            return [dict(x) for x in c.execute("SELECT first_message_at,first_reply_at FROM chat_conversations WHERE project_id=? AND first_message_at>=? ORDER BY first_message_at DESC,id DESC LIMIT 30000",(project_id,since))]

    def chat_origin_allowed(self, origin):
        with self.transaction() as c:
            return any(origin in json.loads(row[0]) for row in c.execute("SELECT origins FROM chat_settings WHERE enabled=1"))

    def push_settings(self):
        with self.transaction() as c:
            row = c.execute('SELECT * FROM push_settings WHERE singleton=1').fetchone()
            return dict(row) if row else None

    def push_prepare(self,user_id,session_id,url,proof):
        with self.transaction(True) as c:
            self._admin_session(c,user_id,session_id)
            row=c.execute('SELECT * FROM push_settings WHERE singleton=1').fetchone()
            if row and row['relay_url']!=url:
                conflict('The relay origin is fixed after enrollment; restore or migrate configuration deliberately')
            c.execute('INSERT INTO push_settings(singleton,relay_url,enrollment_proof) VALUES(1,?,?) ON CONFLICT DO NOTHING',(url,proof))
            return dict(c.execute('SELECT * FROM push_settings WHERE singleton=1').fetchone())

    def push_enable(self,user_id,session_id,version,credential):
        with self.transaction(True) as c:
            self._admin_session(c,user_id,session_id)
            result=c.execute('UPDATE push_settings SET enabled=1,credential=?,last_error=NULL,version=version+1 WHERE singleton=1 AND version=?',(credential,version))
            if not result.rowcount:
                conflict()

    def push_disable(self,user_id,session_id):
        with self.transaction(True) as c:
            self._admin_session(c,user_id,session_id)
            c.execute('UPDATE push_settings SET enabled=0,version=version+1 WHERE singleton=1')
            c.execute("UPDATE push_deliveries SET cancel_requested=1,next_attempt_at=0,failure_count=0,state=CASE WHEN state='failed' THEN 'queued' ELSE state END WHERE state IN ('queued','awaiting','sending') OR (state='failed' AND submitted=1)")

    def push_error(self,version,error):
        with self.transaction(True) as c:
            c.execute('UPDATE push_settings SET last_error=? WHERE singleton=1 AND version=?',(error,version))

    def _queue_push(self,c,event_id,kind,resource_id):
        settings=c.execute('SELECT enabled,credential FROM push_settings WHERE singleton=1').fetchone()
        if not settings or not settings['enabled'] or not settings['credential']:
            return
        ts=time.time()
        preference={'chat.message':'chat_notifications','request.created':'request_notifications','form.submission_created':'form_notifications'}[kind]
        rows=c.execute("SELECT d.* FROM devices d JOIN users u ON u.id=d.user_id JOIN sessions s ON s.id=d.session_id WHERE u.role='admin' AND d.enabled=1 AND d."+preference+"=1 AND s.revoked_at IS NULL AND s.expires_at>?",(ts,)).fetchall()
        for d in rows:
            c.execute('INSERT INTO push_deliveries(id,event_id,kind,resource_id,device_id,generation,token,environment,next_attempt_at,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                      (new_id(),event_id,kind,resource_id,d['id'],d['generation'],d['token'],d['environment'],ts,ts,ts+86400))

    def _push_eligible(self,c,job,ts):
        if job['cancel_requested']:
            return False
        preference={'chat.message':'chat_notifications','request.created':'request_notifications','form.submission_created':'form_notifications'}[job['kind']]
        return bool(c.execute("SELECT 1 FROM devices d JOIN users u ON u.id=d.user_id JOIN sessions s ON s.id=d.session_id JOIN push_settings p ON p.singleton=1 WHERE d.id=? AND d.generation=? AND d.enabled=1 AND d."+preference+"=1 AND u.role='admin' AND s.revoked_at IS NULL AND s.expires_at>? AND p.enabled=1 AND p.credential IS NOT NULL",(job['device_id'],job['generation'],ts)).fetchone())

    def push_deliveries(self,limit,offset):
        with self.transaction() as c:
            return [dict(x) for x in c.execute('SELECT * FROM push_deliveries ORDER BY created_at DESC,id LIMIT ? OFFSET ?',(limit,offset))]

    def claim_push(self,ts,lease_seconds=60):
        with self.transaction(True) as c:
            # Tokens only remain for the 24-hour retry window. Diagnostic history is bounded.
            c.execute("UPDATE push_deliveries SET token='' WHERE expires_at<=?",(ts,))
            c.execute("DELETE FROM push_deliveries WHERE created_at<? AND state IN ('succeeded','failed','cancelled','expired')",(ts-30*86400,))
            # A lost/malformed receipt can leave a failed local job accepted remotely.
            # Revocation must discover these jobs even after send retry exhaustion.
            c.execute("""UPDATE push_deliveries SET state='queued',next_attempt_at=0,failure_count=0
                WHERE state='failed' AND submitted=1 AND cancel_requested=0 AND (expires_at<=? OR NOT EXISTS(
                SELECT 1 FROM devices d JOIN users u ON u.id=d.user_id JOIN sessions s ON s.id=d.session_id
                JOIN push_settings p ON p.singleton=1 WHERE d.id=push_deliveries.device_id
                AND d.generation=push_deliveries.generation AND d.enabled=1 AND u.role='admin'
                AND s.revoked_at IS NULL AND s.expires_at>? AND p.enabled=1
                AND ((push_deliveries.kind='request.created' AND d.request_notifications=1)
                  OR (push_deliveries.kind='chat.message' AND d.chat_notifications=1)
                  OR (push_deliveries.kind='form.submission_created' AND d.form_notifications=1))))""",(ts,ts))
            job=c.execute("SELECT * FROM push_deliveries WHERE (state IN ('queued','awaiting') AND next_attempt_at<=?) OR (state='sending' AND lease_until<=?) ORDER BY next_attempt_at,id LIMIT 1",(ts,ts)).fetchone()
            if not job:
                return None
            job=dict(job)
            eligible=job['expires_at']>ts and self._push_eligible(c,job,ts)
            if not eligible and not job['cancel_requested']:
                c.execute('UPDATE push_deliveries SET cancel_requested=1,failure_count=0 WHERE id=?',(job['id'],))
                job['failure_count']=0
            lease=new_id()
            c.execute("UPDATE push_deliveries SET state='sending',lease_token=?,lease_until=?,attempt_count=attempt_count+1 WHERE id=?",(lease,ts+lease_seconds,job['id']))
            settings=c.execute('SELECT * FROM push_settings WHERE singleton=1').fetchone()
            return {**job,'lease_token':lease,'attempt_count':job['attempt_count']+1,'cancel':not eligible,
                    'relay_url':settings['relay_url'],'credential':settings['credential']}

    def push_before_send(self,jid,lease):
        with self.transaction(True) as c:
            job=c.execute("SELECT * FROM push_deliveries WHERE id=? AND lease_token=? AND lease_until>? AND state='sending'",(jid,lease,time.time())).fetchone()
            if not job:
                return False
            if job['expires_at']<=time.time() or not self._push_eligible(c,job,time.time()):
                return False
            c.execute('UPDATE push_deliveries SET submitted=1 WHERE id=?',(jid,))
            return True

    def finish_push(self,jid,lease,state,relay_id,error,next_attempt,invalid_token=False,failed_attempt=False):
        with self.transaction(True) as c:
            job=c.execute("SELECT * FROM push_deliveries WHERE id=? AND lease_token=? AND lease_until>? AND state='sending'",(jid,lease,time.time())).fetchone()
            if not job:
                return False
            failures=job['failure_count']+1 if failed_attempt else 0
            if state=='queued' and failures>=8:
                state,error='failed','relay_retry_limit'
            # A mutation while HTTP was in flight schedules cancellation instead of accepting a stale result.
            if state in ('awaiting','succeeded') and (job['expires_at']<=time.time() or not self._push_eligible(c,job,time.time())):
                state='queued'
                next_attempt=0
            if invalid_token:
                c.execute('UPDATE devices SET enabled=0,generation=generation+1 WHERE id=? AND generation=?',(job['device_id'],job['generation']))
            c.execute("UPDATE push_deliveries SET state=?,relay_id=COALESCE(?,relay_id),error=?,next_attempt_at=?,lease_token=NULL,lease_until=NULL,failure_count=?,manual_retry=CASE WHEN ?=1 THEN manual_retry ELSE 0 END,token=CASE WHEN ? IN ('succeeded','cancelled','expired') THEN '' ELSE token END WHERE id=?",
                      (state,relay_id,error,next_attempt,failures,failed_attempt,state,jid))
            return True

    def retry_push(self,user_id,session_id,jid):
        with self.transaction(True) as c:
            self._admin_session(c,user_id,session_id)
            job=c.execute('SELECT * FROM push_deliveries WHERE id=?',(jid,)).fetchone()
            if not job:
                missing()
            cancellation=job['submitted'] and job['cancel_requested'] and job['created_at']+172800>time.time()
            if job['state']!='failed' or job['retry_count']>=3 or (not cancellation and (job['expires_at']<=time.time() or not self._push_eligible(c,job,time.time()))):
                conflict('This delivery is no longer eligible for retry')
            c.execute("UPDATE push_deliveries SET state='queued',next_attempt_at=0,error=NULL,manual_retry=1,retry_count=retry_count+1,failure_count=0 WHERE id=?",(jid,))

    # Optional AI durable state. These methods never perform network or inference.
    @staticmethod
    def _ai_json(value, limit=65536):
        try:
            result = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'), sort_keys=True)
            if len(result.encode('utf-8')) > limit:
                raise ValueError()
            return result
        except (TypeError, ValueError, UnicodeError, OverflowError):
            raise Error(400, 'invalid_input', 'AI data is invalid or too large') from None

    def _ai_admin(self, c, principal):
        if not isinstance(principal, dict) or principal.get('kind') != 'admin':
            raise Error(403, 'forbidden', 'Native administrator access required')
        self._admin_session(c, principal.get('user_id'), principal.get('session_id'))

    @staticmethod
    def _ai_project(c, project):
        row = c.execute('SELECT id FROM projects WHERE id=? OR identifier=?', (project, project)).fetchone()
        if not row:
            missing()
        RelationalStorage._active_project(c, row[0])
        return row[0]

    @staticmethod
    def _ai_config(c, project_id):
        row = c.execute('SELECT * FROM ai_settings WHERE project_id=?', (project_id,)).fetchone()
        result = dict(row) if row else {'project_id': project_id, 'version': 0, 'credential_version': 0,
            'protected_key': None, 'catalogue': '[]', 'capabilities': '{}', 'capabilities_model': None,
            'capabilities_credential_version': None, 'updated_at': None, 'settings': '{}'}
        saved = json.loads(result['settings'])
        # Absent opt-ins deny even for older or incomplete stored settings.
        settings = default_ai_settings()
        settings.update(saved)
        settings['enabled'] = saved.get('enabled') is True
        settings['allow_guest_reports'] = saved.get('allow_guest_reports') is True
        settings['tools'] = {k: saved.get('tools', {}).get(k) is True for k in default_ai_settings()['tools']}
        result.update(settings=settings, catalogue=json.loads(result['catalogue']), capabilities=json.loads(result['capabilities']))
        return RelationalStorage._ai_pool_config(c, result)

    @staticmethod
    def _ai_model_key(c, row, model):
        key = dict(row)
        evidence = c.execute("SELECT * FROM ai_key_model_evidence WHERE key_id=? AND role='generation' AND model=? AND key_version=?", (key['id'], model, key['version'])).fetchone()
        key.update(capabilities=evidence['capabilities'] if evidence else '{}',
                   capabilities_model=evidence['model'] if evidence else None,
                   capabilities_key_version=evidence['key_version'] if evidence else None)
        return key

    @staticmethod
    def _ai_pool_rows(c, project_id, provider, model=None):
        if model is None:
            saved = c.execute('SELECT settings FROM ai_settings WHERE project_id=?', (project_id,)).fetchone()
            model = json.loads(saved[0]).get('model') if saved else None
        rows = []
        for row in c.execute("SELECT k.*,b.is_default AS binding_default FROM ai_generation_keys b JOIN ai_provider_keys k ON k.id=b.key_id WHERE b.project_id=? AND k.provider=? AND k.purpose!='decision' AND k.deleted_at IS NULL ORDER BY b.is_default DESC,k.created_at,k.id", (project_id, provider)):
            item = RelationalStorage._ai_model_key(c, row, model)
            item['is_default'] = item.pop('binding_default')
            item['catalogue'], item['capabilities'] = json.loads(item['catalogue']), json.loads(item['capabilities'])
            rows.append(item)
        return rows

    @staticmethod
    def _ai_key_verified(key, settings, remote_tools=False):
        caps = key['capabilities']
        return bool(settings['model'] and settings['model'] in key['catalogue'] and
            key['capabilities_model'] == settings['model'] and key['capabilities_key_version'] == key['version'] and
            caps.get('text_generation') is True and (not (remote_tools or any(settings['tools'].values())) or caps.get('tool_calling') is True))

    @staticmethod
    def _ai_pool_config(c, config):
        rows = RelationalStorage._ai_pool_rows(c, config['project_id'], config['settings']['provider'], config['settings']['model'])
        default = next((row for row in rows if row['is_default']), None)
        catalogue = sorted({model for row in rows for model in row['catalogue']})
        if len(catalogue) > 1000:
            raise Error(409, 'ai_catalogue_limit', 'The provider pool has too many model IDs')
        verified = next((row for row in rows if row['enabled'] and not row['blocked_reason'] and RelationalStorage._ai_key_verified(row, config['settings'], RelationalStorage._mcp_needs_tools(c,config['project_id']))), None)
        # Preserve text evidence for actionable diagnostics when only tool access is missing.
        if verified is None:
            text_settings = config['settings'] | {'tools': {}}
            verified = next((row for row in rows if row['enabled'] and not row['blocked_reason'] and RelationalStorage._ai_key_verified(row, text_settings)), None)
        config.update(protected_key=default['protected_key'] if default else None, catalogue=catalogue,
            capabilities=verified['capabilities'] if verified else {},
            capabilities_model=config['settings']['model'] if verified else None,
            capabilities_credential_version=config['credential_version'] if verified else None)
        return config

    def _ai_write_config(self, c, config):
        c.execute('''INSERT INTO ai_settings(project_id,settings,version,credential_version,protected_key,catalogue,capabilities,capabilities_model,capabilities_credential_version,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET settings=excluded.settings,version=excluded.version,
            credential_version=excluded.credential_version,protected_key=excluded.protected_key,catalogue=excluded.catalogue,
            capabilities=excluded.capabilities,capabilities_model=excluded.capabilities_model,
            capabilities_credential_version=excluded.capabilities_credential_version,updated_at=excluded.updated_at''',
            (config['project_id'], self._ai_json(config['settings'], 131072), config['version'], config['credential_version'],
             None, self._ai_json(config['catalogue']), self._ai_json(config['capabilities']),
             config['capabilities_model'], config['capabilities_credential_version'], config['updated_at'] or now()))

    @staticmethod
    def _ai_pool_label(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 80 or any(ord(x) < 32 or ord(x) == 127 for x in value):
            raise Error(400, 'invalid_input', 'Use a name of 1 to 80 characters without control characters')
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise Error(400, 'invalid_input', 'Invalid provider name') from None
        return value

    def _ai_group(self, c, provider, label):
        self._ai_pool_label(label)
        row = c.execute('SELECT * FROM ai_provider_quota_groups WHERE provider=? AND label=?', (provider, label)).fetchone()
        if not row:
            c.execute('INSERT INTO ai_provider_quota_groups(id,provider,label) VALUES(?,?,?)', (new_id(), provider, label))
            row = c.execute('SELECT * FROM ai_provider_quota_groups WHERE provider=? AND label=?', (provider, label)).fetchone()
        return row

    def _ai_new_key(self, c, config, label, group, protected_key, default=False):
        self._ai_pool_label(label)
        if not isinstance(protected_key, str) or not 1 <= len(protected_key) <= 16384:
            raise Error(400, 'invalid_input', 'Invalid protected provider key')
        if c.execute('SELECT count(*) FROM ai_provider_keys WHERE provider=? AND deleted_at IS NULL', (config['settings']['provider'],)).fetchone()[0] >= 99:
            raise Error(409, 'ai_key_limit', 'The account library can contain at most 99 provider keys per provider')
        gid = self._ai_group(c, config['settings']['provider'], group)['id']
        kid, ts = new_id(), now()
        c.execute('''INSERT INTO ai_provider_keys(id,project_id,provider,label,quota_group_id,is_default,protected_key,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)''', (kid, config['project_id'], config['settings']['provider'], label, gid, int(default), protected_key, ts, ts))
        if config.get('project_id'):
            c.execute('INSERT INTO ai_generation_keys(project_id,key_id,is_default) VALUES(?,?,?)', (config['project_id'], kid, int(default)))
        c.execute('UPDATE ai_key_library SET version=version+1 WHERE singleton=1')
        return kid

    @staticmethod
    def _ai_remove_key(c, kid):
        c.execute('DELETE FROM ai_key_model_evidence WHERE key_id=?', (kid,))
        c.execute('DELETE FROM ai_key_decision_catalogues WHERE key_id=?', (kid,))
        c.execute("UPDATE ai_provider_keys SET deleted_at=?,updated_at=?,version=version+1,enabled=0,is_default=0,protected_key=NULL,catalogue='[]',capabilities='{}',capabilities_model=NULL,capabilities_key_version=NULL,health_epoch=health_epoch+1 WHERE id=?", (now(), now(), kid))

    @staticmethod
    def _ai_promote_default(c, pid):
        c.execute('DELETE FROM ai_generation_keys WHERE project_id=? AND key_id IN (SELECT id FROM ai_provider_keys WHERE deleted_at IS NOT NULL)', (pid,))
        if not c.execute('SELECT 1 FROM ai_generation_keys WHERE project_id=? AND is_default=1', (pid,)).fetchone():
            row = c.execute("SELECT k.id FROM ai_generation_keys b JOIN ai_provider_keys k ON k.id=b.key_id WHERE b.project_id=? AND k.deleted_at IS NULL ORDER BY k.created_at,k.id LIMIT 1", (pid,)).fetchone()
            if row:
                c.execute('UPDATE ai_generation_keys SET is_default=1 WHERE project_id=? AND key_id=?', (pid, row[0]))

    def _ai_config_view(self, c, row):
        return {k: row[k] for k in ('project_id', 'version', 'credential_version', 'settings', 'catalogue', 'capabilities', 'updated_at')} | {'has_key': bool(row['protected_key']), 'readiness': self._ai_readiness(c, row)}

    def ai_settings(self, project, principal):
        with self.transaction() as c:
            self._ai_admin(c, principal)
            return self._ai_config_view(c, self._ai_config(c, self._ai_project(c, project)))

    @staticmethod
    def _ai_cancel_generation(c, project_id):
        c.execute("UPDATE ai_jobs SET state='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? WHERE project_id=? AND kind IN ('turn','preview','sections') AND state IN ('queued','running')", (now(), project_id))
        c.execute("UPDATE ai_proposals SET state='cancelled',version=version+1 WHERE project_id=? AND state='pending'", (project_id,))

    def _ai_readiness(self, c, config, require_enabled=False, authoring=False):
        settings, caps, issues = config['settings'], config['capabilities'], []
        if c.execute('SELECT archived_at FROM projects WHERE id=?', (config['project_id'],)).fetchone()[0]: issues.append('project_archived')
        if require_enabled and settings.get('enabled') is not True:
            issues.append('ai_disabled')
        if not config['protected_key']:
            issues.append('answer_key_missing')
        if not settings['model']:
            issues.append('answer_model_missing')
        elif settings['model'] not in config['catalogue']:
            issues.append('answer_model_unavailable')
        elif (config['capabilities_model'] != settings['model']
              or config['capabilities_credential_version'] != config['credential_version']
              or caps.get('text_generation') is not True):
            issues.append('answer_model_unverified')
        elif any(settings['tools'].values()) and caps.get('tool_calling') is not True:
            issues.append('answer_tools_unverified')
        mcp = self._mcp_project(c,config['project_id'])
        mcp_tools = self._mcp_tools(c,config['project_id'])
        if mcp['source_mode']!='knowledge' and mcp_tools and caps.get('tool_calling') is not True and 'answer_tools_unverified' not in issues:
            issues.append('answer_tools_unverified')
        if len(mcp_tools)+sum(settings['tools'].values())>8:
            issues.append('too_many_tools')
        knowledge_issues=[]
        runtime = c.execute('SELECT state,active_manifest FROM ai_runtime WHERE singleton=1').fetchone()
        if c.execute('SELECT mode FROM ai_embedding_settings WHERE singleton=1').fetchone()[0] == 'local' and (runtime['state'] != 'ready' or not runtime['active_manifest']):
            knowledge_issues.append('embedding_model_not_ready')
        if not c.execute("""SELECT 1 FROM ai_sources s JOIN ai_revisions r ON r.id=s.active_revision_id AND r.source_id=s.id
            WHERE s.project_id=? AND r.project_id=s.project_id AND s.active=1 AND s.approved=1 AND s.deleted_at IS NULL
            AND r.state='active' AND r.profile=? LIMIT 1""", (config['project_id'], self._ai_json(self._ai_embedding_profile(c)))).fetchone():
            knowledge_issues.append('knowledge_not_ready')
        if authoring or mcp['source_mode']=='knowledge':
            issues.extend(knowledge_issues)
        elif mcp['source_mode']=='tools':
            if not mcp_tools:issues.append('connected_tools_not_ready')
        elif knowledge_issues and not mcp_tools:
            issues.extend(knowledge_issues)
        return {'ready': not issues, 'issues': issues, 'knowledge_ready':not knowledge_issues, 'tools_ready':bool(mcp_tools), 'source_mode':mcp['source_mode']}

    def _ai_ready(self, c, config, require_enabled=False):
        return self._ai_readiness(c, config, require_enabled)['ready']

    def _ai_require_ready(self, c, config, authoring=False):
        messages = {
            'connected_tools_not_ready': 'Discover and select approved tools in Tool connections.',
            'too_many_tools': 'Select at most eight built-in and connected tools altogether.',
            'answer_key_missing': 'Select an available answer API key in Models & keys.',
            'answer_model_missing': 'Choose and save an answer model in Models & keys.',
            'answer_model_unavailable': 'Refresh the selected answer key’s models and choose an available model in Models & keys.',
            'answer_model_unverified': 'Verify the selected answer model in Models & keys before generating content.',
            'answer_tools_unverified': 'Verify tool access for the selected answer model in Models & keys, or turn the assistant tools off.',
            'embedding_model_not_ready': 'Choose an embedding provider in AI runtime and wait until it is ready.',
            'knowledge_not_ready': 'Review and approve a source, then wait for its indexing to finish before generating content.',
        }
        issues = self._ai_readiness(c, config, authoring=authoring)['issues']
        if issues:
            raise Error(409, 'ai_not_ready', messages[issues[0]])

    def ai_configure(self, project, principal, version, settings, protected_key=_AI_UNSET, *, library_version=None):
        settings = json.loads(self._ai_json(validate_ai_settings(settings), 131072))
        if protected_key is not _AI_UNSET and protected_key is not None and (not isinstance(protected_key, str) or not 1 <= len(protected_key) <= 16384):
            raise Error(400, 'invalid_input', 'Invalid protected provider key')
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            self._ai_library_admin(c,principal,library_version)
            pid = self._ai_project(c, project)
            old = self._ai_config(c, pid)
            if type(version) is not int or old['version'] != version:
                conflict()
            for key in ('feature_board_id', 'bug_board_id'):
                if settings[key] and not c.execute('SELECT 1 FROM boards WHERE id=? AND project_id=? AND archived_at IS NULL', (settings[key], pid)).fetchone():
                    missing()
            provider_changed = settings['provider'] != old['settings']['provider']
            if provider_changed:
                # A saved credential/model belongs to exactly one provider.
                # A direct API write must not carry either into another adapter.
                settings['model'] = None
            key = (None if provider_changed else old['protected_key']) if protected_key is _AI_UNSET else protected_key
            key_changed = key != old['protected_key'] or provider_changed
            model_changed = settings['model'] != old['settings']['model']
            new = old | {'settings': settings, 'protected_key': key, 'credential_version': old['credential_version'] + int(key_changed)}
            if provider_changed:
                c.execute('DELETE FROM ai_generation_keys WHERE project_id=?', (pid,))
                c.execute('UPDATE ai_key_library SET version=version+1 WHERE singleton=1')
            if key_changed and protected_key is not _AI_UNSET:
                default = self._ai_pool_key(c, pid, required=False)
                if default and key is None:
                    c.execute('DELETE FROM ai_generation_keys WHERE project_id=? AND key_id=?', (pid, default['id']))
                    self._ai_promote_default(c, pid)
                    c.execute('UPDATE ai_key_library SET version=version+1 WHERE singleton=1')
                elif default:
                    c.execute("UPDATE ai_provider_keys SET protected_key=?,version=version+1,catalogue='[]',capabilities='{}',capabilities_model=NULL,capabilities_key_version=NULL,blocked_reason=NULL,reason=NULL,health_epoch=health_epoch+1,updated_at=? WHERE id=?", (key, now(), default['id']))
                    c.execute('DELETE FROM ai_key_model_evidence WHERE key_id=?', (default['id'],))
                    c.execute('DELETE FROM ai_key_decision_catalogues WHERE key_id=?', (default['id'],))
                    self._ai_shared_changed(c, default['id'], exclude_pid=pid)
                elif key is not None:
                    self._ai_new_key(c, new, 'Default key', 'Default account', key, True)
            # Evidence belongs to a key/model/role, not to a project's selection.
            new = self._ai_pool_config(c, new)
            if settings['enabled']:
                self._ai_require_ready(c, new)
            if settings == old['settings'] and not key_changed:
                return self._ai_config_view(c, old)
            new['version'], new['updated_at'] = version + 1, now()
            self._ai_write_config(c, new)
            self._ai_cancel_generation(c, pid)
            return self._ai_config_view(c, new)

    def ai_provider_snapshot(self, project, principal, version, *, key_id=None):
        """Private service input; never serialize this credential snapshot in HTTP."""
        with self.transaction() as c:
            self._ai_admin(c, principal)
            row = self._ai_config(c, self._ai_project(c, project))
            if row['version'] != version:
                conflict()
            key = self._ai_pool_key(c, row['project_id'], key_id, required=key_id is not None)
            result = {k: row[k] for k in ('project_id', 'version', 'credential_version', 'protected_key', 'settings')}
            if not key:
                return result | {'key_id': None, 'key_version': None, 'health_epoch': None, 'group_epoch': None,
                    'quota_group': None, 'catalogue': [], 'capabilities': {}}
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            return result | {'protected_key': key['protected_key'], 'key_id': key['id'], 'key_version': key['version'],
                'health_epoch': key['health_epoch'], 'group_epoch': group['health_epoch'], 'quota_group': group['label'],
                'catalogue': json.loads(key['catalogue']), 'capabilities': json.loads(key['capabilities'])}

    def ai_store_provider_evidence(self, project, principal, version, credential_version, model, *, catalogue=None, capabilities=None, key_id=None, key_version=None, health_epoch=None, group_epoch=None):
        if catalogue is None and capabilities is None:
            raise Error(400, 'invalid_input', 'Provider evidence is required')
        if catalogue is not None and (not isinstance(catalogue, list) or len(catalogue) > 1000 or any(not isinstance(x, str) or not 1 <= len(x) <= 256 for x in catalogue) or len(set(catalogue)) != len(catalogue)):
            raise Error(400, 'invalid_input', 'Invalid model catalogue')
        if capabilities is not None and (not isinstance(capabilities, dict) or set(capabilities) != {'text_generation', 'tool_calling', 'streaming', 'structured_output'} or any(type(x) is not bool for x in capabilities.values())):
            raise Error(400, 'invalid_input', 'Invalid model capabilities')
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            row = self._ai_config(c, pid)
            if row['version'] != version or row['credential_version'] != credential_version or row['settings']['model'] != model or not row['protected_key']:
                conflict()
            key = self._ai_pool_key(c, pid, key_id)
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            self._ai_key_fences(key, group, key_version, health_epoch, group_epoch)
            cat = json.loads(key['catalogue']) if catalogue is None else catalogue
            caps = json.loads(key['capabilities']) if capabilities is None else capabilities
            if capabilities is not None and any(capabilities.values()) and model not in cat:
                raise Error(409, 'ai_not_ready', 'The selected model is not in the current catalogue')
            vanished = catalogue is not None and model not in cat
            if vanished:
                caps = {}
            repair = capabilities is not None and caps.get('text_generation') is True
            c.execute('''UPDATE ai_provider_keys SET catalogue=?,capabilities=?,capabilities_model=?,capabilities_key_version=?,
                blocked_reason=CASE WHEN ?=1 THEN NULL ELSE blocked_reason END,reason=CASE WHEN ?=1 THEN NULL ELSE reason END,
                health_epoch=health_epoch+?,updated_at=? WHERE id=?''',
                (self._ai_json(cat), self._ai_json(caps), None if vanished else model if capabilities is not None else key['capabilities_model'],
                 None if vanished else key['version'] if capabilities is not None else key['capabilities_key_version'], repair, repair,
                 int(capabilities is not None or catalogue is not None), now(), key['id']))
            if catalogue is not None:
                self._ai_prune_model_evidence(c, key['id'], 'generation', cat)
            if capabilities is not None and model is not None:
                self._ai_model_evidence(c,key['id'],'generation',model,key['version'],caps,caps.get('text_generation') is True)
            self._ai_shared_evidence_changed(c,key['id'],exclude_pid=pid)
            updated = self._ai_config(c, pid)
            # Losing access or capability invalidates the current enabled generation.
            if row['settings']['enabled'] and not self._ai_ready(c, updated):
                updated['settings']['enabled'] = False
                updated['version'] += 1
                self._ai_cancel_generation(c, pid)
            updated['updated_at'] = now()
            self._ai_write_config(c, updated)
            return self._ai_config_view(c, updated)

    @staticmethod
    def _ai_pool_key(c, pid, key_id=None, required=True):
        query = "SELECT k.*,b.is_default AS binding_default FROM ai_generation_keys b JOIN ai_provider_keys k ON k.id=b.key_id WHERE b.project_id=? AND k.purpose!='decision' AND k.deleted_at IS NULL AND "
        row = c.execute(query + ('b.is_default=1' if key_id is None else 'k.id=?'), (pid,) if key_id is None else (pid, key_id)).fetchone()
        if not row and required:
            missing()
        if not row:
            return None
        saved = c.execute('SELECT settings FROM ai_settings WHERE project_id=?', (pid,)).fetchone()
        key = RelationalStorage._ai_model_key(c, row, json.loads(saved[0]).get('model') if saved else None)
        key['is_default'] = key.pop('binding_default')
        return key

    @staticmethod
    def _ai_key_fences(key, group, version=None, health_epoch=None, group_epoch=None):
        if any(expected is not None and (type(expected) is not int or expected != actual) for expected, actual in
               ((version, key['version']), (health_epoch, key['health_epoch']), (group_epoch, group['health_epoch']))):
            conflict('The provider key or account state changed')

    @staticmethod
    def _ai_trial_live(c, group, ts):
        if not group['trial_job_id'] or not group['trial_until'] or group['trial_until'] <= ts:
            return False
        return bool(c.execute("SELECT 1 FROM ai_jobs WHERE id=? AND attempt_count=? AND lease_token=? AND state='running' AND lease_until>?",
            (group['trial_job_id'], group['trial_attempt'], group['trial_token'], ts)).fetchone())

    @staticmethod
    def _ai_key_busy(c, kid, ts, excluding=None):
        row = c.execute("""SELECT j.id,j.lease_until FROM ai_provider_attempts a JOIN ai_jobs j ON j.id=a.job_id AND j.attempt_count=a.attempt
            WHERE a.key_id=? AND j.state='running' AND j.lease_until>? AND j.lease_token=a.lease_token
            AND (? IS NULL OR j.id!=?) ORDER BY j.lease_until LIMIT 1""", (kid, ts, excluding, excluding)).fetchone()
        decision = c.execute("""SELECT j.lease_until FROM ai_decision_attempts a JOIN ai_jobs j ON j.id=a.job_id AND j.attempt_count=a.attempt
            WHERE a.key_id=? AND j.state='running' AND j.lease_until>? AND j.lease_token=a.lease_token
            AND (? IS NULL OR j.id!=?) ORDER BY j.lease_until LIMIT 1""", (kid, ts, excluding, excluding)).fetchone()
        check = c.execute("SELECT lease_until FROM ai_decision_checks WHERE key_id=? AND state='running' AND lease_until>? ORDER BY lease_until LIMIT 1", (kid, ts)).fetchone()
        admin=c.execute('SELECT lease_until FROM ai_provider_admin_checks WHERE key_id=? AND lease_until>? ORDER BY lease_until LIMIT 1',(kid,ts)).fetchone()
        return min([r['lease_until'] for r in (row, decision, check, admin) if r], default=None)

    @staticmethod
    def _ai_group_checks(c, gid, ts):
        return c.execute("""SELECT min(lease_until) FROM (
            SELECT d.lease_until FROM ai_decision_checks d JOIN ai_provider_keys k ON k.id=d.key_id
            WHERE k.quota_group_id=? AND d.state='running' AND d.lease_until>?
            UNION ALL SELECT lease_until FROM ai_provider_admin_checks WHERE quota_group_id=? AND lease_until>?) AS checks""",(gid,ts,gid,ts)).fetchone()[0]

    def ai_admin_check_begin(self, principal, snapshot):
        ts=time.time()
        with self.transaction(True) as c:
            self._ai_admin(c,principal)
            key=c.execute('SELECT * FROM ai_provider_keys WHERE id=? AND deleted_at IS NULL',(snapshot['key_id'],)).fetchone()
            if not key:
                conflict('The provider key changed')
            group=c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?',(key['quota_group_id'],)).fetchone()
            self._ai_key_fences(key,group,snapshot['key_version'],snapshot['health_epoch'],snapshot['group_epoch'])
            if snapshot.get('project_id'):
                config=self._ai_config(c,snapshot['project_id'])
                if config['version']!=snapshot['version'] or config['settings']['model']!=snapshot['settings']['model']:
                    conflict('Project settings changed')
            else:
                self._ai_library_admin(c,principal,snapshot['version'])
            if self._ai_key_busy(c,key['id'],ts) or self._ai_trial_live(c,group,ts) or self._ai_group_checks(c,group['id'],ts):
                raise Error(429,'ai_operation_running','This provider key or account is in use. Wait and retry.')
            c.execute('DELETE FROM ai_provider_admin_checks WHERE lease_until<=?',(ts,))
            cid,token=new_id(),secrets.token_urlsafe(32)
            c.execute('INSERT INTO ai_provider_admin_checks VALUES(?,?,?,?,?)',(cid,token,key['id'],group['id'],ts+120))
            return {'id':cid,'token':token}

    def ai_admin_check_end(self, check):
        with self.transaction(True) as c:
            c.execute('DELETE FROM ai_provider_admin_checks WHERE id=? AND token=?',(check['id'],check['token']))

    def _ai_pool_view(self, c, config, ts=None):
        ts = time.time() if ts is None else ts
        rows, groups, keys = self._ai_pool_rows(c, config['project_id'], config['settings']['provider']), {}, []
        for key in rows:
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            trial = self._ai_trial_live(c, group, ts)
            group_status = 'cooling_down' if group['cooldown_until'] > ts else 'recovering' if trial else 'ready'
            groups[group['id']] = {'id': group['id'], 'label': group['label'], 'status': group_status,
                'reason': group['reason'], 'retry_at': group['cooldown_until'] if group['cooldown_until'] > ts else group['trial_until'] if trial else None,
                'health_epoch': group['health_epoch']}
            eligible = self._ai_key_verified(key, config['settings'], self._mcp_needs_tools(c,config['project_id']))
            busy = self._ai_key_busy(c, key['id'], ts)
            status = ('paused' if not key['enabled'] else 'blocked' if key['blocked_reason'] else 'unverified' if not eligible else
                group_status if group_status != 'ready' else 'busy' if busy else 'ready')
            keys.append({k: key[k] for k in ('id', 'label', 'version', 'created_at', 'updated_at')} | {
                'quota_group': group['label'], 'quota_group_id': group['id'], 'enabled': bool(key['enabled']),
                'is_default': bool(key['is_default']), 'status': status, 'reason': key['blocked_reason'] or key['reason'] or group['reason'],
                'retry_at': groups[group['id']]['retry_at'] or busy, 'has_key': bool(key['protected_key']),
                'catalogue_count': len(key['catalogue']), 'model_available': config['settings']['model'] in key['catalogue'],
                'capabilities': key['capabilities'], 'capabilities_model': key['capabilities_model'], 'eligible': eligible})
        return {'project_id': config['project_id'], 'provider': config['settings']['provider'], 'version': config['version'],
            'credential_version': config['credential_version'], 'maximum_keys': 99, 'selected_model': config['settings']['model'],
            'catalogue': config['catalogue'], 'verified_key_count': sum(k['eligible'] for k in keys),
            'available_key_count': sum(k['status'] == 'ready' for k in keys), 'keys': keys, 'groups': list(groups.values())}

    def ai_provider_keys(self, project, principal):
        with self.transaction() as c:
            self._ai_admin(c, principal)
            return self._ai_pool_view(c, self._ai_config(c, self._ai_project(c, project)))

    def ai_provider_key_secrets(self, project, principal, version):
        with self.transaction() as c:
            config = self._ai_pool_admin(c, project, principal, version)
            return [dict(row) for row in c.execute('SELECT id,protected_key FROM ai_provider_keys WHERE provider=? AND deleted_at IS NULL', (config['settings']['provider'],))]

    def _ai_pool_admin(self, c, project, principal, version):
        self._ai_admin(c, principal)
        config = self._ai_config(c, self._ai_project(c, project))
        if type(version) is not int or config['version'] != version:
            conflict()
        return config

    def _ai_pool_changed(self, c, config):
        config.update(version=config['version'] + 1, credential_version=config['credential_version'] + 1, updated_at=now())
        config = self._ai_pool_config(c, config)
        if config['settings']['enabled'] and not self._ai_ready(c, config):
            config['settings']['enabled'] = False
        self._ai_write_config(c, config)
        self._ai_cancel_generation(c, config['project_id'])
        return self._ai_pool_view(c, config)

    def ai_provider_key_add(self, project, principal, version, label, quota_group, protected_key, *, library_version=None):
        with self.transaction(True) as c:
            config = self._ai_pool_admin(c, project, principal, version)
            self._ai_library_admin(c,principal,library_version)
            default = self._ai_pool_key(c, config['project_id'], required=False)
            self._ai_new_key(c, config, label, quota_group, protected_key, not default)
            return self._ai_pool_changed(c, config)

    def ai_provider_key_update(self, project, principal, version, key_id, key_version, *, label, quota_group, enabled, protected_key=_AI_UNSET, library_version=None):
        self._ai_pool_label(label)
        self._ai_pool_label(quota_group)
        if type(key_version) is not int or type(enabled) is not bool or (protected_key is not _AI_UNSET and (not isinstance(protected_key, str) or not 1 <= len(protected_key) <= 16384)):
            raise Error(400, 'invalid_input', 'Invalid provider key update')
        with self.transaction(True) as c:
            config = self._ai_pool_admin(c, project, principal, version)
            self._ai_library_admin(c,principal,library_version)
            key = self._ai_pool_key(c, config['project_id'], key_id)
            group = self._ai_group(c, config['settings']['provider'], quota_group)
            self._ai_key_fences(key, group, key_version)
            secret = key['protected_key'] if protected_key is _AI_UNSET else protected_key
            if (label, group['id'], int(enabled), secret) == (key['label'], key['quota_group_id'], key['enabled'], key['protected_key']):
                return self._ai_pool_view(c, config)
            changed = secret != key['protected_key']
            c.execute('''UPDATE ai_provider_keys SET label=?,quota_group_id=?,enabled=?,protected_key=?,version=version+1,
                catalogue=?,capabilities=?,capabilities_model=?,capabilities_key_version=?,blocked_reason=?,reason=?,
                health_epoch=health_epoch+1,updated_at=? WHERE id=?''',
                (label, group['id'], int(enabled), secret, '[]' if changed else key['catalogue'], '{}' if changed else key['capabilities'],
                 None if changed else key['capabilities_model'], None if changed or key['capabilities_key_version'] is None else key['version'] + 1,
                 None if changed else key['blocked_reason'], None if changed else key['reason'], now(), key_id))
            if changed:
                c.execute('DELETE FROM ai_key_model_evidence WHERE key_id=?', (key_id,))
                c.execute('DELETE FROM ai_key_decision_catalogues WHERE key_id=?', (key_id,))
            else:
                c.execute('UPDATE ai_key_model_evidence SET key_version=key_version+1 WHERE key_id=? AND key_version=?', (key_id,key_version))
            self._ai_shared_changed(c,key_id,exclude_pid=config['project_id'])
            return self._ai_pool_changed(c, config)

    def ai_provider_key_delete(self, project, principal, version, key_id, key_version):
        with self.transaction(True) as c:
            config = self._ai_pool_admin(c, project, principal, version)
            key = self._ai_pool_key(c, config['project_id'], key_id)
            if type(key_version) is not int or key['version'] != key_version:
                conflict()
            self._ai_remove_key(c, key_id)
            self._ai_shared_changed(c,key_id,exclude_pid=config['project_id'])
            c.execute('DELETE FROM ai_decision_keys WHERE key_id=?',(key_id,))
            self._ai_promote_default(c, config['project_id'])
            return self._ai_pool_changed(c, config)

    def ai_provider_key_retry(self, project, principal, version, key_id, key_version):
        with self.transaction(True) as c:
            config = self._ai_pool_admin(c, project, principal, version)
            key = self._ai_pool_key(c, config['project_id'], key_id)
            if type(key_version) is not int or key['version'] != key_version:
                conflict()
            c.execute('''UPDATE ai_provider_quota_groups SET health_epoch=health_epoch+1,reset_epoch=health_epoch+1,cooldown_until=0,recovery_required=1,
                trial_job_id=NULL,trial_attempt=NULL,trial_token=NULL,trial_until=NULL WHERE id=?''', (key['quota_group_id'],))
            for row in c.execute('SELECT id FROM ai_provider_keys WHERE quota_group_id=? AND deleted_at IS NULL',(key['quota_group_id'],)).fetchall():
                self._ai_shared_changed(c,row[0],exclude_pid=config['project_id'])
            return self._ai_pool_changed(c, config)

    @staticmethod
    def _ai_model_evidence(c, key_id, role, model, version, capabilities, verified):
        c.execute("""INSERT INTO ai_key_model_evidence(key_id,role,model,key_version,capabilities,verified) VALUES(?,?,?,?,?,?)
            ON CONFLICT(key_id,role,model) DO UPDATE SET key_version=excluded.key_version,capabilities=excluded.capabilities,verified=excluded.verified""",
            (key_id, role, model, version, RelationalStorage._ai_json(capabilities), int(verified)))

    @staticmethod
    def _ai_prune_model_evidence(c, key_id, role, models):
        # Catalogue inputs are validated and bounded by the caller. Batch exact
        # stale models without provider JSON functions or excessive round trips;
        # 252 bindings remain below SQLite builds' 999-variable ceiling.
        retained = set(models)
        existing = c.execute('SELECT model FROM ai_key_model_evidence WHERE key_id=? AND role=?', (key_id, role)).fetchall()
        stale = [row[0] for row in existing if row[0] not in retained]
        for start in range(0, len(stale), 250):
            batch = stale[start:start + 250]
            placeholders = ','.join('?' for _ in batch)
            c.execute('DELETE FROM ai_key_model_evidence WHERE key_id=? AND role=? AND model IN (' + placeholders + ')', (key_id, role, *batch))

    @staticmethod
    def _ai_decision_models(c, key_id, models):
        c.execute('INSERT INTO ai_key_decision_catalogues(key_id,catalogue) VALUES(?,?) ON CONFLICT(key_id) DO UPDATE SET catalogue=excluded.catalogue', (key_id, RelationalStorage._ai_json(list(models))))
        RelationalStorage._ai_prune_model_evidence(c, key_id, 'decision', models)

    def _ai_shared_changed(self, c, kid, exclude_pid=None, *, generation_projects=(), decision_projects=()):
        generations = set(generation_projects) | {row[0] for row in c.execute('SELECT project_id FROM ai_generation_keys WHERE key_id=?', (kid,))}
        decisions = set(decision_projects) | {row[0] for row in c.execute('SELECT project_id FROM ai_decision_keys WHERE key_id=?', (kid,))}
        for pid in generations:
            self._ai_promote_default(c, pid)
            if pid != exclude_pid:
                self._ai_pool_changed(c, self._ai_config(c, pid))
        for pid in decisions:
            # Keep an enabled gate enabled on credential loss: unavailable fails closed.
            self._ai_topic_changed(c, self._ai_topic(c, pid))
        c.execute('UPDATE ai_key_library SET version=version+1 WHERE singleton=1')

    def _ai_shared_evidence_changed(self, c, kid, exclude_pid=None):
        for row in c.execute('SELECT project_id FROM ai_generation_keys WHERE key_id=?', (kid,)).fetchall():
            if row[0] != exclude_pid:
                self._ai_provider_health_projection(c, row[0])

    def _ai_library_admin(self, c, principal, version=None):
        self._ai_admin(c, principal)
        current = c.execute('SELECT version FROM ai_key_library WHERE singleton=1').fetchone()[0]
        if version is not None and (type(version) is not int or version != current):
            conflict('Account keys changed. Reload and review your edits.')
        return current

    def _ai_library_view(self, c):
        keys = []
        ts = time.time()
        for row in c.execute('SELECT k.*,g.label AS quota_group,g.cooldown_until,g.reason AS group_reason FROM ai_provider_keys k JOIN ai_provider_quota_groups g ON g.id=k.quota_group_id WHERE k.deleted_at IS NULL ORDER BY k.provider,k.created_at,k.id'):
            key = {name: row[name] for name in ('id','provider','label','version','purpose','created_at','updated_at','quota_group')}
            projects = [dict(p) for p in c.execute("""SELECT p.id,p.name,'generation' AS role FROM ai_generation_keys b JOIN projects p ON p.id=b.project_id WHERE b.key_id=?
                UNION ALL SELECT p.id,p.name,'decision' AS role FROM ai_decision_keys b JOIN projects p ON p.id=b.project_id WHERE b.key_id=? ORDER BY name,id,role""", (row['id'],row['id']))]
            decision = c.execute('SELECT catalogue FROM ai_key_decision_catalogues WHERE key_id=?', (row['id'],)).fetchone()
            key.update(enabled=bool(row['enabled']),has_key=bool(row['protected_key']),projects=projects,
                catalogue=json.loads(row['catalogue']),decision_catalogue=json.loads(decision[0]) if decision else [],
                verified_models=[dict(e) | {'capabilities': json.loads(e['capabilities'])} for e in c.execute('SELECT role,model,capabilities FROM ai_key_model_evidence WHERE key_id=? AND key_version=? AND verified=1 ORDER BY role,model', (row['id'],row['version']))],
                status='paused' if not row['enabled'] else 'blocked' if row['blocked_reason'] else 'cooling_down' if row['cooldown_until']>ts else 'saved',
                reason=row['blocked_reason'] or row['reason'] or row['group_reason'],retry_at=row['cooldown_until'] if row['cooldown_until']>ts else None)
            keys.append(key)
        return {'version': c.execute('SELECT version FROM ai_key_library WHERE singleton=1').fetchone()[0], 'maximum_keys':99,'keys':keys}

    def ai_account_keys(self, principal):
        with self.transaction() as c:
            self._ai_library_admin(c, principal)
            return self._ai_library_view(c)

    def ai_account_key_snapshot(self, principal, version, key_id=None, key_version=None, provider=None):
        with self.transaction() as c:
            self._ai_library_admin(c, principal, version)
            if key_id is None:
                return [dict(r) for r in c.execute('SELECT id,protected_key FROM ai_provider_keys WHERE provider=? AND deleted_at IS NULL', (provider,))]
            key = c.execute('SELECT * FROM ai_provider_keys WHERE id=? AND deleted_at IS NULL', (key_id,)).fetchone()
            if not key:
                missing()
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            self._ai_key_fences(key, group, key_version)
            return {'key':dict(key),'group':dict(group),'version':version}

    def ai_account_key_add(self, principal, version, provider, purpose, label, group, protected_key):
        from feedback.ai_providers import PROVIDERS
        from feedback.ai_decision_providers import DECISION_PROVIDERS
        if purpose not in ('generation','decision','both') or (purpose!='decision' and provider not in PROVIDERS) or (purpose!='generation' and provider not in DECISION_PROVIDERS):
            raise Error(400, 'invalid_input', 'Choose supported provider roles')
        with self.transaction(True) as c:
            self._ai_library_admin(c, principal, version)
            kid = self._ai_new_key(c, {'project_id':None,'settings':{'provider':provider}}, label, group, protected_key)
            c.execute('UPDATE ai_provider_keys SET purpose=? WHERE id=?', (purpose,kid))
            return self._ai_library_view(c)

    def ai_account_key_update(self, principal, version, key_id, key_version, *, label, quota_group, enabled, protected_key=_AI_UNSET, purpose=None):
        self._ai_pool_label(label)
        self._ai_pool_label(quota_group)
        if type(enabled) is not bool or type(key_version) is not int or (protected_key is not _AI_UNSET and (not isinstance(protected_key,str) or not 1<=len(protected_key)<=16384)):
            raise Error(400,'invalid_input','Invalid provider key update')
        with self.transaction(True) as c:
            self._ai_library_admin(c, principal, version)
            key = c.execute('SELECT * FROM ai_provider_keys WHERE id=? AND deleted_at IS NULL', (key_id,)).fetchone()
            if not key:
                missing()
            group = self._ai_group(c, key['provider'], quota_group)
            self._ai_key_fences(key, group, key_version)
            from feedback.ai_providers import PROVIDERS
            from feedback.ai_decision_providers import DECISION_PROVIDERS
            purpose=key['purpose'] if purpose is None else purpose
            if purpose not in ('generation','decision','both') or (purpose!='decision' and key['provider'] not in PROVIDERS) or (purpose!='generation' and key['provider'] not in DECISION_PROVIDERS):
                raise Error(400,'invalid_input','Choose supported provider roles')
            generation_projects=[r[0] for r in c.execute('SELECT project_id FROM ai_generation_keys WHERE key_id=?',(key_id,))]
            decision_projects=[r[0] for r in c.execute('SELECT project_id FROM ai_decision_keys WHERE key_id=?',(key_id,))]
            secret = key['protected_key'] if protected_key is _AI_UNSET else protected_key
            if purpose==key['purpose'] and (label,group['id'],int(enabled),secret)==(key['label'],key['quota_group_id'],key['enabled'],key['protected_key']):
                return self._ai_library_view(c)
            changed = secret!=key['protected_key']
            c.execute('UPDATE ai_provider_keys SET label=?,quota_group_id=?,enabled=?,protected_key=?,version=version+1,health_epoch=health_epoch+1,updated_at=? WHERE id=?', (label,group['id'],int(enabled),secret,now(),key_id))
            if changed:
                c.execute("UPDATE ai_provider_keys SET catalogue='[]',capabilities='{}',capabilities_model=NULL,capabilities_key_version=NULL,blocked_reason=NULL,reason=NULL WHERE id=?", (key_id,))
                c.execute('DELETE FROM ai_key_model_evidence WHERE key_id=?', (key_id,))
                c.execute('DELETE FROM ai_key_decision_catalogues WHERE key_id=?', (key_id,))
            else:
                c.execute('UPDATE ai_key_model_evidence SET key_version=key_version+1 WHERE key_id=? AND key_version=?', (key_id,key_version))
            c.execute('UPDATE ai_provider_keys SET purpose=? WHERE id=?',(purpose,key_id))
            if purpose=='decision':
                c.execute('DELETE FROM ai_generation_keys WHERE key_id=?',(key_id,))
                c.execute("DELETE FROM ai_key_model_evidence WHERE key_id=? AND role='generation'",(key_id,))
                c.execute("UPDATE ai_provider_keys SET catalogue='[]',capabilities='{}',capabilities_model=NULL,capabilities_key_version=NULL WHERE id=?",(key_id,))
            if purpose=='generation':
                c.execute('DELETE FROM ai_decision_keys WHERE key_id=?',(key_id,))
                c.execute("DELETE FROM ai_key_model_evidence WHERE key_id=? AND role='decision'",(key_id,))
                c.execute('DELETE FROM ai_key_decision_catalogues WHERE key_id=?',(key_id,))
            self._ai_shared_changed(c,key_id,generation_projects=generation_projects,decision_projects=decision_projects)
            return self._ai_library_view(c)

    def ai_account_key_action(self, principal, version, key_id, key_version, action):
        if action not in ('delete','retry'):
            raise Error(400,'invalid_input','Invalid key action')
        with self.transaction(True) as c:
            self._ai_library_admin(c,principal,version)
            key=c.execute('SELECT * FROM ai_provider_keys WHERE id=? AND deleted_at IS NULL',(key_id,)).fetchone()
            if not key:
                missing()
            if type(key_version) is not int or key['version']!=key_version:
                conflict()
            if action=='delete':
                self._ai_remove_key(c,key_id)
                self._ai_shared_changed(c,key_id)
                c.execute('DELETE FROM ai_decision_keys WHERE key_id=?',(key_id,))
            else:
                c.execute('UPDATE ai_provider_quota_groups SET health_epoch=health_epoch+1,reset_epoch=health_epoch+1,cooldown_until=0,recovery_required=1,trial_job_id=NULL,trial_attempt=NULL,trial_token=NULL,trial_until=NULL WHERE id=?',(key['quota_group_id'],))
                # Every key in this quota account shares the recovery fence.
                for row in c.execute('SELECT id FROM ai_provider_keys WHERE quota_group_id=? AND deleted_at IS NULL',(key['quota_group_id'],)).fetchall():
                    self._ai_shared_changed(c,row[0])
            return self._ai_library_view(c)

    def ai_account_key_models(self, principal, snapshot, role, models):
        if role not in ('generation','decision') or not isinstance(models,(list,tuple)) or len(models)>1000 or len(set(models))!=len(models) or any(not isinstance(m,str) or not 1<=len(m)<=256 for m in models):
            raise Error(502,'ai_invalid_catalogue','Invalid model catalogue')
        with self.transaction(True) as c:
            self._ai_library_admin(c,principal,snapshot['version'])
            key=c.execute('SELECT * FROM ai_provider_keys WHERE id=? AND deleted_at IS NULL',(snapshot['key']['id'],)).fetchone()
            if not key:
                conflict()
            group=c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?',(key['quota_group_id'],)).fetchone()
            self._ai_key_fences(key,group,snapshot['key']['version'],snapshot['key']['health_epoch'],snapshot['group']['health_epoch'])
            if role=='decision':
                self._ai_decision_models(c,key['id'],models)
            else:
                c.execute('UPDATE ai_provider_keys SET catalogue=?,updated_at=? WHERE id=?',(self._ai_json(list(models)),now(),key['id']))
                self._ai_prune_model_evidence(c, key['id'], 'generation', models)
                self._ai_shared_evidence_changed(c,key['id'])
            c.execute('UPDATE ai_provider_keys SET health_epoch=health_epoch+1 WHERE id=?',(key['id'],))
            return self._ai_library_view(c)

    def ai_account_key_failure(self, principal, snapshot, outcome, retry_after=None, role='generation'):
        self._ai_provider_outcome_input(outcome,retry_after)
        with self.transaction(True) as c:
            self._ai_library_admin(c,principal,snapshot['version'])
            key=c.execute('SELECT * FROM ai_provider_keys WHERE id=? AND deleted_at IS NULL',(snapshot['key']['id'],)).fetchone()
            if not key:
                conflict()
            group=c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?',(key['quota_group_id'],)).fetchone()
            self._ai_key_fences(key,group,snapshot['key']['version'],snapshot['key']['health_epoch'],snapshot['group']['health_epoch'])
            if role=='decision' and outcome in ('access_denied','model_or_endpoint_unavailable','capability_unverified'):
                c.execute("DELETE FROM ai_key_model_evidence WHERE key_id=? AND role='decision'",(key['id'],))
                c.execute('DELETE FROM ai_key_decision_catalogues WHERE key_id=?',(key['id'],))
            else:
                self._ai_provider_health(c,key,group,outcome,time.time(),retry_after,admin=True,catalogue_failure=True)
            self._ai_shared_evidence_changed(c,key['id'])

    def ai_project_key_selection(self, project, principal, version, library_version, role, key_ids):
        if role not in ('generation','decision') or not isinstance(key_ids,list) or len(key_ids)>99 or any(not isinstance(k,str) for k in key_ids) or len(set(key_ids))!=len(key_ids):
            raise Error(400,'invalid_input','Choose up to 99 distinct account keys')
        with self.transaction(True) as c:
            self._ai_library_admin(c,principal,library_version)
            selection=self._ai_pool_admin(c,project,principal,version) if role=='generation' else self._ai_topic_admin(c,project,principal,version)
            pid=selection['project_id']
            provider=selection['settings']['provider'] if role=='generation' else selection['policy']['provider']
            for kid in key_ids:
                key=c.execute('SELECT * FROM ai_provider_keys WHERE id=? AND deleted_at IS NULL',(kid,)).fetchone()
                if not key or key['provider']!=provider or key['purpose'] not in (role,'both'):
                    raise Error(409,'ai_key_selection_invalid','Choose account keys for this provider and role')
            table='ai_generation_keys' if role=='generation' else 'ai_decision_keys'
            old=[r[0] for r in c.execute('SELECT key_id FROM '+table+' WHERE project_id=? ORDER BY key_id',(pid,))]
            if sorted(key_ids)==old:
                return self._ai_pool_view(c,selection) if role=='generation' else self._ai_topic_view(c,selection)
            c.execute('DELETE FROM '+table+' WHERE project_id=?',(pid,))
            for index,kid in enumerate(key_ids):
                if role=='generation':
                    c.execute('INSERT INTO ai_generation_keys(project_id,key_id,is_default) VALUES(?,?,?)',(pid,kid,int(index==0)))
                else:
                    c.execute('INSERT INTO ai_decision_keys(project_id,key_id) VALUES(?,?)',(pid,kid))
            c.execute('UPDATE ai_key_library SET version=version+1 WHERE singleton=1')
            return self._ai_pool_changed(c,selection) if role=='generation' else self._ai_topic_changed(c,selection)

    @staticmethod
    def _ai_provider_outcome_input(outcome, retry_after):
        if outcome not in ('success', 'unchanged', 'capability_unverified', 'rate_limited', 'quota_exhausted',
                           'invalid_credentials', 'payment_required', 'access_denied', 'model_or_endpoint_unavailable', 'transient'):
            raise Error(400, 'invalid_input', 'Invalid provider outcome')
        if retry_after is not None and (type(retry_after) not in (int, float) or not math.isfinite(retry_after) or not 0 <= retry_after <= 86400):
            raise Error(400, 'invalid_input', 'Invalid provider retry time')

    def _ai_provider_health(self, c, key, group, outcome, ts, retry_after, *, job=None, admin=False, catalogue_failure=False):
        owned_trial = job is not None and (group['trial_job_id'], group['trial_attempt'], group['trial_token']) == (job['id'], job['attempt_count'], job['lease_token'])
        if outcome == 'success':
            if owned_trial:
                c.execute('''UPDATE ai_provider_quota_groups SET health_epoch=health_epoch+1,reset_epoch=health_epoch+1,cooldown_until=0,failure_count=0,recovery_required=0,reason=NULL,
                    trial_job_id=NULL,trial_attempt=NULL,trial_token=NULL,trial_until=NULL WHERE id=?''', (group['id'],))
        elif outcome == 'unchanged':
            if owned_trial:
                c.execute('UPDATE ai_provider_quota_groups SET trial_job_id=NULL,trial_attempt=NULL,trial_token=NULL,trial_until=NULL WHERE id=?', (group['id'],))
        elif outcome in ('rate_limited', 'quota_exhausted', 'transient'):
            delay = 86400 if outcome == 'quota_exhausted' else min(86400, 60 * 2 ** min(group['failure_count'], 11))
            until = max(group['cooldown_until'], ts + max(delay, retry_after or 0))
            c.execute('''UPDATE ai_provider_quota_groups SET health_epoch=health_epoch+1,cooldown_until=?,failure_count=failure_count+1,
                recovery_required=1,reason=?,trial_job_id=NULL,trial_attempt=NULL,trial_token=NULL,trial_until=NULL WHERE id=?''', (until, outcome, group['id']))
        else:
            blocked = outcome if outcome in ('invalid_credentials', 'payment_required') else key['blocked_reason']
            clear_caps = outcome in ('access_denied', 'model_or_endpoint_unavailable', 'capability_unverified', 'invalid_credentials')
            if clear_caps:
                if outcome=='invalid_credentials':
                    c.execute('DELETE FROM ai_key_model_evidence WHERE key_id=?',(key['id'],))
                    c.execute('DELETE FROM ai_key_decision_catalogues WHERE key_id=?',(key['id'],))
                elif catalogue_failure:
                    c.execute("DELETE FROM ai_key_model_evidence WHERE key_id=? AND role='generation'",(key['id'],))
                else:
                    model = self._ai_config(c,job['project_id'])['settings']['model'] if job else key['capabilities_model']
                    c.execute("DELETE FROM ai_key_model_evidence WHERE key_id=? AND role='generation' AND model=?",(key['id'],model))
            clear_catalogue = outcome == 'invalid_credentials' or (catalogue_failure and outcome in ('access_denied','model_or_endpoint_unavailable'))
            c.execute('''UPDATE ai_provider_keys SET blocked_reason=?,reason=?,health_epoch=health_epoch+1,
                catalogue=?,capabilities=?,capabilities_model=?,capabilities_key_version=?,updated_at=? WHERE id=?''',
                (blocked, outcome, '[]' if clear_catalogue else key['catalogue'], '{}' if clear_caps else key['capabilities'],
                 None if clear_caps else key['capabilities_model'], None if clear_caps else key['capabilities_key_version'], now(), key['id']))
            if owned_trial:
                c.execute('UPDATE ai_provider_quota_groups SET trial_job_id=NULL,trial_attempt=NULL,trial_token=NULL,trial_until=NULL WHERE id=?', (group['id'],))

    def _ai_provider_health_projection(self, c, pid):
        config = self._ai_config(c, pid)
        if not self._ai_ready(c, config):
            # Health loss is an explicit terminal failure, including private
            # previews where website AI was already off. Mark jobs before the
            # human transition cancels any still-pending work for that chat.
            jobs = c.execute("SELECT * FROM ai_jobs WHERE project_id=? AND kind IN ('turn','preview','sections') AND state IN ('queued','running')", (pid,)).fetchall()
            c.execute("UPDATE ai_jobs SET state='failed',error='unavailable',lease_token=NULL,lease_until=NULL,updated_at=? WHERE project_id=? AND kind IN ('turn','preview','sections') AND state IN ('queued','running')", (now(), pid))
            for job in jobs:
                if job['kind'] == 'turn':
                    self._ai_turn_failed(c, job)
            if config['settings']['enabled']:
                config['settings']['enabled'] = False
                config['version'] += 1
            self._ai_cancel_generation(c, pid)
        config['updated_at'] = now()
        self._ai_write_config(c, config)
        return config

    def ai_provider_check_outcome(self, project, principal, version, key_id, key_version, health_epoch, group_epoch, outcome, *, retry_after=None, catalogue_failure=False):
        if type(catalogue_failure) is not bool:
            raise Error(400,'invalid_input','Invalid catalogue failure flag')
        self._ai_provider_outcome_input(outcome, retry_after)
        if outcome == 'success' or any(type(value) is not int for value in (key_version, health_epoch, group_epoch)):
            raise Error(400, 'invalid_input', 'A provider check needs exact evidence and state versions')
        with self.transaction(True) as c:
            config = self._ai_pool_admin(c, project, principal, version)
            key = self._ai_pool_key(c, config['project_id'], key_id)
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            self._ai_key_fences(key, group, key_version, health_epoch, group_epoch)
            self._ai_provider_health(c, key, group, outcome, time.time(), retry_after, admin=True,catalogue_failure=catalogue_failure)
            self._ai_shared_evidence_changed(c,key['id'],exclude_pid=config['project_id'])
            return self._ai_pool_view(c, self._ai_provider_health_projection(c, config['project_id']))

    def _ai_bound_key(self, c, job, *, required=False):
        bound = c.execute('SELECT * FROM ai_provider_attempts WHERE job_id=? AND attempt=?', (job['id'], job['attempt_count'])).fetchone()
        if not bound:
            return None
        key = self._ai_pool_key(c, job['project_id'], bound['key_id'], required=False)
        config = self._ai_config(c, job['project_id'])
        if not key or bound['lease_token'] != job['lease_token'] or key['version'] != bound['key_version'] or not key['enabled'] or key['blocked_reason'] or key['provider'] != config['settings']['provider']:
            if required:
                conflict('The selected provider key is no longer eligible')
            return False
        decoded = dict(key) | {'catalogue': json.loads(key['catalogue']), 'capabilities': json.loads(key['capabilities'])}
        if not self._ai_key_verified(decoded, config['settings'], self._mcp_needs_tools(c,config['project_id'])):
            if required:
                conflict('The selected provider model is no longer verified')
            return False
        return key

    def _ai_provider_available(self, c, config, ts, *, job=None, pinned=None):
        candidates = self._ai_pool_rows(c, config['project_id'], config['settings']['provider'])
        candidates = sorted((k for k in candidates if k['enabled'] and not k['blocked_reason'] and self._ai_key_verified(k, config['settings'], self._mcp_needs_tools(c,config['project_id']))), key=lambda k: (k['last_selected'], k['created_at'], k['id']))
        if pinned:
            candidates = [k for k in candidates if k['id'] == pinned['id']]
        waits = []
        for key in candidates:
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            check_until = self._ai_group_checks(c,group['id'],ts)
            if check_until:
                waits.append(('busy', check_until))
                continue
            if group['cooldown_until'] > ts:
                waits.append(('cooldown', group['cooldown_until']))
                continue
            own_trial = job is not None and (group['trial_job_id'], group['trial_attempt'], group['trial_token']) == (job['id'], job['attempt_count'], job['lease_token'])
            if self._ai_trial_live(c, group, ts) and not own_trial:
                waits.append(('busy', group['trial_until']))
                continue
            busy = self._ai_key_busy(c, key['id'], ts, job['id'] if job else None)
            if busy:
                waits.append(('busy', busy))
                continue
            return key, group, None
        wait = min(waits, key=lambda item: item[1]) if waits else ('unverified', None)
        return None, None, {'available': False, 'reason': wait[0], 'retry_at': wait[1]}

    def _ai_provider_claim(self, c, job, ts):
        config = self._ai_config(c, job['project_id'])
        bound = self._ai_bound_key(c, job, required=True)
        key, group, unavailable = self._ai_provider_available(c, config, ts, job=job, pinned=bound)
        if unavailable:
            return unavailable
        if not bound:
            sequence = c.execute('SELECT COALESCE(MAX(last_selected),0)+1 FROM ai_provider_keys', ()).fetchone()[0]
            c.execute('UPDATE ai_provider_keys SET last_selected=? WHERE id=?', (sequence, key['id']))
            c.execute('''INSERT INTO ai_provider_attempts(job_id,attempt,key_id,key_version,health_epoch,quota_group_id,group_epoch,lease_token)
                VALUES(?,?,?,?,?,?,?,?)''', (job['id'], job['attempt_count'], key['id'], key['version'], key['health_epoch'], group['id'], group['health_epoch'], job['lease_token']))
        if group['recovery_required']:
            c.execute('UPDATE ai_provider_quota_groups SET trial_job_id=?,trial_attempt=?,trial_token=?,trial_until=? WHERE id=?',
                (job['id'], job['attempt_count'], job['lease_token'], job['lease_until'], group['id']))
        return {'available': True, 'provider': config['settings']['provider'], 'model': config['settings']['model'],
            'key_id': key['id'], 'key_version': key['version'], 'protected_key': key['protected_key'], 'capabilities': key['capabilities'],
            'credential_version': config['credential_version'], 'quota_group_id': group['id'], 'health_epoch': key['health_epoch'], 'group_epoch': group['health_epoch']}

    def ai_provider_claim(self, job_id, token, current_time=None):
        ts = time.time() if current_time is None else current_time
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, ts)
            if not job or job['kind'] not in ('turn', 'preview', 'sections'):
                conflict('This provider operation is no longer authorized')
            return self._ai_provider_claim(c, job, ts)

    def ai_provider_outcome(self, job_id, token, call_index, outcome, *, retry_after=None, current_time=None):
        self._ai_provider_outcome_input(outcome, retry_after)
        if type(call_index) is not int or call_index < 0:
            raise Error(400, 'invalid_input', 'Invalid provider call index')
        ts = time.time() if current_time is None else current_time
        denied = {'accepted': False, 'retryable': False, 'retry_at': None}
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, ts, allow_provider_unavailable=True)
            if not job or job['kind'] not in ('turn', 'preview', 'sections'):
                return denied
            reservation = c.execute('SELECT * FROM ai_usage_reservations WHERE job_id=? AND attempt=? AND call_index=?', (job_id, job['attempt_count'], call_index)).fetchone()
            if not reservation or not reservation['dispatched'] or not reservation['provider_key_id']:
                return denied
            if reservation['provider_outcome'] is not None:
                return denied
            key = self._ai_pool_key(c, job['project_id'], reservation['provider_key_id'], required=False)
            if not key or key['version'] != reservation['provider_key_version'] or key['health_epoch'] != reservation['provider_health_epoch']:
                return denied
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (reservation['provider_group_id'],)).fetchone()
            c.execute('UPDATE ai_usage_reservations SET provider_outcome=? WHERE job_id=? AND attempt=? AND call_index=?', (outcome, job_id, job['attempt_count'], call_index))
            # An older successful response can still be delivered, but it cannot
            # erase an account cooldown recorded after its dispatch.
            cooldown_failure = outcome in ('rate_limited', 'quota_exhausted', 'transient')
            same_recovery_cycle = (cooldown_failure and group['recovery_required'] and
                group['reset_epoch'] <= reservation['provider_group_epoch'] <= group['health_epoch'])
            # Concurrent failures in one unrecovered cycle can extend a limit;
            # responses from before a manual reset or successful recovery cannot.
            if outcome not in ('success', 'unchanged', 'rate_limited', 'quota_exhausted', 'transient') or group['health_epoch'] == reservation['provider_group_epoch'] or same_recovery_cycle:
                self._ai_provider_health(c, key, group, outcome, ts, retry_after, job=job)
            self._ai_shared_evidence_changed(c,key['id'],exclude_pid=job['project_id'])
            config = self._ai_provider_health_projection(c, job['project_id'])
            if outcome in ('success', 'unchanged', 'capability_unverified'):
                return {'accepted': True, 'retryable': False, 'retry_at': None}
            candidate, _, unavailable = self._ai_provider_available(c, config, ts, job=job)
            retry_at = ts if candidate else unavailable['retry_at']
            return {'accepted': True, 'retryable': retry_at is not None and job['attempt_count'] < job['max_attempts'], 'retry_at': retry_at}

    def ai_provider_defer(self, job_id, token, retry_at, current_time=None, *, error='unavailable'):
        ts = time.time() if current_time is None else current_time
        if type(retry_at) not in (int, float) or not math.isfinite(retry_at) or retry_at < ts or error not in ('unavailable', 'rate_limited', 'timeout'):
            raise Error(400, 'invalid_input', 'Invalid provider deferral')
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, ts, allow_provider_unavailable=True)
            if not job or job['kind'] not in ('turn', 'preview', 'sections'):
                return False
            paid = bool(c.execute('SELECT 1 FROM ai_usage_reservations WHERE job_id=? AND attempt=? AND dispatched=1', (job_id, job['attempt_count'])).fetchone())
            deadline = datetime.fromisoformat(job['created_at'].replace('Z', '+00:00')).timestamp() + 300
            fail = retry_at > deadline or ts >= deadline or (paid and job['attempt_count'] >= job['max_attempts'])
            if not paid:
                # Any reserved but undispatched allowance remains conservative;
                # its stable call index may be reused only with the same tokens.
                c.execute('DELETE FROM ai_provider_attempts WHERE job_id=? AND attempt=?', (job_id, job['attempt_count']))
            c.execute('''UPDATE ai_jobs SET state=?,error=?,next_attempt=?,attempt_count=attempt_count-?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?''',
                ('failed' if fail else 'queued', error, retry_at, int(not paid and not fail), now(), job_id))
            if fail:
                self._ai_turn_failed(c, job)
            return True

    @staticmethod
    def _ai_runtime_view(row):
        result = {k: row[k] for k in ('version', 'state', 'operation_id', 'action', 'progress_bytes', 'total_bytes', 'attempt_count', 'error', 'updated_at', 'active_cache_id')}
        result['active_manifest'] = json.loads(row['active_manifest']) if row['active_manifest'] else None
        return result

    def _ai_topic(self, c, pid):
        row = c.execute('SELECT * FROM ai_topic_policies WHERE project_id=?', (pid,)).fetchone()
        return {'project_id': pid, 'version': row['version'] if row else 0,
                'policy': json.loads(row['policy']) if row else default_topic_policy(),
                'updated_at': row['updated_at'] if row else None}

    def _ai_decision_rows(self, c, pid, provider):
        model = self._ai_topic(c, pid)['policy']['model']
        return [dict(row) for row in c.execute("""SELECT k.*,COALESCE(cat.catalogue,'[]') AS decision_catalogue,e.model AS decision_model,
            e.key_version AS decision_key_version,COALESCE(e.verified,0) AS verified FROM ai_decision_keys d JOIN ai_provider_keys k ON k.id=d.key_id
            LEFT JOIN ai_key_decision_catalogues cat ON cat.key_id=k.id
            LEFT JOIN ai_key_model_evidence e ON e.key_id=k.id AND e.role='decision' AND e.model=? AND e.key_version=k.version
            WHERE d.project_id=? AND k.provider=? AND k.purpose!='generation' AND k.deleted_at IS NULL ORDER BY k.last_selected,k.created_at,k.id""", (model, pid, provider))]

    @staticmethod
    def _ai_decision_verified(key, policy):
        return bool(key['enabled'] and not key['blocked_reason'] and key['verified'] and
            key['decision_model'] == policy['model'] and key['decision_key_version'] == key['version']
            and policy['model'] in json.loads(key['decision_catalogue']))

    def _ai_topic_view(self, c, topic):
        keys = []
        for key in self._ai_decision_rows(c, topic['project_id'], topic['policy']['provider']):
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            keys.append({'id': key['id'], 'label': key['label'], 'version': key['version'],
                'quota_group': group['label'], 'enabled': bool(key['enabled']), 'has_key': bool(key['protected_key']),
                'shared_with_generation': bool(c.execute('SELECT 1 FROM ai_generation_keys WHERE project_id=? AND key_id=?',(topic['project_id'],key['id'])).fetchone()), 'catalogue': json.loads(key['decision_catalogue']),
                'verified': self._ai_decision_verified(key, topic['policy']), 'retry_at': group['cooldown_until'] or None,
                'reason': key['blocked_reason'] or group['reason']})
        return topic | {'keys': keys, 'preset_version': POLICY_VERSION, 'maximum_keys': 99}

    def ai_topic_settings(self, project, principal):
        with self.transaction() as c:
            self._ai_admin(c, principal)
            return self._ai_topic_view(c, self._ai_topic(c, self._ai_project(c, project)))

    def ai_topic_decisions(self, project, principal, limit=25, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise Error(400, 'invalid_input', 'Invalid decision history page')
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            rows = [dict(row) for row in c.execute('''SELECT r.job_id,r.attempt,r.policy_version,r.preset_version,r.outcome,r.probability,r.model,r.error,r.created_at
                FROM ai_topic_receipts r JOIN ai_jobs j ON j.id=r.job_id WHERE j.project_id=? ORDER BY r.created_at DESC,r.job_id LIMIT ? OFFSET ?''', (pid, limit+1, offset))]
            return {'items': rows[:limit], 'has_more': len(rows)>limit, 'limit': limit, 'offset': offset}

    def _ai_topic_changed(self, c, topic):
        topic = topic | {'version': topic['version'] + 1, 'updated_at': now()}
        c.execute('''INSERT INTO ai_topic_policies(project_id,version,policy,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET version=excluded.version,policy=excluded.policy,updated_at=excluded.updated_at''',
            (topic['project_id'], topic['version'], self._ai_json(topic['policy']), topic['updated_at']))
        config = self._ai_config(c, topic['project_id'])
        config.update(version=config['version'] + 1, updated_at=now())
        self._ai_write_config(c, config)
        self._ai_cancel_generation(c, topic['project_id'])
        return self._ai_topic_view(c, topic)

    def _ai_topic_admin(self, c, project, principal, version):
        self._ai_admin(c, principal)
        topic = self._ai_topic(c, self._ai_project(c, project))
        if type(version) is not int or topic['version'] != version:
            conflict('Topic settings changed. Reload and review your edits.')
        return topic

    def ai_topic_configure(self, project, principal, version, policy):
        policy = json.loads(self._ai_json(validate_topic_policy(policy)))
        with self.transaction(True) as c:
            topic = self._ai_topic_admin(c, project, principal, version)
            if policy['provider'] != topic['policy']['provider']:
                policy['model'], policy['enabled'] = None, False
                self._ai_decision_detach_all(c, topic['project_id'])
            if policy['enabled'] and not any(self._ai_decision_verified(key, policy) for key in
                    self._ai_decision_rows(c, topic['project_id'], policy['provider'])):
                raise Error(409, 'ai_decision_not_ready', 'Verify an exact decision model and key before enabling allowed topics')
            if policy == topic['policy']:
                return self._ai_topic_view(c, topic)
            return self._ai_topic_changed(c, topic | {'policy': policy})

    def _ai_decision_detach_all(self, c, pid):
        c.execute('UPDATE ai_key_library SET version=version+1 WHERE singleton=1')
        c.execute('DELETE FROM ai_decision_keys WHERE project_id=?', (pid,))

    def ai_decision_key_add(self, project, principal, version, label, quota_group, protected_key=None, *, reuse_key_id=None, configuration_version=None, library_version=None):
        with self.transaction(True) as c:
            topic = self._ai_topic_admin(c, project, principal, version)
            self._ai_library_admin(c,principal,library_version)
            pid, provider = topic['project_id'], topic['policy']['provider']
            if configuration_version is not None and self._ai_config(c, pid)['version'] != configuration_version:
                conflict('Provider keys changed. Reload and retry.')
            if reuse_key_id is not None:
                key = c.execute("SELECT k.* FROM ai_generation_keys b JOIN ai_provider_keys k ON k.id=b.key_id WHERE k.id=? AND b.project_id=? AND k.provider=? AND k.deleted_at IS NULL", (reuse_key_id, pid, provider)).fetchone()
                if not key:
                    missing()
                kid = key['id']
                c.execute("UPDATE ai_provider_keys SET purpose='both' WHERE id=?", (kid,))
            else:
                kid = self._ai_new_key(c, {'project_id': None, 'settings': {'provider': provider}}, label, quota_group, protected_key)
                c.execute("UPDATE ai_provider_keys SET purpose='decision' WHERE id=?", (kid,))
            c.execute('INSERT INTO ai_decision_keys(key_id,project_id) VALUES(?,?)', (kid, pid))
            c.execute('UPDATE ai_key_library SET version=version+1 WHERE singleton=1')
            return self._ai_topic_changed(c, topic)

    def ai_decision_key_remove(self, project, principal, version, key_id, key_version):
        with self.transaction(True) as c:
            topic = self._ai_topic_admin(c, project, principal, version)
            key = self._ai_decision_key(c, topic, key_id, key_version)
            c.execute('DELETE FROM ai_decision_keys WHERE project_id=? AND key_id=?', (topic['project_id'], key_id))
            c.execute('UPDATE ai_key_library SET version=version+1 WHERE singleton=1')
            # Never turn the gate off on credential failure/removal.
            return self._ai_topic_changed(c, topic)

    def _ai_decision_key(self, c, topic, key_id, key_version=None):
        key = next((key for key in self._ai_decision_rows(c, topic['project_id'], topic['policy']['provider']) if key['id'] == key_id), None)
        if not key:
            missing()
        if key_version is not None and (type(key_version) is not int or key['version'] != key_version):
            conflict('The decision key changed')
        return key

    def ai_decision_snapshot(self, project, principal, version, key_id, key_version):
        with self.transaction() as c:
            topic = self._ai_topic_admin(c, project, principal, version)
            key = self._ai_decision_key(c, topic, key_id, key_version)
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            return {'topic': topic, 'key': key, 'group': dict(group)}

    def ai_decision_key_secrets(self, project, principal, version):
        with self.transaction() as c:
            topic = self._ai_topic_admin(c, project, principal, version)
            config_version = self._ai_config(c, topic['project_id'])['version']
            keys = [dict(row) for row in c.execute('SELECT id,protected_key FROM ai_provider_keys WHERE provider=? AND deleted_at IS NULL', (topic['policy']['provider'],))]
            return {'configuration_version': config_version, 'provider': topic['policy']['provider'], 'keys': keys}

    def ai_decision_catalogue(self, project, principal, version, key_id, key_version, models, *, health_epoch=None, group_epoch=None):
        if not isinstance(models, (list, tuple)) or len(models) > 1000 or any(not isinstance(m, str) or not 1 <= len(m) <= 256 for m in models):
            raise Error(502, 'ai_decision_invalid', 'Invalid decision model catalogue')
        with self.transaction(True) as c:
            topic = self._ai_topic_admin(c, project, principal, version)
            key=self._ai_decision_key(c, topic, key_id, key_version)
            group=c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?',(key['quota_group_id'],)).fetchone()
            self._ai_key_fences(key,group,key_version,health_epoch,group_epoch)
            c.execute('UPDATE ai_provider_keys SET health_epoch=health_epoch+1 WHERE id=?',(key_id,))
            self._ai_decision_models(c, key_id, models)
            return self._ai_topic_changed(c, topic)

    def _ai_decision_health(self, c, key, group, outcome, ts, retry_after=None, job=None):
        if outcome in ('access_denied', 'model_or_endpoint_unavailable', 'capability_unverified'):
            # A decision model failure must not erase text/tool evidence.
            c.execute("DELETE FROM ai_key_model_evidence WHERE key_id=? AND role='decision' AND model=?", (key['id'], key['decision_model']))
        else:
            self._ai_provider_health(c, key, group, outcome, ts, retry_after, job=job)

    def ai_decision_check_begin(self, project, principal, version, key_id, key_version):
        ts = time.time()
        with self.transaction(True) as c:
            topic = self._ai_topic_admin(c, project, principal, version)
            key = self._ai_decision_key(c, topic, key_id, key_version)
            model = topic['policy']['model']
            if not model or model not in json.loads(key['decision_catalogue']):
                raise Error(409, 'ai_decision_not_ready', 'Refresh and select an exact decision model first')
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            if not key['enabled'] or key['blocked_reason'] or group['cooldown_until'] > ts or self._ai_trial_live(c, group, ts) or self._ai_key_busy(c, key_id, ts):
                raise Error(429, 'ai_decision_wait', 'This provider key or account is not available. Wait and retry.')
            # Recovery is exclusive across all keys in the same account group.
            if self._ai_group_checks(c,group['id'],ts):
                raise Error(429, 'ai_decision_wait', 'An account check is already running')
            day, reservation = datetime.fromtimestamp(ts, timezone.utc).date().isoformat(), 32768
            settings = self._ai_config(c, topic['project_id'])['settings']
            c.execute('INSERT INTO ai_usage(project_id,day) VALUES(?,?) ON CONFLICT DO NOTHING', (topic['project_id'], day))
            usage = c.execute('SELECT * FROM ai_usage WHERE project_id=? AND day=?', (topic['project_id'], day)).fetchone()
            if usage['reserved_requests'] + 1 > settings['daily_request_limit'] or usage['reserved_tokens'] + reservation > settings['daily_token_limit']:
                raise Error(429, 'ai_budget_exhausted', 'The project AI usage allowance is exhausted')
            cid = new_id()
            c.execute('''INSERT INTO ai_decision_checks(id,project_id,policy_version,key_id,key_version,health_epoch,group_epoch,principal,state,lease_until,model,day,reserved_tokens,created_at)
                VALUES(?,?,?,?,?,?,?,?,'running',?,?,?,?,?)''', (cid, topic['project_id'], version, key_id, key_version, key['health_epoch'], group['health_epoch'], self._ai_json(principal), ts+60, model, day, reservation, now()))
            c.execute('UPDATE ai_usage SET reserved_requests=reserved_requests+1,reserved_tokens=reserved_tokens+? WHERE project_id=? AND day=?', (reservation, topic['project_id'], day))
            return {'id': cid, 'topic': topic, 'protected_key': key['protected_key'], 'model': model}

    def ai_decision_check_finish(self, check_id, result=None, *, error=None, outcome='unchanged', retry_after=None, probe=False):
        self._ai_provider_outcome_input(outcome, retry_after)
        with self.transaction(True) as c:
            row = c.execute('SELECT * FROM ai_decision_checks WHERE id=?', (check_id,)).fetchone()
            if not row or row['state'] != 'running' or row['lease_until'] <= time.time():
                conflict('This decision check expired')
            self._ai_admin(c, json.loads(row['principal']))
            topic = self._ai_topic(c, row['project_id'])
            key = self._ai_decision_key(c, topic, row['key_id'], row['key_version'])
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            self._ai_key_fences(key, group, row['key_version'], row['health_epoch'], row['group_epoch'])
            if topic['version'] != row['policy_version'] or topic['policy']['model'] != row['model']:
                conflict('Topic settings changed during the check')
            probability, decision = None, 'unavailable'
            if result is not None:
                from feedback.ai_decision_providers import validate_decision
                validate_decision(result, row['model'])
                probability, decision = result.probability, probability_outcome(result.probability, topic['policy']['preset'])
                usage = result.usage
                if usage.input_tokens is not None and usage.output_tokens is not None:
                    extra = max(0, usage.input_tokens + usage.output_tokens - row['reserved_tokens'])
                    c.execute('UPDATE ai_usage SET reserved_tokens=reserved_tokens+?,reported_input_tokens=reported_input_tokens+?,reported_output_tokens=reported_output_tokens+? WHERE project_id=? AND day=?', (extra, usage.input_tokens, usage.output_tokens, row['project_id'], row['day']))
                if probe:
                    self._ai_model_evidence(c, row['key_id'], 'decision', row['model'], row['key_version'], {}, True)
                if group['recovery_required']:
                    c.execute('UPDATE ai_provider_quota_groups SET health_epoch=health_epoch+1,reset_epoch=health_epoch+1,cooldown_until=0,recovery_required=0,failure_count=0,reason=NULL WHERE id=?', (group['id'],))
            else:
                self._ai_decision_health(c, key, group, outcome, time.time(), retry_after)
            c.execute('UPDATE ai_decision_checks SET state=?,outcome=?,probability=?,error=? WHERE id=?', ('succeeded' if result else 'failed', decision, probability, error, check_id))
            return {'outcome': decision, 'probability': probability, 'model': row['model'], 'preset': topic['policy']['preset'], 'preset_version': POLICY_VERSION,
                    'reply': None if decision == 'allow' else policy_reply(topic['policy'], decision, check_id), 'error': error}

    def _ai_topic_receipt(self, c, job):
        topic = self._ai_topic(c, job['project_id'])
        if job['kind'] == 'sections' or not topic['policy']['enabled']:
            return None
        row = c.execute('SELECT * FROM ai_topic_receipts WHERE job_id=? AND attempt=?', (job['id'], job['attempt_count'])).fetchone()
        if not row or row['lease_token'] != job['lease_token'] or row['policy_version'] != topic['version']:
            conflict('A current topic decision is required')
        if row['key_id']:
            key = self._ai_decision_key(c, topic, row['key_id'], row['key_version'])
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            self._ai_key_fences(key, group, row['key_version'], row['health_epoch'], row['group_epoch'])
            if row['outcome'] != 'unavailable' and not self._ai_decision_verified(key, topic['policy']):
                conflict('The decision key is no longer verified')
        return dict(row)

    def _ai_topic_require_allow(self, c, job):
        receipt = self._ai_topic_receipt(c, job)
        if receipt and receipt['outcome'] != 'allow':
            conflict('The topic policy did not allow this request')

    def ai_topic_require_allow(self, job_id, token):
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, time.time())
            if not job:
                conflict('This answer is no longer authorized')
            self._ai_topic_require_allow(c, job)

    def ai_topic_job_snapshot(self, job_id, token):
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, time.time())
            if not job or job['kind'] not in ('turn', 'preview'):
                conflict('This topic decision is no longer authorized')
            topic = self._ai_topic(c, job['project_id'])
            receipt = c.execute('SELECT * FROM ai_topic_receipts WHERE job_id=? AND attempt=?', (job_id, job['attempt_count'])).fetchone()
            return {'topic': topic, 'receipt': self._ai_topic_receipt(c, job) if receipt and topic['policy']['enabled'] else None}

    def ai_topic_claim(self, job_id, token):
        ts = time.time()
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, ts)
            if not job or job['kind'] not in ('turn', 'preview'):
                conflict('This decision is no longer authorized')
            topic = self._ai_topic(c, job['project_id'])
            if not topic['policy']['enabled']:
                conflict('Allowed topics are disabled')
            bound = c.execute('SELECT * FROM ai_decision_attempts WHERE job_id=? AND attempt=?', (job_id, job['attempt_count'])).fetchone()
            waits = []
            for key in self._ai_decision_rows(c, job['project_id'], topic['policy']['provider']):
                if not self._ai_decision_verified(key, topic['policy']) or bound and key['id'] != bound['key_id']:
                    continue
                group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
                if bound:
                    if bound['lease_token'] != token:
                        conflict()
                    self._ai_key_fences(key, group, bound['key_version'], bound['health_epoch'], bound['group_epoch'])
                owned = (group['trial_job_id'], group['trial_attempt'], group['trial_token']) == (job_id, job['attempt_count'], token)
                check_busy = self._ai_group_checks(c,group['id'],ts)
                busy = self._ai_key_busy(c, key['id'], ts, job_id) or check_busy
                if group['cooldown_until'] > ts or self._ai_trial_live(c, group, ts) and not owned or busy:
                    waits.append(max(group['cooldown_until'], group['trial_until'] or 0, busy or 0, ts+1))
                    continue
                if not bound:
                    c.execute('INSERT INTO ai_decision_attempts(job_id,attempt,lease_token,key_id,key_version,health_epoch,group_epoch) VALUES(?,?,?,?,?,?,?)', (job_id, job['attempt_count'], token, key['id'], key['version'], key['health_epoch'], group['health_epoch']))
                    c.execute('UPDATE ai_provider_keys SET last_selected=(SELECT COALESCE(MAX(last_selected),0)+1 FROM ai_provider_keys) WHERE id=?', (key['id'],))
                if group['recovery_required']:
                    c.execute('UPDATE ai_provider_quota_groups SET trial_job_id=?,trial_attempt=?,trial_token=?,trial_until=? WHERE id=?', (job_id, job['attempt_count'], token, job['lease_until'], group['id']))
                return {'available': True, 'protected_key': key['protected_key'], 'provider': topic['policy']['provider'], 'model': topic['policy']['model']}
            return {'available': False, 'retry_at': min(waits) if waits else None}

    def ai_topic_before_dispatch(self, job_id, token, tokens):
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, time.time())
            if not job:
                return False
            index = self._ai_config(c, job['project_id'])['settings']['max_tool_rounds'] + 1
            bound = c.execute('SELECT * FROM ai_decision_attempts WHERE job_id=? AND attempt=?', (job_id, job['attempt_count'])).fetchone()
            reservation = c.execute('SELECT * FROM ai_usage_reservations WHERE job_id=? AND attempt=? AND call_index=?', (job_id, job['attempt_count'], index)).fetchone()
            if not bound or bound['lease_token'] != token or not reservation or reservation['dispatched'] or reservation['tokens'] != tokens:
                return False
            topic = self._ai_topic(c, job['project_id'])
            key = self._ai_decision_key(c, topic, bound['key_id'], bound['key_version'])
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            self._ai_key_fences(key, group, bound['key_version'], bound['health_epoch'], bound['group_epoch'])
            if not topic['policy']['enabled'] or not self._ai_decision_verified(key, topic['policy']) or group['cooldown_until'] > time.time():
                return False
            c.execute('''UPDATE ai_usage_reservations SET dispatched=1,provider_key_id=?,provider_key_version=?,provider_health_epoch=?,provider_group_id=?,provider_group_epoch=?
                WHERE job_id=? AND attempt=? AND call_index=?''', (key['id'], key['version'], key['health_epoch'], group['id'], group['health_epoch'], job_id, job['attempt_count'], index))
            return True

    def ai_topic_record(self, job_id, token, context_hash, *, result=None, local_outcome=None, error=None, outcome='unchanged', retry_after=None):
        self._ai_provider_outcome_input(outcome, retry_after)
        if not isinstance(context_hash, str) or len(context_hash) != 64 or local_outcome not in (None, 'greeting', 'clarify', 'unavailable') or error not in (None, 'unavailable', 'invalid_response', 'rate_limited', 'timeout'):
            raise Error(400, 'invalid_input', 'Invalid decision receipt')
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, time.time())
            if not job or job['kind'] not in ('turn', 'preview'):
                conflict('This topic decision is no longer authorized')
            topic = self._ai_topic(c, job['project_id'])
            if not topic['policy']['enabled']:
                conflict('Allowed topics are disabled')
            existing = c.execute('SELECT * FROM ai_topic_receipts WHERE job_id=? AND attempt=?', (job_id, job['attempt_count'])).fetchone()
            if existing:
                existing = self._ai_topic_receipt(c, job)
                if existing['context_hash'] != context_hash:
                    conflict('Decision context changed')
                return existing
            key, group = None, None
            probability, decision = None, local_outcome or 'unavailable'
            if result is not None or local_outcome is None:
                bound = c.execute('SELECT * FROM ai_decision_attempts WHERE job_id=? AND attempt=?', (job_id, job['attempt_count'])).fetchone()
                if not bound or bound['lease_token'] != token:
                    conflict('No decision key is pinned')
                key = self._ai_decision_key(c, topic, bound['key_id'], bound['key_version'])
                group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
                self._ai_key_fences(key, group, bound['key_version'], bound['health_epoch'], bound['group_epoch'])
                index = self._ai_config(c, job['project_id'])['settings']['max_tool_rounds'] + 1
                dispatched = c.execute('SELECT dispatched FROM ai_usage_reservations WHERE job_id=? AND attempt=? AND call_index=?', (job_id, job['attempt_count'], index)).fetchone()
                if not dispatched or not dispatched['dispatched']:
                    conflict('No decision dispatch is reserved')
                if result is not None:
                    from feedback.ai_decision_providers import validate_decision
                    validate_decision(result, topic['policy']['model'])
                    probability, decision = result.probability, probability_outcome(result.probability, topic['policy']['preset'])
                    if result.usage.input_tokens is not None and result.usage.output_tokens is not None:
                        self._ai_report_usage(c, job, index, result.usage.input_tokens, result.usage.output_tokens)
                    self._ai_decision_health(c, key, group, 'success', time.time(), job=job)
                else:
                    self._ai_decision_health(c, key, group, outcome, time.time(), retry_after, job=job)
                # Final key/account epochs include our own fenced recovery.
                key = self._ai_decision_key(c, topic, key['id'])
                group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            body = None if decision == 'allow' else policy_reply(topic['policy'], decision, job_id)
            c.execute('''INSERT INTO ai_topic_receipts(job_id,attempt,lease_token,policy_version,preset_version,context_hash,trigger_sequence,outcome,probability,model,key_id,key_version,health_epoch,group_epoch,body,error,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (job_id, job['attempt_count'], token, topic['version'], POLICY_VERSION, context_hash, json.loads(job['authority']).get('trigger_sequence'), decision, probability, topic['policy']['model'], key['id'] if key else None, key['version'] if key else None, key['health_epoch'] if key else None, group['health_epoch'] if group else None, body, error, now()))
            return dict(c.execute('SELECT * FROM ai_topic_receipts WHERE job_id=? AND attempt=?', (job_id, job['attempt_count'])).fetchone())

    def _ai_embedding_profile(self, c):
        row = c.execute('SELECT mode,profile FROM ai_embedding_settings WHERE singleton=1').fetchone()
        return json.loads(row['profile']) if row['mode'] == 'external' else EMBEDDING_PROFILE

    def ai_embedding_settings(self, principal=None, *, internal=False):
        with self.transaction() as c:
            if not internal:
                self._ai_admin(c, principal)
            row = c.execute('SELECT * FROM ai_embedding_settings WHERE singleton=1').fetchone()
            result = {k: row[k] for k in ('version', 'mode', 'updated_at')}
            result['config'] = json.loads(row['config']) if row['config'] else None
            result['profile'] = self._ai_embedding_profile(c)
            result['has_key'] = bool(row['protected_key'])
            if internal:
                result['protected_key'] = row['protected_key']
            return result

    def _ai_embedding_reindex(self, c, principal):
        queued, remaining = 0, 0
        profile = self._ai_json(self._ai_embedding_profile(c))
        for source in c.execute("""SELECT s.* FROM ai_sources s JOIN projects p ON p.id=s.project_id
                LEFT JOIN ai_revisions r ON r.id=s.active_revision_id
                WHERE s.active=1 AND s.approved=1 AND s.deleted_at IS NULL AND p.archived_at IS NULL
                AND s.latest_document_id IS NOT NULL AND (r.id IS NULL OR r.profile!=? OR r.document_id!=s.latest_document_id)
                ORDER BY s.project_id,s.id""", (profile,)).fetchall():
            if c.execute("SELECT 1 FROM ai_jobs WHERE project_id=? AND resource_id=? AND kind='ingest' AND state IN ('queued','running')", (source['project_id'], source['id'])).fetchone():
                continue
            try:
                self._ai_document_job(c, source['project_id'], principal, source['id'], source['latest_document_id'], 'embedding:' + new_id())
                queued += 1
            except Error as error:
                if error.code != 'ai_busy':
                    raise
                remaining += 1
        return {'queued': queued, 'remaining': remaining}

    def ai_embedding_configure(self, principal, version, mode, config, profile, protected_key):
        if mode not in ('local', 'external'):
            raise Error(400, 'invalid_input', 'Unknown embedding mode')
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            row = c.execute('SELECT * FROM ai_embedding_settings WHERE singleton=1').fetchone()
            if row['version'] != version:
                conflict('Embedding settings changed; reload before saving')
            c.execute('UPDATE ai_embedding_settings SET version=version+1,mode=?,config=?,profile=?,protected_key=?,updated_at=? WHERE singleton=1',
                (mode, self._ai_json(config) if config else None, self._ai_json(profile) if profile else None, protected_key, now()))
            # A provider/model change fences all queued and running work before
            # another model can write/query this installation's knowledge.
            c.execute("UPDATE ai_jobs SET state='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? WHERE state IN ('queued','running')", (now(),))
            c.execute("UPDATE ai_revisions SET state='discarded' WHERE state='staging'")
            for project in c.execute('SELECT id FROM projects').fetchall():
                cfg = self._ai_config(c, project[0])
                if cfg['settings']['enabled']:
                    cfg['settings']['enabled'] = False
                    c.execute('UPDATE ai_settings SET settings=?,version=version+1,updated_at=? WHERE project_id=?', (self._ai_json(cfg['settings'], 131072), now(), project[0]))
                self._ai_knowledge_changed(c, project[0])
            return self._ai_embedding_reindex(c, principal)

    def ai_embedding_reindex(self, principal, version):
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            if c.execute('SELECT version FROM ai_embedding_settings WHERE singleton=1').fetchone()[0] != version:
                conflict()
            return self._ai_embedding_reindex(c, principal)

    def ai_runtime(self, principal):
        with self.transaction() as c:
            self._ai_admin(c, principal)
            return self._ai_runtime_view(c.execute('SELECT * FROM ai_runtime WHERE singleton=1').fetchone())

    def ai_runtime_begin(self, principal, version, action, protected_token=None):
        if action not in ('download', 'remove') or (protected_token is not None and (not isinstance(protected_token, str) or not 1 <= len(protected_token) <= 16384)):
            raise Error(400, 'invalid_input', 'Invalid runtime operation')
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            row = c.execute('SELECT * FROM ai_runtime WHERE singleton=1').fetchone()
            if row['version'] != version or row['operation_id']:
                conflict()
            if action == 'remove':
                # Removal invalidates the cache before touching files. A failed
                # remove/download must never resurrect readiness from this record.
                c.execute('UPDATE ai_runtime SET active_manifest=NULL,active_cache_id=NULL WHERE singleton=1')
                for cfg in c.execute('SELECT project_id FROM ai_settings').fetchall():
                    current = self._ai_config(c, cfg[0])
                    if current['settings']['enabled']:
                        current['settings']['enabled'] = False
                        c.execute('UPDATE ai_settings SET settings=?,version=version+1,updated_at=? WHERE project_id=?', (self._ai_json(current['settings'], 131072), now(), cfg[0]))
                    self._ai_cancel_generation(c, cfg[0])
            authority = {k: principal[k] for k in ('kind', 'user_id', 'session_id')}
            c.execute('''UPDATE ai_runtime SET version=version+1,state=?,operation_id=?,action=?,protected_token=?,authority=?,
                progress_bytes=0,total_bytes=0,lease_token=NULL,lease_until=NULL,attempt_count=0,error=NULL,updated_at=? WHERE singleton=1''',
                ('downloading' if action == 'download' else 'removing', new_id(), action, protected_token if action == 'download' else None, self._ai_json(authority), now()))
            return self._ai_runtime_view(c.execute('SELECT * FROM ai_runtime WHERE singleton=1').fetchone())

    def _ai_runtime_authorized(self, c, row):
        try:
            self._ai_admin(c, json.loads(row['authority'] or '{}'))
            return True
        except Error:
            return False

    @staticmethod
    def _ai_runtime_clear(c, row, error=None):
        c.execute('''UPDATE ai_runtime SET version=version+1,state=?,operation_id=NULL,action=NULL,protected_token=NULL,authority=NULL,
            lease_token=NULL,lease_until=NULL,error=?,updated_at=? WHERE singleton=1''',
            ('failed' if error and row['action'] == 'remove' else ('ready' if row['active_manifest'] else ('failed' if error else 'not_downloaded')), error, now()))

    def ai_runtime_claim(self, current_time, lease_seconds=60):
        self._ai_lease_input(current_time, lease_seconds)
        with self.transaction(True) as c:
            row = c.execute('SELECT * FROM ai_runtime WHERE singleton=1').fetchone()
            if not row['operation_id'] or (row['lease_until'] is not None and row['lease_until'] > current_time):
                return None
            if not self._ai_runtime_authorized(c, row) or row['attempt_count'] >= 3:
                self._ai_runtime_clear(c, row, 'cancelled')
                return None
            token = new_id()
            c.execute('UPDATE ai_runtime SET lease_token=?,lease_until=?,attempt_count=attempt_count+1,updated_at=? WHERE singleton=1', (token, current_time + lease_seconds, now()))
            result = dict(c.execute('SELECT * FROM ai_runtime WHERE singleton=1').fetchone())
            result['active_manifest'] = json.loads(result['active_manifest']) if result['active_manifest'] else None
            # Returned only to the worker, which decrypts the token outside the transaction.
            return result

    def _ai_runtime_lease(self, c, operation_id, token, current_time):
        self._ai_lease_input(current_time, 1)
        row = c.execute('SELECT * FROM ai_runtime WHERE singleton=1').fetchone()
        if row['operation_id'] != operation_id or row['lease_token'] != token or row['lease_until'] is None or row['lease_until'] <= current_time:
            return None
        if not self._ai_runtime_authorized(c, row):
            self._ai_runtime_clear(c, row, 'cancelled')
            return None
        return row

    def ai_runtime_advance(self, operation_id, token, progress_bytes, total_bytes, *, verifying=False, current_time=None, lease_seconds=60):
        current_time = time.time() if current_time is None else current_time
        self._ai_lease_input(current_time, lease_seconds)
        if type(progress_bytes) is not int or type(total_bytes) is not int or not 0 <= progress_bytes <= total_bytes <= 4 * 1024**3:
            raise Error(400, 'invalid_input', 'Invalid download progress')
        with self.transaction(True) as c:
            row = self._ai_runtime_lease(c, operation_id, token, current_time)
            if not row:
                return False
            if progress_bytes < row['progress_bytes']:
                conflict()
            c.execute('UPDATE ai_runtime SET progress_bytes=?,total_bytes=?,state=?,lease_until=?,updated_at=? WHERE singleton=1',
                (progress_bytes, total_bytes, 'verifying' if verifying and row['action'] == 'download' else row['state'], current_time + lease_seconds, now()))
            return True

    def ai_runtime_finish(self, operation_id, token, *, manifest=None, cache_id=None, error=None, current_time=None):
        current_time = time.time() if current_time is None else current_time
        if error not in (None, 'download_failed', 'verification_failed', 'cancelled', 'unavailable', 'model_access_required'):
            raise Error(400, 'invalid_input', 'Invalid runtime error')
        if manifest is not None and manifest != EMBEDDING_PROFILE:
            raise Error(400, 'invalid_input', 'Unsupported embedding profile')
        if cache_id is not None:
            from uuid import UUID
            try:
                if str(UUID(cache_id)) != cache_id:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise Error(400, 'invalid_input', 'Invalid model cache ID') from None
        with self.transaction(True) as c:
            row = self._ai_runtime_lease(c, operation_id, token, current_time)
            if not row:
                return False
            if error is None:
                if row['action'] == 'download' and manifest is None:
                    raise Error(400, 'invalid_input', 'Verified model manifest required')
                c.execute('UPDATE ai_runtime SET active_manifest=?,active_cache_id=? WHERE singleton=1', (self._ai_json(manifest) if row['action'] == 'download' else None, (cache_id or operation_id) if row['action'] == 'download' else None))
                row = c.execute('SELECT * FROM ai_runtime WHERE singleton=1').fetchone()
            self._ai_runtime_clear(c, row, error)
            return True

    def ai_runtime_cancel(self, principal, version):
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            row = c.execute('SELECT * FROM ai_runtime WHERE singleton=1').fetchone()
            if row['version'] != version:
                conflict()
            if row['action'] == 'remove':
                conflict('Model removal cannot be cancelled once started')
            if row['operation_id']:
                self._ai_runtime_clear(c, row)
            return self._ai_runtime_view(c.execute('SELECT * FROM ai_runtime WHERE singleton=1').fetchone())

    def ai_runtime_local(self):
        """Internal cache reconciliation metadata. No credentials or authority."""
        with self.transaction() as c:
            row = c.execute('SELECT * FROM ai_runtime WHERE singleton=1').fetchone()
            return {k: self._ai_runtime_view(row)[k] for k in ('version', 'state', 'active_manifest', 'active_cache_id', 'operation_id', 'action')}

    def ai_runtime_abandon_download(self, expected_version):
        """Cancel an unresumable startup download intent, preserving verified cache metadata."""
        with self.transaction(True) as c:
            row = c.execute('SELECT * FROM ai_runtime WHERE singleton=1').fetchone()
            if row['version'] != expected_version or row['action'] != 'download' or not row['operation_id']:
                return False
            self._ai_runtime_clear(c, row)
            return True

    def ai_runtime_unavailable(self, expected_version, *, cancel_operation=False):
        """Fence unavailable local cache metadata; never download or recreate it."""
        with self.transaction(True) as c:
            row = c.execute('SELECT * FROM ai_runtime WHERE singleton=1').fetchone()
            if row['version'] != expected_version or (row['operation_id'] and not cancel_operation):
                return False
            if row['state'] == 'not_downloaded' and row['active_manifest'] is None and row['active_cache_id'] is None:
                return True
            c.execute("UPDATE ai_runtime SET state='not_downloaded',active_manifest=NULL,active_cache_id=NULL,operation_id=NULL,action=NULL,protected_token=NULL,authority=NULL,lease_token=NULL,lease_until=NULL,version=version+1,error='unavailable',updated_at=? WHERE singleton=1", (now(),))
            for cfg in c.execute('SELECT project_id FROM ai_settings').fetchall():
                current = self._ai_config(c, cfg[0])
                if current['settings']['enabled']:
                    current['settings']['enabled'] = False
                    c.execute('UPDATE ai_settings SET settings=?,version=version+1,updated_at=? WHERE project_id=?', (self._ai_json(current['settings'], 131072), now(), cfg[0]))
                self._ai_cancel_generation(c, cfg[0])
            return True

    @staticmethod
    def _ai_knowledge_version(c, project_id):
        row = c.execute('SELECT version FROM ai_knowledge_state WHERE project_id=?', (project_id,)).fetchone()
        return row[0] if row else 0

    def _ai_knowledge_changed(self, c, project_id):
        c.execute('INSERT INTO ai_knowledge_state(project_id,version) VALUES(?,1) ON CONFLICT(project_id) DO UPDATE SET version=ai_knowledge_state.version+1', (project_id,))
        self._ai_cancel_generation(c, project_id)

    @staticmethod
    def _ai_source(c, project_id, source_id):
        row = c.execute('SELECT * FROM ai_sources WHERE id=? AND project_id=?', (source_id, project_id)).fetchone()
        if not row:
            missing()
        return row

    def _ai_check_corpus(self, c, project_id, source_id, replacement_count=None):
        count = c.execute("SELECT count(*) FROM ai_chunks k JOIN ai_revisions r ON r.id=k.revision_id JOIN ai_sources s ON s.active_revision_id=r.id WHERE s.project_id=? AND s.id!=? AND s.active=1 AND s.deleted_at IS NULL AND r.profile=?", (project_id, source_id, self._ai_json(self._ai_embedding_profile(c)))).fetchone()[0]
        if replacement_count is None:
            replacement_count = c.execute("SELECT count(*) FROM ai_chunks k JOIN ai_revisions r ON r.id=k.revision_id JOIN ai_sources s ON s.active_revision_id=r.id WHERE s.id=? AND r.profile=?", (source_id, self._ai_json(self._ai_embedding_profile(c)))).fetchone()[0]
        if count + replacement_count > 10000:
            raise Error(409, 'knowledge_limit', 'This project has reached its 10,000 active-passage limit')

    def ai_sources(self, project, principal, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise Error(400, 'invalid_input', 'Invalid source page')
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            return [dict(x) for x in c.execute('SELECT * FROM ai_sources WHERE project_id=? ORDER BY created_at,id LIMIT ? OFFSET ?', (pid, limit, offset))]

    def ai_source_create(self, project, principal, kind, name, location=None):
        if kind not in ('markdown', 'website') or not isinstance(name, str) or not 1 <= len(name) <= 300 or (location is not None and (not isinstance(location, str) or len(location) > 2048)):
            raise Error(400, 'invalid_input', 'Invalid knowledge source')
        self._ai_json([name, location], 12000)
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid, sid, ts = self._ai_project(c, project), new_id(), now()
            if c.execute('SELECT count(*) FROM ai_sources WHERE project_id=? AND deleted_at IS NULL', (pid,)).fetchone()[0] >= 100:
                raise Error(409, 'source_limit', 'Project knowledge source limit reached')
            c.execute('INSERT INTO ai_sources(id,project_id,kind,name,location,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (sid, pid, kind, name, location, ts, ts))
            return dict(self._ai_source(c, pid, sid))

    def ai_source_update(self, project, principal, source_id, version, *, active, approved, delete=False):
        if any(type(x) is not bool for x in (active, approved, delete)):
            raise Error(400, 'invalid_input', 'Explicit source flags required')
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            row = self._ai_source(c, pid, source_id)
            if row['version'] != version or row['deleted_at']:
                conflict()
            if bool(row['active']) == active and bool(row['approved']) == approved and not delete:
                return dict(row)
            if active and not row['active'] and not delete:
                self._ai_check_corpus(c, pid, source_id)
            c.execute('UPDATE ai_sources SET active=?,approved=?,deleted_at=?,version=version+1,updated_at=? WHERE id=?', (False if delete else active, approved, now() if delete else None, now(), source_id))
            c.execute("UPDATE ai_jobs SET state='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? WHERE project_id=? AND resource_id=? AND state IN ('queued','running')", (now(), pid, source_id))
            c.execute("UPDATE ai_revisions SET state='discarded' WHERE source_id=? AND state='staging'", (source_id,))
            self._ai_knowledge_changed(c, pid)
            return dict(self._ai_source(c, pid, source_id))

    def ai_revision_stage(self, project, principal, source_id, version, title, content, expected_chunks, profile=None, *, job_id=None, lease_token=None):
        """Stage a synchronous admin import, or bind a worker revision to its job.

        Background workers MUST supply job_id and lease_token and use the source
        version from the claimed job authority. Unbound calls are synchronous only.
        """
        if not isinstance(title, str) or len(title) > 2000 or not isinstance(content, str) or not 1 <= len(content) <= 1048576 or type(expected_chunks) is not int or not 1 <= expected_chunks <= 10000:
            raise Error(400, 'invalid_input', 'Invalid knowledge revision')
        # Also reject invalid Unicode before storing authoritative source text.
        self._ai_json([title, content], 5 * 1048576)
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            current_profile = self._ai_embedding_profile(c)
            profile = current_profile if profile is None else profile
            if profile != current_profile:
                conflict('Embedding model changed')
            pid = self._ai_project(c, project)
            source = self._ai_source(c, pid, source_id)
            if source['version'] != version or not source['active'] or source['deleted_at']:
                conflict()
            document_id = None
            if job_id is not None:
                job = self._ai_revision_worker(c, pid, source_id, job_id, lease_token)
                document_id = json.loads(job['payload']).get('document_id')
                if document_id:
                    document = c.execute('SELECT * FROM ai_documents WHERE id=? AND project_id=? AND source_id=?', (document_id, pid, source_id)).fetchone()
                    if not document or document['content'] != content or document['title'] != title:
                        conflict('The index must preserve its original document provenance')
            c.execute("UPDATE ai_revisions SET state='discarded' WHERE source_id=? AND state='staging'", (source_id,))
            rid = new_id()
            c.execute("INSERT INTO ai_revisions(id,source_id,project_id,source_version,state,title,content,content_hash,profile,expected_chunks,job_id,document_id,created_at) VALUES(?,?,?,?,'staging',?,?,?,?,?,?,?,?)",
                (rid, source_id, pid, version, title, content, hashlib.sha256(content.encode()).hexdigest(), self._ai_json(profile), expected_chunks, job_id, document_id, now()))
            return {'id': rid, 'source_id': source_id, 'project_id': pid, 'source_version': version, 'state': 'staging', 'expected_chunks': expected_chunks}

    def _ai_revision_worker(self, c, project_id, source_id, job_id, token):
        job = self._ai_job_lease(c, job_id, token, time.time())
        if not job or job['project_id'] != project_id or job['resource_id'] != source_id or job['kind'] not in ('ingest', 'crawl'):
            conflict('The knowledge worker no longer owns this operation')
        return job

    def _ai_staging(self, c, project_id, revision_id, lease_token=None):
        row = c.execute('SELECT r.*,s.version current_version,s.active,s.deleted_at FROM ai_revisions r JOIN ai_sources s ON s.id=r.source_id AND s.project_id=r.project_id WHERE r.id=? AND r.project_id=?', (revision_id, project_id)).fetchone()
        if not row:
            missing()
        if row['profile'] != self._ai_json(self._ai_embedding_profile(c)) or row['state'] != 'staging' or not row['active'] or row['deleted_at'] or row['source_version'] != row['current_version']:
            conflict()
        if row['job_id']:
            self._ai_revision_worker(c, project_id, row['source_id'], row['job_id'], lease_token)
        return row

    def ai_revision_chunks(self, project, principal, revision_id, chunks, *, lease_token=None):
        if not isinstance(chunks, list) or not 1 <= len(chunks) <= 100:
            raise Error(400, 'invalid_input', 'Supply 1 to 100 chunks')
        prepared = []
        for chunk in chunks:
            if not isinstance(chunk, dict) or set(chunk) != {'ordinal', 'title', 'content', 'location', 'vector'} or type(chunk['ordinal']) is not int or chunk['ordinal'] < 0 or not isinstance(chunk['title'], str) or len(chunk['title']) > 2000 or not isinstance(chunk['content'], str) or not 1 <= len(chunk['content']) <= 20000 or (chunk['location'] is not None and (not isinstance(chunk['location'], str) or len(chunk['location']) > 2048)):
                raise Error(400, 'invalid_input', 'Invalid knowledge chunk')
            vector = chunk['vector']
            try:
                valid_vector = isinstance(vector, (list, tuple)) and 1 <= len(vector) <= 4096 and all(type(x) in (int, float) and math.isfinite(x) for x in vector) and abs(sum(x*x for x in vector) - 1) <= .02
            except (OverflowError, ValueError):
                valid_vector = False
            if not valid_vector:
                raise Error(400, 'invalid_input', 'Supply a finite normalized embedding vector')
            self._ai_json([chunk['title'], chunk['content'], chunk['location']], 100000)
            prepared.append((chunk['ordinal'], chunk['title'], chunk['content'], chunk['location'], struct.pack('<' + str(len(vector)) + 'f', *vector)))
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            revision = self._ai_staging(c, pid, revision_id, lease_token)
            document = c.execute('SELECT * FROM ai_documents WHERE id=?', (revision['document_id'],)).fetchone() if revision['document_id'] else None
            for ordinal, title, content, location, vector in prepared:
                if len(vector) != json.loads(revision['profile'])['dimensions'] * 4:
                    raise Error(400, 'invalid_input', 'Embedding dimensions do not match this index')
                if ordinal >= revision['expected_chunks']:
                    raise Error(400, 'invalid_input', 'Chunk ordinal exceeds the revision size')
                page_id = None
                if document:
                    if document['crawl_run_id']:
                        page = c.execute("SELECT p.* FROM ai_crawl_pages p JOIN ai_crawl_runs r ON r.id=p.run_id JOIN ai_jobs j ON j.id=r.job_id WHERE p.run_id=? AND p.attempt=j.attempt_count AND p.status='fetched' AND p.url=?", (document['crawl_run_id'], location)).fetchone()
                        if not page or content not in page['content']:
                            conflict('The chunk does not match an original crawled page')
                        page_id = page['id']
                    elif location is not None or content not in document['content']:
                        conflict('The chunk does not match its original Markdown document')
                old = c.execute('SELECT ordinal,title,content,location,vector FROM ai_chunks WHERE revision_id=? AND ordinal=?', (revision_id, ordinal)).fetchone()
                if old:
                    if tuple(old) != (ordinal, title, content, location, vector):
                        conflict('A chunk with different content already exists')
                    continue
                c.execute('INSERT INTO ai_chunks(id,revision_id,project_id,ordinal,title,content,location,vector,page_id) VALUES(?,?,?,?,?,?,?,?,?)', (new_id(), revision_id, pid, ordinal, title, content, location, vector, page_id))
            return c.execute('SELECT count(*) FROM ai_chunks WHERE revision_id=?', (revision_id,)).fetchone()[0]

    def ai_revision_publish(self, project, principal, revision_id, *, lease_token=None):
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            revision = self._ai_staging(c, pid, revision_id, lease_token)
            if c.execute('SELECT count(*) FROM ai_chunks WHERE revision_id=?', (revision_id,)).fetchone()[0] != revision['expected_chunks']:
                conflict('The knowledge revision is incomplete')
            self._ai_check_corpus(c, pid, revision['source_id'], revision['expected_chunks'])
            c.execute("UPDATE ai_revisions SET state='retired' WHERE source_id=? AND state='active'", (revision['source_id'],))
            c.execute("UPDATE ai_revisions SET state='active' WHERE id=?", (revision_id,))
            c.execute('UPDATE ai_sources SET active_revision_id=?,version=version+1,updated_at=? WHERE id=?', (revision_id, now(), revision['source_id']))
            self._ai_knowledge_changed(c, pid)
            if revision['job_id']:
                c.execute("UPDATE ai_jobs SET state='succeeded',lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?", (now(), revision['job_id']))
                c.execute('UPDATE ai_sources SET pending_job_id=NULL WHERE id=? AND pending_job_id=?', (revision['source_id'], revision['job_id']))
            return dict(self._ai_source(c, pid, revision['source_id']))

    def ai_knowledge_chunks(self, project, principal, query=None, limit=20):
        """Admin-only bounded original passages; vector ranking is added by AI08."""
        if type(limit) is not int or not 1 <= limit <= 100 or (query is not None and (not isinstance(query, str) or len(query) > 1000)):
            raise Error(400, 'invalid_input', 'Invalid knowledge query')
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            terms = None
            if query is not None:
                import re
                terms = re.findall(r'\w+', query, re.UNICODE)[:32]
                if not terms:
                    return []
            return [dict(row) for row in self._ai_knowledge_chunk_rows(c, pid, terms, limit)]

    @staticmethod
    def _ai_knowledge_chunks_sql():
        return """SELECT k.id,k.revision_id,r.source_id,k.ordinal,k.title,k.content,k.location
            FROM ai_chunks k JOIN ai_revisions r ON r.id=k.revision_id AND r.project_id=k.project_id
            JOIN ai_sources s ON s.id=r.source_id AND s.project_id=r.project_id AND s.active_revision_id=r.id
            WHERE k.project_id=? AND r.profile=? AND r.state='active' AND s.active=1 AND s.approved=1 AND s.deleted_at IS NULL"""

    @staticmethod
    def _ai_lease_input(current_time, lease_seconds):
        if type(current_time) not in (int, float) or not math.isfinite(current_time) or type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise Error(400, 'invalid_input', 'Invalid worker lease')

    @staticmethod
    def _ai_job_view(row):
        result = dict(row)
        result['authority'] = json.loads(result['authority'])
        result['payload'] = json.loads(result['payload'])
        return result

    def _ai_enqueue(self, c, project_id, kind, resource_id, dedupe_key, config_version, authority, payload=None):
        """Private transactional seam; chat message enqueue is added in AI10."""
        if kind not in AI_JOB_KINDS or not isinstance(resource_id, str) or not 1 <= len(resource_id) <= 100 or not isinstance(dedupe_key, str) or not 1 <= len(dedupe_key) <= 200 or type(config_version) is not int or config_version < 0:
            raise Error(400, 'invalid_input', 'Invalid AI job')
        if payload is not None and not isinstance(payload, dict):
            raise Error(400, 'invalid_input', 'AI job payload must be an object')
        payload_json = self._ai_json({} if payload is None else payload, 32768)
        authority_json = self._ai_json(authority, 8192)
        old = c.execute('SELECT * FROM ai_jobs WHERE project_id=? AND kind=? AND dedupe_key=?', (project_id, kind, dedupe_key)).fetchone()
        if old:
            if (old['resource_id'], old['config_version'], old['authority'], old['payload']) != (resource_id, config_version, authority_json, payload_json):
                conflict('This job key already describes different work')
            return self._ai_job_view(old)
        if c.execute("SELECT count(*) FROM ai_jobs WHERE project_id=? AND state IN ('queued','running')", (project_id,)).fetchone()[0] >= 50 or c.execute("SELECT count(*) FROM ai_jobs WHERE state IN ('queued','running')").fetchone()[0] >= 1000:
            raise Error(429, 'ai_busy', 'The AI work queue is full; try again later')
        jid, ts = new_id(), now()
        c.execute('''INSERT INTO ai_jobs(id,project_id,kind,resource_id,dedupe_key,config_version,authority,payload,next_attempt,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (jid, project_id, kind, resource_id, dedupe_key, config_version, authority_json, payload_json, time.time(), ts, ts))
        return self._ai_job_view(c.execute('SELECT * FROM ai_jobs WHERE id=?', (jid,)).fetchone())

    def ai_enqueue(self, project, principal, kind, resource_id, dedupe_key, config_version, payload=None):
        if kind not in ('ingest', 'crawl', 'sections', 'preview'):
            raise Error(400, 'invalid_input', 'Unsupported administrator job')
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            existing = c.execute('SELECT * FROM ai_jobs WHERE project_id=? AND kind=? AND dedupe_key=?', (pid, kind, dedupe_key)).fetchone()
            if existing:
                if (existing['resource_id'], existing['config_version'], existing['payload']) != (resource_id, config_version, self._ai_json({} if payload is None else payload, 32768)):
                    conflict('This job key already describes different work')
                # A receipt retry never revives cancelled work or changes source intent.
                return self._ai_job_view(existing)
            config = self._ai_config(c, pid)
            authority = {'principal': {k: principal[k] for k in ('kind', 'user_id', 'session_id')}}
            if kind in ('ingest', 'crawl'):
                if config_version != 0:
                    conflict('Ingestion is independent of generation configuration')
            else:
                if config['version'] != config_version:
                    conflict('AI settings changed; reload before generating content')
                self._ai_require_ready(c, config)
            if kind != 'preview':
                source = self._ai_source(c, pid, resource_id)
                if not source['active'] or source['deleted_at']:
                    conflict()
                authority['source_version'] = source['version']
                if kind in ('ingest', 'crawl'):
                    # New import intent supersedes older work, while its last
                    # complete approved revision remains available to customers.
                    c.execute('UPDATE ai_sources SET version=version+1,updated_at=? WHERE id=?', (now(), resource_id))
                    authority['source_version'] += 1
                    c.execute("UPDATE ai_jobs SET state='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? WHERE project_id=? AND resource_id=? AND kind IN ('ingest','crawl','sections') AND state IN ('queued','running')", (now(), pid, resource_id))
                    c.execute("UPDATE ai_revisions SET state='discarded' WHERE source_id=? AND state='staging'", (resource_id,))
            elif resource_id != pid:
                missing()
            if kind in ('preview', 'sections'):
                authority['knowledge_version'] = self._ai_knowledge_version(c, pid)
            return self._ai_enqueue(c, pid, kind, resource_id, dedupe_key, config_version, authority, payload)

    def _ai_job_authorized(self, c, row):
        authority = json.loads(row['authority'])
        config = self._ai_config(c, row['project_id'])
        try:
            self._active_project(c, row['project_id'])
            if row['kind'] == 'turn':
                if authority.get('mcp_version', 0) != self._mcp_project(c,row['project_id'])['version']:
                    return False
                cv = self.chat_authorize(c, authority['conversation_id'], authority['principal'])
                if cv['project_id'] != row['project_id'] or cv['customer_id'] != authority['customer_id'] or cv['state'] != 'open' or cv['support_mode'] != 'ai' or cv['mode_version'] != authority['mode_version'] or cv['last_sequence'] != authority['trigger_sequence']:
                    return False
                if not self._ai_ready(c, config, require_enabled=True):
                    return False
            else:
                self._ai_admin(c, authority['principal'])
            if row['kind'] in ('turn', 'preview', 'sections'):
                if config['version'] != row['config_version'] or not self._ai_readiness(c, config, authoring=row['kind'] != 'turn')['ready'] or authority['knowledge_version'] != self._ai_knowledge_version(c, row['project_id']):
                    return False
            if row['kind'] in ('ingest', 'crawl', 'sections'):
                source = self._ai_source(c, row['project_id'], row['resource_id'])
                if not source['active'] or source['deleted_at'] or source['version'] != authority['source_version']:
                    return False
                if row['kind'] == 'ingest' and not source['approved'] and c.execute('SELECT mode FROM ai_embedding_settings WHERE singleton=1').fetchone()[0] == 'external':
                    return False
                document_id = json.loads(row['payload']).get('document_id')
                if document_id and (source['latest_document_id'] != document_id or not c.execute('SELECT 1 FROM ai_documents WHERE id=? AND project_id=? AND source_id=?', (document_id, row['project_id'], row['resource_id'])).fetchone()):
                    return False
            return True
        except (Error, KeyError, TypeError, AttributeError):
            return False

    @staticmethod
    def _ai_cancel_job(c, job_id):
        c.execute("UPDATE ai_jobs SET state='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=? AND state IN ('queued','running')", (now(), job_id))

    def ai_claim_job(self, current_time, lease_seconds=60, *, kinds=None):
        self._ai_lease_input(current_time, lease_seconds)
        kinds = tuple(AI_JOB_KINDS) if kinds is None else kinds
        if not isinstance(kinds, (tuple, list)) or not kinds or any(kind not in AI_JOB_KINDS for kind in kinds):
            raise Error(400, 'invalid_input', 'Invalid AI worker kinds')
        with self.transaction(True) as c:
            candidates = c.execute("SELECT * FROM ai_jobs WHERE kind IN (" + ','.join('?' for _ in kinds) + ") AND ((state='queued' AND next_attempt<=?) OR (state='running' AND lease_until<=?)) ORDER BY next_attempt,created_at,id LIMIT 100", (*kinds, current_time, current_time)).fetchall()
            for row in candidates:
                if not self._ai_job_authorized(c, row):
                    self._ai_cancel_job(c, row['id'])
                    continue
                previously_waited = row['attempt_count'] > 0 or row['error'] in ('unavailable', 'rate_limited', 'timeout')
                if row['kind'] in ('turn', 'preview', 'sections') and previously_waited:
                    deadline = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00')).timestamp() + 300
                    if current_time >= deadline:
                        # A restart after the scheduled retry must not revive a
                        # provider wait whose absolute customer deadline passed.
                        c.execute("UPDATE ai_jobs SET state='failed',error='unavailable',lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?", (now(), row['id']))
                        self._ai_turn_failed(c, row)
                        continue
                if row['attempt_count'] >= row['max_attempts']:
                    c.execute("UPDATE ai_jobs SET state='failed',error='attempt_limit',lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?", (now(), row['id']))
                    self._ai_turn_failed(c, row)
                    continue
                token = new_id()
                c.execute("UPDATE ai_jobs SET state='running',attempt_count=attempt_count+1,lease_token=?,lease_until=?,updated_at=? WHERE id=?", (token, current_time + lease_seconds, now(), row['id']))
                return self._ai_job_view(c.execute('SELECT * FROM ai_jobs WHERE id=?', (row['id'],)).fetchone())
        return None

    def ai_defer_job(self, job_id, token, retry_at, current_time=None):
        """Release unpaid local work when the embedding runtime cannot accept it."""
        current_time = time.time() if current_time is None else current_time
        if type(retry_at) not in (int, float) or not math.isfinite(retry_at) or not current_time <= retry_at <= current_time + 300:
            raise Error(400, 'invalid_input', 'Invalid local retry time')
        with self.transaction(True) as c:
            row = self._ai_job_lease(c, job_id, token, current_time)
            if not row or row['kind'] != 'ingest':
                return False
            c.execute("UPDATE ai_jobs SET state='queued',attempt_count=CASE WHEN attempt_count>0 THEN attempt_count-1 ELSE 0 END,lease_token=NULL,lease_until=NULL,next_attempt=?,updated_at=? WHERE id=?", (retry_at, now(), job_id))
            return True

    def _ai_job_lease(self, c, job_id, token, current_time, *, allow_provider_unavailable=False):
        self._ai_lease_input(current_time, 1)
        row = c.execute('SELECT * FROM ai_jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row['state'] != 'running' or row['lease_token'] != token or row['lease_until'] is None or row['lease_until'] <= current_time:
            return None
        if not self._ai_job_authorized(c, row):
            self._ai_cancel_job(c, job_id)
            return None
        if not allow_provider_unavailable and self._ai_bound_key(c, row) is False:
            return None
        return row

    def ai_renew_job(self, job_id, token, current_time=None, lease_seconds=60):
        current_time = time.time() if current_time is None else current_time
        self._ai_lease_input(current_time, lease_seconds)
        with self.transaction(True) as c:
            if not self._ai_job_lease(c, job_id, token, current_time):
                return False
            c.execute('UPDATE ai_jobs SET lease_until=?,updated_at=? WHERE id=?', (current_time + lease_seconds, now(), job_id))
            c.execute('UPDATE ai_provider_quota_groups SET trial_until=? WHERE trial_job_id=? AND trial_token=?',
                      (current_time + lease_seconds, job_id, token))
            return True

    def ai_job_before_dispatch(self, job_id, token, current_time=None, call_index=0):
        current_time = time.time() if current_time is None else current_time
        with self.transaction(True) as c:
            row = self._ai_job_lease(c, job_id, token, current_time)
            if not row:
                return False
            if row['kind'] in ('turn', 'preview', 'sections'):
                reservation = c.execute('SELECT * FROM ai_usage_reservations WHERE job_id=? AND attempt=? AND call_index=?', (job_id, row['attempt_count'], call_index)).fetchone()
                if not reservation or reservation['dispatched']:
                    return False
                self._ai_topic_require_allow(c, row)
                selected = self._ai_provider_claim(c, row, current_time)
                if not selected['available']:
                    return False
                # Bind health evidence to this exact paid call, not an earlier tool round.
                c.execute('''UPDATE ai_usage_reservations SET dispatched=1,provider_key_id=?,provider_key_version=?,
                    provider_health_epoch=?,provider_group_id=?,provider_group_epoch=? WHERE job_id=? AND attempt=? AND call_index=?''',
                    (selected['key_id'], selected['key_version'], selected['health_epoch'], selected['quota_group_id'],
                     selected['group_epoch'], job_id, row['attempt_count'], call_index))
            return True

    def ai_job_material(self, job_id, token, current_time=None):
        """Private leased-worker snapshot; no caller-supplied project or credential."""
        current_time = time.time() if current_time is None else current_time
        with self.transaction(True) as c:
            row = self._ai_job_lease(c, job_id, token, current_time)
            if not row:
                return None
            config = self._ai_config(c, row['project_id'])
            result = {'job': self._ai_job_view(row), 'settings': config['settings'], 'capabilities': config['capabilities'], 'protected_key': config['protected_key'] if row['kind'] in ('turn', 'preview', 'sections') else None}
            result['source_readiness'] = self._ai_readiness(c,config)
            bound = self._ai_bound_key(c, row)
            if bound:
                result.update(capabilities=json.loads(bound['capabilities']), protected_key=bound['protected_key'])
            document_id = json.loads(row['payload']).get('document_id')
            if row['kind'] == 'ingest' and document_id:
                document = c.execute('SELECT * FROM ai_documents WHERE id=? AND project_id=? AND source_id=?', (document_id, row['project_id'], row['resource_id'])).fetchone()
                result['document'] = self._ai_document_view(document, True)
                if document['crawl_run_id']:
                    result['pages'] = [dict(page) for page in c.execute("SELECT p.id,p.url,p.title,p.content,p.content_hash FROM ai_crawl_pages p JOIN ai_crawl_runs r ON r.id=p.run_id JOIN ai_jobs j ON j.id=r.job_id WHERE r.id=? AND r.document_id=? AND p.attempt=j.attempt_count AND p.status='fetched' ORDER BY p.url", (document['crawl_run_id'], document_id))]
            if row['kind'] == 'crawl':
                run = c.execute('SELECT * FROM ai_crawl_runs WHERE job_id=?', (job_id,)).fetchone()
                if run:
                    result['crawl'] = {'id': run['id'], 'config': json.loads(run['config'])}
            if row['kind'] == 'turn':
                trigger = c.execute('SELECT * FROM chat_messages WHERE id=?', (row['resource_id'],)).fetchone()
                result['question'] = trigger['body']
                result['context'] = json.loads(trigger['context'] or 'null')
                result['mcp'] = self._mcp_project(c,row['project_id'])
                result['mcp_tools'] = self._mcp_tools(c,row['project_id'])
                # Preserve the immediate four whole messages for the decision
                # gate even when generation's wider history is too large.
                result['topic_messages'] = list(reversed([
                    {'sender_role': 'customer' if item['sender'] == 'visitor' else item['sender'], 'body': item['body']}
                    for item in c.execute('SELECT sender,body FROM chat_messages WHERE conversation_id=? AND sequence<? ORDER BY sequence DESC LIMIT 4', (trigger['conversation_id'], trigger['sequence']))]))
                history, size = [], 0
                for item in c.execute('SELECT sender,body,context FROM chat_messages WHERE conversation_id=? AND sequence<? ORDER BY sequence DESC LIMIT 12', (trigger['conversation_id'], trigger['sequence'])):
                    if size + len(item['body']) > 12000:
                        break
                    history.append({'sender_role': 'customer' if item['sender'] == 'visitor' else item['sender'], 'body': item['body'], 'context':json.loads(item['context'] or 'null')})
                    size += len(item['body'])
                result['messages'] = list(reversed(history))
            if row['kind'] in ('preview', 'sections'):
                authoring = c.execute('SELECT id,query,coverage FROM ai_authoring_runs WHERE job_id=?', (job_id,)).fetchone()
                if authoring:
                    result['run'] = dict(authoring)
                    result['run']['coverage'] = json.loads(authoring['coverage']) if authoring['coverage'] else None
                if row['kind'] == 'sections':
                    result['source_passages'], result['source_coverage'] = self._ai_section_material(c, row['project_id'], row['resource_id'], result.get('run', {}).get('coverage'))
            return result

    def ai_reserve_usage(self, job_id, token, tokens, current_time=None, call_index=0):
        current_time = time.time() if current_time is None else current_time
        if type(tokens) is not int or not 1 <= tokens <= 1000000 or type(call_index) is not int or call_index < 0:
            raise Error(400, 'invalid_input', 'Invalid usage reservation')
        with self.transaction(True) as c:
            row = self._ai_job_lease(c, job_id, token, current_time)
            if not row or row['kind'] not in ('turn', 'preview', 'sections'):
                return False
            settings = self._ai_config(c, row['project_id'])['settings']
            # Answer/Preview: final draft plus check, one correction and recheck.
            # Long authoring runs keep global batch receipt indices across bounded jobs.
            maximum_call_index = settings['max_tool_rounds'] + (4 if row['kind'] == 'turn' else 3) if row['kind'] in ('turn', 'preview') else 3
            if row['kind'] == 'sections':
                run = c.execute('SELECT coverage FROM ai_authoring_runs WHERE job_id=?', (job_id,)).fetchone()
                coverage = json.loads(run[0]) if run and run[0] else {}
                if coverage.get('scope') == 'all':
                    maximum_call_index = coverage['total_batches'] - 1
                    if call_index != coverage['completed_batches']:
                        return False
            if call_index > maximum_call_index:
                return False
            old = c.execute('SELECT tokens FROM ai_usage_reservations WHERE job_id=? AND attempt=? AND call_index=?', (job_id, row['attempt_count'], call_index)).fetchone()
            if old:
                if old['tokens'] != tokens:
                    conflict('This call already has a different usage reservation')
                return True
            day = datetime.fromtimestamp(current_time, timezone.utc).date().isoformat()
            c.execute('INSERT INTO ai_usage(project_id,day) VALUES(?,?) ON CONFLICT DO NOTHING', (row['project_id'], day))
            usage = c.execute('SELECT * FROM ai_usage WHERE project_id=? AND day=?', (row['project_id'], day)).fetchone()
            if usage['reserved_requests'] + 1 > settings['daily_request_limit'] or usage['reserved_tokens'] + tokens > settings['daily_token_limit']:
                return False
            c.execute('INSERT INTO ai_usage_reservations(job_id,attempt,call_index,project_id,day,requests,tokens) VALUES(?,?,?,?,?,1,?)', (job_id, row['attempt_count'], call_index, row['project_id'], day, tokens))
            c.execute('UPDATE ai_usage SET reserved_requests=reserved_requests+1,reserved_tokens=reserved_tokens+? WHERE project_id=? AND day=?', (tokens, row['project_id'], day))
            return True

    def _ai_report_usage(self, c, row, call_index, input_tokens, output_tokens):
        if type(input_tokens) is not int or type(output_tokens) is not int or not 0 <= input_tokens <= 10000000 or not 0 <= output_tokens <= 10000000:
            raise Error(400, 'invalid_input', 'Invalid provider usage')
        reservation = c.execute('SELECT * FROM ai_usage_reservations WHERE job_id=? AND attempt=? AND call_index=?', (row['id'], row['attempt_count'], call_index)).fetchone()
        if not reservation or not reservation['dispatched']:
            raise Error(409, 'conflict', 'No dispatched reservation exists')
        if reservation['reported']:
            return
        # Reconcile known usage exactly once; unreported/uncertain calls keep their
        # full reservation. Transactional serialization prevents double refunds.
        adjustment = input_tokens + output_tokens - reservation['tokens']
        c.execute('UPDATE ai_usage SET reserved_tokens=reserved_tokens+?,reported_input_tokens=reported_input_tokens+?,reported_output_tokens=reported_output_tokens+? WHERE project_id=? AND day=?', (adjustment, input_tokens, output_tokens, row['project_id'], reservation['day']))
        c.execute('UPDATE ai_usage_reservations SET reported=1 WHERE job_id=? AND attempt=? AND call_index=?', (row['id'], row['attempt_count'], call_index))

    def ai_report_usage(self, job_id, token, call_index, input_tokens, output_tokens, current_time=None):
        current_time = time.time() if current_time is None else current_time
        with self.transaction(True) as c:
            row = self._ai_job_lease(c, job_id, token, current_time)
            if not row:
                return False
            self._ai_report_usage(c, row, call_index, input_tokens, output_tokens)
            return True

    def ai_finish_job(self, job_id, token, state, error=None, retry_at=None, input_tokens=None, output_tokens=None, current_time=None):
        current_time = time.time() if current_time is None else current_time
        if state not in ('succeeded', 'failed', 'queued', 'cancelled') or error not in (None, 'unavailable', 'invalid_response', 'incomplete_response', 'request_rejected', 'invalid_request', 'rate_limited', 'timeout', 'source_failed', 'embedding_failed', 'budget_exhausted', 'cancelled'):
            raise Error(400, 'invalid_input', 'Invalid AI job result')
        if state == 'queued' and (type(retry_at) not in (int, float) or not math.isfinite(retry_at) or not current_time <= retry_at <= current_time + 86400):
            raise Error(400, 'invalid_input', 'Invalid retry time')
        with self.transaction(True) as c:
            row = self._ai_job_lease(c, job_id, token, current_time, allow_provider_unavailable=state != 'succeeded')
            if not row:
                return False
            if input_tokens is not None or output_tokens is not None:
                self._ai_report_usage(c, row, 0, input_tokens, output_tokens)
            if state == 'queued' and row['attempt_count'] >= row['max_attempts']:
                state = 'failed'
            c.execute('UPDATE ai_jobs SET state=?,error=?,next_attempt=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?', (state, error, retry_at if state == 'queued' else current_time, now(), job_id))
            if state == 'failed':
                self._ai_turn_failed(c, row)
            return True

    def ai_jobs(self, project, principal, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise Error(400, 'invalid_input', 'Invalid job page')
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            return [{k: row[k] for k in ('id', 'project_id', 'kind', 'resource_id', 'config_version', 'state', 'attempt_count', 'next_attempt', 'error', 'created_at', 'updated_at')} | {'answer_outcome': json.loads(row['payload']).get('answer_outcome'), 'stage': json.loads(row['payload']).get('ai_progress', {}).get('stage')}
                for row in c.execute('SELECT * FROM ai_jobs WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (pid, limit, offset))]

    def ai_usage(self, project, principal, day=None):
        day = datetime.now(timezone.utc).date().isoformat() if day is None else day
        if not isinstance(day, str) or len(day) != 10:
            raise Error(400, 'invalid_input', 'Invalid usage day')
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            row = c.execute('SELECT * FROM ai_usage WHERE project_id=? AND day=?', (pid, day)).fetchone()
            return dict(row) if row else {'project_id': pid, 'day': day, 'reserved_requests': 0, 'reserved_tokens': 0, 'reported_input_tokens': 0, 'reported_output_tokens': 0}

    @staticmethod
    def _ai_document_view(row, include_content=False):
        fields = ('id', 'source_id', 'project_id', 'source_version', 'revision_number', 'filename', 'title', 'content_hash', 'byte_count', 'created_at')
        result = {k: row[k] for k in fields}
        if include_content:
            result['content'] = row['content']
        return result

    def _ai_knowledge_source_view(self, c, project_id, source_id):
        source = self._ai_source(c, project_id, source_id)
        result = {k: source[k] for k in ('id', 'project_id', 'kind', 'name', 'location', 'version', 'active_revision_id', 'created_at', 'updated_at', 'deleted_at')}
        result.update(active=bool(source['active']), approved=bool(source['approved']))
        document = c.execute('SELECT * FROM ai_documents WHERE id=? AND project_id=? AND source_id=?', (source['latest_document_id'], project_id, source_id)).fetchone()
        result['latest_document'] = self._ai_document_view(document) if document else None
        job = c.execute('SELECT * FROM ai_jobs WHERE id=? AND project_id=? AND resource_id=?', (source['pending_job_id'], project_id, source_id)).fetchone()
        active = c.execute("SELECT document_id,profile FROM ai_revisions WHERE id=? AND project_id=? AND source_id=? AND state='active'", (source['active_revision_id'], project_id, source_id)).fetchone()
        result['index_ready'] = bool(active and active['profile'] == self._ai_json(self._ai_embedding_profile(c)))
        current_index = bool(result['index_ready'] and document and active['document_id'] == document['id'])
        result['index_stale'] = bool(active and document and not current_index)
        if active and source['kind'] == 'website':
            latest_crawl = c.execute(f'SELECT id FROM ai_crawl_runs WHERE source_id=? ORDER BY {self._ai_crawl_order_sql} DESC LIMIT 1', (source_id,)).fetchone()
            indexed_crawl = c.execute('SELECT crawl_run_id FROM ai_documents WHERE id=?', (active['document_id'],)).fetchone()
            if latest_crawl and (not indexed_crawl or indexed_crawl[0] != latest_crawl[0]):
                result['index_stale'] = True
        if source['deleted_at']:
            status = 'deleted'
        elif not source['active']:
            status = 'inactive'
        elif job and job['state'] in ('queued', 'running', 'failed', 'cancelled'):
            status = {'queued': 'pending', 'running': 'indexing', 'failed': 'failed', 'cancelled': 'cancelled'}[job['state']]
        elif current_index or (result['index_ready'] and not document):
            status = 'indexed'
        else:
            status = 'cancelled' if document else 'empty'
        result['index_status'] = status
        result['job'] = {k: job[k] for k in ('id', 'state', 'attempt_count', 'error', 'created_at', 'updated_at')} if job else None
        return result

    def ai_knowledge_sources(self, project, principal, limit=25, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1000000:
            raise Error(400, 'invalid_input', 'Invalid knowledge source page')
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            rows = c.execute('SELECT id FROM ai_sources WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (pid, limit + 1, offset)).fetchall()
            return {'items': [self._ai_knowledge_source_view(c, pid, row[0]) for row in rows[:limit]], 'limit': limit, 'offset': offset, 'has_more': len(rows) > limit}

    def ai_knowledge_library(self, project, principal, query='', source_id=None, limit=25, offset=0, item_id=None):
        if not isinstance(query,str) or len(query)>200 or type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or not 0<=offset<=1000000:
            raise Error(400,'invalid_input','Invalid library page')
        with self.transaction() as c:
            self._ai_admin(c,principal)
            pid=self._ai_project(c,project)
            if source_id:
                self._ai_source(c,pid,source_id)
            # Read the currently indexed original when one exists; failed refreshes never replace it.
            sql=f"""WITH originals AS (
                SELECT s.id source_id,s.name source_name,s.kind,s.active,s.approved,s.active_revision_id,
                       d.id document_id,d.title document_title,d.content document_content,d.crawl_run_id,d.created_at,
                       CASE WHEN r.id IS NOT NULL AND r.profile=? THEN 1 ELSE 0 END index_available
                FROM ai_sources s LEFT JOIN ai_revisions r ON r.id=s.active_revision_id AND r.state='active'
                JOIN ai_documents d ON d.id=COALESCE(r.document_id,s.latest_document_id) AND d.project_id=s.project_id AND d.source_id=s.id
                WHERE s.project_id=? AND s.deleted_at IS NULL
            ), entries AS (
                SELECT document_id id,source_id,source_name,kind,document_title title,document_content content,NULL url,created_at,active,approved,index_available FROM originals WHERE kind='markdown'
                UNION ALL
                SELECT p.id,o.source_id,o.source_name,o.kind,p.title,p.content,p.url,o.created_at,o.active,o.approved,(CASE WHEN o.index_available=1 AND EXISTS (SELECT 1 FROM ai_chunks k WHERE k.page_id=p.id AND k.revision_id=o.active_revision_id) THEN 1 ELSE 0 END)
                FROM originals o JOIN ai_crawl_pages p ON p.run_id=o.crawl_run_id
                JOIN ai_crawl_runs cr ON cr.id=p.run_id JOIN ai_jobs j ON j.id=cr.job_id
                WHERE o.kind='website' AND p.status='fetched' AND p.attempt=j.attempt_count
            ) SELECT * FROM entries WHERE ({self._text_contains_sql('title')} OR {self._text_contains_sql('content')})
                AND (? IS NULL OR source_id=?) AND (? IS NULL OR id=?) ORDER BY source_name,title,id LIMIT ? OFFSET ?"""
            rows=c.execute(sql,(self._ai_json(self._ai_embedding_profile(c)),pid,query,query,source_id,source_id,item_id,item_id,limit+1,offset)).fetchall()
            items=[]
            for row in rows[:limit]:
                item={k:row[k] for k in ('id','source_id','source_name','kind','title','url','created_at')}
                item['ready']=bool(row['active'] and row['approved'] and row['index_available'])
                item['status']='Ready for AI' if item['ready'] else 'Inactive' if not row['active'] else 'Needs approval' if not row['approved'] else 'Indexing required'
                item['excerpt']=row['content'][:240]
                if item_id: item['content']=row['content']
                items.append(item)
            if item_id and not items: missing()
            return items[0] if item_id else {'items':items,'has_more':len(rows)>limit,'limit':limit,'offset':offset}

    def ai_knowledge_source(self, project, principal, source_id):
        with self.transaction() as c:
            self._ai_admin(c, principal)
            return self._ai_knowledge_source_view(c, self._ai_project(c, project), source_id)

    def ai_knowledge_documents(self, project, principal, source_id, limit=25, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1000000:
            raise Error(400, 'invalid_input', 'Invalid document page')
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            self._ai_source(c, pid, source_id)
            rows = c.execute('SELECT * FROM ai_documents WHERE project_id=? AND source_id=? ORDER BY revision_number DESC LIMIT ? OFFSET ?', (pid, source_id, limit + 1, offset)).fetchall()
            return {'items': [self._ai_document_view(row) for row in rows[:limit]], 'limit': limit, 'offset': offset, 'has_more': len(rows) > limit}

    def ai_knowledge_document(self, project, principal, source_id, document_id):
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            row = c.execute('SELECT * FROM ai_documents WHERE id=? AND project_id=? AND source_id=?', (document_id, pid, source_id)).fetchone()
            if not row:
                missing()
            return self._ai_document_view(row, True)

    @staticmethod
    def _ai_supersede_import(c, project_id, source_id):
        c.execute('UPDATE ai_sources SET version=version+1,updated_at=? WHERE id=? AND project_id=?', (now(), source_id, project_id))
        c.execute("UPDATE ai_jobs SET state='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? WHERE project_id=? AND resource_id=? AND kind IN ('ingest','crawl','sections') AND state IN ('queued','running')", (now(), project_id, source_id))
        c.execute("UPDATE ai_revisions SET state='discarded' WHERE source_id=? AND state='staging'", (source_id,))

    def _ai_document_job(self, c, project_id, principal, source_id, document_id, dedupe_key):
        source = self._ai_source(c, project_id, source_id)
        authority = {'principal': {k: principal[k] for k in ('kind', 'user_id', 'session_id')}, 'source_version': source['version']}
        job = self._ai_enqueue(c, project_id, 'ingest', source_id, dedupe_key, 0, authority, {'document_id': document_id})
        c.execute('UPDATE ai_sources SET pending_job_id=? WHERE id=?', (job['id'], source_id))
        return job

    def ai_knowledge_import(self, project, principal, client_id, filename, name, content, *, source_id=None, version=None):
        """Preserve original Markdown and its indexing intent in one transaction."""
        from feedback.ai_knowledge import validate_markdown
        validate_markdown(filename, name, content)
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 100:
            raise Error(400, 'invalid_input', 'Invalid import identifier')
        original = content.encode('utf-8')
        request_hash = hashlib.sha256(self._ai_json([source_id, version, filename, name, content], 512000).encode()).hexdigest()
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            old = c.execute('SELECT * FROM ai_documents WHERE project_id=? AND client_id=?', (pid, client_id)).fetchone()
            if old:
                if old['request_hash'] != request_hash:
                    conflict('This import identifier already describes different content')
                return {'source': self._ai_knowledge_source_view(c, pid, old['source_id']), 'document': self._ai_document_view(old)}
            if source_id is None:
                if version is not None:
                    raise Error(400, 'invalid_input', 'New sources do not have a version')
                if c.execute('SELECT count(*) FROM ai_sources WHERE project_id=? AND deleted_at IS NULL', (pid,)).fetchone()[0] >= 100:
                    raise Error(409, 'source_limit', 'Project knowledge source limit reached')
                source_id, ts = new_id(), now()
                c.execute("INSERT INTO ai_sources(id,project_id,kind,name,created_at,updated_at) VALUES(?,?,'markdown',?,?,?)", (source_id, pid, name, ts, ts))
            else:
                source = self._ai_source(c, pid, source_id)
                if source['kind'] != 'markdown' or source['deleted_at'] or not source['active'] or type(version) is not int or source['version'] != version:
                    conflict()
            if c.execute('SELECT count(*) FROM ai_documents WHERE source_id=?', (source_id,)).fetchone()[0] >= 100:
                raise Error(409, 'revision_limit', 'This source has reached its 100-document revision limit')
            self._ai_supersede_import(c, pid, source_id)
            source = self._ai_source(c, pid, source_id)
            document_id = new_id()
            job = self._ai_document_job(c, pid, principal, source_id, document_id, 'document:' + client_id)
            number = c.execute('SELECT COALESCE(MAX(revision_number),0)+1 FROM ai_documents WHERE source_id=?', (source_id,)).fetchone()[0]
            c.execute('''INSERT INTO ai_documents(id,source_id,project_id,source_version,revision_number,filename,title,content,content_hash,byte_count,created_by,created_at,client_id,request_hash,job_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (document_id, source_id, pid, source['version'], number, filename, name, content, hashlib.sha256(original).hexdigest(), len(original), principal['user_id'], now(), client_id, request_hash, job['id']))
            c.execute('UPDATE ai_sources SET name=?,latest_document_id=? WHERE id=?', (name, document_id, source_id))
            document = c.execute('SELECT * FROM ai_documents WHERE id=?', (document_id,)).fetchone()
            return {'source': self._ai_knowledge_source_view(c, pid, source_id), 'document': self._ai_document_view(document)}

    def ai_knowledge_change(self, project, principal, source_id, version, *, active, approved, delete=False):
        if any(type(x) is not bool for x in (active, approved, delete)):
            raise Error(400, 'invalid_input', 'Explicit source flags required')
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            source = self._ai_source(c, pid, source_id)
            if type(version) is not int or source['version'] != version or source['deleted_at']:
                conflict()
            if bool(source['active']) == active and bool(source['approved']) == approved and not delete:
                return self._ai_knowledge_source_view(c, pid, source_id)
            if active and not source['active'] and not delete:
                self._ai_check_corpus(c, pid, source_id)
            self._ai_supersede_import(c, pid, source_id)
            c.execute('UPDATE ai_sources SET active=?,approved=?,deleted_at=?,pending_job_id=NULL WHERE id=?', (False if delete else active, approved, now() if delete else None, source_id))
            self._ai_knowledge_changed(c, pid)
            # Removing approval must never depend on spare indexing capacity.
            # Explicit approval or reactivation may queue the preserved original.
            if active and not delete and source['latest_document_id'] and (approved or not source['active']):
                indexed = c.execute("SELECT 1 FROM ai_revisions WHERE id=? AND document_id=? AND state='active'", (source['active_revision_id'], source['latest_document_id'])).fetchone()
                if not indexed:
                    self._ai_document_job(c, pid, principal, source_id, source['latest_document_id'], 'source:' + source_id + ':' + str(version + 1))
            return self._ai_knowledge_source_view(c, pid, source_id)

    def _ai_crawl_view(self, c, run, include_pages=False):
        job = c.execute('SELECT * FROM ai_jobs WHERE id=?', (run['job_id'],)).fetchone()
        result = {k: run[k] for k in ('id', 'project_id', 'source_id', 'source_version', 'version', 'created_at', 'finished_at', 'document_id', 'job_id', 'stop_reason')}
        result.update(config=json.loads(run['config']), status=job['state'], error=job['error'], attempt_count=job['attempt_count'])
        counts = {row['status']: row['total'] for row in c.execute('SELECT status,count(*) total FROM ai_crawl_pages WHERE run_id=? AND attempt=? GROUP BY status', (run['id'], job['attempt_count']))}
        result.update(fetched_count=counts.get('fetched', 0), skipped_count=counts.get('skipped', 0), failed_count=counts.get('failed', 0))
        if include_pages:
            result['pages'] = [dict(row) for row in c.execute('SELECT id,attempt,url,title,content,content_hash,status,error,created_at FROM ai_crawl_pages WHERE run_id=? AND attempt=? ORDER BY url LIMIT 60', (run['id'], job['attempt_count']))]
        return result

    def ai_crawl_begin(self, project, principal, client_id, name, config, *, source_id=None, version=None):
        from feedback.ai_crawler import validate_crawl_config
        config = validate_crawl_config(config)
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 100 or not isinstance(name, str) or not name.strip() or len(name) > 300:
            raise Error(400, 'invalid_input', 'Invalid crawl import')
        config_json = self._ai_json(config, 32768)
        request_hash = hashlib.sha256(self._ai_json([source_id, version, name, config], 40000).encode()).hexdigest()
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            old = c.execute('SELECT * FROM ai_crawl_runs WHERE project_id=? AND client_id=?', (pid, client_id)).fetchone()
            if old:
                if old['request_hash'] != request_hash:
                    conflict('This crawl identifier already describes different work')
                return {'source': self._ai_knowledge_source_view(c, pid, old['source_id']), 'run': self._ai_crawl_view(c, old, True)}
            if source_id is None:
                if version is not None:
                    raise Error(400, 'invalid_input', 'New website sources do not have a version')
                if c.execute('SELECT count(*) FROM ai_sources WHERE project_id=? AND deleted_at IS NULL', (pid,)).fetchone()[0] >= 100:
                    raise Error(409, 'source_limit', 'Project knowledge source limit reached')
                source_id, ts = new_id(), now()
                c.execute("INSERT INTO ai_sources(id,project_id,kind,name,location,created_at,updated_at) VALUES(?,?,'website',?,?,?,?)", (source_id, pid, name, config['seed_url'], ts, ts))
            else:
                source = self._ai_source(c, pid, source_id)
                if source['kind'] != 'website' or source['deleted_at'] or not source['active'] or type(version) is not int or source['version'] != version:
                    conflict()
            if c.execute('SELECT count(*) FROM ai_crawl_runs WHERE source_id=?', (source_id,)).fetchone()[0] >= 100:
                raise Error(409, 'revision_limit', 'This source has reached its 100-crawl history limit')
            self._ai_supersede_import(c, pid, source_id)
            source = self._ai_source(c, pid, source_id)
            run_id = new_id()
            authority = {'principal': {k: principal[k] for k in ('kind', 'user_id', 'session_id')}, 'source_version': source['version']}
            job = self._ai_enqueue(c, pid, 'crawl', source_id, 'crawl:' + client_id, 0, authority, {'crawl_run_id': run_id})
            c.execute('''INSERT INTO ai_crawl_runs(id,project_id,source_id,source_version,config,client_id,request_hash,job_id,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)''', (run_id, pid, source_id, source['version'], config_json, client_id, request_hash, job['id'], now()))
            c.execute('UPDATE ai_sources SET pending_job_id=?,name=?,location=? WHERE id=?', (job['id'], name, config['seed_url'], source_id))
            run = c.execute('SELECT * FROM ai_crawl_runs WHERE id=?', (run_id,)).fetchone()
            return {'source': self._ai_knowledge_source_view(c, pid, source_id), 'run': self._ai_crawl_view(c, run, True)}

    def ai_crawl_runs(self, project, principal, source_id, limit=25, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1000000:
            raise Error(400, 'invalid_input', 'Invalid crawl page')
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            self._ai_source(c, pid, source_id)
            rows = c.execute(f'SELECT * FROM ai_crawl_runs WHERE project_id=? AND source_id=? ORDER BY created_at DESC,{self._ai_crawl_order_sql} DESC LIMIT ? OFFSET ?', (pid, source_id, limit + 1, offset)).fetchall()
            return {'items': [self._ai_crawl_view(c, row) for row in rows[:limit]], 'limit': limit, 'offset': offset, 'has_more': len(rows) > limit}

    def ai_crawl_run(self, project, principal, source_id, run_id):
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            row = c.execute('SELECT * FROM ai_crawl_runs WHERE id=? AND project_id=? AND source_id=?', (run_id, pid, source_id)).fetchone()
            if not row:
                missing()
            return self._ai_crawl_view(c, row, True)

    def ai_crawl_cancel(self, project, principal, source_id, run_id, version):
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            run = c.execute('SELECT * FROM ai_crawl_runs WHERE id=? AND project_id=? AND source_id=?', (run_id, pid, source_id)).fetchone()
            if not run:
                missing()
            if type(version) is not int or run['version'] != version:
                conflict()
            job = c.execute('SELECT * FROM ai_jobs WHERE id=?', (run['job_id'],)).fetchone()
            if job['state'] == 'cancelled':
                return self._ai_crawl_view(c, run, True)
            if job['state'] not in ('queued', 'running'):
                conflict()
            self._ai_cancel_job(c, job['id'])
            c.execute('UPDATE ai_crawl_runs SET version=version+1,finished_at=? WHERE id=?', (now(), run_id))
            c.execute('UPDATE ai_sources SET pending_job_id=NULL WHERE id=? AND pending_job_id=?', (source_id, job['id']))
            return self._ai_crawl_view(c, c.execute('SELECT * FROM ai_crawl_runs WHERE id=?', (run_id,)).fetchone(), True)

    def _ai_crawl_lease(self, c, job_id, token):
        job = self._ai_job_lease(c, job_id, token, time.time())
        if not job or job['kind'] != 'crawl':
            return None, None
        run_id = json.loads(job['payload']).get('crawl_run_id')
        run = c.execute('SELECT * FROM ai_crawl_runs WHERE id=? AND job_id=? AND project_id=? AND source_id=?', (run_id, job_id, job['project_id'], job['resource_id'])).fetchone()
        return (job, run) if run else (None, None)

    def ai_crawl_record(self, job_id, token, page):
        from feedback.ai_crawler import CrawlError, canonical_url, in_scope
        if not isinstance(page, dict) or set(page) != {'url', 'title', 'content', 'status', 'error'} or page['status'] not in ('fetched', 'failed', 'skipped') or page['error'] not in CrawlError.CODES | {None} or not isinstance(page['title'], str) or len(page['title']) > 300 or not isinstance(page['content'], str):
            raise Error(400, 'invalid_input', 'Invalid crawl page outcome')
        self._ai_json(page, 300000)
        try:
            url = canonical_url(page['url'])
        except CrawlError:
            raise Error(400, 'invalid_input', 'Invalid crawl page URL') from None
        content = page['content']
        if len(content.encode()) > 65536 or (page['status'] == 'fetched' and (not content.strip() or page['error'] is not None)) or (page['status'] != 'fetched' and content):
            raise Error(400, 'invalid_input', 'Invalid crawl page content')
        with self.transaction(True) as c:
            job, run = self._ai_crawl_lease(c, job_id, token)
            if not job:
                return False
            config = json.loads(run['config'])
            if page['status'] == 'fetched' and not in_scope(url, config):
                conflict('The page is outside this crawl scope')
            old = c.execute('SELECT * FROM ai_crawl_pages WHERE run_id=? AND attempt=? AND url=?', (run['id'], job['attempt_count'], url)).fetchone()
            if old:
                if (old['title'], old['content'], old['status'], old['error']) != (page['title'], content, page['status'], page['error']):
                    conflict('This crawl page already has a different outcome')
                return True
            totals = c.execute(f"SELECT count(*) total,SUM(CASE WHEN status='fetched' THEN 1 ELSE 0 END) fetched,COALESCE(SUM({self._text_bytes_sql('content')}),0) bytes FROM ai_crawl_pages WHERE run_id=? AND attempt=?", (run['id'], job['attempt_count'])).fetchone()
            if totals['total'] >= 60 or (page['status'] == 'fetched' and (totals['fetched'] or 0) >= config['max_pages']) or totals['bytes'] + len(content.encode()) > 512 * 1024:
                raise Error(409, 'crawl_limit', 'The bounded crawl outcome limit was reached')
            c.execute('INSERT INTO ai_crawl_pages(id,run_id,attempt,url,title,content,content_hash,status,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (new_id(), run['id'], job['attempt_count'], url, page['title'], content, hashlib.sha256(content.encode()).hexdigest(), page['status'], page['error'], now()))
            return True

    def ai_crawl_finish(self, job_id, token, success, error=None, *, stop_reason=None):
        from feedback.ai_crawler import CrawlError
        if type(success) is not bool or error not in CrawlError.CODES | {None} or stop_reason not in (None, 'page_limit'):
            raise Error(400, 'invalid_input', 'Invalid crawl result')
        with self.transaction(True) as c:
            job, run = self._ai_crawl_lease(c, job_id, token)
            if not job:
                return False
            pages = c.execute('SELECT * FROM ai_crawl_pages WHERE run_id=? AND attempt=? ORDER BY url', (run['id'], job['attempt_count'])).fetchall()
            fetched = [row for row in pages if row['status'] == 'fetched']
            if not fetched or any(row['status'] == 'failed' for row in pages) or error:
                success = False
            # Skipped document attempts also consume the configured page budget.
            # Metadata outcomes may add to this total; fetched pages remain capped by record().
            if success and stop_reason and len(pages) < json.loads(run['config'])['max_pages']:
                raise Error(400, 'invalid_input', 'A page-limit completion must include the configured number of attempts')
            if success:
                source = self._ai_source(c, job['project_id'], job['resource_id'])
                content = '\n\n'.join('# ' + row['title'] + '\n\n' + row['content'] for row in fetched)
                original = content.encode('utf-8')
                if len(original) > 1048576:
                    raise Error(409, 'crawl_limit', 'The complete website bundle is too large')
                document_id = new_id()
                principal = json.loads(job['authority'])['principal']
                # Free this queue slot before inserting its indexing continuation.
                c.execute("UPDATE ai_jobs SET state='succeeded',lease_token=NULL,lease_until=NULL,error=NULL,updated_at=? WHERE id=?", (now(), job_id))
                ingest = self._ai_document_job(c, job['project_id'], principal, job['resource_id'], document_id, 'crawl-index:' + run['id'])
                number = c.execute('SELECT COALESCE(MAX(revision_number),0)+1 FROM ai_documents WHERE source_id=?', (job['resource_id'],)).fetchone()[0]
                c.execute('''INSERT INTO ai_documents(id,source_id,project_id,source_version,revision_number,filename,title,content,content_hash,byte_count,created_by,created_at,client_id,request_hash,job_id,crawl_run_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (document_id, job['resource_id'], job['project_id'], source['version'], number, 'website.md', source['name'], content, hashlib.sha256(original).hexdigest(), len(original), principal['user_id'], now(), 'crawl:' + run['id'], run['request_hash'], ingest['id'], run['id']))
                c.execute('UPDATE ai_sources SET latest_document_id=? WHERE id=?', (document_id, job['resource_id']))
                c.execute('UPDATE ai_crawl_runs SET document_id=? WHERE id=?', (document_id, run['id']))
            else:
                c.execute("UPDATE ai_jobs SET state='failed',lease_token=NULL,lease_until=NULL,error=?,updated_at=? WHERE id=?", (error or 'crawl_failed', now(), job_id))
            c.execute('UPDATE ai_crawl_runs SET version=version+1,finished_at=?,stop_reason=? WHERE id=?', (now(), stop_reason if success else None, run['id']))
            return True

    def ai_retrieval_context(self, project, principal):
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            return {'project_id': pid, 'knowledge_version': self._ai_knowledge_version(c, pid)}

    @classmethod
    def _ai_eligible_sql(cls, with_vectors=True):
        return f"""SELECT k.id,{cls._ai_chunk_order_sql} chunk_rowid,k.revision_id,r.source_id,s.kind,s.name source_name,
            k.title,k.content,k.location,k.page_id,""" + ('k.vector,' if with_vectors else '') + """p.url page_url
            FROM ai_chunks k JOIN ai_revisions r ON r.id=k.revision_id AND r.project_id=k.project_id
            JOIN ai_sources s ON s.id=r.source_id AND s.project_id=r.project_id AND s.active_revision_id=r.id
            LEFT JOIN ai_documents d ON d.id=r.document_id AND d.project_id=r.project_id AND d.source_id=r.source_id
            LEFT JOIN ai_crawl_pages p ON p.id=k.page_id
            WHERE k.project_id=? AND r.profile=? AND r.state='active' AND s.active=1 AND s.approved=1 AND s.deleted_at IS NULL
            AND ((s.kind='markdown' AND k.location IS NULL) OR (s.kind='website' AND p.status='fetched' AND p.run_id=d.crawl_run_id AND p.url=k.location))"""

    def _ai_retrieve(self, c, project_id, query, vector, knowledge_version, limit):
        import re
        from feedback.ai_runtime import validate_vectors
        from feedback.ai_runtime import EmbeddingRuntimeError
        if type(limit) is not int or not 1 <= limit <= 20 or not isinstance(query, str) or not 1 <= len(query) <= 1000:
            raise Error(400, 'invalid_input', 'Invalid retrieval query')
        try:
            values = validate_vectors([vector], 1, dimensions=self._ai_embedding_profile(c)['dimensions'])[0]
        except (EmbeddingRuntimeError, OverflowError):
            raise Error(400, 'invalid_input', 'Invalid query embedding') from None
        if self._ai_knowledge_version(c, project_id) != knowledge_version:
            conflict('Project knowledge changed while this answer was being prepared')
        take = min(100, max(20, limit * 4))
        semantic, lexical = self._ai_search_candidates(c, project_id, query, values, take)
        if len(lexical) > 10000:
            raise Error(409, 'knowledge_limit', 'This project exceeds the active-passage search limit')
        terms = re.findall(r'\w+', query, re.UNICODE)[:32]
        if terms:
            # Term frequencies are scoped to eligible project passages, never
            # installation-wide statistics such as a database's default rank.
            import unicodedata
            def words(text):
                text = ''.join(ch for ch in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(ch))
                return re.findall(r'\w+', text, re.UNICODE)
            wanted = set(words(' '.join(terms)))
            matches, frequencies = [], {term: 0 for term in wanted}
            for row in lexical:
                title_matches = wanted.intersection(words(row['title']))
                body_words = words(row['content'])
                body_matches = wanted.intersection(body_words)
                # Retain only query-term intersections and length; full token
                # sets for a 10,000-passage corpus need not remain in memory.
                matches.append((row, title_matches, body_matches, len(body_words)))
                for term in title_matches | body_matches:
                    frequencies[term] += 1
            weights = {term: math.log1p((len(lexical) - count + .5) / (count + .5))
                       for term, count in frequencies.items()}
            def relevance(match):
                row, title_matches, body_matches, body_length = match
                coverage = 3 * math.fsum(weights[term] for term in title_matches)
                coverage += math.fsum(weights[term] for term in body_matches)
                return (-coverage / (1 + .05 * math.log1p(body_length)), row['id'])
            lexical = [match[0] for match in sorted(matches, key=relevance)[:take]]
        ranked = {}
        for rows in (semantic, lexical):
            for rank, row in enumerate(rows, 1):
                if row['id'] not in ranked:
                    ranked[row['id']] = {k: row[k] for k in ('id', 'source_id', 'revision_id', 'kind', 'source_name', 'title', 'content', 'page_url')}
                    ranked[row['id']]['score'] = 0.0
                ranked[row['id']]['score'] += 1.0 / (60 + rank)
        passages = sorted(ranked.values(), key=lambda row: (-row['score'], row['id']))[:limit]
        return {'project_id': project_id, 'knowledge_version': knowledge_version, 'passages': passages}

    def ai_retrieve(self, project, principal, query, vector, knowledge_version, limit=6):
        with self.transaction() as c:
            self._ai_admin(c, principal)
            return self._ai_retrieve(c, self._ai_project(c, project), query, vector, knowledge_version, limit)

    def ai_retrieve_job(self, job_id, token, query, vector, limit=6):
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, time.time())
            if not job or job['kind'] not in ('turn', 'preview', 'sections'):
                conflict('This answer is no longer authorized')
            self._ai_topic_require_allow(c, job)
            return self._ai_retrieve(c, job['project_id'], query, vector, json.loads(job['authority'])['knowledge_version'], limit)

    def ai_validate_citations(self, project, principal, knowledge_version, chunk_ids):
        if not isinstance(chunk_ids, list) or len(chunk_ids) > MAX_SOURCES or any(not isinstance(x, str) or len(x) > 100 for x in chunk_ids) or len(set(chunk_ids)) != len(chunk_ids):
            raise Error(400, 'invalid_input', 'Invalid citations')
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            if self._ai_knowledge_version(c, pid) != knowledge_version:
                conflict('Project knowledge changed while this answer was being prepared')
            if not chunk_ids:
                return []
            rows = c.execute('SELECT * FROM (' + self._ai_eligible_sql(False) + ') AS eligible WHERE id IN (' + ','.join('?' for _ in chunk_ids) + ')', (pid, self._ai_json(self._ai_embedding_profile(c)), *chunk_ids)).fetchall()
            if len(rows) != len(chunk_ids):
                conflict('A citation is no longer eligible for this project')
            found = {row['id']: {k: row[k] for k in ('id', 'source_id', 'revision_id', 'kind', 'source_name', 'title', 'content', 'page_url')} for row in rows}
            return [found[cid] for cid in chunk_ids]

    def ai_reusable_chunks(self, job_id, token, chunks):
        if not isinstance(chunks, list) or not 1 <= len(chunks) <= 100:
            raise Error(400, 'invalid_input', 'Invalid reusable passage batch')
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, time.time())
            if not job or job['kind'] != 'ingest':
                conflict('The knowledge worker no longer owns this operation')
            result = {}
            for chunk in chunks:
                row = c.execute('''SELECT k.vector FROM ai_chunks k JOIN ai_revisions r ON r.id=k.revision_id
                    WHERE k.project_id=? AND r.source_id=? AND r.profile=? AND k.title=? AND k.content=? AND (k.location=? OR (k.location IS NULL AND ? IS NULL))
                    ORDER BY r.created_at DESC,k.id LIMIT 1''', (job['project_id'], job['resource_id'], self._ai_json(self._ai_embedding_profile(c)), chunk['title'], chunk['content'], chunk['location'], chunk['location'])).fetchone()
                if row:
                    result[chunk['ordinal']] = list(struct.unpack('<' + str(self._ai_embedding_profile(c)['dimensions']) + 'f', row['vector']))
            return result

    @staticmethod
    def _chat_cancel_ai(c, conversation_id):
        c.execute("UPDATE ai_jobs SET state='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? WHERE id IN (SELECT job_id FROM ai_turns WHERE conversation_id=?) AND state IN ('queued','running')", (now(), conversation_id))
        c.execute("UPDATE ai_proposals SET state='cancelled',version=version+1 WHERE conversation_id=? AND state='pending'", (conversation_id,))

    def _chat_set_mode(self, c, conversation, mode, actor, reason):
        if conversation['support_mode'] == mode:
            return
        cid, ts, eid = conversation['id'], now(), new_id()
        self._chat_cancel_ai(c, cid)
        c.execute('UPDATE chat_conversations SET support_mode=?,mode_version=mode_version+1,updated_at=? WHERE id=?', (mode, ts, cid))
        project = c.execute('SELECT identifier FROM projects WHERE id=?', (conversation['project_id'],)).fetchone()
        payload = {'id': eid, 'type': 'chat.support_mode_changed', 'schema_version': 2, 'occurred_at': ts,
                   'project': {'id': conversation['project_id'], 'identifier': project['identifier']},
                   'conversation': {'id': cid, 'support_mode': mode, 'mode_version': conversation['mode_version'] + 1},
                   'previous_mode': conversation['support_mode'], 'reason': reason, 'actor': actor}
        self._persist_event(c, conversation['project_id'], None, None, payload)
        if mode == 'waiting_human':
            self._queue_push(c, eid, 'chat.message', cid)
            c.execute('UPDATE chat_conversations SET last_alert_at=? WHERE id=?', (time.time(), cid))

    def chat_ai_available(self, project):
        with self.transaction() as c:
            pid = self._ai_project(c, project)
            return self._ai_ready(c, self._ai_config(c, pid), require_enabled=True)

    def chat_support_mode(self, conversation_id, principal, mode, mode_version):
        if mode not in ('ai', 'waiting_human', 'human') or type(mode_version) is not int:
            raise Error(400, 'invalid_input', 'Choose an explicit support mode and version')
        if principal.get('kind') != 'admin' and mode == 'human':
            raise Error(403, 'forbidden', 'Only an administrator can take over support')
        with self.transaction(True) as c:
            row = self.chat_authorize(c, conversation_id, principal)
            self._active_project(c, row['project_id'])
            if row['state'] != 'open' or row['mode_version'] != mode_version:
                conflict()
            if row['support_mode'] == mode:
                return self.chat_view(c, conversation_id, principal)
            if mode == 'ai' and not self._ai_ready(c, self._ai_config(c, row['project_id']), require_enabled=True):
                raise Error(409, 'ai_not_ready', 'AI support is currently unavailable; human support remains available')
            actor = {'kind': 'admin' if principal['kind'] == 'admin' else 'customer', 'user_id': principal.get('user_id')}
            self._chat_set_mode(c, row, mode, actor, 'requested')
            return self.chat_view(c, conversation_id, principal)

    def _chat_enqueue_turn(self, c, conversation_id, principal, message_id, sequence):
        self._chat_cancel_ai(c, conversation_id)
        row = self.chat_authorize(c, conversation_id, principal)
        config = self._ai_config(c, row['project_id'])
        if not self._ai_ready(c, config, require_enabled=True):
            self._chat_set_mode(c, row, 'waiting_human', {'kind': 'system', 'user_id': None}, 'ai_unavailable')
            return
        authority = {'principal': {k: principal[k] for k in ('kind', 'session_id', 'origin') if k in principal},
                     'conversation_id': conversation_id, 'customer_id': row['customer_id'],
                     'mode_version': row['mode_version'], 'trigger_sequence': sequence,
                     'mcp_version': self._mcp_project(c,row['project_id'])['version'],
                     'knowledge_version': self._ai_knowledge_version(c, row['project_id'])}
        # Messages always survive AI admission failures. Human support remains
        # available even when the bounded generation queue is full.
        try:
            job = self._ai_enqueue(c, row['project_id'], 'turn', message_id, message_id, config['version'], authority)
        except Error as error:
            if error.code != 'ai_busy':
                raise
            self._chat_set_mode(c, row, 'waiting_human', {'kind': 'system', 'user_id': None}, 'ai_unavailable')
            return
        c.execute('INSERT INTO ai_turns(job_id,conversation_id,trigger_message_id) VALUES(?,?,?)', (job['id'], conversation_id, message_id))

    def _ai_turn_failed(self, c, job):
        if job['kind'] != 'turn':
            return
        authority = json.loads(job['authority'])
        row = c.execute('SELECT * FROM chat_conversations WHERE id=?', (authority['conversation_id'],)).fetchone()
        if row and row['state'] == 'open' and row['support_mode'] == 'ai' and row['mode_version'] == authority['mode_version'] and row['last_sequence'] == authority['trigger_sequence']:
            # Technical failures are retryable by the visitor, not a request for
            # human takeover. Retain the current mode and explicit takeover fences.
            payload = json.loads(job['payload'])
            payload['answer_outcome'] = 'failed'
            c.execute('UPDATE ai_jobs SET payload=? WHERE id=?', (self._ai_json(payload), job['id']))

    def _ai_citation_rows(self, c, project_id, chunk_ids):
        if not isinstance(chunk_ids, list) or len(chunk_ids) > MAX_SOURCES or any(not isinstance(x, str) or not 1 <= len(x) <= 100 for x in chunk_ids) or len(set(chunk_ids)) != len(chunk_ids):
            raise Error(400, 'invalid_input', 'Invalid citations')
        if not chunk_ids:
            return []
        rows = c.execute('SELECT * FROM (' + self._ai_eligible_sql(False) + ') AS eligible WHERE id IN (' + ','.join('?' for _ in chunk_ids) + ')', (project_id, self._ai_json(self._ai_embedding_profile(c)), *chunk_ids)).fetchall()
        if len(rows) != len(chunk_ids):
            conflict('A citation is no longer eligible for this project')
        found = {row['id']: {k: row[k] for k in ('id', 'source_id', 'revision_id', 'kind', 'source_name', 'title', 'content', 'page_url')} for row in rows}
        return [found[cid] for cid in chunk_ids]

    @staticmethod
    def _ai_validate_source_ids(ids):
        if (not isinstance(ids, list) or len(ids) > MAX_SOURCES
                or any(not isinstance(x, str) or not 1 <= len(x) <= 100 for x in ids)
                or len(set(ids)) != len(ids)):
            raise Error(400, 'invalid_input', 'Use at most twenty distinct citations')

    def ai_job_progress(self, job_id, token, stage):
        if stage not in AI_PROGRESS_STAGES:
            raise Error(400, 'invalid_input', 'Invalid AI progress stage')
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, time.time())
            if not job or job['kind'] != 'turn':
                return False
            payload = json.loads(job['payload'])
            progress = {'stage': stage, 'attempt': job['attempt_count']}
            if payload.get('ai_progress') != progress:
                payload['ai_progress'] = progress
                c.execute('UPDATE ai_jobs SET payload=? WHERE id=?', (self._ai_json(payload), job_id))
            return True

    def ai_job_validate_citations(self, job_id, token, chunk_ids):
        self._ai_validate_source_ids(chunk_ids)
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, time.time())
            if not job or job['kind'] not in ('turn', 'preview', 'sections'):
                conflict('This answer is no longer authorized')
            remote_ids = [x for x in chunk_ids if isinstance(x,str) and x.startswith('mcp:')]
            if remote_ids and job['kind']!='turn':conflict('Remote sources are only available in chat')
            remote = self._mcp_evidence_rows(c,job_id,token,remote_ids)
            rows = self._ai_citation_rows(c, job['project_id'], [x for x in chunk_ids if x not in remote_ids])
            if job['kind'] == 'sections' and any(row['source_id'] != job['resource_id'] for row in rows):
                conflict('Sections must cite the selected source')
            return {'knowledge_version': json.loads(job['authority'])['knowledge_version'], 'passages': rows, 'tool_sources':remote}

    def ai_provider_access_lost(self, job_id, token):
        """Legacy denial seam, scoped to the attempt's key or its default entry."""
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, time.time())
            if not job or job['kind'] not in ('turn', 'preview', 'sections'):
                return False
            key = self._ai_bound_key(c, job) or self._ai_pool_key(c, job['project_id'])
            group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (key['quota_group_id'],)).fetchone()
            self._ai_provider_health(c, key, group, 'access_denied', time.time(), None, job=job, admin=True)
            c.execute("UPDATE ai_jobs SET state='failed',error='unavailable',lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?", (now(), job_id))
            self._ai_turn_failed(c, job)
            self._ai_shared_evidence_changed(c,key['id'],exclude_pid=job['project_id'])
            self._ai_provider_health_projection(c, job['project_id'])
            return True

    def ai_commit_reply(self, job_id, token, body, citation_ids, knowledge_version, *, policy_outcome=None, answer_outcome='answered'):
        if answer_outcome not in ('answered', 'insufficient', 'evidence_rejected', 'check_unavailable', 'action', 'refuse', 'clarify', 'greeting', 'unavailable'):
            raise Error(400, 'invalid_input', 'Invalid answer outcome')
        self._ai_validate_source_ids(citation_ids)
        if not isinstance(body, str) or not body.strip() or len(body) > 4000:
            raise Error(400, 'invalid_input', 'An AI reply must contain at most 4,000 characters')
        reply_hash = hashlib.sha256(self._ai_json([body, citation_ids, knowledge_version], 32768).encode()).hexdigest()
        with self.transaction(True) as c:
            turn = c.execute('SELECT * FROM ai_turns WHERE job_id=?', (job_id,)).fetchone()
            if not turn:
                return None
            if turn['reply_message_id']:
                if turn['reply_lease_token'] != token:
                    return None
                if turn['reply_hash'] != reply_hash:
                    conflict('This turn already committed a different reply')
                original = c.execute('SELECT project_id,authority FROM ai_jobs WHERE id=?', (job_id,)).fetchone()
                authority = json.loads(original['authority'])
                try:
                    current = self.chat_authorize(c, turn['conversation_id'], authority['principal'])
                except Error:
                    return None
                if current['project_id'] != original['project_id'] or current['customer_id'] != authority['customer_id']:
                    return None
                self._ai_citation_rows(c, original['project_id'], [x for x in citation_ids if not x.startswith('mcp:')])
                # An exact committed retry returns its saved snapshot, not expired attempt memory.
                return self.chat_message_view(c.execute('SELECT * FROM chat_messages WHERE id=?', (turn['reply_message_id'],)).fetchone())
            job = self._ai_job_lease(c, job_id, token, time.time())
            if not job or job['kind'] != 'turn':
                return None
            receipt = self._ai_topic_receipt(c, job)
            if policy_outcome is not None:
                if (not receipt or receipt['outcome'] != policy_outcome or policy_outcome == 'allow'
                        or receipt['body'] != body or citation_ids):
                    conflict('This is not the saved topic reply')
            else:
                self._ai_topic_require_allow(c, job)
            if json.loads(job['authority'])['knowledge_version'] != knowledge_version:
                conflict('Knowledge changed while this answer was prepared')
            from feedback.ai_retrieval import _passage
            citations = [_passage(row) for row in self._ai_citation_rows(c, job['project_id'], [x for x in citation_ids if not x.startswith('mcp:')])]
            tool_sources = self._mcp_evidence_rows(c,job_id,token,[x for x in citation_ids if x.startswith('mcp:')])
            row = c.execute('SELECT * FROM chat_conversations WHERE id=?', (turn['conversation_id'],)).fetchone()
            mid, ts, seq = new_id(), now(), row['last_sequence'] + 1
            c.execute("INSERT INTO chat_messages(id,conversation_id,sequence,client_message_id,sender,sender_id,body,created_at,citations) VALUES(?,?,?,?,'ai',?,?,?,?)", (mid, row['id'], seq, job_id, 'ai:' + job['project_id'], body, ts, self._ai_json(citations, 131072)))
            c.execute('UPDATE chat_messages SET tool_sources=?,context=(SELECT context FROM chat_messages WHERE id=?) WHERE id=?',(self._ai_json(tool_sources,MAX_TOOL_EVIDENCE_BYTES),job['resource_id'],mid))
            c.execute('UPDATE chat_conversations SET last_sequence=?,updated_at=? WHERE id=?', (seq, ts, row['id']))
            c.execute('UPDATE ai_turns SET reply_message_id=?,reply_lease_token=?,reply_hash=? WHERE job_id=?', (mid, token, reply_hash, job_id))
            job_payload = json.loads(job['payload']) | {'answer_outcome': answer_outcome}
            c.execute("UPDATE ai_jobs SET state='succeeded',error=NULL,lease_token=NULL,lease_until=NULL,updated_at=?,payload=? WHERE id=?", (ts, self._ai_json(job_payload), job_id))
            project = c.execute('SELECT identifier FROM projects WHERE id=?', (row['project_id'],)).fetchone()
            payload = {'id': mid, 'type': 'chat.message_created', 'schema_version': 2, 'occurred_at': ts,
                       'project': {'id': row['project_id'], 'identifier': project['identifier']}, 'conversation': {'id': row['id']},
                       'message': {'id': mid, 'sequence': seq, 'sender_role': 'ai'}, 'actor': {'kind': 'ai', 'user_id': None}}
            self._persist_event(c, row['project_id'], None, None, payload)
            return self.chat_message_view(c.execute('SELECT * FROM chat_messages WHERE id=?', (mid,)).fetchone())

    def _ai_section_material(self, c, project_id, source_id, coverage=None):
        query = 'SELECT * FROM (' + self._ai_eligible_sql(False) + ') AS eligible WHERE source_id=?'
        parameters = (project_id, self._ai_json(self._ai_embedding_profile(c)), source_id)
        total = c.execute('SELECT count(*) FROM (' + query + ') AS selected', parameters).fetchone()[0]
        scope = coverage or {}
        page_ids = scope.get('page_ids')
        if page_ids:
            query += ' AND page_id IN (' + ','.join('?' for _ in page_ids) + ')'
            parameters += tuple(page_ids)
        rows = c.execute(query + ' ORDER BY chunk_rowid LIMIT ?', (*parameters, 10000 if scope.get('scope') == 'all' else 20)).fetchall()
        fields = ('id', 'source_id', 'revision_id', 'kind', 'source_name', 'title', 'content', 'page_url')
        return [{k: row[k] for k in fields} for row in rows], {'selected_passages': len(rows), 'total_passages': total}

    def _ai_retained_citations(self, c, project_id, chunk_ids):
        if not chunk_ids:
            return []
        rows = c.execute('''SELECT k.id,k.revision_id,r.source_id,s.kind,s.name source_name,k.title,k.content,p.url page_url
            FROM ai_chunks k JOIN ai_revisions r ON r.id=k.revision_id AND r.project_id=k.project_id
            JOIN ai_sources s ON s.id=r.source_id AND s.project_id=r.project_id
            LEFT JOIN ai_documents d ON d.id=r.document_id AND d.project_id=r.project_id
            LEFT JOIN ai_crawl_pages p ON p.id=k.page_id AND p.run_id=d.crawl_run_id AND p.status='fetched' AND p.url=k.location
            WHERE k.project_id=? AND k.id IN (''' + ','.join('?' for _ in chunk_ids) + ')', (project_id, *chunk_ids)).fetchall()
        found = {row['id']: dict(row) for row in rows}
        return [found[cid] for cid in chunk_ids if cid in found]

    def _ai_authoring_stale(self, c, run):
        if run['knowledge_version'] != self._ai_knowledge_version(c, run['project_id']) or run['config_version'] != self._ai_config(c, run['project_id'])['version']:
            return True
        if run['source_id']:
            source = self._ai_source(c, run['project_id'], run['source_id'])
            return bool(source['deleted_at'] or not source['active'] or not source['approved'] or source['version'] != run['source_version'])
        return False

    def _ai_section_view(self, c, section, run=None):
        if run is None:
            run = c.execute('SELECT * FROM ai_authoring_runs WHERE id=?', (section['run_id'],)).fetchone()
        result = {k: section[k] for k in ('id', 'run_id', 'kind', 'title', 'body', 'category', 'version', 'deleted_at', 'created_at', 'updated_at')}
        result['citation_ids'] = json.loads(section['citation_ids'])
        result['citations'] = self._ai_retained_citations(c, section['project_id'], result['citation_ids'])
        result['stale'] = not self._ai_section_evidence_current(c, section)
        publication = c.execute('SELECT source_id FROM ai_section_publications WHERE section_id=? ORDER BY created_at DESC LIMIT 1', (section['id'],)).fetchone()
        result['published_source_id'] = publication[0] if publication else None
        return result

    def _ai_authoring_view(self, c, run):
        job = c.execute('SELECT state,error FROM ai_jobs WHERE id=?', (run['job_id'],)).fetchone()
        result = {k: run[k] for k in ('id', 'project_id', 'kind', 'config_version', 'knowledge_version', 'source_id', 'source_version', 'query', 'version', 'created_at', 'finished_at')}
        result.update(status=job['state'], error=job['error'], result=json.loads(run['result']) if run['result'] else None,
                      coverage=json.loads(run['coverage']) if run['coverage'] else None, stale=self._ai_authoring_stale(c, run))
        if job['state'] == 'failed' and (result['coverage'] or {}).get('completed_batches', 0):
            result['status'] = 'partial'
        result['sections'] = [self._ai_section_view(c, row, run) for row in c.execute('SELECT * FROM ai_sections WHERE run_id=? ORDER BY created_at,id LIMIT 100', (run['id'],))]
        result['section_count'] = c.execute('SELECT count(*) FROM ai_sections WHERE run_id=? AND deleted_at IS NULL', (run['id'],)).fetchone()[0]
        if (result['coverage'] or {}).get('scope') == 'all' and run['kind'] == 'sections' and result['result']:
            originals = c.execute('SELECT s.kind,v.* FROM ai_sections s JOIN ai_section_versions v ON v.section_id=s.id AND v.version=1 WHERE s.run_id=? ORDER BY s.created_at,s.id LIMIT 100', (run['id'],)).fetchall()
            result['result']['sections'] = [{k: (json.loads(r[k]) if k == 'citation_ids' else r[k]) for k in ('kind','title','body','category','citation_ids')} for r in originals]
        ids = result['result'].get('citation_ids', []) if result['result'] else []
        if result['result'] and run['kind'] == 'sections':
            ids = list(dict.fromkeys(cid for section in result['result']['sections'] for cid in section['citation_ids']))
        result['citations'] = self._ai_retained_citations(c, run['project_id'], ids)
        return result

    def ai_authoring_begin(self, project, principal, client_id, kind, config_version, *, query=None, source_id=None, source_version=None, page_ids=None):
        if page_ids is not None and (not isinstance(page_ids, list) or not 1 <= len(page_ids) <= 50 or any(not isinstance(x, str) for x in page_ids) or len(set(page_ids)) != len(page_ids)):
            raise Error(400, 'invalid_input', 'Choose up to 50 distinct source pages')
        if kind not in ('preview', 'sections') or not isinstance(client_id, str) or not 1 <= len(client_id) <= 100 or type(config_version) is not int:
            raise Error(400, 'invalid_input', 'Invalid generation request')
        if kind == 'preview' and (not isinstance(query, str) or not query.strip() or len(query) > 1000 or source_id is not None or source_version is not None):
            raise Error(400, 'invalid_input', 'Use a preview question of at most 1,000 characters')
        if kind == 'sections' and (query is not None or not isinstance(source_id, str) or type(source_version) is not int):
            raise Error(400, 'invalid_input', 'Choose a current source to generate sections')
        request_hash = hashlib.sha256(self._ai_json([kind, config_version, query, source_id, source_version] + ([page_ids] if page_ids is not None else [])).encode()).hexdigest()
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            previous = c.execute('SELECT * FROM ai_authoring_runs WHERE project_id=? AND client_id=?', (pid, client_id)).fetchone()
            if previous:
                if previous['request_hash'] != request_hash:
                    conflict('This generation key already describes different work')
                return self._ai_authoring_view(c, previous)
            config = self._ai_config(c, pid)
            if config['version'] != config_version:
                conflict('AI settings changed; reload before generating content')
            authority = {'principal': {k: principal[k] for k in ('kind', 'user_id', 'session_id')}, 'knowledge_version': self._ai_knowledge_version(c, pid)}
            coverage = None
            if kind == 'sections':
                source = self._ai_source(c, pid, source_id)
                if source['version'] != source_version:
                    conflict('This source changed; reload it before generating sections')
                if source['deleted_at'] or not source['active']:
                    raise Error(409, 'ai_not_ready', 'Activate this source before generating sections.')
                if not source['approved']:
                    raise Error(409, 'ai_not_ready', 'Review the original source and approve it for AI answers before generating sections.')
                if not source['active_revision_id']:
                    raise Error(409, 'ai_not_ready', 'Wait for this source to finish crawling and indexing before generating sections.')
                scope = {'scope': 'all', 'page_ids': page_ids}
                if page_ids:
                    valid = {r[0] for r in c.execute('SELECT DISTINCT page_id FROM ai_chunks WHERE revision_id=? AND project_id=?', (source['active_revision_id'], pid))}
                    if not set(page_ids) <= valid:
                        conflict('Selected pages no longer belong to this source index')
                passages, coverage = self._ai_section_material(c, pid, source_id, scope)
                coverage.update(scope)
                if not passages:
                    conflict('The selected source has no compatible approved passages')
                coverage.update(batch_size=5, total_batches=(len(passages) + 4) // 5, completed_batches=0, completed_passages=0)
                authority['source_version'] = source_version
            self._ai_require_ready(c, config, authoring=True)
            if c.execute('SELECT count(*) FROM ai_authoring_runs WHERE project_id=? AND created_at>=?', (pid, now()[:10])).fetchone()[0] >= 1000:
                raise Error(429, 'rate_limited', 'Please wait before starting more generations')
            run_id, ts = new_id(), now()
            job = self._ai_enqueue(c, pid, kind, source_id or pid, 'authoring:' + client_id, config_version, authority, {'run_id': run_id})
            c.execute('''INSERT INTO ai_authoring_runs(id,project_id,job_id,kind,client_id,request_hash,config_version,knowledge_version,source_id,source_version,query,coverage,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', (run_id, pid, job['id'], kind, client_id, request_hash, config_version, authority['knowledge_version'], source_id, source_version, query, self._ai_json(coverage) if coverage else None, ts))
            return self._ai_authoring_view(c, c.execute('SELECT * FROM ai_authoring_runs WHERE id=?', (run_id,)).fetchone())

    def ai_authoring_runs(self, project, principal, kind=None, limit=25, offset=0):
        if kind not in (None, 'preview', 'sections') or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise Error(400, 'invalid_input', 'Invalid generation page')
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            rows = c.execute('SELECT * FROM ai_authoring_runs WHERE project_id=? AND (? IS NULL OR kind=?) ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (pid, kind, kind, limit + 1, offset)).fetchall()
            return {'items': [self._ai_authoring_view(c, row) for row in rows[:limit]], 'has_more': len(rows) > limit, 'limit': limit, 'offset': offset}

    def ai_authoring_run(self, project, principal, run_id):
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            row = c.execute('SELECT * FROM ai_authoring_runs WHERE id=? AND project_id=?', (run_id, pid)).fetchone()
            if not row:
                missing()
            return self._ai_authoring_view(c, row)

    def ai_authoring_cancel(self, project, principal, run_id, version):
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            row = c.execute('SELECT * FROM ai_authoring_runs WHERE id=? AND project_id=?', (run_id, pid)).fetchone()
            if not row:
                missing()
            if type(version) is not int or row['version'] != version:
                conflict()
            job = c.execute('SELECT state FROM ai_jobs WHERE id=?', (row['job_id'],)).fetchone()
            if job['state'] in ('queued', 'running'):
                self._ai_cancel_job(c, row['job_id'])
                c.execute('UPDATE ai_authoring_runs SET version=version+1,finished_at=? WHERE id=?', (now(), run_id))
            return self._ai_authoring_view(c, c.execute('SELECT * FROM ai_authoring_runs WHERE id=?', (run_id,)).fetchone())

    def ai_authoring_retry(self, project, principal, run_id, version, client_id):
        if type(version) is not int or not isinstance(client_id, str) or not 1 <= len(client_id) <= 100:
            raise Error(400, 'invalid_input', 'Invalid generation retry')
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            run = c.execute('SELECT * FROM ai_authoring_runs WHERE id=? AND project_id=?', (run_id, pid)).fetchone()
            if not run:
                missing()
            previous = c.execute("SELECT * FROM ai_jobs WHERE project_id=? AND kind='sections' AND dedupe_key=?", (pid, 'authoring-retry:' + client_id)).fetchone()
            if previous:
                if json.loads(previous['payload']) != {'run_id': run_id, 'retry_version': version}:
                    conflict('This retry key already describes different work')
                return self._ai_authoring_view(c, run)
            if run['version'] != version:
                conflict()
            old_job = c.execute('SELECT * FROM ai_jobs WHERE id=?', (run['job_id'],)).fetchone()
            coverage = json.loads(run['coverage']) if run['coverage'] else {}
            if run['kind'] != 'sections' or old_job['state'] != 'failed' or not coverage.get('batch_size'):
                conflict('Only unfinished batched section runs can be retried')
            if self._ai_authoring_stale(c, run):
                conflict('Settings or knowledge changed. Start a new generation; earlier drafts remain in history.')
            config = self._ai_config(c, pid)
            self._ai_require_ready(c, config, authoring=True)
            if c.execute("SELECT count(*) FROM ai_jobs WHERE project_id=? AND kind='sections' AND created_at>=?", (pid, now()[:10])).fetchone()[0] >= 1000:
                raise Error(429, 'rate_limited', 'Please wait before retrying more generations')
            authority = {'principal': {k: principal[k] for k in ('kind', 'user_id', 'session_id')},
                         'knowledge_version': run['knowledge_version'], 'source_version': run['source_version']}
            job = self._ai_enqueue(c, pid, 'sections', run['source_id'], 'authoring-retry:' + client_id,
                config['version'], authority, {'run_id': run_id, 'retry_version': version})
            # Old jobs and their paid receipts remain immutable historical work.
            # The failed job cannot hold a live lease; a retry receives a new one.
            c.execute('UPDATE ai_authoring_runs SET job_id=?,version=version+1,finished_at=NULL WHERE id=?', (job['id'], run_id))
            return self._ai_authoring_view(c, c.execute('SELECT * FROM ai_authoring_runs WHERE id=?', (run_id,)).fetchone())

    @staticmethod
    def _ai_section_data(value):
        if not isinstance(value, dict) or set(value) != {'kind', 'title', 'body', 'category', 'citation_ids'} or value['kind'] not in ('section', 'faq'):
            raise Error(400, 'invalid_input', 'Invalid derived section')
        if any(not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > bound for key, bound in (('title', 200), ('body', 4000), ('category', 100))):
            raise Error(400, 'invalid_input', 'Invalid derived section text')
        if not isinstance(value['citation_ids'], list) or not 1 <= len(value['citation_ids']) <= MAX_SOURCES or any(not isinstance(cid, str) or not 1 <= len(cid) <= 100 for cid in value['citation_ids']) or len(set(value['citation_ids'])) != len(value['citation_ids']):
            raise Error(400, 'invalid_input', 'Derived sections must cite original evidence')
        return value

    @staticmethod
    def _ai_section_snapshot(c, section_id):
        c.execute('INSERT INTO ai_section_versions SELECT id,version,title,body,category,citation_ids,deleted_at,updated_at FROM ai_sections WHERE id=?', (section_id,))

    def ai_authoring_finish(self, job_id, token, result, citation_ids, *, batch_index=None):
        self._ai_json(result, 131072)
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, time.time())
            if not job or job['kind'] not in ('preview', 'sections'):
                return False
            run = c.execute('SELECT * FROM ai_authoring_runs WHERE job_id=?', (job_id,)).fetchone()
            if not run or not isinstance(result, dict) or result.get('knowledge_version') != run['knowledge_version']:
                conflict('This result no longer matches its knowledge generation')
            if job['kind'] == 'preview':
                if set(result) != {'body', 'citation_ids', 'knowledge_version', 'status'} or result['status'] not in ('answered', 'insufficient', 'refuse', 'clarify', 'greeting', 'unavailable') or not isinstance(result['body'], str) or not result['body'].strip() or len(result['body']) > 12000:
                    raise Error(400, 'invalid_input', 'Invalid preview result')
                expected_ids = result['citation_ids']
                if (result['status'] == 'answered' and not expected_ids) or (result['status'] == 'insufficient' and expected_ids):
                    raise Error(400, 'invalid_input', 'Invalid preview citations')
                if result['status'] in ('refuse', 'clarify', 'greeting', 'unavailable'):
                    receipt = self._ai_topic_receipt(c, job)
                    if not receipt or receipt['outcome'] != result['status'] or receipt['body'] != result['body'] or expected_ids:
                        conflict('This is not the saved topic preview')
                else:
                    self._ai_topic_require_allow(c, job)
            else:
                if set(result) != {'sections', 'knowledge_version'} or not isinstance(result['sections'], list) or len(result['sections']) > 12:
                    raise Error(400, 'invalid_input', 'Invalid generated sections')
                values = [self._ai_section_data(value) for value in result['sections']]
                expected_ids = list(dict.fromkeys(cid for value in values for cid in value['citation_ids']))
            if expected_ids != citation_ids:
                conflict('Result citations differ from the validated references')
            rows = self._ai_citation_rows(c, job['project_id'], citation_ids)
            if job['kind'] == 'sections' and any(row['source_id'] != run['source_id'] for row in rows):
                conflict('Derived sections must cite the selected source')
            coverage = json.loads(run['coverage']) if run['coverage'] else None
            if job['kind'] == 'sections' and (coverage or {}).get('batch_size') and batch_index is None:
                raise Error(400, 'invalid_input', 'Specify the section batch being committed')
            complete = True
            snapshot = result
            if batch_index is not None:
                if job['kind'] != 'sections' or type(batch_index) is not int or not coverage or not coverage.get('batch_size') or batch_index != coverage['completed_batches'] or batch_index >= coverage['total_batches']:
                    conflict('This section batch has already finished or is out of order')
                if len(values) > 3:
                    raise Error(400, 'invalid_input', 'A section batch can produce at most three drafts')
                passages, _ = self._ai_section_material(c, job['project_id'], run['source_id'], coverage)
                batch = passages[batch_index * coverage['batch_size']:(batch_index + 1) * coverage['batch_size']]
                if not batch or any(cid not in {p['id'] for p in batch} for cid in citation_ids):
                    conflict('Section references must belong to this batch')
                coverage['completed_batches'] += 1
                coverage['completed_passages'] += len(batch)
                complete = coverage['completed_batches'] == coverage['total_batches']
                earlier = json.loads(run['result'])['sections'] if run['result'] and coverage.get('scope') != 'all' else []
                snapshot = result | {'sections': earlier + values}
            ts = now()
            if job['kind'] == 'sections':
                for value in values:
                    sid = new_id()
                    c.execute('INSERT INTO ai_sections(id,run_id,project_id,kind,title,body,category,citation_ids,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', (sid, run['id'], job['project_id'], value['kind'], value['title'], value['body'], value['category'], self._ai_json(value['citation_ids']), ts, ts))
                    self._ai_section_snapshot(c, sid)
            c.execute('UPDATE ai_authoring_runs SET result=?,coverage=?,version=version+1,finished_at=? WHERE id=?',
                (self._ai_json(snapshot, 262144 if batch_index is not None else 131072), self._ai_json(coverage) if coverage else None, ts if complete else None, run['id']))
            if complete:
                c.execute("UPDATE ai_jobs SET state='succeeded',error=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?", (ts, job_id))
            return True

    def ai_section_update(self, project, principal, section_id, version, data):
        if not isinstance(data, dict) or set(data) != {'title', 'body', 'category', 'citation_ids'}:
            raise Error(400, 'invalid_input', 'Supply every editable section field')
        self._ai_json(data, 32768)
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            section = c.execute('SELECT * FROM ai_sections WHERE id=? AND project_id=?', (section_id, pid)).fetchone()
            if not section:
                missing()
            run = c.execute('SELECT * FROM ai_authoring_runs WHERE id=?', (section['run_id'],)).fetchone()
            if type(version) is not int or section['version'] != version or section['deleted_at'] or not self._ai_section_evidence_current(c, section):
                conflict('The section or its source evidence changed')
            self._ai_section_data(data | {'kind': section['kind']})
            rows = self._ai_citation_rows(c, pid, data['citation_ids'])
            if any(row['source_id'] != run['source_id'] for row in rows):
                conflict('Sections must cite the selected source')
            if all(data[key] == (json.loads(section[key]) if key == 'citation_ids' else section[key]) for key in data):
                return self._ai_section_view(c, section, run)
            c.execute('UPDATE ai_sections SET title=?,body=?,category=?,citation_ids=?,version=version+1,updated_at=? WHERE id=?', (data['title'], data['body'], data['category'], self._ai_json(data['citation_ids']), now(), section_id))
            self._ai_section_snapshot(c, section_id)
            return self._ai_section_view(c, c.execute('SELECT * FROM ai_sections WHERE id=?', (section_id,)).fetchone(), run)

    def _ai_section_evidence_current(self, c, section):
        try:
            self._ai_citation_rows(c, section['project_id'], json.loads(section['citation_ids']))
            return True
        except Error:
            return False

    def ai_authoring_continue(self, job_id, token):
        """Checkpoint long source authoring into another bounded job, retaining one run."""
        with self.transaction(True) as c:
            job = self._ai_job_lease(c, job_id, token, time.time())
            if not job or job['kind'] != 'sections':
                return False
            run = c.execute('SELECT * FROM ai_authoring_runs WHERE job_id=?', (job_id,)).fetchone()
            coverage = json.loads(run['coverage'])
            if coverage['completed_batches'] >= coverage['total_batches']:
                return False
            c.execute("UPDATE ai_jobs SET state='succeeded',lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?", (now(), job_id))
            next_job = self._ai_enqueue(c, job['project_id'], 'sections', run['source_id'],
                'authoring-next:' + job_id + ':' + str(coverage['completed_batches']), job['config_version'],
                json.loads(job['authority']), {'run_id': run['id']})
            c.execute('UPDATE ai_authoring_runs SET job_id=?,version=version+1 WHERE id=?', (next_job['id'], run['id']))
            return True

    def ai_sections_list(self, project, principal, query='', limit=25, offset=0):
        if not isinstance(query, str) or len(query)>200 or type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or not 0<=offset<=1000000:
            raise Error(400, 'invalid_input', 'Invalid draft page')
        with self.transaction() as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            rows = c.execute(f"SELECT * FROM ai_sections WHERE project_id=? AND deleted_at IS NULL AND ({self._text_contains_sql('title')} OR {self._text_contains_sql('body')}) ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (pid, query, query, limit+1, offset)).fetchall()
            return {'items': [self._ai_section_view(c,r) for r in rows[:limit]], 'has_more':len(rows)>limit,'limit':limit,'offset':offset}

    def ai_section_publish(self, project, principal, section_id, version):
        """Explicit human approval creates an independent original with immutable provenance."""
        if type(version) is not int or version < 1:
            raise Error(400, 'invalid_input', 'Use the current draft version')
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            section = c.execute('SELECT * FROM ai_sections WHERE id=? AND project_id=?', (section_id,pid)).fetchone()
            if not section:
                missing()
            old = c.execute('SELECT source_id FROM ai_section_publications WHERE section_id=? AND section_version=?', (section_id,version)).fetchone()
            if old:
                return self._ai_knowledge_source_view(c,pid,old[0])
            if type(version) is not int or section['version']!=version or section['deleted_at'] or not self._ai_section_evidence_current(c,section):
                conflict('Review the current draft and its source evidence before approving')
            if c.execute('SELECT count(*) FROM ai_sources WHERE project_id=? AND deleted_at IS NULL',(pid,)).fetchone()[0]>=100:
                raise Error(409,'source_limit','Project knowledge source limit reached')
            sid,did,ts=new_id(),new_id(),now()
            content='# '+section['title']+'\n\n'+section['body']
            client_id='draft:'+section_id+':'+str(version)
            c.execute("INSERT INTO ai_sources(id,project_id,kind,name,approved,created_at,updated_at) VALUES(?,?,'markdown',?,1,?,?)",(sid,pid,section['title'],ts,ts))
            job=self._ai_document_job(c,pid,principal,sid,did,'document:'+client_id)
            content_hash=hashlib.sha256(content.encode()).hexdigest()
            c.execute("INSERT INTO ai_documents(id,source_id,project_id,source_version,revision_number,filename,title,content,content_hash,byte_count,created_by,created_at,client_id,request_hash,job_id) VALUES(?,?,?,1,1,'reviewed-answer.md',?,?,?,?,?,?,?,?,?)",(did,sid,pid,section['title'],content,content_hash,len(content.encode()),principal['user_id'],ts,client_id,content_hash,job['id']))
            c.execute('UPDATE ai_sources SET latest_document_id=? WHERE id=?',(did,sid))
            c.execute('INSERT INTO ai_section_publications(section_id,section_version,source_id,approved_by,created_at) VALUES(?,?,?,?,?)',(section_id,version,sid,principal['user_id'],ts))
            return self._ai_knowledge_source_view(c,pid,sid)

    def ai_section_delete(self, project, principal, section_id, version):
        with self.transaction(True) as c:
            self._ai_admin(c, principal)
            pid = self._ai_project(c, project)
            section = c.execute('SELECT * FROM ai_sections WHERE id=? AND project_id=?', (section_id, pid)).fetchone()
            if not section:
                missing()
            if type(version) is not int or section['version'] != version:
                conflict()
            if not section['deleted_at']:
                c.execute('UPDATE ai_sections SET deleted_at=?,updated_at=?,version=version+1 WHERE id=?', (now(), now(), section_id))
                self._ai_section_snapshot(c, section_id)
            return self._ai_section_view(c, c.execute('SELECT * FROM ai_sections WHERE id=?', (section_id,)).fetchone())

    @staticmethod
    def _ai_report_text(title, description):
        if not isinstance(title, str) or not title.strip() or len(title) > 200 or not isinstance(description, str) or len(description) > 20000:
            raise Error(400, 'invalid_input', 'Use a title of 1–200 characters and a description of at most 20,000 characters')
        try:
            (title + description).encode('utf-8')
        except UnicodeError:
            raise Error(400, 'invalid_input', 'Use valid text for this report') from None
        if any(ord(ch) < 32 and ch not in '\n\t' or ord(ch) == 127 for ch in title + description):
            raise Error(400, 'invalid_input', 'Use readable text for this report')

    def _ai_tool_job(self, c, job_id, token, name):
        if name not in default_ai_settings()['tools']:
            raise Error(400, 'tool_not_enabled', 'This assistant operation is unavailable')
        job = self._ai_job_lease(c, job_id, token, time.time())
        if not job or job['kind'] != 'turn':
            conflict('This assistant operation is no longer authorized')
        self._ai_topic_require_allow(c, job)
        config = self._ai_config(c, job['project_id'])
        if not config['settings']['enabled'] or config['settings']['tools'].get(name) is not True:
            raise Error(403, 'tool_not_enabled', 'This assistant operation is disabled')
        return job, config

    def ai_tool_context(self, job_id, token, name):
        with self.transaction(True) as c:
            job, config = self._ai_tool_job(c, job_id, token, name)
            authority = json.loads(job['authority'])
            customer = self._chat_customer_authorize(c, authority['principal'])
            return {'job': self._ai_job_view(job), 'settings': config['settings'],
                    'conversation_id': authority['conversation_id'], 'customer_kind': customer['kind']}

    def ai_tool_source(self, job_id, token, chunk_id):
        with self.transaction(True) as c:
            job, _ = self._ai_tool_job(c, job_id, token, 'support_link')
            from feedback.ai_retrieval import _passage
            return _passage(self._ai_citation_rows(c, job['project_id'], [chunk_id])[0])

    def ai_tool_request_human(self, job_id, token, tool_call_id):
        if not isinstance(tool_call_id, str) or not 1 <= len(tool_call_id) <= 200:
            raise Error(400, 'invalid_input', 'Invalid assistant operation receipt')
        with self.transaction(True) as c:
            job, _ = self._ai_tool_job(c, job_id, token, 'request_human')
            authority = json.loads(job['authority'])
            conversation = self.chat_authorize(c, authority['conversation_id'], authority['principal'])
            self._chat_set_mode(c, conversation, 'waiting_human', {'kind': 'ai', 'user_id': None}, 'assistant_requested')
            return {'support_mode': 'waiting_human', 'mode_version': conversation['mode_version'] + 1}

    def _ai_report_owner(self, c, conversation_id, principal):
        if not isinstance(principal, dict) or principal.get('kind') not in ('visitor', 'customer'):
            raise Error(403, 'forbidden', 'A current customer chat credential is required')
        conversation = self.chat_authorize(c, conversation_id, principal)
        customer = self._chat_customer_authorize(c, principal)
        if customer['id'] != conversation['customer_id'] or customer['project_id'] != conversation['project_id']:
            missing()
        if customer['kind'] == 'anonymous' and (principal['kind'] != 'visitor' or not principal.get('origin')):
            raise Error(403, 'forbidden', 'Guest reports require the original website chat credential')
        return conversation, customer

    def _ai_report_policy(self, c, conversation, customer, kind, *, submission=False):
        config = self._ai_config(c, conversation['project_id'])
        tool = 'create_feature_request' if kind == 'feature' else 'create_bug_report'
        if (conversation['state'] != 'open' or conversation['support_mode'] != 'ai'
                or config['settings']['enabled'] is not True or config['settings']['tools'].get(tool) is not True):
            raise Error(409, 'report_unavailable', 'This report action is no longer enabled for this conversation')
        if submission and customer['kind'] == 'anonymous' and config['settings']['allow_guest_reports'] is not True:
            raise Error(403, 'guest_reports_disabled', 'Open the feedback page to submit this draft')
        return config

    def _ai_proposal_owner(self, c, conversation_id, principal, proposal_id):
        conversation, customer = self._ai_report_owner(c, conversation_id, principal)
        proposal = c.execute('SELECT * FROM ai_proposals WHERE id=? AND conversation_id=? AND project_id=? AND customer_id=?', (proposal_id, conversation_id, conversation['project_id'], customer['id'])).fetchone()
        if not proposal:
            missing()
        if customer['kind'] == 'anonymous':
            authority = json.loads(proposal['authority']) if proposal['authority'] else {}
            original = authority.get('principal', {})
            if original.get('session_id') != principal['session_id'] or original.get('origin') != principal.get('origin'):
                missing()
        return proposal, conversation, customer

    def _ai_proposal_current(self, c, proposal, conversation, customer, *, submission=False):
        config = self._ai_report_policy(c, conversation, customer, proposal['kind'], submission=submission)
        if (proposal['state'] != 'pending' or proposal['expires_at'] is None or proposal['expires_at'] <= time.time()
                or proposal['config_version'] != config['version'] or proposal['mode_version'] != conversation['mode_version']):
            conflict('This draft changed, expired or is no longer available for confirmation')
        authority = json.loads(proposal['authority']) if proposal['authority'] else {}
        original_principal = authority.get('principal', {})
        if original_principal.get('kind') not in ('visitor', 'customer') or not original_principal.get('session_id'):
            conflict('This draft has no current customer authority')
        original = self._chat_customer_authorize(c, original_principal)
        if original['id'] != customer['id'] or original['project_id'] != conversation['project_id']:
            missing()
        if config['settings'][proposal['kind'] + '_board_id'] != proposal['board_id'] or not c.execute('SELECT 1 FROM boards WHERE id=? AND project_id=? AND archived_at IS NULL', (proposal['board_id'], conversation['project_id'])).fetchone():
            conflict('The configured feedback destination changed')
        return config

    def _ai_feedback_destination(self, c, project_id, kind, board_id):
        settings = self._ai_config(c, project_id)['settings']
        from urllib.parse import quote
        custom = settings[kind + '_feedback_url'] or settings['feedback_url']
        return custom or self.base_url.rstrip('/') + '/boards/' + quote(board_id, safe=''), bool(custom)

    @staticmethod
    def _ai_draft_hash(proposal):
        content = [proposal[key] for key in ('id', 'kind', 'board_id', 'title', 'description', 'version', 'config_version', 'mode_version')]
        return hashlib.sha256(json.dumps(content, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()

    @staticmethod
    def _ai_report_message_sequence(c, proposal_id):
        row = c.execute('''SELECT m.sequence FROM ai_proposals p
            JOIN ai_turns t ON t.job_id=p.job_id
            JOIN chat_messages m ON m.id=COALESCE(t.reply_message_id,t.trigger_message_id)
            AND m.conversation_id=p.conversation_id WHERE p.id=?''', (proposal_id,)).fetchone()
        return row[0] if row else None

    def _ai_proposal_view(self, c, proposal, conversation, customer):
        eligible = False
        try:
            self._ai_proposal_current(c, proposal, conversation, customer)
            eligible = True
        except (Error, KeyError, TypeError, ValueError):
            pass
        settings = self._ai_config(c, proposal['project_id'])['settings']
        guest = customer['kind'] == 'anonymous'
        board = c.execute('SELECT id,name FROM boards WHERE id=? AND project_id=? AND archived_at IS NULL', (proposal['board_id'], proposal['project_id'])).fetchone()
        destination, _ = self._ai_feedback_destination(c, proposal['project_id'], proposal['kind'], proposal['board_id'])
        report = c.execute('SELECT proposal_id FROM ai_reports WHERE proposal_id=?', (proposal['id'],)).fetchone()
        return {key: proposal[key] for key in ('id', 'conversation_id', 'kind', 'title', 'description', 'version', 'state', 'draft_hash')} | {
            'message_sequence': self._ai_report_message_sequence(c, proposal['id']),
            'board': dict(board), 'expires_at': utc_timestamp(proposal['expires_at']) if proposal['expires_at'] else None,
            'can_confirm': eligible and (not guest or settings['allow_guest_reports'] is True), 'can_edit': eligible,
            'guest': guest, 'feedback_url': destination, 'report_id': report['proposal_id'] if report else None,
            'tracking_notice': ("Want to keep track of your request in your account? Sign in and submit it on our feedback page instead. Guest reports stay linked to this browser's chat and won't appear in your account." if guest else None)}

    def ai_tool_prepare_report(self, job_id, token, tool_call_id, kind, title, description):
        self._ai_report_text(title, description)
        if kind not in ('feature', 'bug') or not isinstance(tool_call_id, str) or not 1 <= len(tool_call_id) <= 200:
            raise Error(400, 'invalid_input', 'Invalid report preparation')
        with self.transaction(True) as c:
            tool = 'create_feature_request' if kind == 'feature' else 'create_bug_report'
            job, config = self._ai_tool_job(c, job_id, token, tool)
            authority = json.loads(job['authority'])
            principal = authority['principal']
            conversation, customer = self._ai_report_owner(c, authority['conversation_id'], principal)
            previous = c.execute('SELECT * FROM ai_proposals WHERE job_id=? AND kind=?', (job_id, kind)).fetchone()
            if previous:
                self._ai_proposal_current(c, previous, conversation, customer)
                if previous['title'] != title or previous['description'] != description:
                    conflict('This turn already prepared a different draft; edit the existing draft explicitly')
                return self._ai_proposal_view(c, previous, conversation, customer)
            board_id = config['settings'][kind + '_board_id']
            if not board_id or not c.execute('SELECT 1 FROM boards WHERE id=? AND project_id=? AND archived_at IS NULL', (board_id, job['project_id'])).fetchone():
                raise Error(409, 'destination_unavailable', 'Choose a feedback destination before preparing this report')
            day = now()[:10]
            if (c.execute('SELECT count(*) FROM ai_proposals WHERE customer_id=? AND created_at>=?', (customer['id'], day)).fetchone()[0] >= 30
                    or c.execute('SELECT count(*) FROM ai_proposals WHERE project_id=? AND created_at>=?', (job['project_id'], day)).fetchone()[0] >= 500):
                raise Error(429, 'rate_limited', 'Please wait before preparing more feedback drafts')
            table = 'chat_sessions' if principal['kind'] == 'visitor' else 'chat_customer_sessions'
            credential = c.execute('SELECT expires_at FROM ' + table + ' WHERE id=?', (principal['session_id'],)).fetchone()
            proposal_id, ts = new_id(), now()
            expires = min(time.time() + 1800, credential['expires_at'])
            c.execute('''INSERT INTO ai_proposals(id,project_id,conversation_id,customer_id,kind,board_id,title,description,config_version,mode_version,client_id,created_at,authority,job_id,tool_call_id,expires_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (proposal_id, job['project_id'], conversation['id'], customer['id'], kind, board_id, title, description, job['config_version'], conversation['mode_version'], 'turn:' + job_id + ':' + kind, ts, self._ai_json({'principal': principal}), job_id, tool_call_id, expires, ts))
            proposal = c.execute('SELECT * FROM ai_proposals WHERE id=?', (proposal_id,)).fetchone()
            c.execute('UPDATE ai_proposals SET draft_hash=? WHERE id=?', (self._ai_draft_hash(proposal), proposal_id))
            return self._ai_proposal_view(c, c.execute('SELECT * FROM ai_proposals WHERE id=?', (proposal_id,)).fetchone(), conversation, customer)

    def ai_proposals(self, conversation_id, principal, limit=25, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise Error(400, 'invalid_input', 'Invalid draft page')
        with self.transaction() as c:
            conversation, customer = self._ai_report_owner(c, conversation_id, principal)
            rows = c.execute('SELECT * FROM ai_proposals WHERE conversation_id=? AND customer_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (conversation_id, customer['id'], limit + 1, offset)).fetchall()
            # Every guest draft belongs to the original visitor credential.
            items = []
            for row in rows[:limit]:
                self._ai_proposal_owner(c, conversation_id, principal, row['id'])
                items.append(self._ai_proposal_view(c, row, conversation, customer))
            return {'items': items, 'limit': limit, 'offset': offset, 'has_more': len(rows) > limit}

    def ai_proposal(self, conversation_id, principal, proposal_id):
        with self.transaction() as c:
            proposal, conversation, customer = self._ai_proposal_owner(c, conversation_id, principal, proposal_id)
            return self._ai_proposal_view(c, proposal, conversation, customer)

    def ai_proposal_update(self, conversation_id, principal, proposal_id, version, title, description):
        self._ai_report_text(title, description)
        with self.transaction(True) as c:
            proposal, conversation, customer = self._ai_proposal_owner(c, conversation_id, principal, proposal_id)
            self._ai_proposal_current(c, proposal, conversation, customer)
            if type(version) is not int or proposal['version'] != version:
                conflict()
            if proposal['title'] != title or proposal['description'] != description:
                c.execute('UPDATE ai_proposals SET title=?,description=?,version=version+1,updated_at=? WHERE id=?', (title, description, now(), proposal_id))
                proposal = c.execute('SELECT * FROM ai_proposals WHERE id=?', (proposal_id,)).fetchone()
                c.execute('UPDATE ai_proposals SET draft_hash=? WHERE id=?', (self._ai_draft_hash(proposal), proposal_id))
                if proposal['job_id']:
                    self._ai_cancel_job(c, proposal['job_id'])
            return self._ai_proposal_view(c, c.execute('SELECT * FROM ai_proposals WHERE id=?', (proposal_id,)).fetchone(), conversation, customer)

    def ai_proposal_cancel(self, conversation_id, principal, proposal_id, version):
        with self.transaction(True) as c:
            proposal, conversation, customer = self._ai_proposal_owner(c, conversation_id, principal, proposal_id)
            if type(version) is not int or proposal['version'] != version or proposal['state'] == 'confirmed':
                conflict()
            if proposal['state'] == 'pending':
                c.execute("UPDATE ai_proposals SET state='cancelled',version=version+1,updated_at=? WHERE id=?", (now(), proposal_id))
                if proposal['job_id']:
                    self._ai_cancel_job(c, proposal['job_id'])
            return self._ai_proposal_view(c, c.execute('SELECT * FROM ai_proposals WHERE id=?', (proposal_id,)).fetchone(), conversation, customer)

    @staticmethod
    def _ai_report_actor(c, customer, principal, creator_id):
        if customer['kind'] == 'anonymous':
            return GuestActor(user_id=creator_id, role='participant', ip_address=principal.get('ip_address'))
        session = c.execute('SELECT issuing_key_id FROM chat_customer_sessions WHERE id=?', (principal['session_id'],)).fetchone()
        return Actor(user_id=creator_id, role='participant', project_id=customer['project_id'],
                     key_id=session['issuing_key_id'], permissions=('participate',), ip_address=principal.get('ip_address'))

    def _ai_report_owned(self, c, conversation_id, principal, report_id):
        conversation, customer = self._ai_report_owner(c, conversation_id, principal)
        report = c.execute('''SELECT r.*,p.kind,p.board_id,p.title original_title,p.description original_description
            FROM ai_reports r JOIN ai_proposals p ON p.id=r.proposal_id
            WHERE r.proposal_id=? AND r.conversation_id=? AND r.project_id=? AND r.customer_id=?''',
            (report_id, conversation_id, conversation['project_id'], customer['id'])).fetchone()
        if not report:
            missing()
        if customer['kind'] == 'anonymous':
            if report['guest_session_id'] != principal['session_id'] or report['guest_origin'] != principal.get('origin'):
                missing()
        elif report['guest_session_id'] is not None or report['creator_id'] != customer['user_id']:
            missing()
        return report, conversation, customer

    def _ai_report_view(self, c, report):
        request = c.execute('SELECT r.*,b.visibility board_visibility FROM requests r JOIN boards b ON b.id=r.board_id WHERE r.id=? AND r.creator_id=? AND b.project_id=?',
                            (report['request_id'], report['creator_id'], report['project_id'])).fetchone()
        tombstone = c.execute('SELECT * FROM ai_report_deletions WHERE proposal_id=?', (report['proposal_id'],)).fetchone() if request is None else None
        destination, custom = self._ai_feedback_destination(c, report['project_id'], report['kind'], report['board_id'])
        guest = report['guest_session_id'] is not None
        editable = request is not None and request['accepted_at'] is None
        return {'id': report['proposal_id'], 'conversation_id': report['conversation_id'], 'request_id': report['request_id'],
                'message_sequence': self._ai_report_message_sequence(c, report['proposal_id']),
                'kind': report['kind'], 'title': request['title'] if request else tombstone['title'] if tombstone else report['original_title'],
                'description': request['description'] if request else tombstone['description'] if tombstone else report['original_description'],
                'moderation': request['moderation'] if request else None, 'status': request['status'] if request else None,
                'accepted_at': request['accepted_at'] if request else None,
                'version': request['version'] if request else tombstone['version'] + 1 if tombstone else 0,
                'deleted': request is None, 'guest': guest, 'can_edit': editable, 'can_delete': editable,
                'feedback_url': destination,
                'public_url': self.base_url.rstrip('/') + '/requests/' + report['request_id'] if request and request['moderation'] == 'accepted' and request['board_visibility'] == 'public' and not custom else None,
                'tracking_notice': ("Submitted as a guest. Private status is available in this chat while its credential remains valid. This report will not appear in your account." if guest else None),
                'created_at': report['created_at']}

    def ai_proposal_confirm(self, conversation_id, principal, proposal_id, version, draft_hash, client_id):
        if type(version) is not int or not isinstance(draft_hash, str) or len(draft_hash) != 64 or not isinstance(client_id, str) or not 1 <= len(client_id) <= 100:
            raise Error(400, 'invalid_input', 'Confirm the exact current draft with a unique submission key')
        with self.transaction(True) as c:
            proposal, conversation, customer = self._ai_proposal_owner(c, conversation_id, principal, proposal_id)
            old = c.execute('SELECT * FROM ai_report_confirmations WHERE customer_id=? AND client_id=?', (customer['id'], client_id)).fetchone()
            if old and (old['proposal_id'] != proposal_id or old['version'] != version or old['draft_hash'] != draft_hash):
                conflict('This submission key already describes a different confirmation')
            confirmation = c.execute('SELECT * FROM ai_report_confirmations WHERE proposal_id=?', (proposal_id,)).fetchone()
            if confirmation:
                if confirmation['version'] != version or confirmation['draft_hash'] != draft_hash:
                    conflict('This confirmation differs from the submitted draft')
                report, _, _ = self._ai_report_owned(c, conversation_id, principal, proposal_id)
                if not old:
                    if c.execute('SELECT count(*) FROM ai_report_confirmations WHERE proposal_id=?', (proposal_id,)).fetchone()[0] >= 100:
                        raise Error(429, 'rate_limited', 'Reuse the existing confirmation receipt')
                    c.execute('INSERT INTO ai_report_confirmations(customer_id,client_id,proposal_id,version,draft_hash,created_at) VALUES(?,?,?,?,?,?)', (customer['id'], client_id, proposal_id, version, draft_hash, now()))
                return self._ai_report_view(c, report)
            self._ai_proposal_current(c, proposal, conversation, customer, submission=True)
            if proposal['version'] != version or proposal['draft_hash'] != draft_hash or self._ai_draft_hash(proposal) != draft_hash:
                conflict('Review the updated draft before confirming it')
            guest, day = customer['kind'] == 'anonymous', now()[:10]
            if (c.execute('SELECT count(*) FROM ai_reports WHERE customer_id=? AND created_at>=?', (customer['id'], day)).fetchone()[0] >= (5 if guest else 20)
                    or c.execute('SELECT count(*) FROM ai_reports WHERE project_id=? AND created_at>=? AND (CASE WHEN guest_session_id IS NOT NULL THEN 1 ELSE 0 END)=?', (conversation['project_id'], day, int(guest))).fetchone()[0] >= (100 if guest else 1000)):
                raise Error(429, 'rate_limited', 'Please wait before submitting more feedback')
            creator_id, ts = customer['user_id'], now()
            if guest:
                owner = c.execute('SELECT user_id FROM ai_guest_owners WHERE customer_id=? AND project_id=?', (customer['id'], customer['project_id'])).fetchone()
                creator_id = owner['user_id'] if owner else new_id()
                if not owner:
                    c.execute("INSERT INTO users(id,display_name,role,created_at,updated_at) VALUES(?,'Website guest','participant',?,?)", (creator_id, ts, ts))
                    c.execute('INSERT INTO ai_guest_owners(customer_id,project_id,user_id,created_at) VALUES(?,?,?,?)', (customer['id'], customer['project_id'], creator_id, ts))
            actor, request_id = self._ai_report_actor(c, customer, principal, creator_id), new_id()
            c.execute("INSERT INTO requests(id,board_id,creator_id,title,description,moderation,created_at,updated_at,submitted_ip_address) VALUES(?,?,?,?,?,'pending',?,?,?)",
                      (request_id, proposal['board_id'], creator_id, proposal['title'], proposal['description'], ts, ts, principal.get('ip_address')))
            c.execute('''INSERT INTO ai_reports(proposal_id,request_id,project_id,conversation_id,customer_id,creator_id,created_at,guest_session_id,guest_origin)
                VALUES(?,?,?,?,?,?,?,?,?)''', (proposal_id, request_id, conversation['project_id'], conversation_id, customer['id'], creator_id, ts, principal['session_id'] if guest else None, principal.get('origin') if guest else None))
            c.execute('INSERT INTO ai_report_confirmations(customer_id,client_id,proposal_id,version,draft_hash,created_at) VALUES(?,?,?,?,?,?)', (customer['id'], client_id, proposal_id, version, draft_hash, ts))
            c.execute("UPDATE ai_proposals SET state='confirmed',version=version+1,updated_at=? WHERE id=?", (ts, proposal_id))
            if proposal['job_id']:
                self._ai_cancel_job(c, proposal['job_id'])
            self._event(c, 'request.created', self._request(c, request_id, creator_id), actor)
            report, _, _ = self._ai_report_owned(c, conversation_id, principal, proposal_id)
            return self._ai_report_view(c, report)

    def ai_reports(self, conversation_id, principal, limit=25, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise Error(400, 'invalid_input', 'Invalid report page')
        with self.transaction() as c:
            _, customer = self._ai_report_owner(c, conversation_id, principal)
            rows = c.execute('SELECT proposal_id FROM ai_reports WHERE conversation_id=? AND customer_id=? ORDER BY created_at DESC,proposal_id DESC LIMIT ? OFFSET ?', (conversation_id, customer['id'], limit + 1, offset)).fetchall()
            return {'items': [self._ai_report_view(c, self._ai_report_owned(c, conversation_id, principal, row['proposal_id'])[0]) for row in rows[:limit]],
                    'limit': limit, 'offset': offset, 'has_more': len(rows) > limit}

    def ai_report(self, conversation_id, principal, report_id):
        with self.transaction() as c:
            return self._ai_report_view(c, self._ai_report_owned(c, conversation_id, principal, report_id)[0])

    def ai_report_update(self, conversation_id, principal, report_id, version, title, description):
        self._ai_report_text(title, description)
        with self.transaction(True) as c:
            report, _, customer = self._ai_report_owned(c, conversation_id, principal, report_id)
            r = self._request(c, report['request_id'], report['creator_id'])
            self._active_board(c, r['board_id'])
            if r['creator_id'] != report['creator_id'] or r['project_id'] != report['project_id']:
                missing()
            if r['accepted_at'] is not None:
                conflict('Accepted requests cannot be edited or deleted by their creator')
            if type(version) is not int or r['version'] != version:
                conflict()
            resubmit = r['moderation'] == 'rejected'
            if resubmit or r['title'] != title or r['description'] != description:
                previous = {key: r[key] for key in ('title', 'description', 'moderation', 'rejection_reason')}
                c.execute("UPDATE requests SET title=?,description=?,moderation='pending',rejection_reason=NULL,updated_at=?,version=version+1 WHERE id=?", (title, description, now(), r['id']))
                self._event(c, 'request.resubmitted' if resubmit else 'request.updated', self._request(c, r['id'], report['creator_id']), self._ai_report_actor(c, customer, principal, report['creator_id']), previous)
            return self._ai_report_view(c, report)

    def ai_report_delete(self, conversation_id, principal, report_id, version):
        with self.transaction(True) as c:
            report, _, customer = self._ai_report_owned(c, conversation_id, principal, report_id)
            tombstone = c.execute('SELECT * FROM ai_report_deletions WHERE proposal_id=?', (report_id,)).fetchone()
            if tombstone:
                if type(version) is not int or version != tombstone['version']:
                    conflict()
                return self._ai_report_view(c, report)
            r = self._request(c, report['request_id'], report['creator_id'])
            self._active_board(c, r['board_id'])
            if r['creator_id'] != report['creator_id'] or r['project_id'] != report['project_id']:
                missing()
            if r['accepted_at'] is not None:
                conflict('Accepted requests cannot be edited or deleted by their creator')
            if type(version) is not int or r['version'] != version:
                conflict()
            previous = {key: r[key] for key in ('title', 'description', 'moderation', 'rejection_reason')}
            self._event(c, 'request.deleted', r, self._ai_report_actor(c, customer, principal, report['creator_id']), previous)
            c.execute('INSERT INTO ai_report_deletions(proposal_id,request_id,version,title,description,deleted_at) VALUES(?,?,?,?,?,?)', (report_id, r['id'], version, r['title'], r['description'], now()))
            c.execute('DELETE FROM requests WHERE id=?', (r['id'],))
            return self._ai_report_view(c, report)

    # Connected apps store configuration only. Remote records never enter installation storage.
    def _connected_row(self, c, project, principal):
        if principal.get('kind') != 'admin': raise Error(403,'forbidden','Administrator access required')
        self._admin_session(c, principal.get('user_id'), principal.get('session_id'))
        p=c.execute('SELECT * FROM projects WHERE id=? OR identifier=?',(project,project)).fetchone()
        if not p: missing()
        row=c.execute('SELECT * FROM connected_apps WHERE project_id=?',(p['id'],)).fetchone()
        return dict(p),dict(row) if row else None

    @staticmethod
    def _connected_view(project,row):
        return {'project_id':project['id'],'project_name':project['name'],
                'version':row['version'] if row else 0,'connection_id':row['id'] if row else None,
                'base_url':row['base_url'] if row else None,'has_token':bool(row and row['protected_token']),
                'enabled':bool(row and row['enabled']),'app_instance_id':row['app_instance_id'] if row else None,
                'link_origins':json.loads(row['link_origins']) if row else [],
                'support_linked':bool(row and row['support_linked'])}

    def connected_read(self, project, principal):
        with self.transaction() as c:
            p,r=self._connected_row(c,project,principal)
            return self._connected_view(p,r)

    def connected_list(self, principal):
        with self.transaction() as c:
            if principal.get('kind') != 'admin': raise Error(403,'forbidden','Administrator access required')
            self._admin_session(c,principal.get('user_id'),principal.get('session_id'))
            return [self._connected_view(dict(p),dict(r) if r else None) for p in c.execute('SELECT * FROM projects WHERE archived_at IS NULL ORDER BY created_at,id').fetchall()
                    for r in [c.execute('SELECT * FROM connected_apps WHERE project_id=?',(p['id'],)).fetchone()]]

    def connected_change(self, project, principal, version, action, values=None):
        values=values or {}
        with self.transaction(True) as c:
            p,r=self._connected_row(c,project,principal)
            if type(version) is not int or version!=(r['version'] if r else 0): conflict('Connection changed; reload it')
            if action=='save':
                if r and r['base_url']!=values['base_url'] and not values.get('protected_token'):
                    raise Error(400,'token_required','Supply a new token when changing the address')
                token=values.get('protected_token') or (r['protected_token'] if r else None)
                if not token: raise Error(400,'token_required','Supply the app read token')
                if r:
                    c.execute('UPDATE connected_apps SET base_url=?,protected_token=?,enabled=0,version=version+1,updated_at=? WHERE project_id=?',(values['base_url'],token,now(),p['id']))
                else:
                    c.execute('INSERT INTO connected_apps(project_id,id,base_url,protected_token,updated_at) VALUES(?,?,?,?,?)',(p['id'],new_id(),values['base_url'],token,now()))
            elif action=='enable':
                if not r or not r['protected_token'] or not r['base_url']:conflict('Save the connection first')
                if r['app_instance_id'] and r['app_instance_id']!=values['app_instance_id']:conflict('This project is bound to a different app. Use a new project.')
                duplicate=c.execute('SELECT 1 FROM connected_apps WHERE app_instance_id=? AND project_id!=?',(values['app_instance_id'],p['id'])).fetchone()
                if duplicate:conflict('This app is already connected to another project')
                c.execute('UPDATE connected_apps SET enabled=1,app_instance_id=?,link_origins=?,support_linked=?,version=version+1,updated_at=? WHERE project_id=?',
                          (values['app_instance_id'],json.dumps(values['link_origins']),int(values['support_linked']),now(),p['id']))
            elif action in ('disable','remove'):
                if not r:return self._connected_view(p,r)
                if action=='disable' and not r['enabled']:return self._connected_view(p,r)
                if action=='remove' and not r['protected_token'] and not r['base_url']:return self._connected_view(p,r)
                if action=='remove':
                    c.execute('UPDATE connected_apps SET base_url=NULL,protected_token=NULL,enabled=0,support_linked=0,version=version+1,updated_at=? WHERE project_id=?',(now(),p['id']))
                else:c.execute('UPDATE connected_apps SET enabled=0,version=version+1,updated_at=? WHERE project_id=?',(now(),p['id']))
            else:raise Error(400,'invalid_input','Unknown connection action')
            current=dict(c.execute('SELECT * FROM connected_apps WHERE project_id=?',(p['id'],)).fetchone())
            c.execute('INSERT INTO connected_app_audit VALUES(?,?,?,?,?,?)',(new_id(),p['id'],principal['user_id'],action,current['version'],now()))
            return self._connected_view(p,current)

    def connected_snapshot(self, project, principal, version=None, *, enabled=True, connection_id=None):
        with self.transaction() as c:
            p,r=self._connected_row(c,project,principal)
            self._active_project(c, p['id'])
            if not r or not r['base_url'] or not r['protected_token']:raise Error(409,'connection_missing','Configure an app connection first')
            if version is not None and version!=r['version']:conflict('Connection changed; refresh the view')
            if connection_id is not None and connection_id!=r['id']:conflict('Connection changed; refresh the view')
            if enabled and not r['enabled']:raise Error(409,'connection_disabled','This app connection is disabled')
            return r|{'project_name':p['name']}

    def connected_preferences(self, principal, version=None, zone=None):
        with self.transaction(zone is not None) as c:
            if principal.get('kind') != 'admin': raise Error(403,'forbidden','Administrator access required')
            self._admin_session(c,principal.get('user_id'),principal.get('session_id'))
            row=dict(c.execute('SELECT version,timezone FROM connected_app_preferences WHERE singleton=1').fetchone())
            if zone is not None:
                if row['version']!=version:conflict('Timezone changed; reload it')
                if row['timezone']!=zone:
                    c.execute('UPDATE connected_app_preferences SET timezone=?,version=version+1 WHERE singleton=1',(zone,))
                    row={'timezone':zone,'version':version+1}
            return row

    def connected_related(self, project, principal, external_id, version, limit, offset):
        with self.transaction() as c:
            p,r=self._connected_row(c,project,principal)
            if not r or not r['enabled'] or r['version']!=version:conflict('Connection changed; refresh the view')
            if not r['support_linked']:return {'linked':False,'requests':[],'conversations':[],'has_more':False}
            identity=c.execute('SELECT user_id FROM external_identities WHERE project_id=? AND external_user_id=?',(p['id'],self._encode_external_id(external_id))).fetchone()
            if not identity:return {'linked':True,'requests':[],'conversations':[],'has_more':False}
            requests=[dict(x) for x in c.execute('SELECT r.id,r.title,r.created_at FROM requests r JOIN boards b ON b.id=r.board_id WHERE b.project_id=? AND r.creator_id=? ORDER BY r.created_at DESC,r.id DESC LIMIT ? OFFSET ?',(p['id'],identity[0],limit+1,offset))]
            chats=[dict(x) for x in c.execute('SELECT v.id,v.created_at FROM chat_conversations v JOIN chat_customers u ON u.id=v.customer_id WHERE u.project_id=? AND u.user_id=? ORDER BY v.created_at DESC,v.id DESC LIMIT ? OFFSET ?',(p['id'],identity[0],limit+1,offset))]
            return {'linked':True,'requests':requests[:limit],'conversations':chats[:limit],'has_more':len(requests)>limit or len(chats)>limit}
