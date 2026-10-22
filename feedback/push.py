"""Opt-in relay client. No APNs keys or provider dependencies in an installation."""
import json
import logging
import random
import re
import secrets
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID
from feedback.errors import Error
from feedback.webhooks import destination, PinnedHTTP

DEFAULT_RELAY = 'https://relay.supaknock.com'
log = logging.getLogger(__name__)


class RelayFailure(Exception):
    def __init__(self, code, retryable=False):
        self.code, self.retryable = code, retryable


def relay_origin(url, allow_internal=False):
    u = urlsplit(url)
    if (u.scheme != 'https' and not (allow_internal and u.scheme == 'http')) or not u.hostname or u.path not in ('','/') or u.query or u.fragment or u.username or u.password:
        raise Error(400,'invalid_relay_url','Use an HTTPS relay origin without a path')
    # Repeated immediately before every connection, with pinned DNS and no redirects.
    destination(url, allow_internal)
    return url.rstrip('/')


def relay_request(origin, path, credential=None, body=None, allow_internal=False):
    try:
        u,address,port = destination(origin, allow_internal)
        conn = PinnedHTTP(u.hostname,address,port,u.scheme=='https')
        headers = {'Content-Type':'application/json','User-Agent':'SuggestionBox/0.2'}
        if credential:
            headers['Authorization'] = 'Bearer ' + credential
        try:
            conn.request('POST' if body is not None else 'GET', '/api/v1'+path,
                         json.dumps(body,separators=(',',':')).encode() if body is not None else None, headers)
            response = conn.getresponse()
            if not 200 <= response.status < 300:
                raise RelayFailure('relay_http_'+str(response.status),response.status in (408,425,429) or response.status >= 500)
            raw = response.read(16385)
            if len(raw)>16384:
                raise RelayFailure('invalid_relay_response')
            value = json.loads(raw)
            if not isinstance(value,dict):
                raise RelayFailure('invalid_relay_response')
            return value
        finally:
            conn.close()
    except RelayFailure:
        raise
    except Error:
        raise RelayFailure('relay_destination_blocked') from None
    except Exception:
        raise RelayFailure('relay_connection_failed',True) from None


class PushService:
    def __init__(self,storage,vault,allow_internal=False,transport=relay_request):
        self.storage,self.vault,self.allow_internal,self.transport = storage,vault,allow_internal,transport
        self.lock = threading.Lock()

    def settings(self):
        row = self.storage.push_settings()
        return {'installation_id':self.storage.installation_id(), 'enabled':bool(row and row['enabled']),
                'connected':bool(row and row['credential']), 'relay_url':row['relay_url'] if row else DEFAULT_RELAY,
                'last_error':row['last_error'] if row else None}

    def configure(self,user_id,session_id,enabled,url,rotate=False):
        with self.lock:
            row = self.storage.push_settings()
            # Disabling must work during a relay outage or DNS failure.
            if not enabled:
                self.storage.push_disable(user_id,session_id)
                return self.settings()
            url = relay_origin(url,self.allow_internal)
            row = self.storage.push_prepare(user_id,session_id,url,self.vault.protect(secrets.token_urlsafe(32)))
            if row['credential'] and not rotate:
                self.storage.push_enable(user_id,session_id,row['version'],row['credential'])
                return self.settings()
            body = {'installation_id':self.storage.installation_id(),'enrollment_proof':self.vault.reveal(row['enrollment_proof'])}
            try:
                # Enroll also recovers interrupted rotations using the persistent proof.
                result = self.transport(url,'/installations/enroll',None,body,self.allow_internal)
                if result.get('installation_id')!=body['installation_id'] or not re.fullmatch(r'sr_[A-Za-z0-9_-]{43,200}',result.get('credential','')):
                    raise RelayFailure('invalid_relay_response')
                self.storage.push_enable(user_id,session_id,row['version'],self.vault.protect(result['credential']))
            except RelayFailure as exc:
                self.storage.push_error(row['version'],exc.code)
                raise Error(503,'relay_unavailable','Relay enrollment failed; check settings and retry') from None
            return self.settings()

    def rotate(self,user_id,session_id):
        row = self.storage.push_settings()
        if not row or not row['enabled']:
            raise Error(409,'conflict','Enable push before rotating its credential')
        return self.configure(user_id,session_id,True,row['relay_url'],rotate=True)


def delivery_view(row):
    from feedback.domain import utc_timestamp
    fields = ('id','event_id','kind','resource_id','device_id','state','attempt_count','retry_count','error')
    return {**{k:row[k] for k in fields},'created_at':utc_timestamp(row['created_at']),
            'expires_at':utc_timestamp(row['expires_at'])}


class PushWorker:
    def __init__(self,storage,vault,allow_internal=False,transport=relay_request):
        self.storage,self.vault,self.allow_internal,self.transport=storage,vault,allow_internal,transport
        self.stop_event,self.thread=threading.Event(),None

    def once(self,current_time=None):
        ts=time.time() if current_time is None else current_time
        job=self.storage.claim_push(ts)
        if not job:
            return False
        relay_id=job['relay_id']
        state,error,invalid,failed='failed',None,False,False
        next_attempt=ts+15
        try:
            credential=self.vault.reveal(job['credential'])
            def call(path,body=None):
                return self.transport(job['relay_url'],path,credential,body,self.allow_internal)
            # Claim and immediate pre-send checks fence current eligibility. Disable in
            # the HTTP window is reconciled by finish_push and a durable cancellation.
            cancel=job['cancel'] or not self.storage.push_before_send(job['id'],job['lease_token'])
            if cancel:
                if job['submitted']:
                    receipt=call('/notifications/cancel-by-key',{'event_id':job['event_id'],'recipient_id':job['device_id']})
                    if receipt.get('state') not in ('cancelled','accepted','failed','expired'):
                        raise RelayFailure('invalid_relay_response',True)
                state='expired' if job['expires_at']<=ts else 'cancelled'
            else:
                if relay_id and not job['manual_retry']:
                    result=call('/notifications/'+relay_id)
                else:
                    body={'event_id':job['event_id'],'recipient_id':job['device_id'],'resource_id':job['resource_id'],
                          'type':job['kind'],'environment':job['environment'],'token':self.vault.reveal(job['token'])}
                    path='/notifications/'+relay_id+'/retry' if relay_id and job['manual_retry'] else '/notifications'
                    result=call(path,body)
                try:
                    relay_id=str(UUID(result['id']))
                    remote=result['state']
                    if remote not in ('queued','retry','sending','accepted','failed','cancelled','expired'):
                        raise ValueError()
                except (KeyError,ValueError,TypeError):
                    raise RelayFailure('invalid_relay_response') from None
                state={'accepted':'succeeded','failed':'failed','cancelled':'cancelled','expired':'expired'}.get(remote,'awaiting')
                error='relay_delivery_failed' if state=='failed' else None
                invalid=result.get('invalid_token') is True
        except RelayFailure as exc:
            failed=True
            state='queued' if exc.retryable or job['cancel'] else 'failed'
            error=exc.code
            next_attempt=ts+min(3600,5*2**min(job['failure_count'],10))*random.uniform(.75,1.25)
        except Exception:
            failed,state,error=True,'failed','push_configuration_failed'
        self.storage.finish_push(job['id'],job['lease_token'],state,relay_id,error,next_attempt,invalid,failed)
        return True

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                try:
                    busy=self.once()
                except Exception:
                    log.error('Push queue processing failed; will retry')
                    busy=False
                if not busy:
                    self.stop_event.wait(1)
        self.thread=threading.Thread(target=loop,name='push-worker',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
