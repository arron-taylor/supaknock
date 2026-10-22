"""Native-admin transport boundary for connected apps."""
import asyncio
from typing import Annotated,Any,Generic,TypeVar
from fastapi import Depends,Request
from pydantic import Field,StrictStr,StrictInt,StrictBool
from feedback.ai_http import admin_dependency
from feedback.connected_apps import ConnectedApps
from feedback.connected_apps_protocol import Input,Range,UserQuery,Lookup,ActivityQuery,ID,Summary,Profile,UserPage,ActivityPage
from feedback.errors import Error

Version=Annotated[StrictInt,Field(ge=0)]
Receipt=Annotated[StrictStr,Field(min_length=1,max_length=16384)]
class Edit(Input):
    version: Version
class Save(Edit):
    base_url: Annotated[StrictStr,Field(min_length=1,max_length=2048)]
    token: Annotated[StrictStr,Field(min_length=1,max_length=4096)]|None=None
class Enable(Edit):
    receipt: Receipt
    link_origins: Annotated[list[StrictStr],Field(max_length=8)]=[]
    support_linked: StrictBool=False
class Preferences(Edit):
    timezone: Annotated[StrictStr,Field(min_length=1,max_length=100)]
class SummaryRead(Edit):
    query: Range
class UserRead(Edit):
    query: UserQuery
    continuation: Receipt|None=None
class ProfileRead(Edit):
    query: Lookup
class ActivityRead(Edit):
    query: ActivityQuery
    continuation: Receipt|None=None
class Related(Edit):
    user_id: ID
    limit: Annotated[StrictInt,Field(ge=1,le=100)]=25
    offset: Annotated[StrictInt,Field(ge=0,le=1000000)]=0
T=TypeVar('T')
class ReadOutput(Input,Generic[T]):
    data: T
    version: int
    project_id: str
    capabilities: dict[str,bool]
    user_filters: list[str]
    continuation: str|None
    expires_at: str|None=None


def install_connected_apps(app,storage,vault,actor):
    service=ConnectedApps(storage,vault);app.state.connected_apps=service
    Admin=Annotated[dict,Depends(admin_dependency(actor))]
    prefix='/api/v1/admin/connected-apps'
    @app.get(prefix,tags=['Connected apps'])
    def connections(principal:Admin):return {'items':storage.connected_list(principal)}
    @app.get(prefix+'/preferences',tags=['Connected apps'])
    def preferences(principal:Admin):return service.preferences(principal)
    @app.put(prefix+'/preferences',tags=['Connected apps'])
    def preferences_save(body:Preferences,principal:Admin):return service.preferences(principal,body.version,body.timezone)
    root='/api/v1/projects/{project}/app-data'
    @app.get(root+'/connection',tags=['Connected apps'])
    def connection(project:str,principal:Admin):return storage.connected_read(project,principal)
    @app.put(root+'/connection',tags=['Connected apps'])
    def save(project:str,body:Save,principal:Admin):return service.save(project,principal,body.version,body.base_url,body.token)
    @app.post(root+'/connection/test',tags=['Connected apps'])
    async def test(project:str,body:Edit,principal:Admin):return await service.test(project,principal,body.version)
    @app.post(root+'/connection/enable',tags=['Connected apps'])
    def enable(project:str,body:Enable,principal:Admin):return service.enable(project,principal,body.version,body.receipt,body.link_origins,body.support_linked)
    @app.post(root+'/connection/disable',tags=['Connected apps'])
    def disable(project:str,body:Edit,principal:Admin):return storage.connected_change(project,principal,body.version,'disable')
    @app.post(root+'/connection/remove',tags=['Connected apps'])
    def remove(project:str,body:Edit,principal:Admin):return storage.connected_change(project,principal,body.version,'remove')
    async def read(project,principal,operation,body,request):
        try:
            async with asyncio.timeout(10):
                task=asyncio.create_task(service.read(project,principal,operation,body.version,body.query.model_dump(by_alias=True),getattr(body,'continuation',None)))
                try:
                    while not task.done():
                        if await request.is_disconnected():raise Error(499,'app_read_cancelled','Read cancelled')
                        await asyncio.sleep(.02)
                    return await task
                finally:
                    if not task.done():
                        task.cancel()
                        try:await task
                        except asyncio.CancelledError:pass
        except TimeoutError:raise Error(504,'app_timeout','The app took too long. Try again.') from None
    @app.post(root+'/summary/query',response_model=ReadOutput[Summary],tags=['Connected apps'])
    async def summary(project:str,body:SummaryRead,principal:Admin,request:Request):return await read(project,principal,'summary',body,request)
    @app.post(root+'/users/query',response_model=ReadOutput[UserPage],tags=['Connected apps'])
    async def users(project:str,body:UserRead,principal:Admin,request:Request):return await read(project,principal,'users',body,request)
    @app.post(root+'/profile/query',response_model=ReadOutput[Profile],tags=['Connected apps'])
    async def profile(project:str,body:ProfileRead,principal:Admin,request:Request):return await read(project,principal,'profile',body,request)
    @app.post(root+'/activity/query',response_model=ReadOutput[ActivityPage],tags=['Connected apps'])
    async def activity(project:str,body:ActivityRead,principal:Admin,request:Request):return await read(project,principal,'activity',body,request)
    @app.post(root+'/related/query',tags=['Connected apps'])
    def related(project:str,body:Related,principal:Admin):
        service.admission.budget(principal)
        return storage.connected_related(project,principal,body.user_id,body.version,body.limit,body.offset)
