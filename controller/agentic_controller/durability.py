"""Durability barriers at outgoing transport boundaries."""
import asyncio

import aiohttp


async def durable(store):
    await store.durable()


def client_session(store, **kwargs):
    if store is None:
        raise ValueError("Controller Store is required for outgoing requests")
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
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        # Retrieve any failure without replacing the caller's cancellation.
        if not task.cancelled():
            task.exception()
        raise
