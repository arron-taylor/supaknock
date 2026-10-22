"""Untrusted, immutable per-message resource hints; never authentication."""
import math
from feedback.ai_mcp_protocol import encode
from feedback.errors import Error


def chat_context(value):
    if value is None:return None
    if not isinstance(value,dict) or set(value)-{'version','resource','position_seconds'} or type(value.get('version')) is not int or value['version']!=1:
        raise Error(400,'invalid_context','Use version 1 episode context')
    resource=value.get('resource')
    if not isinstance(resource,dict) or set(resource)!={'kind','id'} or resource['kind']!='podcast_episode' or not isinstance(resource['id'],str) or not 1<=len(resource['id'])<=200 or any(ord(x)<32 or ord(x)==127 for x in resource['id']):
        raise Error(400,'invalid_context','Supply an episode identifier')
    if 'position_seconds' in value and (type(value['position_seconds']) not in (int,float) or not math.isfinite(value['position_seconds']) or not 0<=value['position_seconds']<=86400):
        raise Error(400,'invalid_context','Use a valid playback position in seconds')
    encode(value,2048)
    return value
