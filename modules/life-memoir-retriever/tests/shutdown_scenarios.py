"""Subprocess probes; run directly to repeat lifecycle races without API calls."""
import argparse
import asyncio
import faulthandler
import json
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

from life_memoir.backends import FixtureBackend
from life_memoir._compat import timeout
from life_memoir.config import Principal, Settings
from life_memoir.service import MemoryService


SCENARIOS = ('idle', 'pending', 'finish_race', 'cancel_host', 'cancel_closer',
             'cancel_close_waiter', 'timeout', 'backend_error', 'restart', 'runner_cleanup')
PRINCIPAL = Principal('host', frozenset({'u'}), frozenset({'host', 'maintenance'}),
                      frozenset({'consent'}))


class ControlledBackend(FixtureBackend):
    def __init__(self, *, defer_cleanup=False, fail=False):
        super().__init__(0)
        self.started = 0
        self.finished = 0
        self.release = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.defer_cleanup = defer_cleanup
        self.fail = fail

    async def analyze(self, operation, data):
        self.started += 1
        try:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cleanup_started.set()
                if self.defer_cleanup:
                    await self.cleanup_release.wait()
                raise
            if self.fail:
                raise RuntimeError('simulated provider failure')
            return await super().analyze(operation, data)
        finally:
            self.finished += 1


async def call(service, operation, **data):
    result = await service.execute(operation, dict(request_id=uuid.uuid4().hex, **data), PRINCIPAL)
    assert result['status'] in {'ok', 'accepted'}, result
    return result['data']


async def wait_until(predicate):
    async with timeout(2):
        while not predicate():
            await asyncio.sleep(0)


async def enqueue(service, count):
    await call(service, 'set_policy', user_id='u', expected_version=0, consent_ref='consent',
               grants=dict(long_term_memory=True, profile_learning=False,
                           family_digest=False, remote_analysis=False))
    jobs = []
    for i in range(count):
        sid = f'session-{i}'
        await call(service, 'open_session', user_id='u', session_id=sid, locale='zh-CN')
        turn = await call(service, 'prepare_turn', session_id=sid,
                          turn=dict(turn_id='turn', speaker='user', text='我是1950年出生的。',
                                    is_final=True, occurred_at='2026-09-28T12:00:00+08:00',
                                    text_locale='zh-CN'))
        closed = await call(service, 'close_session', session_id=sid, reason='completed',
                            expected_session_version=turn['observation']['session_version'])
        jobs.append(closed['job_id'])
    return jobs


def assert_closed(service, workers=()):
    assert service.closed and service._close_complete
    assert service.runner is None or service.runner.done()
    assert service._close_task is None or service._close_task.done()
    assert all(task.done() for task in workers)
    assert not service.tasks and not service.buffers and not service.cache
    try:
        service.db.execute('SELECT 1')
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError('database is still open after close()')


async def expect_cancelled(task):
    try:
        await task
    except asyncio.CancelledError:
        return
    raise AssertionError('caller cancellation was swallowed')


async def exercise(scenario, iteration, database):
    loop = asyncio.get_running_loop()
    errors = []
    loop.set_exception_handler(lambda _, context: errors.append(context))
    concurrent = 1 + iteration % 4
    deferred = scenario in {'cancel_host', 'cancel_closer', 'cancel_close_waiter'}
    backend = ControlledBackend(defer_cleanup=deferred, fail=scenario == 'backend_error')
    cfg = Settings(storage_path=database, max_concurrency=concurrent,
                   total_budget_seconds=0.1 if scenario == 'timeout' else 45)
    service = await MemoryService(cfg, backend=backend).start()
    if scenario == 'idle':
        # Alternate closing before the scheduler starts and after it reaches wait().
        if iteration % 2:
            await asyncio.sleep(0)
        begin = time.monotonic()
        await asyncio.gather(*(service.close() for _ in range(5)))
        duration = time.monotonic() - begin
        assert_closed(service)
    else:
        count = 1 if scenario in {'timeout', 'backend_error'} else concurrent + 2
        jobs = await enqueue(service, count)
        await wait_until(lambda: backend.started >= min(concurrent, count))
        if count > concurrent:
            await wait_until(lambda: len(service.tasks) == count)
        workers = tuple(service.tasks)
        begin = time.monotonic()
        if scenario == 'runner_cleanup':
            # Leave active workers for asyncio.run()'s _cancel_all_tasks itself.
            return service, backend, errors, 0.0
        if scenario == 'finish_race':
            # Exercise both orders: a model result and a shutdown become ready together.
            if iteration % 2:
                backend.release.set()
                await asyncio.sleep(0)
            closing = asyncio.create_task(service.close())
            backend.release.set()
            await closing
        elif scenario in {'cancel_closer', 'cancel_close_waiter'}:
            closing = asyncio.create_task(service.close())
            await backend.cleanup_started.wait()
            cancelled = closing
            if scenario == 'cancel_close_waiter':
                cancelled = asyncio.create_task(service.close())
                await asyncio.sleep(0)
            # Repeated cancellation must neither abort cleanup nor swallow cancellation.
            for _ in range(3):
                cancelled.cancel()
                await asyncio.sleep(0)
            backend.cleanup_release.set()
            await expect_cancelled(cancelled)
            if cancelled is not closing:
                await closing
        elif scenario == 'cancel_host':
            entered = asyncio.Event()
            async def host():
                try:
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    await service.close()
            hosting = asyncio.create_task(host())
            await entered.wait()
            hosting.cancel()
            await backend.cleanup_started.wait()
            hosting.cancel()  # A second cancellation while the host is in finally.
            await asyncio.sleep(0)
            backend.cleanup_release.set()
            await expect_cancelled(hosting)
        elif scenario in {'timeout', 'backend_error'}:
            if scenario == 'backend_error':
                backend.release.set()
            await service.wait_idle(timeout=2)
            job = await call(service, 'get_job', job_id=jobs[0])
            expected = 'MODEL_TIMEOUT' if scenario == 'timeout' else 'BACKGROUND_INTERNAL_ERROR'
            assert job['state'] == 'failed' and job['failure']['code'] == expected, job
            await service.close()
        else:
            await asyncio.gather(service.close(), service.close())
        duration = time.monotonic() - begin
        assert backend.started == backend.finished
        assert_closed(service, workers)
        await service.close()
        if scenario == 'restart':
            recovered = await MemoryService(cfg).start()
            try:
                for job_id in jobs:
                    job = await call(recovered, 'get_job', job_id=job_id)
                    assert job['state'] == 'failed', job
                    assert job['failure']['code'] == 'INPUT_UNAVAILABLE', job
            finally:
                await recovered.close()
            assert_closed(recovered)
    assert not (asyncio.all_tasks() - {asyncio.current_task()}), 'pending task after shutdown'
    assert not errors, errors
    return service, backend, errors, duration


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('scenario', choices=SCENARIOS)
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args()
    faulthandler.enable()
    # The parent process enforces the hard deadline; print stacks shortly before it.
    faulthandler.dump_traceback_later(25)
    durations = []
    for i in range(args.repeat):
        before_threads = set(threading.enumerate())
        with tempfile.TemporaryDirectory(prefix='memoir-shutdown-') as temp:
            database = str(Path(temp) / 'memory.sqlite') if args.scenario == 'restart' else ':memory:'
            service, backend, errors, duration = asyncio.run(exercise(args.scenario, i, database))
            if args.scenario == 'runner_cleanup':
                assert service.runner.done() and not service.tasks
                assert backend.started == backend.finished
                service.db.close()
            assert not errors, errors
        assert not (set(threading.enumerate()) - before_threads), 'executor thread survived asyncio.run()'
        durations.append(duration)
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps(dict(scenario=args.scenario, iterations=args.repeat,
                         max_shutdown_ms=round(max(durations) * 1000, 3),
                         pending_tasks=0, unhandled_errors=0)))


if __name__ == '__main__':
    main()
