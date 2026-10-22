"""Native-admin-only MCP connection library and explicit project approvals."""
from typing import Annotated,Literal
from urllib.parse import urlsplit
from fastapi import Depends
from pydantic import BaseModel,Field,SecretStr
from feedback.ai_settings_api import AIInput
from feedback.ai_http import admin_dependency
from feedback.ai_mcp_protocol import endpoint,validate
from feedback.ai_mcp_transport import MCPClient,Admission
from feedback.errors import Error


class ConnectionWrite(AIInput):
    version:int=Field(ge=0)
    label:str=Field(min_length=1,max_length=80)
    url:str=Field(max_length=2048)
    enabled:bool=False
    token:SecretStr|None=Field(default=None,max_length=4096)
    link_origins:list[str]=Field(default_factory=list,max_length=10)


class Version(AIInput):
    version:int=Field(ge=1)


class ToolTest(Version):
    name:str=Field(min_length=1,max_length=128)
    arguments:dict


class ToolSelection(AIInput):
    name:str=Field(min_length=1,max_length=128)
    digest:str=Field(pattern='^[0-9a-f]{64}$')


class Binding(AIInput):
    connection_id:str
    connection_version:int=Field(ge=1)
    tools:list[ToolSelection]=Field(max_length=8)
    context_kind:Literal['podcast_episode']|None=None


class ProjectWrite(AIInput):
    version:int=Field(ge=0)
    source_mode:Literal['knowledge','tools','combined']
    bindings:list[Binding]=Field(max_length=4)


class ConnectionOutput(BaseModel):
    id:str
    label:str
    url:str
    version:int
    enabled:bool
    has_token:bool
    tested:bool
    tested_at:str|None
    error:str|None
    created_at:str
    updated_at:str
    catalogue:list[dict]
    link_origins:list[str]
    projects:list[dict]


class ConnectionList(BaseModel):
    items:list[ConnectionOutput]


class RemovedOutput(BaseModel):
    removed:bool


class TestOutput(BaseModel):
    result:dict
    notice:str


class BindingOutput(BaseModel):
    connection_id:str
    connection_version:int
    context_kind:Literal["podcast_episode"]|None=None
    tools:list[ToolSelection]


class ProjectOutput(BaseModel):
    project_id:str
    version:int
    source_mode:Literal['knowledge','tools','combined']
    bindings:list[BindingOutput]


class MCPAdmin:
    def __init__(self,storage,vault,*,client_factory=MCPClient):
        self.storage=storage;self.vault=vault;self.client_factory=client_factory;self.admission=Admission()

    def save(self,p,data,cid=None):
        old=self.storage.mcp_snapshot(p,cid,data.version) if cid else None
        url=endpoint(data.url)
        supplied='token' in data.model_fields_set
        if old and old['url']!=url and not supplied:raise Error(400,'mcp_replace_token','Supply a replacement token or explicitly choose no authentication when changing the endpoint')
        token=data.token.get_secret_value() if data.token else None
        if token and any(ord(x)<=32 or ord(x)>=127 for x in token):raise Error(400,'invalid_input','Use a valid bearer token')
        protected=self.vault.protect(token) if token else None
        if old and not supplied:protected=old['protected_token']
        origins=[]
        for value in data.link_origins:
            parsed=urlsplit(endpoint(value))
            if parsed.path not in ('','/'):raise Error(400,'invalid_input','Source link origins cannot contain paths')
            origins.append('https://'+parsed.netloc.lower())
        return self.storage.mcp_save(p,cid,data.version,label=data.label,url=url,enabled=data.enabled,protected_token=protected,link_origins=list(dict.fromkeys(origins)))

    async def discover(self,p,cid,version):
        row=self.storage.mcp_snapshot(p,cid,version)
        client=self.client_factory(row['url'],self.vault.reveal(row['protected_token']) if row['protected_token'] else '',check=lambda:self.storage.mcp_snapshot(p,cid,version))
        try:
            async with self.admission.slot(cid,'admin',p['user_id']):catalog=await client.discover()
            return self.storage.mcp_discovered(p,cid,version,catalog)
        except Error as error:
            self.storage.mcp_discovered(p,cid,version,[],error.code)
            raise
        finally:await client.close()

    async def test(self,p,cid,data):
        row=self.storage.mcp_snapshot(p,cid,data.version)
        import json
        approved=next((t for t in json.loads(row['catalogue']) if t['name']==data.name and not t.get('unsupported')),None)
        if not approved or row['tested_version']!=data.version:raise Error(409,'mcp_not_tested','Discover and review this tool first')
        validate(approved['inputSchema'],data.arguments)
        client=self.client_factory(row['url'],self.vault.reveal(row['protected_token']) if row['protected_token'] else '',check=lambda:self.storage.mcp_snapshot(p,cid,data.version))
        try:
            async with self.admission.slot(cid,'admin',p['user_id']):
                catalog=await client.discover()
                if not any(t.get('digest')==approved['digest'] for t in catalog):raise Error(409,'mcp_changed','The tool definition changed; discover and review it again')
                result=await client.call(approved,data.arguments)
            return {'result':result,'notice':'Read-only test result; not added to project knowledge or a conversation.'}
        finally:await client.close()


def install_mcp(app,storage,vault,actor,service=None):
    service=service or MCPAdmin(storage,vault);app.state.ai_mcp_admin=service
    Admin=Annotated[dict,Depends(admin_dependency(actor))]

    @app.get('/api/v1/ai/tool-connections',response_model=ConnectionList,tags=['AI tool connections'])
    def listing(p:Admin):return storage.mcp_connections(p)

    @app.post('/api/v1/ai/tool-connections',response_model=ConnectionOutput,tags=['AI tool connections'])
    def create(data:ConnectionWrite,p:Admin):return service.save(p,data)

    @app.patch('/api/v1/ai/tool-connections/{cid}',response_model=ConnectionOutput,tags=['AI tool connections'])
    def update(cid:str,data:ConnectionWrite,p:Admin):return service.save(p,data,cid)

    @app.delete('/api/v1/ai/tool-connections/{cid}',response_model=RemovedOutput,tags=['AI tool connections'])
    def remove(cid:str,data:Version,p:Admin):return storage.mcp_remove(p,cid,data.version)

    @app.post('/api/v1/ai/tool-connections/{cid}/discover',response_model=ConnectionOutput,tags=['AI tool connections'])
    async def discover(cid:str,data:Version,p:Admin):return await service.discover(p,cid,data.version)

    @app.post('/api/v1/ai/tool-connections/{cid}/test',response_model=TestOutput,tags=['AI tool connections'])
    async def test(cid:str,data:ToolTest,p:Admin):return await service.test(p,cid,data)

    @app.get('/api/v1/projects/{project}/ai/tool-connections',response_model=ProjectOutput,tags=['AI tool connections'])
    def project_read(project:str,p:Admin):return storage.mcp_project(p,project)

    @app.put('/api/v1/projects/{project}/ai/tool-connections',response_model=ProjectOutput,tags=['AI tool connections'])
    def project_save(project:str,data:ProjectWrite,p:Admin):return storage.mcp_bind(p,project,data.version,data.source_mode,[b.model_dump() for b in data.bindings])
