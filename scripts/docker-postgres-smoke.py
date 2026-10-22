"""Disposable TLS PostgreSQL/Supabase container persistence and restore check.

Set SMOKE_IMAGE to the locally built app image. Only uniquely named resources
created by this script are removed; no production database or provider is used.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import secrets
import subprocess
import tempfile
import time
from uuid import uuid4
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.fernet import Fernet
from cryptography.x509.oid import NameOID

suffix = secrets.token_hex(6)
network = 'supaknock-pg-smoke-' + suffix
server, app = network + '-db', network + '-app'
volume = network + '-data'
image = os.environ.get('SMOKE_IMAGE', 'supaknock:database-local')
port = int(os.environ.get('SMOKE_PORT', '18089'))
base = f'http://127.0.0.1:{port}'
password, token, key = secrets.token_urlsafe(32), secrets.token_urlsafe(32), Fernet.generate_key().decode()


def docker(*args, data=None):
    result = subprocess.run(['docker', *args], input=data, capture_output=True)
    if result.returncode:
        raise RuntimeError('Docker smoke operation failed: ' + args[0])
    return result.stdout.decode().strip()


def ready():
    for _ in range(150):
        try:
            if httpx.get(base + '/readyz', timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.2)
    raise RuntimeError('PostgreSQL app container did not become ready')


with tempfile.TemporaryDirectory(prefix=network) as directory:
    root = Path(directory)
    try:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'database')])
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(private.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1))
                .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('database')]), critical=False)
                .sign(private, hashes.SHA256()))
        (root/'server.key').write_bytes(private.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        (root/'server.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (root/'server.key').chmod(0o600)
        pg_env=root/'postgres.env';pg_env.write_text('POSTGRES_PASSWORD='+password+'\nPOSTGRES_DB=supaknock\n');pg_env.chmod(0o600)
        docker('network', 'create', network)
        docker('volume', 'create', volume)
        docker('run', '-d', '--name', server, '--network', network, '--network-alias', 'database',
               '--env-file', str(pg_env), '-v', str(root)+':/tls:ro', os.environ.get('SMOKE_POSTGRES_IMAGE','postgres:17-alpine'),
               'sh', '-c', 'cp /tls/server.key /tls/server.crt /tmp/; chown postgres:postgres /tmp/server.key /tmp/server.crt; chmod 600 /tmp/server.key; exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key')
        for _ in range(100):
            if subprocess.run(['docker','exec',server,'pg_isready','-U','postgres'],capture_output=True).returncode==0:break
            time.sleep(.2)
        else:raise RuntimeError('TLS PostgreSQL server did not become ready')

        def start(provider='postgres', database='supaknock'):
            env=root/'app.env'
            env.write_text('\n'.join(['DATABASE_PROVIDER='+provider,
                f'DATABASE_URL=postgresql://postgres:{password}@database:5432/{database}?sslmode=require',
                'APPLICATION_SECRET='+key,'SETUP_TOKEN='+token,'PUBLIC_BASE_URL='+base,
                'AUTH_RATE_LIMIT_PER_MINUTE=10000','RATE_LIMIT_PER_MINUTE=10000'])+'\n')
            env.chmod(0o600)
            docker('run','-d','--name',app,'--network',network,'--env-file',str(env),
                   '-p',f'127.0.0.1:{port}:8000','-v',volume+':/data',image)
            ready()

        start()
        client=httpx.Client(base_url=base+'/api/v1',timeout=15)
        setup=client.post('/setup',json={'email':'smoke@example.test','password':password,'setup_token':token}).raise_for_status().json()
        client.headers.update({'Origin':base,'X-CSRF-Token':setup['csrf_token']})
        project=client.post('/projects',json={'identifier':'smoke','name':'PostgreSQL smoke'}).raise_for_status().json()
        board=client.post('/projects/'+project['id']+'/boards',json={'identifier':'ideas','name':'Ideas','visibility':'private'}).raise_for_status().json()
        request=client.post('/boards/'+board['id']+'/requests',json={'title':'Durable feedback','description':'Survives database restore.'}).raise_for_status().json()
        form=client.post('/admin/projects/'+project['id']+'/forms',json={'name':'Early access','enabled':True,'public_enabled':True,'embed_enabled':True,'embed_origins':['https://embed.example'],'fields':[{'key':'email','label':'Email','type':'email','required':True}]}).raise_for_status().json()
        form_key=client.post('/projects/'+project['id']+'/keys',json={'name':'Forms backend','permissions':['forms:submit']}).raise_for_status().json()['secret']
        forms_backend=httpx.Client(base_url=base+'/api/v1',timeout=15,headers={'Authorization':'Bearer '+form_key})
        form_body={'client_request_id':str(uuid4()),'answers':{'email':'test@example.com'}}
        receipt=forms_backend.post('/forms/'+form['id']+'/submissions',json=form_body).raise_for_status().json()
        public_forms=httpx.Client(base_url=base+'/api/v1',timeout=15,headers={'Origin':base})
        public_ticket=public_forms.get('/public/forms/'+form['id']).raise_for_status().json()['ticket']
        embed_forms=httpx.Client(base_url=base+'/api/v1',timeout=15,headers={'Origin':'https://embed.example'})
        embed_ticket=embed_forms.get('/embed/forms/'+form['id']).raise_for_status().json()['ticket']
        embed_body={'client_request_id':str(uuid4()),'answers':{'email':'embed@example.com'},'ticket':embed_ticket}
        assert embed_forms.post('/embed/forms/'+form['id']+'/submissions',json=embed_body).raise_for_status().json()=={'ok':True}
        public_body={'client_request_id':str(uuid4()),'ticket':public_ticket,'answers':{'email':'public@example.com'}}
        assert public_forms.post('/public/forms/'+form['id']+'/submissions',json=public_body).raise_for_status().json()=={'ok':True}
        connection_path='/projects/'+project['id']+'/app-data/connection'
        connection=client.put(connection_path,json={'version':0,'base_url':'https://app.example.test','token':'synthetic-token'}).raise_for_status().json()
        assert connection['has_token']
        native=httpx.Client(base_url=base+'/api/v1',timeout=15)
        login=native.post('/auth/native/login',json={'email':'smoke@example.test','password':password}).raise_for_status().json()
        native.headers['Authorization']='Bearer '+login['token']
        native.put('/admin/chat/projects/'+project['id'],json={'enabled':True,'origins':['https://widget.example.test'],'version':0}).raise_for_status()
        visitor=httpx.Client(base_url=base+'/api/v1',timeout=15,headers={'Origin':'https://widget.example.test'})
        session=visitor.post('/chat/projects/smoke/sessions').raise_for_status().json()
        visitor.headers['X-Chat-Token']=session['token']
        messages='/chat/conversations/'+session['conversation']['id']+'/messages'
        message={'client_message_id':str(uuid4()),'body':'Durable chat message'}
        first=visitor.post(messages,json=message).raise_for_status().json()

        def verify():
            assert public_forms.get('/public/forms/'+form['id']).status_code==200
            assert embed_forms.get('/embed/forms/'+form['id']).status_code==200
            assert embed_forms.post('/embed/forms/'+form['id']+'/submissions',json=embed_body).raise_for_status().json()=={'ok':True}
            assert public_forms.post('/public/forms/'+form['id']+'/submissions',json=public_body).raise_for_status().json()=={'ok':True}
            assert len(client.get('/admin/forms/'+form['id']+'/submissions').raise_for_status().json()['items'])==3
            assert client.get('/admin/form-submissions/'+receipt['id']).raise_for_status().json()['answers']==form_body['answers']
            assert forms_backend.post('/forms/'+form['id']+'/submissions',json=form_body).raise_for_status().json()==receipt
            assert client.get('/boards/'+board['id']).raise_for_status().json()['visibility']=='private'
            assert public_forms.get('/requests/'+request['id']).status_code==404
            assert client.get('/requests/'+request['id']).raise_for_status().json()['title']=='Durable feedback'
            assert client.get(connection_path).raise_for_status().json()['has_token']
            assert native.get('/auth/me').status_code==200
            assert visitor.post(messages,json=message).raise_for_status().json()['id']==first['id']
            assert len(visitor.get(messages).raise_for_status().json()['items'])==1

        docker('restart',app);ready();verify()
        docker('rm','-f',app);start('supabase');verify()
        docker('exec',app,'python','-m','feedback.maintenance','backup','/data/backup')
        docker('cp',app+':/data/backup',str(root/'backup'))
        bundle=root/'backup'
        assert (bundle/'application.key').read_text()==key
        assert (bundle/'setup.token').read_text()==token
        assert json.loads((bundle/'backup.json').read_text())['database_provider']=='supabase'
        docker('exec',server,'createdb','-U','postgres','restored')
        docker('exec','-i',server,'pg_restore','-U','postgres','-d','restored','--no-owner','--no-acl','--exit-on-error',data=(bundle/'feedback.dump').read_bytes())
        docker('rm','-f',app);start('postgres','restored');verify()
        followup=visitor.post(messages,json={'client_message_id':str(uuid4()),'body':'New message after restore.'}).raise_for_status().json()
        assert followup['sequence']==2
        assert not (bundle/'feedback.sqlite').exists()
        print(json.dumps({'tls_postgres':True,'supabase_provider_alias':True,'restart':True,'recreate':True,
                          'backup_restore':True,'secrets_preserved':True,'sessions_and_chat_idempotence':True,
                          'post_restore_write':True,'hosted_supabase_tested':False}))
    finally:
        for name in (app,server):subprocess.run(['docker','rm','-f','-v',name],capture_output=True)
        subprocess.run(['docker','volume','rm',volume],capture_output=True)
        subprocess.run(['docker','network','rm',network],capture_output=True)
