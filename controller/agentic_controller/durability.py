"""Durability barriers at outgoing transport boundaries."""
import asyncio
from contextvars import ContextVar

import aiohttp

current_store = ContextVar("controller_store", default=None)


async def durable(store):
    if store is not None:
        await store.durable()


def client_session(store=None, **kwargs):
    store = store if store is not None else current_store.get()
    trace = aiohttp.TraceConfig()

    async def before_request(session, context, params):
        if params.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            await durable(store)

    trace.on_request_start.append(before_request)
    traces = list(kwargs.pop("trace_configs", ()))
    return aiohttp.ClientSession(trace_configs=[*traces, trace], **kwargs)


async def file_io(function, *args):
    """Finish an atomic file operation before releasing its caller's locks."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise
