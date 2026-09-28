import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from life_memoir.backends import ChatCompletionsBackend, BackendError
from life_memoir.config import Settings, Principal
from life_memoir.http import create_app
from life_memoir.service import MemoryService


def test_http_auth_query_validation_and_all_schemas():
    principal = Principal('h', frozenset({'u'}), frozenset({'host', 'maintenance'}))
    service = MemoryService(Settings(storage_path=':memory:'))
    with TestClient(create_app(service, {'x'*24: principal})) as c:
        assert c.get('/healthz').status_code == 200
        path = '/v1/memory/users/u/jobs'
        assert c.get(path).status_code == 401
        c.headers['Authorization'] = 'Bearer ' + 'x'*24
        assert c.get(path + '?limit=10').status_code == 200
        assert c.get(path + '?limit=oops').status_code == 422
        assert c.get(path + '?unknown=1').status_code == 422
        bad = c.post('/v1/memory/sessions', json={'request_id': 'r', 'user_id': 'u', 'session_id': 's', 'locale': 'zh-CN', 'secret': 'never-echo-this'})
        assert bad.status_code == 422 and 'never-echo-this' not in bad.text
        schema = c.get('/openapi.json').json()
        operations = [op for path in schema['paths'].values() for op in path.values() if isinstance(op, dict) and 'operationId' in op]
        assert len(operations) == 14
        assert all(op.get('security') for op in operations if op['operationId'] != 'health_healthz_get')


def test_compatible_backend_request_and_json_validation():
    seen = []
    def transport(request):
        seen.append(json.loads(request.content))
        assert request.url.path == '/v1/chat/completions'
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"candidates":[],"warnings":[]}'}}]})
    cfg = Settings(backend='chat_completions', base_url='http://127.0.0.1:8000/v1', model='fixture-model')
    backend = ChatCompletionsBackend(cfg, httpx.MockTransport(transport))
    result = asyncio.run(backend.analyze('extract_candidates', {'eligible_turns': []}))
    assert not result.candidates
    assert seen[0]['model'] == 'fixture-model' and seen[0]['response_format']['type'] == 'json_object'


@pytest.mark.parametrize('mode,code', [('malformed', 'MODEL_OUTPUT_INVALID'), ('redirect', 'MODEL_HTTP_ERROR'), ('timeout', 'MODEL_TIMEOUT')])
def test_provider_errors_are_sanitized_and_bounded(mode, code):
    calls = []
    def transport(request):
        calls.append(1)
        if mode == 'timeout':
            raise httpx.ReadTimeout('sensitive upstream payload', request=request)
        if mode == 'redirect':
            return httpx.Response(302, headers={'location': 'https://example.invalid/secret'})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'sensitive invalid JSON'}}]})
    cfg = Settings(backend='chat_completions', base_url='http://127.0.0.1:8000/v1', model='test')
    with pytest.raises(BackendError) as error:
        asyncio.run(ChatCompletionsBackend(cfg, httpx.MockTransport(transport)).analyze('extract_candidates', {}))
    assert str(error.value) == code
    assert len(calls) == (2 if mode == 'timeout' else 1)
