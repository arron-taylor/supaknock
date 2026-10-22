"""SQL-free AI configuration and actor contracts; no network or model loading."""
from dataclasses import dataclass
from feedback.domain import Actor
from feedback.errors import Error

AI_TOOLS = ('search_knowledge', 'support_link', 'request_human', 'create_feature_request', 'create_bug_report')
AI_JOB_KINDS = ('ingest', 'crawl', 'sections', 'preview', 'turn')
EMBEDDING_PROFILE = {'model_id': 'google/embeddinggemma-300m', 'revision': '57c266a740f537b4dc058e1b0cda161fd15afa75',
                     'dimensions': 768, 'normalized': True, 'precision': 'float32',
                     'preprocessing': 'embeddinggemma-retrieval-v1', 'chunking': 'headings-v1'}


def default_ai_settings():
    return {'enabled': False, 'allow_guest_reports': False, 'provider': 'kodekey', 'model': None,
            'tools': {name: False for name in AI_TOOLS}, 'soul': '',
            'feature_board_id': None, 'bug_board_id': None, 'feedback_url': None,
            'feature_feedback_url': None, 'bug_feedback_url': None,
            'daily_request_limit': 100, 'daily_token_limit': 100000,
            'max_output_tokens': 4096, 'max_tool_rounds': 4}


def validate_ai_settings(data):
    """Require a complete settings write: omissions never silently grant access."""
    defaults = default_ai_settings()
    if not isinstance(data, dict) or set(data) != set(defaults):
        raise Error(400, 'invalid_input', 'Supply every AI setting')
    if type(data['enabled']) is not bool or type(data['allow_guest_reports']) is not bool:
        raise Error(400, 'invalid_input', 'AI opt-ins must be explicit booleans')
    if not isinstance(data['tools'], dict) or set(data['tools']) != set(AI_TOOLS) or any(type(x) is not bool for x in data['tools'].values()):
        raise Error(400, 'invalid_input', 'Supply an explicit allow flag for every tool')
    # The reviewed adapter registry is the only source of provider choices.
    # Selecting an ID never loads user-supplied modules or network endpoints.
    from feedback.ai_providers import PROVIDERS
    if (not isinstance(data['provider'], str) or data['provider'] not in PROVIDERS
            or (data['model'] is not None and (not isinstance(data['model'], str) or not 1 <= len(data['model']) <= 256 or any(not 32 < ord(c) < 127 for c in data['model'])))):
        raise Error(400, 'invalid_input', 'Unsupported provider or model')
    if not isinstance(data['soul'], str) or len(data['soul']) > 20000:
        raise Error(400, 'invalid_input', 'Personality must be at most 20,000 characters')
    for key, cap in (('daily_request_limit',100000),('daily_token_limit',100000000),('max_output_tokens',8192),('max_tool_rounds',8)):
        if type(data[key]) is not int or not 1 <= data[key] <= cap:
            raise Error(400, 'invalid_input', 'AI resource limit is invalid')
    from urllib.parse import urlsplit
    for key in ('feedback_url','feature_feedback_url','bug_feedback_url'):
        value = data[key]
        if value is None:
            continue
        if not isinstance(value, str):
            raise Error(400, 'invalid_input', 'Use an HTTPS feedback page without credentials')
        try:
            u = urlsplit(value)
            if len(value)>2048 or u.scheme!='https' or not u.hostname or u.username is not None or u.password is not None or u.port not in (None,443) or any(ord(c)<=32 or ord(c)==127 for c in value):
                raise ValueError()
        except (TypeError,ValueError):
            raise Error(400,'invalid_input','Use an HTTPS feedback page without credentials') from None
    for key in ('feature_board_id','bug_board_id'):
        if data[key] is not None and (not isinstance(data[key],str) or not 1 <= len(data[key]) <= 100):
            raise Error(400,'invalid_input','Invalid destination board')
    return data


@dataclass(frozen=True)
class GuestActor(Actor):
    """Internal report attribution only; never a native account or credential."""
    def record(self):
        return {'user_id': self.user_id, 'api_key_id': None, 'kind': 'guest'}
