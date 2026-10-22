"""Audit local candidate source parity and every exported layer; never reads local secrets."""
import hashlib,json,subprocess,sys,tarfile,tempfile
from pathlib import Path
image=sys.argv[1]
files=[*Path('feedback').glob('connected_apps*.py'),Path('feedback/storage/migrations/019_connected_apps.sql'),*Path('feedback/web').glob('connected-*')]
expected={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
program="import json,hashlib,os; names="+repr(list(expected))+"; print(json.dumps({'uid':os.getuid(),'hashes':{p:hashlib.sha256(open('/app/'+p,'rb').read()).hexdigest() for p in names}}))"
actual=json.loads(subprocess.check_output(['docker','run','--rm','--network','none',image,'python','-c',program],text=True))
assert actual['uid']==10001 and actual['hashes']==expected
with tempfile.TemporaryDirectory(prefix='sb-layer-audit-') as directory:
    archive=Path(directory)/'image.tar';subprocess.run(['docker','image','save','-o',str(archive),image],check=True,stdout=subprocess.DEVNULL)
    with tarfile.open(archive) as bundle:
        manifests=json.load(bundle.extractfile('manifest.json'));layers={p for m in manifests for p in m['Layers']};checked=0
        for layer in layers:
            with tarfile.open(fileobj=bundle.extractfile(layer),mode='r|*') as data:
                for member in data:
                    name=member.name.lstrip('./');base=Path(name).name
                    assert base!='creds.txt' and not name.startswith('app/.env'), 'Forbidden credential artifact in layer'
                    assert not name.endswith(('.safetensors','.gguf','pytorch_model.bin')), 'Model weights in layer'
                    assert not name.startswith('app/docs/') and not name.startswith('app/tests/'), 'Non-runtime context in layer'
            checked+=1
print(json.dumps({'image':image,'layers_checked':checked,'nonroot':True,'source_files_matched':len(files),'no_credentials_or_model_weights':True}))
