"""Chat application rules, independent of SQLite and HTTP transports."""
import secrets
import time
from datetime import datetime, timezone, timedelta
from statistics import median
from urllib.parse import urlsplit
from feedback.errors import Error, missing
from feedback.security import digest
from feedback.domain import utc_timestamp


def valid_origin(value):
    try:
        parsed = urlsplit(value)
        parsed.port  # Reject malformed/out-of-range ports before storing origins.
    except ValueError:
        raise Error(400, 'invalid_origin', 'Use an exact valid http(s) website origin') from None
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or value == 'null':
        raise Error(400, 'invalid_origin', 'Use exact http(s) website origins without a path')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '[::1]', '::1'):
        raise Error(400, 'invalid_origin', 'Public website origins must use HTTPS')
    return value


class ChatService:
    def __init__(self, storage, base_url):
        self.storage, self.base_url = storage, base_url

    def settings(self, project):
        return self.storage.chat_settings(project)

    def configure(self, project, principal, enabled, origins, version):
        if principal['kind'] != 'admin':
            raise Error(403, 'forbidden', 'Administrator access required')
        return self.storage.chat_configure(project, principal, enabled, sorted(set(valid_origin(x) for x in origins)), version)

    def config(self, project, origin):
        settings = self.settings(project)
        self.check_origin(settings, origin)
        return {'project': settings['project_identifier'], 'name': settings['project_name'], 'enabled': settings['enabled'], 'ai_available': self.storage.chat_ai_available(settings['project_id']), 'response_time': self.response_time(settings['project_id'], public=True), 'business_hours': self.business_hours(settings['project_id'])}

    def business_hours(self, project):
        from feedback.business_hours import availability
        settings=self.storage.chat_business_hours(project)
        return availability(settings['effective'],settings['source'])

    @staticmethod
    def check_origin(settings, origin):
        if not settings['enabled']:
            raise Error(404, 'not_found', 'Chat is unavailable')
        if not origin or origin not in settings['origins']:
            raise Error(403, 'origin_denied', 'This website is not enabled for chat')

    def create_session(self, project, origin, ip_address=None):
        settings = self.settings(project)
        self.check_origin(settings, origin)
        token = 'cv_' + secrets.token_urlsafe(32)
        expires = time.time() + 30 * 86400
        conversation = self.storage.chat_create_session(settings['project_id'], origin, digest(token), expires, ip_address=ip_address)
        return {'token': token, 'expires_at': utc_timestamp(expires), 'conversation': conversation}

    def visitor(self, token, origin):
        if token and token.startswith('cc_') and len(token) <= 128:
            return self.storage.chat_customer_session(digest(token), origin)
        if not token or not token.startswith('cv_') or len(token) > 128:
            raise Error(401, 'unauthorized', 'Chat session expired; start a new conversation')
        return self.storage.chat_visitor(digest(token), origin)

    def customer_issuer(self, project, actor, external_id):
        settings = self.settings(project)
        if actor.key_id:
            if actor.project_id != settings['project_id']:
                missing()
            if not actor.can(settings['project_id'], 'participate'):
                raise Error(403, 'forbidden', 'Participation permission required')
            if external_id is None or not 1 <= len(external_id) <= 512:
                raise Error(400, 'invalid_identity', 'Provide an opaque external user ID of 1–512 characters')
        else:
            if not actor.user_id:
                raise Error(401, 'unauthorized', 'Sign in to establish customer chat access')
            if external_id is not None:
                raise Error(400, 'invalid_identity', 'External user IDs require a project credential')
        return settings

    def create_customer_session(self, project, actor, external_id, client_type, origin, parent_session_id=None, display_name=None):
        settings = self.customer_issuer(project, actor, external_id)
        if not settings['enabled']:
            raise Error(404, 'not_found', 'Chat is unavailable')
        if client_type == 'browser':
            if not origin:
                raise Error(400, 'invalid_origin', 'Browser sessions require their exact website origin')
            valid_origin(origin)
            if origin not in settings['origins'] and not (not actor.key_id and origin == self.base_url):
                raise Error(403, 'origin_denied', 'This website is not enabled for chat')
        elif origin is not None:
            raise Error(400, 'invalid_origin', 'Native sessions do not use a website origin')
        if display_name is not None and not actor.key_id:
            raise Error(400, 'invalid_input', 'Native customers use their account display name')
        token, expires = 'cc_' + secrets.token_urlsafe(32), time.time() + 3600
        result = self.storage.chat_create_customer_session(settings['project_id'], actor, external_id, digest(token), expires, origin, parent_session_id, display_name)
        return {'token': token, 'expires_at': utc_timestamp(expires), 'customer': result['customer']}

    def revoke_customer_sessions(self, project, actor, external_id, parent_session_id=None):
        settings = self.customer_issuer(project, actor, external_id)
        self.storage.chat_revoke_customer_sessions(settings['project_id'], actor, external_id, parent_session_id)

    @staticmethod
    def require_customer(principal):
        if principal['kind'] not in ('visitor', 'customer'):
            raise Error(403, 'forbidden', 'Customer chat session required')

    def conversations(self, principal, state, limit, offset):
        self.require_customer(principal)
        return self.storage.chat_customer_conversations(principal, state, limit, offset)

    def create_conversation(self, principal, client_conversation_id):
        if principal['kind'] != 'customer':
            raise Error(403, 'forbidden', 'Authenticated customer chat session required')
        return self.storage.chat_create_conversation(principal, client_conversation_id)

    def revoke_customer_session(self, principal):
        if principal['kind'] != 'customer':
            raise Error(403, 'forbidden', 'Authenticated customer chat session required')
        self.storage.chat_revoke_customer_session(principal)

    def message(self, conversation_id, principal, client_message_id, body, follow_up_email=None, context=None):
        if not body.strip() or len(body) > 4000:
            raise Error(400, 'invalid_input', 'Write a message of 1–4,000 characters')
        return self.storage.chat_send(conversation_id, principal, client_message_id, body, follow_up_email, context)

    def support_mode(self, conversation_id, principal, mode, version):
        if principal.get('kind') not in ('admin', 'visitor', 'customer'):
            raise Error(403, 'forbidden', 'A current chat credential is required')
        if principal['kind'] != 'admin' and mode == 'human':
            raise Error(403, 'forbidden', 'Only an administrator can take over support')
        return self.storage.chat_support_mode(conversation_id, principal, mode, version)

    def ticket(self, conversation_id, principal):
        token, expires = 'ct_' + secrets.token_urlsafe(32), time.time() + 60
        self.storage.chat_ticket(conversation_id, principal, digest(token), expires)
        return {'ticket': token, 'expires_at': utc_timestamp(expires)}

    def response_time(self, project_id, public=False):
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        samples = self.storage.chat_response_samples(project_id, since)
        answered = [max(0, (datetime.fromisoformat(x['first_reply_at'].replace('Z', '+00:00')) - datetime.fromisoformat(x['first_message_at'].replace('Z', '+00:00'))).total_seconds()) for x in samples if x['first_reply_at']]
        seconds = median(answered) if len(answered) >= 5 and any(x['first_reply_at'] for x in samples[:5]) else None
        label = None
        if seconds is not None:
            if seconds < 60:
                label = 'Typically responds in under a minute'
            elif seconds < 3600:
                minutes = max(1, round(seconds / 60))
                label = f'Typically responds in {minutes} minute' + ('s' if minutes != 1 else '')
            elif seconds < 86400:
                hours = max(1, round(seconds / 3600))
                label = f'Typically responds in {hours} hour' + ('s' if hours != 1 else '')
            else:
                days = max(1, round(seconds / 86400))
                label = f'Typically responds in {days} day' + ('s' if days != 1 else '')
        if public:
            return {'label': label}
        return {'label': label, 'median_seconds': seconds, 'answered_count': len(answered), 'unanswered_count': len(samples) - len(answered), 'window_days': 30, 'minimum_answered': 5, 'recent_reply': any(x['first_reply_at'] for x in samples[:5])}
