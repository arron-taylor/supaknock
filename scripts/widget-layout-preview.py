"""Disposable real-SQLite widget layout QA; no production data or provider calls.

Run .venv/bin/python scripts/widget-layout-preview.py, then open
http://127.0.0.1:8773/preview. Admin at :8772: admin@example.test / synthetic-preview-password.
"""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import threading
from fastapi.testclient import TestClient
import uvicorn
from feedback.api import create_app
from feedback.config import Config


def main():
    with TemporaryDirectory(prefix='sb-widget-preview-') as work:
        base = 'http://127.0.0.1:8772'
        cfg = Config(Path(work)/'feedback.sqlite', Fernet.generate_key().decode(), 'synthetic-widget-setup-token', worker=False, base_url=base, rate_limit=10000, auth_rate_limit=10000)
        app = create_app(cfg)
        client = TestClient(app)
        result = client.post('/api/v1/setup', json={'email':'admin@example.test','password':'synthetic-preview-password','display_name':'Preview owner','setup_token':cfg.setup_token})
        assert result.status_code == 201
        client.headers.update({'Origin':base,'X-CSRF-Token':result.json()['csrf_token']})
        result = client.post('/api/v1/projects', json={'identifier':'studio','name':'Studio'})
        assert result.status_code == 201
        project = result.json()
        result = client.put('/api/v1/admin/chat/projects/'+project['id'], json={'enabled':True,'origins':['http://127.0.0.1:8773'],'version':0})
        assert result.status_code == 200
        result = client.post('/api/v1/projects/'+project['id']+'/boards', json={'identifier':'ideas','name':'Ideas'})
        assert result.status_code == 201

        def preview(presentation: str = 'bubble', scroll: int = 0, manual: bool = False, dark: bool = False):
            presentation = presentation if presentation in ['bubble','bar','inline'] else 'bubble'
            options = {'presentation':presentation,'revealAfterScroll':max(0,scroll),'title':'Studio support','theme':{'mode':'dark' if dark else 'light'}}
            data = json.dumps(options)
            embed = f'<script type="application/json" id="chat-appearance">{data}</script><script defer src="{base}/widget.js" data-server="{base}" data-project="{project["id"]}" data-target="#support" data-config="chat-appearance"></script>'
            if manual:
                embed = f'<script src="{base}/widget.js"></script><script>SuggestionBoxChat.mount("#support",'+json.dumps(options|{'server':base,'project':project['id']})+');</script>'
            return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Studio · Widget preview</title><style>
body{margin:0;background:#f7f8f5;color:#25382c;font:16px/1.65 system-ui}main{max-width:960px;margin:auto;padding:32px 24px}nav{display:flex;gap:16px;flex-wrap:wrap}a{color:#147d50}h1{font-size:clamp(36px,6vw,64px);line-height:1.1;letter-spacing:-2px;max-width:650px}p{max-width:580px;color:#526256}.hero{min-height:1000px;padding-top:60px}.support-section:has([data-sb-presentation="inline"]){transform:translateZ(0);overflow:hidden}.support-section{max-width:600px;padding:24px;border:1px solid #dbe3de;border-radius:24px;background:#fff}footer{padding:40px 0 160px}button{font:inherit;cursor:pointer;padding:10px 16px;border-radius:10px;border:1px solid #c6d8cb;background:white;color:#147d50}.note{font-size:13px}</style><main>
<nav><strong>STUDIO</strong><a href="/preview">Bubble</a><a href="/preview?presentation=bar&scroll=600">Scroll bar</a><a href="/preview?presentation=inline&scroll=600">Inline</a><a href="/preview?presentation=bar&manual=true&dark=true">Manual dark bar</a></nav>
<section class="hero"><h1>A little help,<br>right where you need it.</h1><p>Explore at your own pace. Our team is here when you have a question.</p><button onclick="SuggestionBoxChat.get('#support').open()">Open support now</button> <a href="#support-section">Find support below ↓</a><p class="note">Disposable layout preview. Real chat APIs, synthetic users, no AI provider requests.</p></section>
<section class="support-section" id="support-section"><h2>Let’s talk</h2><p>This div lives inside a page section. The chat button belongs here. Your conversation opens above the page.</p><div id="support"></div><p id="after-chat">Your page stays right where you left it.</p></section><footer>You’re in good company.</footer></main>'''+embed+'</html>'

        class Website(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query)
                try:
                    scroll = int(query.get('scroll', ['0'])[0])
                except ValueError:
                    scroll = 0
                body = preview(query.get('presentation', ['bubble'])[0], scroll,
                    query.get('manual', ['false'])[0] == 'true', query.get('dark', ['false'])[0] == 'true').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        website = ThreadingHTTPServer(('127.0.0.1', 8773), Website)
        threading.Thread(target=website.serve_forever, daemon=True).start()
        try:
            uvicorn.run(app, host='127.0.0.1', port=8772, access_log=False, log_level='warning')
        finally:
            website.shutdown()
            website.server_close()

if __name__ == '__main__':
    main()
