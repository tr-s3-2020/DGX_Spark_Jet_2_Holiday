import asyncio
import json
import time
import uuid

from life_memoir.backends import FixtureBackend
from life_memoir.config import Settings, Principal
from life_memoir.models import AnalysisResult
from life_memoir.service import MemoryService


P = Principal('host', frozenset({'u'}), frozenset({'host', 'maintenance'}), frozenset({'consent', 'review'}))
FAMILY = Principal('family', frozenset({'u'}), frozenset({'family'}))
STORY = '我是1950年出生的。我二十周岁时进厂。进厂整三年后，我调去了维修车间。以后问题简短一点。'


async def call(s, op, principal=P, **data):
    return await s.execute(op, {'request_id': uuid.uuid4().hex, **data}, principal)


async def policy(s, version=0, **overrides):
    result = await call(s, 'set_policy', user_id='u', expected_version=version, consent_ref='consent',
        grants={'long_term_memory': True, 'profile_learning': True, 'family_digest': False, 'remote_analysis': False, **overrides})
    assert result['status'] == 'ok', result


def turn(text=STORY, **extra):
    return dict(turn_id='t', speaker='user', text=text, is_final=True, occurred_at='2026-09-26T14:00:00+08:00', text_locale='zh-CN', **extra)


async def session(s, sid='s', text=STORY, **extra):
    assert (await call(s, 'open_session', user_id='u', session_id=sid, locale='zh-CN'))['status'] == 'ok'
    response = await call(s, 'prepare_turn', session_id=sid, turn=turn(text, **extra), context={'query': '进厂'})
    assert response['status'] in {'ok', 'degraded'}, response
    return await call(s, 'close_session', session_id=sid,
        expected_session_version=response['data']['observation']['session_version'], reason='completed')


async def entries(s):
    return (await call(s, 'list_entries', user_id='u', limit=50))['data']['items']


async def timeline(s, principal=P, purpose='conversation'):
    return await call(s, 'get_chronicle', principal, user_id='u', purpose=purpose)


def test_persistent_memory_and_temporal_sources(tmp_path):
    async def run():
        cfg = Settings(storage_path=str(tmp_path / 'db.sqlite'), fixture_delay_seconds=0)
        s = await MemoryService(cfg).start()
        await policy(s)
        assert (await session(s))['status'] == 'accepted'
        await s.wait_idle()
        es = await entries(s)
        assert len(es) == 4
        for e in es:
            assert e['evidence'] and all(x['quote'] in STORY for x in e['evidence'])
            assert all(c['source_id'] in {x['evidence_id'] for x in e['evidence']} for c in e['time_assertions'])
        v = (await timeline(s))['data']['content']
        bounds = [(x['time_resolution']['alternatives'][0]['earliest_year'], x['time_resolution']['alternatives'][0]['latest_year']) for x in v['items']]
        assert bounds == [(1950, 1950), (1970, 1971), (1973, 1974)]
        await s.close()
        s = await MemoryService(cfg).start()
        await call(s, 'open_session', user_id='u', session_id='s2', locale='zh-CN')
        ctx = await call(s, 'build_context', session_id='s2', purpose='conversation', query='进厂')
        assert ctx['data']['memories'] and ctx['data']['preferences']
        assert not s.buffers['s2']['turns']
        await s.close()
    asyncio.run(run())


def test_idempotency_partial_turns_and_user_isolation():
    async def run():
        s = MemoryService(Settings(storage_path=':memory:'))
        body = dict(request_id='stable', user_id='u', session_id='s', locale='zh-CN')
        a = await s.execute('open_session', body, P)
        b = await s.execute('open_session', body, P)
        assert a['data']['session_version'] == b['data']['session_version'] == 1
        assert b['meta']['replayed']
        assert (await s.execute('open_session', {**body, 'locale': 'en'}, P))['error']['code'] == 'IDEMPOTENCY_CONFLICT'
        partial = {**turn(), 'is_final': False}
        assert (await call(s, 'prepare_turn', session_id='s', turn=partial))['status'] == 'ignored'
        assert not s.buffers['s']['turns']
        stranger = Principal('stranger', frozenset({'v'}), frozenset({'host', 'maintenance'}))
        assert (await call(s, 'build_context', stranger, session_id='s', purpose='conversation'))['error']['code'] == 'NOT_FOUND'
        assert (await call(s, 'list_entries', FAMILY, user_id='u'))['error']['code'] == 'PERMISSION_DENIED'
        assert (await call(s, 'set_policy', user_id='u', expected_version=0, consent_ref='invented', grants=dict(long_term_memory=True, profile_learning=False, family_digest=False, remote_analysis=False)))['error']['code'] == 'PERMISSION_DENIED'
        await s.close()
    asyncio.run(run())


def test_no_save_excludes_session_and_raw_text_is_not_persisted(tmp_path):
    async def run():
        s = await MemoryService(Settings(storage_path=str(tmp_path / 'db'), fixture_delay_seconds=0)).start()
        await policy(s)
        result = await session(s, text='这段唯一隐私文本不能留下', controls={'do_not_persist': True})
        assert result['data']['state'] == 'skipped'
        await s.wait_idle()
        assert await entries(s) == []
        assert 's' not in s.buffers
        dump = '\n'.join(s.db.iterdump())
        assert '唯一隐私' not in dump
        await s.close()
    asyncio.run(run())


def test_background_wait_does_not_block_context_and_revocation_prevents_publish():
    class PausedBackend(FixtureBackend):
        def __init__(self):
            super().__init__(0)
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
        async def analyze(self, op, data):
            if op == 'extract_candidates':
                self.entered.set()
                await self.release.wait()
            return await super().analyze(op, data)
    async def run():
        b = PausedBackend()
        s = await MemoryService(Settings(storage_path=':memory:'), backend=b).start()
        await policy(s)
        await session(s)
        await asyncio.wait_for(b.entered.wait(), 2)
        await call(s, 'open_session', user_id='u', session_id='live', locale='zh-CN')
        # Completion while analysis is still blocked proves there is no model dependency.
        ctx = await asyncio.wait_for(call(s, 'prepare_turn', session_id='live', turn=turn('现在聊点别的')), 0.5)
        assert ctx['status'] == 'ok' and not b.release.is_set()
        await policy(s, 1, long_term_memory=False, profile_learning=False)
        b.release.set()
        await asyncio.gather(*list(s.tasks), return_exceptions=True)
        assert await entries(s) == []
        await s.close()
    asyncio.run(run())


def test_bad_evidence_cannot_publish_partial_records():
    class BadBackend(FixtureBackend):
        async def analyze(self, op, data):
            result = await super().analyze(op, data)
            if result.candidates:
                result.candidates[-1].evidence_quotes = {result.candidates[-1].source_ids[0]: '从未说过的原话'}
            return result
    async def run():
        s = await MemoryService(Settings(storage_path=':memory:'), backend=BadBackend(0)).start()
        await policy(s)
        r = await session(s)
        await s.wait_idle()
        job = await call(s, 'get_job', job_id=r['data']['job_id'])
        assert job['data']['state'] == 'failed'
        assert job['data']['failure']['code'] == 'MODEL_OUTPUT_INVALID'
        assert await entries(s) == []
        assert s._all('evidence') == []
        await s.close()
    asyncio.run(run())


def test_correction_deletion_and_family_hidden_anchor():
    async def run():
        s = await MemoryService(Settings(storage_path=':memory:', fixture_delay_seconds=0)).start()
        await policy(s, family_digest=True)
        await session(s)
        await s.wait_idle()
        es = await entries(s)
        birth = next(e for e in es if '出生' in e['content'])
        factory = next(e for e in es if '二十' in e['content'])
        r = await call(s, 'revise_entry', entry_id=factory['entry_id'], expected_version=1, action='set_allowed_uses', decision_ref='review', allowed_uses=['conversation', 'family_digest'])
        assert r['status'] == 'ok'
        await s.wait_idle()
        family = (await timeline(s, FAMILY, 'family_digest'))['data']['content']
        assert len(family['items']) == 1
        assert family['items'][0]['time_resolution']['alternatives'][0]['earliest_year'] is None
        assert birth['entry_id'] not in json.dumps(family)
        r = await call(s, 'revise_entry', entry_id=birth['entry_id'], expected_version=1, action='correct_content', decision_ref='review', content='我是1951年出生的')
        assert r['status'] == 'ok'
        await s.wait_idle()
        v = (await timeline(s))['data']['content']
        f = next(e for e in v['items'] if '二十' in e['title'])
        assert f['time_resolution']['alternatives'][0]['earliest_year'] == 1971
        await call(s, 'forget_entries', user_id='u', scope='entries', entry_ids=[birth['entry_id']], decision_ref='review')
        await s.wait_idle()
        v = (await timeline(s))['data']['content']
        f = next(e for e in v['items'] if '二十' in e['title'])
        assert f['time_resolution']['alternatives'][0]['earliest_year'] is None
        assert all('1951' not in e['quote'] for e in s._all('evidence'))
        await s.close()
    asyncio.run(run())


def test_recent_expiry_invalidates_preloaded_context():
    async def run():
        now = [time.time()]
        s = await MemoryService(Settings(storage_path=':memory:', recent_ttl_seconds=5, fixture_delay_seconds=0), clock=lambda: now[0]).start()
        await policy(s)
        await session(s, text='最近我养了一盆茉莉')
        await s.wait_idle()
        await call(s, 'open_session', user_id='u', session_id='next', locale='zh-CN')
        req = dict(session_id='next', purpose='conversation')
        assert (await call(s, 'build_context', **req))['data']['memories']
        now[0] += 6
        assert (await call(s, 'build_context', **req))['data']['memories'] == []
        assert not s._all('evidence')
        await s.close()
    asyncio.run(run())


def test_remote_adapter_not_called_without_permission():
    class Remote(FixtureBackend):
        location = 'remote'
    async def run():
        b = Remote(0)
        s = await MemoryService(Settings(storage_path=':memory:'), backend=b).start()
        await policy(s)
        r = await session(s)
        await s.wait_idle()
        assert b.calls == 0
        assert (await call(s, 'get_job', job_id=r['data']['job_id']))['data']['failure']['code'] == 'REMOTE_ANALYSIS_NOT_ALLOWED'
        await s.close()
    asyncio.run(run())


def test_inferred_preferences_need_confirmation_and_do_not_self_reschedule():
    class LearningBackend(FixtureBackend):
        updates = 0
        async def analyze(self, op, data):
            if op == 'extract_candidates':
                result = await super().analyze(op, data)
                for c in result.candidates:
                    if c.kind == 'preference':
                        c.basis = 'inferred'
                return result
            candidates = []
            for e in data['existing_entries']:
                if e['kind'] == 'preference' and e['basis'] == 'inferred':
                    self.updates += 1
                    ev = next(v for v in data['existing_evidence'] if v['evidence_id'] == e['source_refs'][0]['evidence_id'])
                    candidates.append(dict(local_id='p', kind='preference', content=e['content'], content_locale='zh-CN', basis='inferred', source_ids=[ev['evidence_id']], evidence_quotes={ev['evidence_id']: ev['quote']}, target_entry_id=e['entry_id'], expected_version=e['version']))
            return AnalysisResult(candidates=candidates)
    async def run():
        b = LearningBackend(0)
        s = await MemoryService(Settings(storage_path=':memory:'), backend=b).start()
        await policy(s)
        await session(s, text='以后问题简短一点')
        await s.wait_idle(timeout=2)
        assert b.updates == 1
        await call(s, 'open_session', user_id='u', session_id='next', locale='zh-CN')
        ctx = await call(s, 'build_context', session_id='next', purpose='conversation')
        assert not ctx['data']['preferences'] and ctx['data']['review_suggestions']
        e = (await entries(s))[0]
        r = await call(s, 'revise_entry', entry_id=e['entry_id'], expected_version=e['version'], action='confirm_preference', decision_ref='review')
        assert r['status'] == 'ok'
        assert (await call(s, 'build_context', session_id='next', purpose='conversation'))['data']['preferences']
        await s.close()
    asyncio.run(run())


def test_event_review_version_conflict_and_rejected_inference():
    async def run():
        s = await MemoryService(Settings(storage_path=':memory:', fixture_delay_seconds=0)).start()
        await policy(s)
        await session(s)
        await s.wait_idle()
        v = (await timeline(s))['data']['content']
        event = next(e for e in v['items'] if '二十' in e['title'])
        req = dict(event_id=event['event_id'], expected_event_version=event['version'], action='reject_inference', decision_ref='review', resolution_version=event['time_resolution']['version'], alternative_id=event['time_resolution']['alternatives'][0]['alternative_id'])
        assert (await call(s, 'review_chronicle_event', **req))['status'] == 'ok'
        assert (await call(s, 'review_chronicle_event', **req))['error']['code'] == 'VERSION_CONFLICT'
        await s.wait_idle()
        revised = next(e for e in (await timeline(s))['data']['content']['items'] if e['event_id'] == event['event_id'])
        assert revised['time_resolution']['alternatives'][0]['earliest_year'] is None
        assert revised['time_resolution']['state'] == 'unresolved'
        await s.close()
    asyncio.run(run())
