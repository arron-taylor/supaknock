"""Real Ramp wire adapter and real storage at the final tool-round boundary."""
import json
from uuid import uuid4
import httpx
import pytest
from test_ai_providers import response
from test_ai_chat_isolation import ai_chat, conversation
from test_ai_tools import enable_tools, engine
from feedback.ai_chat import AIChatWorker
from feedback.ai_providers import RampRouterProvider


@pytest.mark.parametrize('reads', [3, 4])
@pytest.mark.parametrize('malformed', [False, True])
def test_real_ramp_adapter_at_tool_round_boundary(ai_chat, reads, malformed):
    env = ai_chat
    enable_tools(env, ['search_knowledge'], rounds=4)
    sent = []

    def transport(request):
        body = json.loads(request.content)
        sent.append(body)
        if len(sent) <= reads:
            output = [{'type': 'function_call', 'call_id': 'read-' + str(len(sent)),
                       'name': 'search_knowledge', 'arguments': '{"query":"return policy"}'}]
        else:
            is_check = 'evidence_check' in json.dumps(body['input'])
            result = ({'assessment': 'The supplied passage supports the draft.', 'supported': True, 'issues': []}
                      if is_check else {'status': 'answered', 'body': 'Returns are accepted for thirty days.',
                                        'citation_ids': [json.loads(body['input'][1]['content'])['passages'][0]['id']]})
            output = [{'type': 'message', 'role': 'assistant',
                       'content': [{'type': 'output_text', 'text': 'Badly formatted draft' if malformed and len(sent) == reads + 1 else json.dumps(result)}]}]
        return response({'status': 'completed', 'output': output,
                                       'usage': {'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30}})

    adapter = RampRouterProvider(transport=httpx.MockTransport(transport))
    generation, _ = engine(env, adapter)
    customer = conversation(env)
    store = env['store']
    store.chat_send(customer['conversation']['id'], customer['principal'], str(uuid4()), 'What is the return policy?')
    completed = AIChatWorker(store, generation).run_once()
    job = store.ai_jobs(env['project'], env['admin'])[0]
    mode = store.chat_conversation(customer['conversation']['id'], customer['principal'])['support_mode']
    assert completed and job['state'] == 'succeeded' and mode == 'ai'
    assert len(sent) == reads + 2 + int(malformed)  # Reads, final draft, independent evidence check.
    assert job['answer_outcome'] == 'answered'
