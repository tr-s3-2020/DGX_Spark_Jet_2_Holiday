"""Same-task timeout context for all supported Python versions."""
import sys

if sys.version_info >= (3, 11):
    from asyncio import timeout
else:
    from async_timeout import timeout

__all__ = ['timeout']
