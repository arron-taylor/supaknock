"""Disposable, two-origin browser QA. No production data or external services."""
from pathlib import Path
from tempfile import TemporaryDirectory
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial
from threading import Thread
import sys,json,re
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import uvicorn
from feedback.api import create_app
from feedback.config import Config

with TemporaryDirectory(prefix='supaknock-embed-preview-') as directory:
    root=Path(directory)
    app=create_app(Config(root/'feedback.sqlite',Fernet.generate_key().decode(),'forms-preview-setup-token',base_url='http://localhost:8776',worker=False))
    admin=TestClient(app)
    login=admin.post('/api/v1/setup',json={'email':'admin@example.com','password':'local-simulator-password','setup_token':'forms-preview-setup-token','display_name':'Alex'}).json()
    admin.headers.update({'Origin':'http://localhost:8776','X-CSRF-Token':login['csrf_token']})
    project=admin.post('/api/v1/projects',json={'identifier':'supaknock','name':'SupaKnock'}).json()
    admin.post('/api/v1/projects/'+project['id']+'/boards',json={'identifier':'ideas','name':'Feature requests'})
    form=admin.post('/api/v1/admin/projects/'+project['id']+'/forms',json={'name':'Try the iPhone app','description':'Leave your email for an early look.','enabled':True,'public_enabled':True,'embed_enabled':True,'embed_origins':['http://localhost:8777'],'fields':[{'key':'email','label':'Email','type':'email','required':True},{'key':'name','label':'Name','type':'text'},{'key':'platform','label':'Your phone','type':'choice','choices':['iPhone','Android']},{'key':'message','label':'What are you building?','type':'textarea'}]}).json()
    assert 'id' in form,form
    from feedback.errors import Error
    real_submit=app.state.forms.public_submit
    interrupted=set()
    def uncertain_submit(fid,data,version,bucket,origin=None):
        result=real_submit(fid,data,version,bucket,origin)
        if data['answers'].get('email')=='retry@example.com' and data['client_request_id'] not in interrupted:
            interrupted.add(data['client_request_id'])
            raise Error(503,'preview_interruption','The preview lost its first confirmation. Try again.')
        return result
    app.state.forms.public_submit=uncertain_submit
    appearance={'submitText':'Count me in','theme':{'mode':'light','radius':18,'light':{'accent':'#226847','onAccent':'#ffffff'},'dark':{'accent':'#a6dcb3','onAccent':'#183b25'}}}
    snippet=f'''<div id="early-access"></div><script type="application/json" id="appearance">{json.dumps(appearance)}</script><script defer src="http://localhost:8776/form-widget.js" data-server="http://localhost:8776" data-form="{form['id']}" data-target="#early-access" data-config="appearance"></script>'''
    html='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Forms embed preview</title><style>body{margin:0;background:#eef2ea;color:#23352a;font:15px/1.8 -apple-system,sans-serif}main{max-width:1040px;margin:70px auto;padding:24px;display:grid;grid-template-columns:1fr 1fr;gap:80px;align-items:start}h1{font-size:56px;letter-spacing:-3px;line-height:1.08;margin:22px 0}p{max-width:340px;color:#5d6f61}button,a{font:inherit;color:inherit}button{padding:10px 18px;background:transparent;border:1px solid #889b8b;border-radius:8px;cursor:pointer}nav{display:flex;flex-wrap:wrap;gap:16px;margin:24px 0}small{color:#5d6f61}body.dark{background:#17221b;color:#e8eee6}.dark p,.dark small{color:#adbfae}@media(max-width:700px){main{grid-template-columns:1fr;gap:28px;margin:12px auto;padding:20px}h1{font-size:44px}}</style><main><section><small>SUPAKNOCK / FORMS</small><h1>Right at home.<br>On your website.</h1><p>A form that fits where you put it. Same theme options as chat. One private inbox for the responses.</p><nav><button id="theme">Switch to dark</button><a href="/multiple.html">Two forms</a></nav><small>Local preview · Synthetic data only</small></section><section>SNIPPET</section></main><script src="/preview.js"></script></html>'''.replace('SNIPPET',snippet)
    css=re.search(r'<style>(.*?)</style>',html)[1]
    (root/'preview.css').write_text(css)
    html=re.sub(r'<style>.*?</style>','<link rel="stylesheet" href="/preview.css">',html)
    html=html.replace('<a href="/multiple.html">Two forms</a>','<a href="/multiple.html">Two forms</a><button id="destroy">Unmount</button><button id="remount">Remount</button>')
    (root/'index.html').write_text(html)
    (root/'preview.js').write_text("document.getElementById('theme').onclick=()=>{const dark=document.body.classList.toggle('dark');SupaKnockForms.get('#early-access').setTheme(dark?'dark':'light');document.getElementById('theme').textContent=dark?'Switch to light':'Switch to dark';};")
    with (root/'preview.js').open('a') as file:
        file.write("document.getElementById('destroy').onclick=()=>SupaKnockForms.get('#early-access')?.destroy();document.getElementById('remount').onclick=()=>SupaKnockForms.mount('#early-access',{server:'http://localhost:8776',form:'"+form['id']+"',submitText:'Count me in'});")
    compact=json.dumps({'density':'compact','showTitle':False,'submitText':'Join beta','theme':{'mode':'dark','radius':0}})
    second=f'<div id="second"></div><script type="application/json" id="second-style">{compact}</script><script defer src="http://localhost:8776/form-widget.js" data-server="http://localhost:8776" data-form="{form["id"]}" data-target="#second" data-config="second-style"></script>'
    (root/'multiple.html').write_text(html.replace('<section><small>SUPAKNOCK / FORMS</small>', '<section><small>SUPAKNOCK / FORMS</small>').replace('<p>A form that fits where you put it. Same theme options as chat. One private inbox for the responses.</p>',second))
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
        def end_headers(self):
            self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self' http://localhost:8776; style-src 'self' http://localhost:8776; connect-src http://localhost:8776; base-uri 'none'; form-action 'none'")
            super().end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',8777),partial(Quiet,directory=str(root)))
    Thread(target=server.serve_forever,daemon=True).start()
    print('Embed preview: http://localhost:8777',flush=True)
    print('Admin form: http://localhost:8776/admin/forms/'+form['id'],flush=True)
    print('Admin sign-in: admin@example.com / local-simulator-password',flush=True)
    uvicorn.run(app,host='127.0.0.1',port=8776,log_level='warning')
