"""Export standalone protocol schemas; no installation or credentials required."""
from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from feedback.connected_apps_protocol import REQUESTS,RESPONSES
root=Path(__file__).resolve().parents[1]/'docs/protocol/v1'
root.mkdir(parents=True,exist_ok=True)
for suffix,models in [('request',REQUESTS),('response',RESPONSES)]:
    for name,model in models.items():
        schema={'$schema':'https://json-schema.org/draft/2020-12/schema',**model.model_json_schema(by_alias=True)}
        (root/f'{name}-{suffix}.json').write_text(json.dumps(schema,indent=2)+'\n')
