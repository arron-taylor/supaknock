"""Version 1 read-only app contract. No network, storage or authority here."""
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit
import re

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool, StrictStr, field_validator, model_validator

ID = Annotated[StrictStr, Field(min_length=1, max_length=512)]
Text = Annotated[StrictStr, Field(max_length=200)]
Stamp = Annotated[StrictStr, Field(pattern=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$')]
Cursor = Annotated[StrictStr, Field(min_length=1, max_length=8192)]
Count = Annotated[StrictInt, Field(ge=0, le=9007199254740991)]


def instant(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z',value):
        raise ValueError('Use a UTC timestamp')
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def safe_origin(value):
    p=urlsplit(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
        raise ValueError('Use an HTTPS origin')
    port=p.port
    host=p.hostname.lower()
    if ':' in host:host='['+host+']'
    return 'https://'+host+((':'+str(port)) if port and port!=443 else '')


class Wire(BaseModel):
    model_config=ConfigDict(extra='ignore',strict=True)

    @model_validator(mode='after')
    def valid_strings(self):
        def walk(v,depth=0):
            if depth>12: raise ValueError('Response nesting limit')
            if isinstance(v,str): v.encode('utf-8',errors='strict')
            if isinstance(v,dict):
                for k,x in v.items(): walk(k,depth+1);walk(x,depth+1)
            if isinstance(v,list):
                for x in v: walk(x,depth+1)
        walk(self.model_dump())
        for name in ('observed_at','expires_at','joined_at','last_active_at','occurred_at','available_from','scan_until'):
            v=getattr(self,name,None)
            if v is not None: instant(v)
        return self


class Input(Wire):
    model_config=ConfigDict(extra='forbid',strict=True)


class Protocol(Wire):
    major: Literal[1]=1
    minor: Annotated[StrictInt,Field(ge=0)]=0

    @field_validator('major',mode='before')
    @classmethod
    def integer_major(cls,value):
        if type(value) is not int:raise ValueError('Protocol major must be an integer')
        return value


class Envelope(Wire):
    protocol: Protocol
    app_instance_id: ID
    observed_at: Stamp


class Capabilities(Wire):
    summary: StrictBool=False
    users: StrictBool=False
    profiles: StrictBool=False
    activity: StrictBool=False
    user_activity: StrictBool=False

    @model_validator(mode='after')
    def supported(self):
        if not any((self.summary,self.users,self.profiles,self.activity)): raise ValueError('No supported capability')
        if self.user_activity and not self.activity: raise ValueError('Activity is required')
        return self


class Coverage(Wire):
    available_from: Stamp | None=None
    complete: StrictBool


class Manifest(Envelope):
    capabilities: Capabilities
    user_filters: Annotated[list[Literal['query','joined_range']],Field(max_length=2)]=[]
    link_origins: Annotated[list[StrictStr],Field(max_length=8)]=[]
    activity_history: Coverage | None=None
    activity_definition: Annotated[StrictStr,Field(max_length=500)] | None=None

    @field_validator('link_origins')
    @classmethod
    def origins(cls,v):
        values=[safe_origin(x) for x in v]
        if len(set(values))!=len(values): raise ValueError('Duplicate origins')
        return values


class Range(Input):
    from_: Stamp=Field(alias='from')
    until: Stamp

    @model_validator(mode='after')
    def valid_range(self):
        if instant(self.from_)>=instant(self.until): raise ValueError('Start must be before end')
        return self


class UserQuery(Input):
    limit: Annotated[StrictInt,Field(ge=1,le=100)]=50
    query: Annotated[StrictStr,Field(max_length=200)]=''
    joined: Range | None=None
    cursor: Cursor | None=None


class ActivityQuery(Range):
    limit: Annotated[StrictInt,Field(ge=1,le=100)]=50
    user_id: ID | None=None
    cursor: Cursor | None=None


class Lookup(Input):
    user_id: ID


class Metric(Wire):
    count: Count | None
    reason: Literal['unsupported','history_incomplete'] | None=None

    @model_validator(mode='after')
    def reason_matches(self):
        if (self.count is None)!=(self.reason is not None): raise ValueError('Unavailable counts need a reason')
        return self


class NewAccounts(Metric):
    basis: Literal['current_accounts','signup_events']
    complete: StrictBool
    available_from: Stamp | None=None

    @model_validator(mode='after')
    def complete_count(self):
        if self.complete!=(self.count is not None): raise ValueError('Incomplete count must be unavailable')
        return self


class Summary(Envelope):
    interval: Range
    total_accounts: Metric
    new_accounts: NewAccounts


class User(Wire):
    id: ID
    display_name: Text | None=None
    email: Annotated[StrictStr,Field(max_length=320)] | None=None
    phone_number: Annotated[StrictStr,Field(max_length=64)] | None=None
    joined_at: Stamp
    status: Literal['active','disabled','unknown']='unknown'
    plan: Text | None=None
    last_active_at: Stamp | None=None


class Detail(Wire):
    key: Annotated[StrictStr,Field(min_length=1,max_length=100)]
    label: Text
    type: Literal['text','number','boolean','timestamp','badge','link']
    value: StrictStr | StrictInt | StrictBool | None

    @model_validator(mode='after')
    def typed(self):
        if self.value is None:return self
        if self.type=='number':
            if type(self.value)!=int or abs(self.value)>9007199254740991:raise ValueError('Invalid number')
        elif self.type=='boolean':
            if type(self.value)!=bool:raise ValueError('Invalid boolean')
        else:
            if type(self.value)!=str or len(self.value)>500:raise ValueError('Invalid detail')
            if self.type=='timestamp':instant(self.value)
        return self


class Profile(Envelope):
    user: User
    details: Annotated[list[Detail],Field(max_length=12)]=[]
    url: Annotated[StrictStr,Field(max_length=2048)] | None=None

    @field_validator('details',mode='before')
    @classmethod
    def optional_types(cls, value):
        if not isinstance(value,list) or len(value)>12:raise ValueError('Invalid detail list')
        return [x for x in value if isinstance(x,dict) and x.get('type') in ('text','number','boolean','timestamp','badge','link')]


class UserPage(Envelope):
    items: Annotated[list[User],Field(max_length=100)]
    scan_until: Stamp
    expires_at: Stamp
    next_cursor: Cursor | None


class Activity(Wire):
    id: ID
    occurred_at: Stamp
    type: Annotated[StrictStr,Field(min_length=1,max_length=100)]
    title: Text
    description: Annotated[StrictStr,Field(max_length=1000)] | None=None
    user_id: ID | None=None
    url: Annotated[StrictStr,Field(max_length=2048)] | None=None


class ActivityPage(Envelope):
    items: Annotated[list[Activity],Field(max_length=100)]
    snapshot: Cursor
    expires_at: Stamp
    next_cursor: Cursor | None
    coverage: Coverage


REQUESTS={'summary':Range,'users':UserQuery,'profile':Lookup,'activity':ActivityQuery}
RESPONSES={'manifest':Manifest,'summary':Summary,'users':UserPage,'profile':Profile,'activity':ActivityPage}
PATHS={'manifest':('GET','/v1/manifest'),'summary':('POST','/v1/summary/query'),
       'users':('POST','/v1/users/query'),'profile':('POST','/v1/users/lookup'),'activity':('POST','/v1/activity/query')}


def validated_response(operation, raw, query, instance=None, origins=()):
    model=RESPONSES[operation].model_validate(raw)
    data=model.model_dump(by_alias=True)
    if instance is not None and data['app_instance_id']!=instance:raise ValueError('App identity changed')
    if operation=='summary' and data['interval']!=query:raise ValueError('Interval mismatch')
    if operation=='profile' and data['user']['id']!=query['user_id']:raise ValueError('User mismatch')
    if operation in ('users','activity'):
        items=data['items'];field='joined_at' if operation=='users' else 'occurred_at'
        if len(items)>query['limit'] or (not items and data['next_cursor']):raise ValueError('Invalid page')
        keys=[(instant(x[field]),x['id'].encode('utf8')) for x in items]
        if keys!=sorted(keys,reverse=True) or len({x['id'] for x in items})!=len(items):raise ValueError('Invalid order')
        if data['next_cursor'] is not None and data['next_cursor']==query.get('cursor'):raise ValueError('Cursor did not advance')
        if operation=='activity':
            for x in items:
                if not instant(query['from'])<=instant(x[field])<instant(query['until']):raise ValueError('Outside range')
                if query.get('user_id') is not None and x['user_id']!=query['user_id']:raise ValueError('Wrong user')
        else:
            for x in items:
                if instant(x[field])>instant(data['scan_until']):raise ValueError('Outside scan')
                if query.get('joined') and not instant(query['joined']['from'])<=instant(x[field])<instant(query['joined']['until']):raise ValueError('Outside range')
    links=[]
    if operation=='profile':
        links=[data['url']]+[x['value'] for x in data['details'] if x['type']=='link']
        if len([x for x in links if x])>2:raise ValueError('Too many links')
    elif operation=='activity':links=[x['url'] for x in data['items']]
    for link in links:
        if link:
            p=urlsplit(link)
            if p.username or p.password or p.scheme!='https' or safe_origin('https://'+p.netloc) not in origins:raise ValueError('Unapproved link')
    return data
