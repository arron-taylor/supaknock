"""Disposable browser QA only: real APIs/SQLite, simulated external/model work.

Run from the repository root with .venv/bin/python scripts/ai-ui-preview.py.
Set SB_AI_WIDGET_PREVIEW=1 for ready synthetic AI projects and a customer site
at localhost:8770, including guest-on, guest-off and human-only variants.
Add SB_AI_SOURCES_PREVIEW=1 for mixed document/website references and long excerpts.
Set SB_AI_KEYS_PREVIEW=1 for a ready Groq key pool plus a 99-key capacity case.
Uses a fresh temporary database and fixed loopback port; never reads credentials,
downloads models or contacts a provider/website. Stop the process to remove it.
"""
import asyncio
import hashlib
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.fernet import Fernet
import httpx
import uvicorn
from feedback.ai_decision_providers import CloudflareDecisionProvider, KodeKeyDecisionProvider
from feedback.ai_domain import EMBEDDING_PROFILE, default_ai_settings
from feedback.ai_providers import GenerationResult, ModelCapabilities, ProviderError, ToolCall, Usage
from feedback.ai_runtime import EmbeddingRuntimeError
from feedback.api import create_app
from feedback.config import Config
from feedback.security import hasher


BASE = 'http://127.0.0.1:8769'
PASSWORD = 'Preview admin passphrase 2026!'
MODEL_BYTES = 1264905363


class FixtureInstallation:
    def __init__(self, storage, vault, config):
        self.storage, self.vault = storage, vault
        self._stop = threading.Event()
        self._thread = None

    def status(self, principal):
        return self.storage.ai_runtime(principal) | {'model_id': EMBEDDING_PROFILE['model_id'],
            'download_bytes': MODEL_BYTES, 'license_url': 'https://huggingface.co/google/embeddinggemma-300m'}

    def download(self, principal, version, token):
        return self.storage.ai_runtime_begin(principal, version, 'download', self.vault.protect(token))

    def cancel(self, principal, version):
        return self.storage.ai_runtime_cancel(principal, version)

    def remove(self, principal, version):
        return self.storage.ai_runtime_begin(principal, version, 'remove')

    def available(self):
        return self.storage.ai_runtime_local()['state'] == 'ready'

    def embed_queries(self, texts):
        if not self.available():
            raise EmbeddingRuntimeError('unavailable')
        return [[1.] + [0.] * 767 for _ in texts]

    def embed_documents(self, texts, *, titles=None):
        return self.embed_queries(texts)

    def start(self):
        self._stop.clear()
        def loop():
            while not self._stop.wait(.1):
                job = self.storage.ai_runtime_claim(time.time())
                if not job:
                    continue
                if job['action'] == 'remove':
                    self.storage.ai_runtime_finish(job['operation_id'], job['lease_token'])
                    continue
                token = self.vault.reveal(job['protected_token'])
                if token == 'deny-model-access':
                    self.storage.ai_runtime_finish(job['operation_id'], job['lease_token'], error='model_access_required')
                    continue
                for step in range(1, 6):
                    if self._stop.wait(.7):
                        return
                    if not self.storage.ai_runtime_advance(job['operation_id'], job['lease_token'], MODEL_BYTES * step // 5, MODEL_BYTES, verifying=step == 5):
                        break
                else:
                    self.storage.ai_runtime_finish(job['operation_id'], job['lease_token'], manifest=EMBEDDING_PROFILE)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5)


class FixtureProvider:
    def __init__(self, provider_id='kodekey'):
        self.id = provider_id
        if provider_id == 'kodekey':
            self.name, self.key, self.models = 'KodeKloud KodeKey', 'preview-kodekey-key', ('gpt-6-luna',)
        elif provider_id == 'gemini':
            self.name, self.key, self.models = 'Google Gemini', 'preview-gemini-key', ('gemini-3.8-flash', 'gemini-2.5-flash')
        elif provider_id == 'mistral':
            self.name, self.key, self.models = 'Mistral AI', 'preview-mistral-key', ('ministral-14b-2512', 'ministral-14b-latest')
        elif provider_id == 'groq':
            self.name, self.key, self.models = 'Groq', 'preview-groq-key', ('openai/gpt-oss-120b', 'openai/gpt-oss-20b')
        elif provider_id == 'ramp':
            self.name, self.key, self.models = 'Ramp Router', 'preview-ramp-key', ('gpt-6-luna', 'gpt-6-sol')
        else:
            raise ProviderError('unsupported_provider')
        self.rate_limited_keys = set()
        self.truncated_batches = set()

    def check(self, key, model=None):
        if not isinstance(key, str) or not re.fullmatch(re.escape(self.key) + r'(?:-(?:[1-9][0-9]?|cooldown|invalid|payment))?', key):
            raise ProviderError('invalid_credentials')
        if key.endswith('-invalid'):
            raise ProviderError('invalid_credentials', status=401)
        if key.endswith('-payment'):
            raise ProviderError('payment_required', status=402)
        if model is not None and model not in self.models:
            raise ProviderError('model_or_endpoint_unavailable')

    async def list_models(self, key):
        self.check(key)
        await asyncio.sleep(.3)
        return self.models

    async def probe_model(self, key, model, *, tools=False):
        self.check(key, model)
        await asyncio.sleep(.3)
        return ModelCapabilities(text_generation=True, tool_calling=tools)

    async def generate(self, key, model, messages, **options):
        self.check(key, model)
        await asyncio.sleep(.5)
        data = json.loads(next(message.content for message in reversed(messages) if message.role == 'user'))
        evidence = data['passages']
        ids = [row['id'] for row in evidence[:3]] if os.environ.get('SB_AI_SOURCES_PREVIEW') == '1' else ([evidence[0]['id']] if evidence else [])
        if data.get('operation') == 'evidence_check':
            # Deterministic fixture only: model-quality evaluation remains separate.
            draft = data['draft']['body']
            supported = bool(draft) and any(draft in passage['text'] for passage in evidence)
            value = {'assessment': 'Fixture compares the draft with original support text.',
                     'issues': [] if supported else [{'quote': draft, 'reason': 'unsupported'}],
                     'supported': supported}
            return GenerationResult(json.dumps(value), (), Usage(70, 40, 110), 'stop')
        question = data.get('question', '').lower()
        if 'rate limit' in question and key.endswith('-cooldown') and key not in self.rate_limited_keys:
            self.rate_limited_keys.add(key)
            raise ProviderError('rate_limited', retryable=True, status=429, retry_after=30)
        if 'slow' in question:
            await asyncio.sleep(8)
        if 'provider failure' in question:
            raise ProviderError('unavailable')
        available = {tool.name for tool in options.get('tools', ())}
        if not any(message.role == 'tool' for message in messages):
            action, arguments = None, {}
            if 'bug' in question:
                action, arguments = 'create_bug_report', {'title': 'Export stops unexpectedly', 'description': 'When I export my feedback, the progress bar stops before the download begins.'}
            elif 'feature' in question:
                action, arguments = 'create_feature_request', {'title': 'Add a weekly feedback digest', 'description': 'Please add a weekly summary so our team can review new ideas together.'}
            elif 'human' in question:
                action = 'request_human'
            elif 'support link' in question and ids:
                action, arguments = 'support_link', {'passage_id': ids[0]}
            elif 'search' in question:
                action, arguments = 'search_knowledge', {'query': 'configuration backup'}
            if action in available:
                return GenerationResult('', (ToolCall('fixture-' + str(uuid4()), action, arguments),), Usage(70, 40, 110), 'tool_calls')
        body = 'Save the configuration, then restart the application. Standard backups include your support data and settings.'
        if 'sections' in messages[0].content and 'Create editable derived' in messages[0].content:
            first_title = evidence[0]['title'] if evidence else ''
            if first_title == 'Batch topic 5' and first_title not in self.truncated_batches:
                self.truncated_batches.add(first_title)
                raise ProviderError('incomplete_response')
            value = {'sections': [
                {'kind': 'section', 'title': 'Getting started', 'body': body, 'category': 'Setup', 'citation_ids': ids},
                {'kind': 'faq', 'title': 'What does a backup include?', 'body': 'Standard backups include support data and settings.', 'category': 'Backups', 'citation_ids': ids}]}
        elif 'unicorn' in question:
            value = {'status': 'insufficient', 'body': '', 'citation_ids': []}
        else:
            value = {'status': 'answered', 'body': body, 'citation_ids': ids}
        return GenerationResult(json.dumps(value), (), Usage(70, 40, 110), 'stop')


def fixture_decision_provider(identifier):
    if identifier == 'kodekey':
        class Unavailable(KodeKeyDecisionProvider):
            async def list_models(self, key):
                return ('typesafe/jev-1.13.0',)
        return Unavailable()
    if identifier != 'cloudflare':
        raise ProviderError('unsupported_provider')
    def handle(request):
        if request.headers.get('authorization') != 'Bearer preview-cloudflare-token':
            return httpx.Response(401)
        if request.method == 'GET':
            result = [{'name': '@cf/cloudflare/clef'}, {'name': '@cf/cloudflare/clef-flash'}]
        else:
            body = json.loads(request.content)
            question = body['state']['current_request'].lower()
            probability = .01 if 'recipe' in question else .5 if 'maybe' in question else .99
            result = {'model': body['model'], 'answers': {'in_scope': {'type': 'noul', 'noul': probability}},
                      'usage': {'input_tokens': 100, 'output_tokens': 0}}
        value = {'success': True, 'errors': [], 'messages': [], 'result': result}
        return httpx.Response(200, headers={'Content-Type': 'application/json'},
                              stream=httpx.ByteStream(json.dumps(value).encode()))
    return CloudflareDecisionProvider(transport=httpx.MockTransport(handle))


def fixture_crawl(config, *, on_page, cancelled):
    time.sleep(.5)
    if cancelled():
        return {'complete': False, 'error': 'cancelled'}
    if 'failure' in config['seed_url']:
        return {'complete': False, 'error': 'http_failed'}
    if os.environ.get('SB_AI_CRAWL_SKIPS_PREVIEW'):
        for suffix, reason in [('login', 'login_page'), ('blank', 'empty_page'), ('redirect', 'redirect_refused')][:max(0, config['max_pages'] - 1)]:
            on_page({'url': config['seed_url'].rstrip('/') + '/' + suffix, 'title': '', 'content': '', 'state': 'skipped', 'error': reason})
    text = '# Setup and backups\n\nSave the configuration, then restart the application. Standard backups include support data and settings.'
    on_page({'url': config['seed_url'], 'title': 'Setup and backups', 'content': text, 'state': 'succeeded', 'error': None})
    return {'complete': True, 'error': None, 'stop_reason': 'page_limit' if config['max_pages'] == 1 else None}


def seed_key_preview(app, project, principal, *, capacity=False):
    """Create real durable pool states with synthetic credentials and evidence."""
    service, storage = app.state.ai_settings, app.state.storage
    saved = service.save(project, principal, 0, default_ai_settings() | {'provider': 'groq'})
    if capacity:
        library=storage.ai_account_keys(principal)
        ids=[key['id'] for key in library['keys'] if key['provider']=='groq' and key['purpose']!='decision']
        pool=storage.ai_project_key_selection(project,principal,saved['version'],library['version'],'generation',ids)
        for index in range(len(ids)+1, 100):
            pool = service.add_key(project, principal, pool['version'], f'Capacity key {index:02d}',
                                   'Capacity demo account', f'preview-groq-key-{index}')
        return {'kind': 'capacity', 'count': len(pool['keys'])}
    imported = storage.ai_knowledge_import(project, principal, str(uuid4()), 'groq-support.md', 'Support guide',
        '# Setup and backups\n\nSave the configuration, then restart the application. Standard backups include your support data and settings.')
    while app.state.ai_ingestion.run_once():
        pass
    source = storage.ai_knowledge_source(project, principal, imported['source']['id'])
    storage.ai_knowledge_change(project, principal, source['id'], source['version'], active=True, approved=True)
    service.add_key(project, principal, saved['version'], 'Main account', 'Preview main account', 'preview-groq-key')
    saved = service.read(project, principal)
    service.save(project, principal, saved['version'], saved['settings'] | {'model': 'openai/gpt-oss-120b'})
    entries = [
        ('Shared backup', 'Preview main account', 'preview-groq-key-2'),
        ('Independent backup', 'Preview independent account', 'preview-groq-key-3'),
        ('Paused backup', 'Preview independent account', 'preview-groq-key-4'),
        ('Not checked yet', 'Preview unchecked account', 'preview-groq-key-5'),
        ('Cooling account', 'Preview cooling account', 'preview-groq-key-cooldown'),
        ('Cooling account backup', 'Preview cooling account', 'preview-groq-key-6'),
        ('Invalid example', 'Preview invalid account', 'preview-groq-key-invalid'),
        ('Billing example', 'Preview billing account', 'preview-groq-key-payment'),
    ]
    for label, group, key in entries:
        pool = service.keys(project, principal)
        service.add_key(project, principal, pool['version'], label, group, key)
    for key in service.keys(project, principal)['keys']:
        if key['label'] == 'Not checked yet':
            continue
        saved = service.read(project, principal)
        storage.ai_store_provider_evidence(project, principal, saved['version'], saved['credential_version'],
            'openai/gpt-oss-120b', key_id=key['id'], key_version=key['version'],
            catalogue=['openai/gpt-oss-120b', 'openai/gpt-oss-20b'],
            capabilities={'text_generation': True, 'tool_calling': True, 'streaming': False, 'structured_output': False})
    pool = service.keys(project, principal)
    paused = next(key for key in pool['keys'] if key['label'] == 'Paused backup')
    service.update_key(project, principal, pool['version'], paused['id'], paused['version'],
                       label=paused['label'], quota_group=paused['quota_group'], enabled=False)
    for label, outcome in [('Cooling account', 'rate_limited'), ('Invalid example', 'invalid_credentials'), ('Billing example', 'payment_required')]:
        pool = service.keys(project, principal)
        key = next(key for key in pool['keys'] if key['label'] == label)
        snapshot = storage.ai_provider_snapshot(project, principal, pool['version'], key_id=key['id'])
        storage.ai_provider_check_outcome(project, principal, pool['version'], key['id'], key['version'],
            snapshot['health_epoch'], snapshot['group_epoch'], outcome, retry_after=600 if outcome == 'rate_limited' else None)
    return {'kind': 'mixed_states', 'count': len(service.keys(project, principal)['keys']),
            'cooldown_seconds': 600, 'automatic_recovery_trigger': 'A rate limit question on the -cooldown key fails once for 30 seconds.'}


def main():
    with TemporaryDirectory(prefix='suggestion-box-ai-ui-') as folder:
        config = Config(Path(folder) / 'feedback.sqlite', Fernet.generate_key().decode(),
                        'preview-setup-token-is-not-a-secret', base_url=BASE,
                        worker=True, rate_limit=10000, auth_rate_limit=10000)
        with patch('feedback.ai_installation.EmbeddingInstallation', FixtureInstallation):
            app = create_app(config)
        storage = app.state.storage
        fakes = {provider_id: FixtureProvider(provider_id) for provider_id in ('kodekey', 'gemini', 'mistral', 'groq', 'ramp')}
        app.state.ai_settings.provider_factory = lambda provider_id: fakes[provider_id]
        app.state.ai_generation.provider_factory = lambda provider_id: fakes[provider_id]
        app.state.ai_topics.provider_factory = fixture_decision_provider
        app.state.ai_generation.topic_gate.provider_factory = fixture_decision_provider
        admin = storage.create_native_account('ai-admin@example.test', hasher.hash(PASSWORD), 'Alex Morgan', 'admin', True)
        storage.create_native_account('participant@example.test', hasher.hash(PASSWORD), 'Jamie Taylor', 'participant', False)
        storage.create_session(admin['id'], hashlib.sha256(b'preview-seed-session').hexdigest(), 'preview-csrf', time.time() + 86400)
        principal = {'kind': 'admin', 'user_id': admin['id'], 'session_id': storage.session(hashlib.sha256(b'preview-seed-session').hexdigest())['session_id']}
        widget_preview = os.environ.get('SB_AI_WIDGET_PREVIEW') == '1'
        keys_preview = os.environ.get('SB_AI_KEYS_PREVIEW') == '1'
        batches_preview = os.environ.get('SB_AI_BATCHES_PREVIEW') == '1'
        if widget_preview or keys_preview or batches_preview:
            operation = storage.ai_runtime_begin(principal, 0, 'download')
            lease = storage.ai_runtime_claim(time.time())
            storage.ai_runtime_finish(operation['operation_id'], lease['lease_token'], manifest=EMBEDDING_PROFILE)
        projects = {}
        rows = [('acme', 'Acme Studio'), ('empty', 'New project')]
        if widget_preview:
            rows += [('guest', 'Acme guest reports'), ('account', 'Acme account feedback')]
        if keys_preview:
            rows += [('keys', 'Groq key management'), ('keycap', 'Groq 99-key limit')]
        if batches_preview:
            rows += [('batches', 'Recoverable section batches')]
        key_scenarios = {}
        for identifier, name in rows:
            project = storage.create_project({'identifier': identifier, 'name': name, 'description': 'Synthetic browser verification project'}, admin['id'])
            projects[identifier] = project['id']
            features = storage.create_board(project['id'], {'identifier': 'features', 'name': 'Feature requests', 'description': ''})
            bugs = storage.create_board(project['id'], {'identifier': 'bugs', 'name': 'Bug reports', 'description': ''})
            storage.chat_configure(project['id'], principal, True, ['http://localhost:8770'], 0)
            if identifier == 'acme':
                storage.ai_knowledge_import(project['id'], principal, str(uuid4()), 'getting-started.md', 'Getting started',
                    '# Setup and backups\n\nSave the configuration, then restart the application. Standard backups include your support data and settings.')
            if identifier in ('keys', 'keycap'):
                key_scenarios[identifier] = seed_key_preview(app, project['id'], principal, capacity=identifier == 'keycap')
            if identifier in ('guest', 'account', 'batches'):
                imported = storage.ai_knowledge_import(project['id'], principal, str(uuid4()), 'support-guide.md', 'Acme support guide',
                    '\n\n'.join(f'# Batch topic {i}\n\nSave the configuration, then restart the application. Standard backups include your support data and settings.' for i in range(12)) if identifier == 'batches' else
                    '# Setup and backups\n\nSave the configuration, then restart the application. Standard backups include your support data and settings.')
                while app.state.ai_ingestion.run_once():
                    pass
                source = storage.ai_knowledge_source(project['id'], principal, imported['source']['id'])
                storage.ai_knowledge_change(project['id'], principal, source['id'], source['version'], active=True, approved=True)
                if os.environ.get('SB_AI_SOURCES_PREVIEW') == '1':
                    # Source-card visual fixture: stored public-page evidence, no fetch.
                    crawl = storage.ai_crawl_begin(project['id'], principal, str(uuid4()), 'Acme help center',
                        {'seed_url':'https://support.acme.example/backups','approved_origins':['https://support.acme.example'],
                         'include_paths':['/'],'exclude_paths':[],'max_pages':1,'max_depth':1})
                    job = storage.ai_claim_job(time.time()+.01, kinds=('crawl',))
                    storage.ai_crawl_record(job['id'],job['lease_token'], {'url':'https://support.acme.example/backups',
                        'title':'Back up your workspace and restore your settings', 'content':
                        'Save the configuration, then restart the application. Standard backups include your support data and settings. '
                        'Store the backup somewhere safe before updating. To restore a workspace, stop the running application, '
                        'restore the backup to its existing data volume, and start the application again. '
                        'Your support team can help you check the restored configuration before you continue.', 'status':'fetched','error':None})
                    storage.ai_crawl_finish(job['id'],job['lease_token'],True)
                    while app.state.ai_ingestion.run_once():
                        pass
                    website_source = storage.ai_knowledge_source(project['id'],principal,crawl['source']['id'])
                    storage.ai_knowledge_change(project['id'],principal,website_source['id'],website_source['version'],active=True,approved=True)
                settings = default_ai_settings() | {'model': 'gpt-6-luna', 'feature_board_id': features['id'], 'bug_board_id': bugs['id'],
                    'feedback_url': 'https://feedback.acme.example/feedback', 'feature_feedback_url': 'https://feedback.acme.example/ideas', 'bug_feedback_url': 'https://feedback.acme.example/bugs'}
                library=storage.ai_account_keys(principal)
                shared=next((key for key in library['keys'] if key['provider']=='kodekey'),None)
                if shared:
                    saved=app.state.ai_settings.save(project['id'],principal,0,settings)
                    storage.ai_project_key_selection(project['id'],principal,saved['version'],library['version'],'generation',[shared['id']])
                    saved=app.state.ai_settings.read(project['id'],principal)
                else:
                    saved = app.state.ai_settings.save(project['id'], principal, 0, settings, key='preview-kodekey-key')
                saved = storage.ai_store_provider_evidence(project['id'], principal, saved['version'], saved['credential_version'], 'gpt-6-luna',
                    catalogue=['gpt-6-luna'], capabilities={'text_generation': True, 'tool_calling': True, 'streaming': False, 'structured_output': False})
                settings.update(enabled=identifier != 'batches', allow_guest_reports=identifier == 'guest', tools={tool: identifier != 'batches' for tool in settings['tools']})
                app.state.ai_settings.save(project['id'], principal, saved['version'], settings)
        if os.environ.get('SB_AI_SETUP_PREVIEW') == '1':
            for label, secret in [('Ramp main', 'preview-ramp-key'), ('Ramp backup', 'preview-ramp-key-2')]:
                library = app.state.ai_account_keys.read(principal)
                app.state.ai_account_keys.add(principal, library['version'], 'ramp', 'generation', label, 'Preview Ramp account', secret)
        app.state.ai_crawl_worker.runner = fixture_crawl
        Path('/tmp/suggestion-box-ai-ui-preview.json').write_text(json.dumps({'base_url': BASE, 'directory': folder, 'projects': projects,
            'admin_email': 'ai-admin@example.test', 'password': PASSWORD, 'provider_key': 'preview-kodekey-key',
            'provider_keys': {provider_id: fake.key for provider_id, fake in fakes.items()},
            'provider_models': {provider_id: fake.models for provider_id, fake in fakes.items()},
            'key_scenarios': key_scenarios,
            'model_token': 'preview-model-token', 'boundary': 'All external provider, embedding and crawl work is simulated.'}, indent=2))
        print('Disposable AI interface fixture at ' + BASE, flush=True)
        website = None
        if widget_preview:
            class Website(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    path = self.path.split('?')[0]
                    identifier = {'/account': 'account', '/human': 'acme', **({'/keys': 'keys'} if keys_preview else {})}.get(path, 'guest')
                    title = {'/ideas': 'Share a feature idea', '/bugs': 'Report a bug', '/feedback': 'Your feedback'}.get(path, 'A little help, right here.')
                    body = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<title>Acme · Support preview</title><style>body{{margin:0;background:#f7f8f5;color:#25382c;font:17px/1.65 system-ui}}'
                        'main{max-width:900px;margin:8vh auto;padding:28px}nav{display:flex;gap:24px;flex-wrap:wrap}a{color:#147d50}'
                        'h1{font-size:clamp(32px,5vw,64px);line-height:1.1;letter-spacing:-2px;max-width:650px}p{max-width:560px;color:#526256}'
                        '.note{font-size:13px;border-top:1px solid #d9e0d7;padding-top:20px;margin-top:50px}</style><main>'
                        '<nav><strong>ACME STUDIO</strong><a href="/">Guest reports</a><a href="/account">Account feedback</a><a href="/human">Human support</a><button id="theme">Use dark chat</button></nav>'
                        f'<h1>{title}</h1><p>Ask about setup and backups, share an idea, or talk to our team. Support stays part of your website.</p>'
                        '<p class="note">Disposable interface preview. Provider responses and embeddings are simulated. No live requests or customer data.</p>'
                        f'</main><div id="support"></div><script src="{BASE}/widget.js"></script><script>'
                        f'const chat=SuggestionBoxChat.mount("#support",{{server:"{BASE}",project:"{projects[identifier]}",title:"Acme support",theme:{{mode:"light"}}}});'
                        'let dark=false;document.getElementById("theme").onclick=()=>{dark=!dark;chat.setTheme(dark?"dark":"light");document.getElementById("theme").textContent=dark?"Use light chat":"Use dark chat";};'
                        '</script></html>').encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            website = ThreadingHTTPServer(('127.0.0.1', 8770), Website)
            threading.Thread(target=website.serve_forever, daemon=True).start()
            print('Disposable customer website at http://localhost:8770', flush=True)
        try:
            uvicorn.run(app, host='127.0.0.1', port=8769, log_level='warning', access_log=False)
        finally:
            if website:
                website.shutdown()


if __name__ == '__main__':
    main()
