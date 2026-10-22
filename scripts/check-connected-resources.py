"""Small synthetic demand-read resource sample, not a network performance claim."""
import importlib.util,json,statistics,sys,time,tracemalloc
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
spec=importlib.util.spec_from_file_location('connected_ui_fixture',Path(__file__).with_name('connected-ui-preview.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
connections=module.client.get('/api/v1/admin/connected-apps').json()['items']
durations=[];tracemalloc.start()
for _ in range(4):
    for p in connections:
        start=time.perf_counter()
        r=module.client.post('/api/v1/projects/'+p['project_id']+'/app-data/users/query',json={'version':p['version'],'query':{'limit':50}})
        assert r.status_code==200
        durations.append((time.perf_counter()-start)*1000)
_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
print(json.dumps({'environment':'local TestClient, real SQLite, four in-memory synthetic apps; no network latency','reads':len(durations),'p50_ms':round(statistics.median(durations),2),'p95_ms':round(sorted(durations)[int(.95*(len(durations)-1))],2),'peak_traced_python_mib':round(peak/1048576,2),'page_limit':50,'note':'Shared admission tests independently verify at most four active calls and two per connection.'},indent=2))
