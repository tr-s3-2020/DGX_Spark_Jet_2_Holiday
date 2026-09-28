import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from life_memoir.config import Principal, Settings
from life_memoir.http import create_app
from life_memoir.service import MemoryService
from shutdown_scenarios import SCENARIOS


def test_close_exits_when_wakeup_wins_scheduler_cancellation():
    class RacingWake(asyncio.Event):
        """Reproduce a completed wait hiding cancellation, as in CPython #86296."""
        def __init__(self):
            super().__init__()
            self.waiting = asyncio.Event()
            self.raced = False

        async def wait(self):
            self.waiting.set()
            try:
                return await super().wait()
            except asyncio.CancelledError:
                if self.is_set() and not self.raced:
                    self.raced = True
                    return True
                raise

    async def run():
        service = MemoryService(Settings(storage_path=':memory:'))
        service.wake = RacingWake()
        await service.start()
        await service.wake.waiting.wait()
        closing = asyncio.create_task(service.close())
        try:
            # wait_for would cancel the close task and could conceal the original hang.
            done, _ = await asyncio.wait({closing}, timeout=0.5)
            assert closing in done, 'close() kept waiting for the scheduler after shutdown'
            await closing
            assert service.wake.raced
            assert service.runner.done()
            assert not service.tasks
        finally:
            # A failing regression must not itself hang asyncio.run() cleanup.
            service.runner.cancel()
            await asyncio.gather(service.runner, return_exceptions=True)
            await closing

    asyncio.run(run())


def test_lifespan_exception_still_closes_service():
    async def run():
        service = MemoryService(Settings(storage_path=':memory:'))
        principal = Principal('host', frozenset({'u'}))
        app = create_app(service, {'x' * 24: principal})
        try:
            with pytest.raises(RuntimeError, match='host failed'):
                async with app.router.lifespan_context(app):
                    raise RuntimeError('host failed')
            assert service.closed
            assert service.runner.done()
            assert not service.tasks
        finally:
            await service.close()

    asyncio.run(run())


def test_closed_service_cannot_restart():
    async def run():
        service = MemoryService(Settings(storage_path=':memory:'))
        await service.close()
        try:
            with pytest.raises(RuntimeError, match='closed'):
                await service.start()
        finally:
            if service.runner:
                service.runner.cancel()
                await asyncio.gather(service.runner, return_exceptions=True)

    asyncio.run(run())


def test_asyncio_run_exits_with_active_and_waiting_jobs():
    # An outer process timeout also covers asyncio.run()'s _cancel_all_tasks,
    # which an in-loop timeout cannot protect. No network or real model is used.
    script = textwrap.dedent('''
        import asyncio
        import uuid
        from life_memoir.backends import FixtureBackend
        from life_memoir._compat import timeout
        from life_memoir.config import Principal, Settings
        from life_memoir.service import MemoryService

        class PausedBackend(FixtureBackend):
            def __init__(self):
                super().__init__(0)
                self.entered = asyncio.Event()
                self.cancelled = asyncio.Event()

            async def analyze(self, operation, data):
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.set()

        async def run():
            backend = PausedBackend()
            service = await MemoryService(Settings(storage_path=':memory:'), backend=backend).start()
            principal = Principal('host', frozenset({'u'}),
                frozenset({'host', 'maintenance'}), frozenset({'consent'}))

            async def call(operation, **data):
                reply = await service.execute(operation,
                    dict(request_id=uuid.uuid4().hex, **data), principal)
                assert reply['status'] in {'ok', 'accepted'}, reply
                return reply['data']

            await call('set_policy', user_id='u', expected_version=0, consent_ref='consent',
                grants=dict(long_term_memory=True, profile_learning=False,
                            family_digest=False, remote_analysis=False))
            for i in range(3):
                sid = f'session-{i}'
                await call('open_session', user_id='u', session_id=sid, locale='zh-CN')
                result = await call('prepare_turn', session_id=sid,
                    turn=dict(turn_id='turn', speaker='user', text='我是1950年出生的。',
                              is_final=True, occurred_at='2026-09-28T12:00:00+08:00', text_locale='zh-CN'))
                await call('close_session', session_id=sid, reason='completed',
                    expected_session_version=result['observation']['session_version'])
            await asyncio.wait_for(backend.entered.wait(), 1)
            async with timeout(1):
                while len(service.tasks) < 3:
                    await asyncio.sleep(0)
            workers = tuple(service.tasks)
            assert len(workers) == 3  # one analyzing, two waiting for the semaphore
            await asyncio.gather(service.close(), service.close())
            await service.close()  # repeated close must finish safely
            assert backend.cancelled.is_set()
            assert service.runner.done() and all(task.done() for task in workers)
            assert not service.tasks and not service.buffers
            assert not (asyncio.all_tasks() - {asyncio.current_task()})

        asyncio.run(run())
        print('asyncio.run exited cleanly')
    ''')
    result = subprocess.run([sys.executable, '-c', script],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'asyncio.run exited cleanly' in result.stdout
    assert not result.stderr


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_shutdown_boundaries_in_subprocess(scenario):
    probe = Path(__file__).with_name('shutdown_scenarios.py')
    result = subprocess.run([sys.executable, str(probe), scenario, '--repeat', '4'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr, result.stderr
