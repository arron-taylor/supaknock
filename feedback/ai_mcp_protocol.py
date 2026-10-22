"""Bounded MCP data/schema contract. Remote schemas and content are untrusted."""
import hashlib
import json
from feedback.ai_limits import MAX_TOOL_EVIDENCE_BYTES, MAX_TOOL_RESULT_BYTES
import math
import re
from urllib.parse import urlsplit
from feedback.errors import Error

VERSION = '2025-11-25'

def finite(value):
    try:return type(value) in (int,float) and math.isfinite(value)
    except OverflowError:return False



def fail(code='mcp_invalid_response', message='The tool server returned an unsupported or invalid response'):
    raise Error(502, code, message)


def encode(value, maximum=262144):
    try:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'), sort_keys=True).encode('utf8')
        if len(body) > maximum: raise ValueError()
        return body
    except (ValueError, TypeError, UnicodeError, RecursionError):
        fail('mcp_limit', 'The tool data exceeds its supported limits')


def decode(body, maximum=262144):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError()
            result[key] = value
        return result
    def invalid(_): raise ValueError()
    def walk(value, depth=0):
        if depth > 12: raise ValueError()
        if isinstance(value, str): value.encode('utf8')
        elif isinstance(value, dict):
            for key, item in value.items(): walk(key, depth+1); walk(item, depth+1)
        elif isinstance(value, list):
            for item in value: walk(item, depth+1)
    try:
        if len(body) > maximum: raise ValueError()
        result = json.loads(body, object_pairs_hook=pairs, parse_constant=invalid)
        walk(result)
        if not isinstance(result, dict): raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError): fail()


def endpoint(value):
    try:
        p = urlsplit(value)
        if not isinstance(value, str) or len(value) > 2048 or any(ord(x) <= 32 or ord(x) == 127 for x in value): raise ValueError()
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment: raise ValueError()
        if p.port is not None and not 1 <= p.port <= 65535: raise ValueError()
        if not re.fullmatch(r'(?:/[A-Za-z0-9_.~-]+)*/?', p.path) or any(x in ('.','..') for x in p.path.split('/')): raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise Error(400, 'mcp_invalid_url', 'Use a public HTTPS tool endpoint without credentials, query parameters or redirects') from None


def schema(value, path='$', depth=0):
    """Portable subset; unsupported keywords fail visibly, never disappear."""
    allowed = {'type','properties','required','additionalProperties','items','enum','description','title','minLength','maxLength','minimum','maximum','minItems','maxItems'}
    if not isinstance(value, dict) or depth > 8 or set(value) - allowed:
        raise Error(400, 'mcp_schema', 'Unsupported tool schema at '+path)
    kind = value.get('type')
    if kind not in ('object','array','string','integer','number','boolean','null'):
        raise Error(400, 'mcp_schema', 'Use a simple type at '+path)
    for key in ('title','description'):
        if key in value and (not isinstance(value[key],str) or len(value[key]) > 2000):
            raise Error(400,'mcp_schema','Invalid description at '+path)
    specific={'object':{'properties','required','additionalProperties'},'array':{'items','minItems','maxItems'},'string':{'minLength','maxLength'},'integer':{'minimum','maximum'},'number':{'minimum','maximum'},'boolean':set(),'null':set()}
    if set(value)-({'type','enum','description','title'}|specific[kind]):
        raise Error(400,'mcp_schema','Keyword does not apply to this type at '+path)
    if kind == 'object':
        props = value.get('properties', {})
        required = value.get('required', [])
        if not isinstance(props, dict) or len(props)>64 or not isinstance(required,list) or any(not isinstance(x,str) for x in required) or len(set(required))!=len(required) or set(required)-set(props) or value.get('additionalProperties',False) is not False:
            raise Error(400,'mcp_schema','Use bounded explicit object properties at '+path)
        for key, sub in props.items():
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',key): raise Error(400,'mcp_schema','Invalid property at '+path)
            schema(sub,path+'.'+key,depth+1)
    elif kind == 'array':
        schema(value.get('items'),path+'[]',depth+1)
    limits = {'minLength','maxLength','minItems','maxItems'}
    for key in limits | {'minimum','maximum'}:
        if key in value and (type(value[key]) not in (int,float) or not finite(value[key]) or (key in limits and (type(value[key]) is not int or not 0 <= value[key] <= 65536))):
            raise Error(400,'mcp_schema','Invalid bound at '+path)
    for lo,hi in [('minLength','maxLength'),('minItems','maxItems'),('minimum','maximum')]:
        if lo in value and hi in value and value[lo]>value[hi]: raise Error(400,'mcp_schema','Contradictory bounds at '+path)
    if 'enum' in value:
        if not isinstance(value['enum'],list) or not 1 <= len(value['enum']) <= 100: raise Error(400,'mcp_schema','Invalid enum at '+path)
        for v in value['enum']: validate({k:x for k,x in value.items() if k!='enum'},v,path)
    encode(value,16384)
    return value


def validate(s, value, path='$'):
    kind=s['type']
    good={'object':isinstance(value,dict),'array':isinstance(value,list),'string':isinstance(value,str),
          'integer':type(value) is int,'number':finite(value),
          'boolean':type(value) is bool,'null':value is None}[kind]
    if not good: raise Error(400,'mcp_arguments','Invalid value at '+path)
    if 'enum' in s and not any(type(x) is type(value) and x==value for x in s['enum']): raise Error(400,'mcp_arguments','Invalid choice at '+path)
    if kind=='object':
        props=s.get('properties',{})
        if set(value)-set(props) or set(s.get('required',[]))-set(value): raise Error(400,'mcp_arguments','Unexpected or missing field at '+path)
        for k,v in value.items():validate(props[k],v,path+'.'+k)
    if kind=='array':
        if len(value)>s.get('maxItems',100) or len(value)<s.get('minItems',0): raise Error(400,'mcp_arguments','Array outside limits at '+path)
        for v in value:validate(s['items'],v,path+'[]')
    if kind=='string':
        if len(value)>s.get('maxLength',16384) or len(value)<s.get('minLength',0): raise Error(400,'mcp_arguments','Text outside limits at '+path)
        encode(value,65536)
    if kind in ('integer','number') and (value<s.get('minimum',-float('inf')) or value>s.get('maximum',float('inf'))): raise Error(400,'mcp_arguments','Number outside limits at '+path)
    return value


def definition(tool):
    if not isinstance(tool,dict) or not isinstance(tool.get('name'),str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',tool['name']): fail()
    description=tool.get('description','')
    if not isinstance(description,str) or len(description)>2000:fail()
    annotations=tool.get('annotations',{})
    if not isinstance(annotations,dict) or annotations.get('destructiveHint') is True or annotations.get('readOnlyHint') is False: raise Error(400,'mcp_write_tool','Only visitor-safe read tools can be approved')
    if not isinstance(tool.get('execution',{}),dict):fail()
    if tool.get('execution',{}).get('taskSupport')=='required': raise Error(400,'mcp_schema','Long-running tool tasks are unsupported')
    result={'name':tool['name'],'description':description,'inputSchema':schema(tool.get('inputSchema'))}
    if result['inputSchema']['type']!='object':raise Error(400,'mcp_schema','Tool inputs must be objects')
    if 'outputSchema' in tool:result['outputSchema']=schema(tool['outputSchema'])
    result['annotations']=annotations
    result['digest']=hashlib.sha256(encode(tool,32768)).hexdigest()
    return result


def tool_result(result, output_schema=None):
    encode(result,MAX_TOOL_RESULT_BYTES)
    if not isinstance(result,dict) or result.get('isError',False) is not False:fail('mcp_tool_error','The connected tool could not complete this read')
    blocks=result.get('content',[])
    if not isinstance(blocks,list) or len(blocks)>64 or any(not isinstance(b,dict) or b.get('type')!='text' or not isinstance(b.get('text'),str) for b in blocks):fail('mcp_content','Only text and structured tool results are supported')
    structured=result.get('structuredContent')
    if structured is not None:
        if not isinstance(structured,dict):fail()
        if output_schema:validate(output_schema,structured)
        if blocks:
            try: same=decode('\n'.join(b['text'] for b in blocks).encode())==structured
            except Error:same=False
            if not same:fail('mcp_content','Text and structured results must describe the same data')
        encode(structured,MAX_TOOL_EVIDENCE_BYTES)
        return structured
    if output_schema:fail()
    text='\n'.join(b['text'] for b in blocks)
    encode(text,MAX_TOOL_EVIDENCE_BYTES)
    return {'text':text}
