"""Repeatable docs/schema parity check. Does not touch an installation database."""
import json,re,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from feedback.api import create_app
from feedback.config import Config
from feedback.connected_apps_protocol import RESPONSES,REQUESTS,validated_response
with tempfile.TemporaryDirectory() as d:
    schema=create_app(Config(Path(d)/'schema.sqlite',Fernet.generate_key().decode(),'schema-only',worker=False)).openapi()
    assert schema==json.loads(Path('docs/openapi.json').read_text()),'Installation OpenAPI drift'
for kind,models in [('request',REQUESTS),('response',RESPONSES)]:
    for name,model in models.items():
        schema=json.loads(Path(f'docs/protocol/v1/{name}-{kind}.json').read_text());schema.pop('$schema');assert schema==model.model_json_schema(by_alias=True)
# Check current navigation/integration docs; historical evaluation links retain their dated meaning.
for path in [Path('README.md'),*map(Path,['docs/README.md','docs/connected-apps.md','docs/connected-apps-agent-brief.md','docs/protocol/v1/README.md','docs/integration.md','docs/operations.md'])]:
    for target in re.findall(r'\]\(([^)]+)\)',path.read_text()):
        target=target.split('#')[0]
        if target and not re.match(r'[a-z]+:',target) and not target.startswith('/'):
            assert (path.parent/target).exists(),f'Broken link: {path}: {target}'
print('Runtime OpenAPI, protocol schemas and current documentation links match.')
