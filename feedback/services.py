"""Application authorization and public/private representations, independent of HTTP."""
from dataclasses import replace
import json
from feedback.domain import Actor, utc_timestamp
from feedback.errors import Error, missing
from feedback.storage.base import Storage


def require_admin(actor):
    if actor.role != 'admin':
        raise Error(403 if actor.role != 'anonymous' else 401, 'forbidden', 'Administrator access required')


def require_manage(actor, project):
    if not actor.manages(project):
        raise Error(403 if actor.role != 'anonymous' else 401, 'forbidden', 'Management permission required')


def public_project(p):
    return {k: p[k] for k in ('id', 'identifier', 'name', 'description', 'created_at', 'updated_at', 'version', 'archived_at')}


def delivery_view(row):
    return dict(row, next_attempt_at=utc_timestamp(row['next_attempt_at']))


def request_view(r, actor):
    private = actor.manages(r['project_id']) or (r['creator_id'] == actor.user_id and (not actor.key_id or actor.project_id == r['project_id']))
    keys = ('id', 'board_id', 'project_id', 'project_identifier', 'project_name', 'board_name', 'board_identifier', 'title', 'description', 'status', 'created_at', 'updated_at', 'version', 'vote_count')
    result = {k: r[k] for k in keys}
    result.update(voted=bool(r['voted']), creator_name=r['creator_name'], owned=r['creator_id'] == actor.user_id,
                  can_edit=r['creator_id'] == actor.user_id and r['accepted_at'] is None and not (r['project_archived_at'] or r['board_archived_at']),
                  archived=bool(r['project_archived_at'] or r['board_archived_at']))
    if private:
        result.update({k: r[k] for k in ('moderation', 'rejection_reason', 'accepted_at')})
    if actor.role == 'admin' and not actor.key_id:
        result['submitted_ip_address'] = r.get('submitted_ip_address')
    return result


class FeedbackService:
    def __init__(self, storage: Storage):
        self.storage = storage

    def scope(self, actor, project):
        if actor.key_id and actor.project_id != project:
            missing()

    def register_device(self, actor, session_id, device_id, data, vault):
        require_admin(actor)
        from feedback.security import digest
        return self.storage.register_device(actor.user_id,session_id,device_id,vault.protect(data['token']),digest(data['token']),data)

    def devices(self, actor):
        require_admin(actor)
        return self.storage.devices(actor.user_id)

    def remove_device(self, actor, session_id, device_id):
        require_admin(actor)
        self.storage.remove_device(actor.user_id,session_id,device_id)

    def participant(self, actor, project, external_id=None):
        self.scope(actor, project)
        if actor.key_id:
            if not actor.can(project, 'participate'):
                raise Error(403, 'forbidden', 'Participation permission required')
            if external_id is None or not 1 <= len(external_id) <= 512:
                raise Error(400, 'invalid_identity', 'Provide an opaque external user ID of 1–512 characters')
            uid = self.storage.resolve_external_identity(project, external_id)
            return replace(actor, user_id=uid, external_id=external_id)
        if external_id is not None:
            raise Error(400, 'invalid_identity', 'External user IDs require a project credential')
        if not actor.user_id:
            raise Error(401, 'unauthorized', 'Sign in to participate')
        return actor

    def projects(self, actor, limit, offset, archive='active'):
        """The installation directory is admin-only; keys see only their project."""
        if archive != 'active':
            require_admin(actor)
        if actor.key_id:
            if not (actor.can(actor.project_id, 'read') or actor.manages(actor.project_id)):
                raise Error(403, 'forbidden', 'Read permission required')
            project = self.storage.project(actor.project_id)
            return [public_project(project)] if offset == 0 and not project['archived_at'] else []
        require_admin(actor)
        return [public_project(p) for p in self.storage.projects(limit, offset, archive)]

    def project(self, ref, actor):
        p = self.storage.project(ref)
        self.scope(actor, p['id'])
        if p['archived_at'] and (actor.role != 'admin' or actor.key_id): missing()
        return p

    @staticmethod
    def trusted_board_reader(actor, project):
        return actor.manages(project) or bool(actor.key_id and actor.can(project, 'read'))

    def board(self, bid, actor):
        b = self.storage.board(bid)
        self.project(b['project_id'], actor)
        if b['archived_at'] and (actor.role != 'admin' or actor.key_id): missing()
        if b['visibility'] == 'private' and not (actor.user_id or actor.key_id): missing()
        return b

    def readable_request(self, rid, actor):
        r = self.storage.request(rid, actor.user_id)
        self.board(r['board_id'], actor)
        if r['board_visibility'] == 'private' and r['creator_id'] != actor.user_id and not self.trusted_board_reader(actor, r['project_id']):
            missing()
        if r['moderation'] != 'accepted' and r['creator_id'] != actor.user_id and not actor.manages(r['project_id']):
            missing()
        return r

    def submit(self, bid, title, description, actor, external_id):
        b = self.board(bid, actor)
        actor = self.participant(actor, b['project_id'], external_id)
        return request_view(self.storage.create_request(bid, title, description, actor), actor)

    def acting_request(self, rid, actor, external_id):
        r = self.storage.request(rid)
        actor = self.participant(actor, r['project_id'], external_id)
        return r, actor

    def edit(self, rid, data, actor, external_id, delete=False):
        _, actor = self.acting_request(rid, actor, external_id)
        r = self.storage.update_creator_request(rid, data.get('title'), data.get('description'), data['version'], actor, delete)
        return request_view(r, actor) if r else None

    def moderate(self, rid, action, value, version, actor):
        r = self.storage.request(rid)
        self.scope(actor, r['project_id'])
        require_manage(actor, r['project_id'])
        return request_view(self.storage.moderate_request(rid, action, value, version, actor), actor)

    def vote(self, rid, present, actor, external_id):
        _, actor = self.acting_request(rid, actor, external_id)
        return self.storage.set_vote(rid, present, actor)

    def history(self, rid, actor, limit, offset):
        r = self.readable_request(rid, actor)
        events = self.storage.history(rid, limit, offset, public=not (actor.manages(r['project_id']) or actor.user_id == r['creator_id']))
        if actor.role != 'admin' or actor.key_id:
            events = [{k: v for k, v in event.items() if k != 'ip_address'} for event in events]
        if actor.manages(r['project_id']) or actor.user_id == r['creator_id']:
            return events
        return [{k: e[k] for k in ('id', 'type', 'occurred_at')} | {'status': e['request']['status'], 'previous_status': (e.get('previous') or {}).get('status')} for e in events if e['type'] in ('request.accepted', 'request.status_changed')]

    def audience(self, rid, actor, limit, offset):
        r = self.readable_request(rid, actor)
        self.scope(actor, r['project_id'])
        if not actor.can(r['project_id'], 'audience:read'):
            raise Error(403, 'forbidden', 'Audience permission required')
        return self.storage.audience(rid, limit, offset)
