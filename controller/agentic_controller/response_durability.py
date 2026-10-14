"""Persist state before sending a mutating HTTP response, including streams."""
from starlette.responses import JSONResponse


class ResponseDurabilityMiddleware:
    def __init__(self, app, store):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] in {'GET', 'HEAD', 'OPTIONS'}:
            return await self.app(scope, receive, send)
        failed = False
        started = False

        async def durable_send(message):
            nonlocal failed, started
            if failed:
                return
            if message['type'] == 'http.response.start':
                try:
                    await self.store.durable()
                except Exception:
                    failed = True
                    response = JSONResponse({'detail': '資料保存失敗，請稍後重試'}, status_code=503)
                    await response(scope, receive, send)
                    return
            if message['type'] == 'http.response.start':
                started = True
            await send(message)

        try:
            await self.app(scope, receive, durable_send)
        except Exception:
            if not self.store.failed or started:
                raise
            if not failed:
                response = JSONResponse({'detail': '資料保存失敗，請稍後重試'}, status_code=503)
                await response(scope, receive, send)
