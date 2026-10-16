"""Persist state before sending a mutating HTTP response, including streams."""
import inspect

from fastapi import HTTPException
from starlette.responses import JSONResponse

RESPONSE_GUARD = "agentic.response_guard"


class ResponseDurabilityMiddleware:
    def __init__(self, app, store):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        mutation = scope['method'] not in {'GET', 'HEAD', 'OPTIONS'}
        failed = False
        started = False

        async def durable_send(message):
            nonlocal failed, started
            if failed:
                return
            if message['type'] == 'http.response.start':
                try:
                    if mutation:
                        await self.store.durable()
                except Exception:
                    failed = True
                    response = JSONResponse({'detail': '資料保存失敗，請稍後重試'}, status_code=503)
                    await response(scope, receive, send)
                    return
                guard = scope.get(RESPONSE_GUARD)
                if guard is not None:
                    try:
                        result = guard()
                        if inspect.isawaitable(result):
                            await result
                    except HTTPException as exc:
                        failed = True
                        response = JSONResponse({'detail': exc.detail}, status_code=exc.status_code,
                                                headers={'Cache-Control': 'no-store'})
                        await response(scope, receive, send)
                        return
            if message['type'] == 'http.response.start':
                started = True
            await send(message)

        try:
            await self.app(scope, receive, durable_send)
        except Exception:
            if (not mutation and RESPONSE_GUARD not in scope) or not self.store.failed or started:
                raise
            if not failed:
                response = JSONResponse({'detail': '資料保存失敗，請稍後重試'}, status_code=503)
                await response(scope, receive, send)
